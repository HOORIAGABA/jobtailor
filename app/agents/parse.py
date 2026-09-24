"""Stage S0.2 — extracted text becomes a structured document. One model call.

This is the stage with the most leverage in the system and the least room for
cleverness. Everything downstream — evidence, planning, validation, the diff a
human approves — is phrased against the document this stage produces. If it is
wrong here, it is wrong everywhere, and no later guarantee can detect it,
because every later guarantee is checked *against* this output.

So the stage is built to be boring:

**The schema mirrors the document, not the domain.** `RawResume` has a list of
sections with verbatim headings. It has no `experience` field, no `projects`
field, no ids. v1's parse schema had those keys, which asks the model to
*classify* — and when it guessed wrong, a project landed under Experience and
v1 needed ~60 lines of `_reclassify_entries` to move it back. Classification is
judgment; judgment is not extraction. It happens in `engine.normalize`, in
code, where it is deterministic and testable. Removing the judgment removes the
error class and its repair layer together.

**No ids in the schema.** Models duplicate and invent identifiers, and an id is
a promise the rest of the system relies on. `engine.normalize` assigns them
from a monotonic counter.

**`temperature=0`.** Extraction has one correct answer, and re-uploading the
same file should give the same parse.

**`max_tokens` scales with the input.** A three-page resume parsed with a
fixed 2048-token ceiling returns JSON that stops mid-string. That surfaces as a
schema error, gets retried, fails identically, and reports itself as "the model
would not follow the schema" — which sends you looking in exactly the wrong
place. The ceiling is computed from the document instead.

**What this stage does not do:** verify itself. `engine.parse_check` compares
the parse against the source text word by word and reports what went missing.
That is a separate module because it is deterministic and because a stage
should not be the judge of its own output.
"""
from __future__ import annotations

import logging

from app.agents.base import as_json, call_structured, prompt_version
from app.domain.models import RawResume
from app.io.llm import LLMClient

logger = logging.getLogger(__name__)

# Roughly four characters to a token, and the JSON restatement of a document
# runs longer than the document — keys, quoting, escapes. Under-provisioning
# truncates the answer; over-provisioning costs nothing on any provider that
# bills output actually produced.
TOKENS_PER_CHAR = 0.55
MIN_OUTPUT_TOKENS = 2048
MAX_OUTPUT_TOKENS = 8192


SYSTEM = """\
You transcribe one resume into JSON. You are a transcriber, not an editor.

THE RULE
Copy the text as written. Do not summarise, rephrase, shorten, fix grammar,
expand an abbreviation, merge two bullets or split one. If a bullet reads
"Worked on data pipelines for the reporting team", that exact sentence is what
goes in the JSON.

LOSING TEXT IS THE WORST THING YOU CAN DO
Every line of the document belongs somewhere in your output. A bullet you leave
out is gone for good — nothing downstream can notice it is missing, and the
candidate's resume will be sent without it. When you are unsure where a line
belongs, put it in the nearest entry rather than dropping it.

INVENTING TEXT IS THE SECOND WORST
Never add a bullet, a date, an employer or a skill that is not printed in the
document. Empty is correct when the document says nothing.

SECTIONS
- heading: copy it EXACTLY as printed, including capitalisation. "WORK
  EXPERIENCE" stays "WORK EXPERIENCE". Never rename, translate or tidy it.
- Do not classify. Do not reorder. Sections appear in document order, and a
  heading you do not recognise is still a section.
- A resume with no headings at all is one section with heading "".

ENTRIES
An entry is one job, one project, one degree, one certificate.
- title: the role, project name or degree.
- org: the employer, school or client. "" when there is none.
- dates: copy the date range verbatim — "Jan 2022 - Present", "2021", "".
- bullets: each bullet point as its own string, WITHOUT the bullet character.
  A bullet that wrapped onto two lines in the PDF is ONE string.

A skills section usually has no real entries. Put each skill line in its own
entry as a single bullet, exactly as printed:
  "Languages: Python, SQL"  ->  entry with bullets: ["Languages: Python, SQL"]
Do not split it into individual skills. That happens later, in code.

CONTACT
Fill only what is printed. Never construct an email from a name or a domain.

SUMMARY
The opening paragraph, if the resume has one, copied verbatim. If it sits under
a heading like "Profile" or "Objective", put it in BOTH the summary field and
its section — the duplication is handled later.

Return only the JSON object."""

PROMPT_VERSION = prompt_version(SYSTEM)


def output_budget(text: str) -> int:
    """How many tokens the answer may need, from how long the document is."""
    estimate = int(len(text) * TOKENS_PER_CHAR)
    return max(MIN_OUTPUT_TOKENS, min(MAX_OUTPUT_TOKENS, estimate))


def parse_resume(
    text: str,
    client: LLMClient,
    *,
    max_tokens: int | None = None,
) -> RawResume:
    """Extracted text in, `RawResume` out. One call, temperature 0.

    Returns an empty `RawResume` for empty input rather than raising: an empty
    document is a legitimate state, and `engine.parse_check` will report the
    loss if the input was not in fact empty.
    """
    body = (text or "").strip()
    if not body:
        return RawResume()

    budget = max_tokens or output_budget(body)
    logger.info("Parsing %d characters with a %d-token ceiling", len(body), budget)

    raw = call_structured(
        client,
        system=SYSTEM,
        user=as_json({"resume_text": body}),
        schema_model=RawResume,
        max_tokens=budget,
        temperature=0.0,
        stage="parse",
    )

    logger.info(
        "Parsed: %d sections, %d entries, %d bullets",
        len(raw.sections),
        sum(len(s.entries) for s in raw.sections),
        sum(len(e.bullets) for s in raw.sections for e in s.entries),
    )
    return raw
