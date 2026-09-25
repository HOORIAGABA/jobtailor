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

**The model is asked for a narrower schema than `RawResume`.** Two reasons,
both learned from a run against three real resumes that returned *zero
sections* and the entire document in one field:

1. `sections` and `entries` are **required**. In `RawResume` they carry
   `default_factory=list`, so they never appear in the schema's `required`
   array — and `{"summary": "<the whole resume>"}` is then a perfectly valid
   answer. It validated, it normalized, and it produced a document with
   nothing addressable in it.
2. The draft has **no `summary` field at all.** A resume's summary is already
   a section ("ABOUT ME", "PROFILE"), and `engine.normalize` already folds such
   a section into `ResumeDoc.summary`. Keeping a second home for free text gave
   the model somewhere to put everything, and a model under a token budget will
   take the cheapest path that satisfies the schema. Removing the field removes
   the option.

**What this stage does not do:** verify itself. `engine.parse_check` compares
the parse against the source text and reports what went missing. That is a
separate module because it is deterministic and because a stage should not be
the judge of its own output.
"""
from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field

from app.agents.base import as_json, call_structured, prompt_version
from app.domain.models import Contact, RawEntry, RawResume, RawSection
from app.io.llm import LLMClient

logger = logging.getLogger(__name__)

# MEASURED, not guessed. The first version of this used 0.55 tokens per source
# character, reasoning loosely that four characters make a token. A real
# 7,660-character resume was then cut off at exactly its 4,213-token ceiling —
# twice — and reported itself as a schema error both times.
#
# Rebuilding the JSON a faithful parse of that resume produces:
#
#     source  7,660 chars
#     json   23,806 chars   = 3.11x   (keys, quoting, indentation, structure)
#            ~5,950 tokens  = 0.78 tokens per source character
#
# So 0.55 was about 30% short. 1.0 leaves room for a document that structures
# into more entries than that one did. Over-provisioning is free: providers
# bill the output actually produced, not the ceiling requested.
#
# The cap stays at a widely-supported 8,192 rather than the hard ceiling —
# some models reject a request asking for more than they can emit. When a run
# genuinely needs more, `agents.base` detects the truncation and retries once
# with double, which is escalation on evidence rather than on assumption.
TOKENS_PER_CHAR = 1.0
MIN_OUTPUT_TOKENS = 2048
MAX_OUTPUT_TOKENS = 8192


# ── what the model returns ────────────────────────────────────────────
# Narrower than `RawResume`: every field required, and no `summary` to dump
# into.
#
# **A model fills what the grammar forces it to fill, and skips the rest.**
# That is the single most useful thing learned from running this against real
# models. Three times now:
#
#   `sections` defaulted to []      -> the whole resume arrived in one field
#   `title`/`org`/`dates` defaulted -> llama3.1:8b returned THREE jobs with
#                                      empty titles, employers and dates,
#                                      while filling `bullets` perfectly
#   `contact` defaulted             -> came back entirely empty
#
# A pydantic default keeps a field out of the schema's `required` array, and
# constrained decoding then permits the model to omit it. Requiring the key
# does not force a value — `""` is still a legal answer for something the
# document does not say — but it forces the model to LOOK. On the same
# document that is the difference between a job entry and a pile of bullets.

class DraftEntry(BaseModel):
    title: str = Field(description="The role, project name or degree. \"\" if none.")
    org: str = Field(description="Employer, school or client. \"\" if none.")
    dates: str = Field(description="The date range, verbatim. \"\" if none.")
    bullets: list[str] = Field(
        description="Each bullet point as its own string, without the bullet "
                    "character. A bullet that wrapped onto two lines is ONE string."
    )


class DraftSection(BaseModel):
    heading: str = Field(description="Copied EXACTLY as printed. \"\" if none.")
    # Required, not defaulted. A defaulted list is absent from the schema's
    # `required` array, and an empty section list then satisfies the schema.
    entries: list[DraftEntry] = Field(
        description="The entries under this heading, in document order."
    )


class DraftContact(BaseModel):
    """Required for the same reason the entry fields are: an optional contact
    block came back as `{}` from a model that had the email on screen."""
    full_name: str = Field(description="As printed. \"\" if absent.")
    email: str = Field(description="Only if literally printed. Never constructed.")
    phone: str = Field(description="As printed. \"\" if absent.")
    location: str = Field(description="City or country. \"\" if absent.")
    linkedin: str = Field(description="The URL as printed. \"\" if absent.")
    github: str = Field(description="The URL as printed. \"\" if absent.")
    website: str = Field(description="Any other URL as printed. \"\" if absent.")


class ResumeDraft(BaseModel):
    contact: DraftContact
    sections: list[DraftSection] = Field(
        description="Every section of the document, in order. Never empty for "
                    "a real resume."
    )


def to_raw(draft: ResumeDraft) -> RawResume:
    """Widen the draft into the domain's `RawResume`. Pure, trivially tested.

    `summary` stays empty here. `engine.normalize` derives it from whichever
    section turns out to be a summary, so there is exactly one place free text
    can live.
    """
    return RawResume(
        contact=Contact(**draft.contact.model_dump()),
        summary="",
        sections=[
            RawSection(
                heading=section.heading,
                entries=[
                    RawEntry(title=e.title, org=e.org, dates=e.dates,
                             bullets=[b for b in e.bullets if b and b.strip()])
                    for e in section.entries
                ],
            )
            for section in draft.sections
        ],
    )


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

EVERY FIELD APPEARS, ALWAYS
Fill in every field of every object. When the document does not say something,
the answer is "" — never a missing key. An entry with bullets but no title,
employer or dates is a broken entry: those three are how the resume is read.

ENTRIES
An entry is one job, one project, one degree, one certificate.
- title: the role, project name or degree.
- org: the employer, school or client. "" when there is none.
- dates: copy the date range verbatim — "Jan 2022 - Present", "2021", "".
- bullets: each bullet point as its own string, WITHOUT the bullet character.
  A bullet that wrapped onto two lines in the PDF is ONE string.

EVERY SECTION HAS ENTRIES
`entries` is never an empty list for a section that has any text under it.
When a section is a paragraph rather than a list of jobs — "ABOUT ME",
"PROFILE", "SUMMARY" — it is still one entry, with the paragraph as its single
bullet:
  ABOUT ME
  AI/ML Engineer with hands-on experience in deploying production systems.
  ->  heading "ABOUT ME", one entry, bullets:
      ["AI/ML Engineer with hands-on experience in deploying production systems."]

A skills section works the same way. Put each skill line in its own entry as a
single bullet, exactly as printed:
  "Languages: Python, SQL"  ->  entry with bullets: ["Languages: Python, SQL"]
Do not split it into individual skills. That happens later, in code.

THERE IS NOWHERE ELSE TO PUT TEXT
Every line of this resume goes into some section's entry. There is no summary
field and no notes field. A reply with an empty `sections` list is always
wrong for a real resume.

CONTACT
Fill only what is printed. Never construct an email from a name or a domain.

Return only the JSON object."""

PROMPT_VERSION = prompt_version(SYSTEM)


def output_budget(text: str) -> int:
    """How many tokens the answer may need, from how long the document is."""
    estimate = int(len(text) * TOKENS_PER_CHAR)
    return max(MIN_OUTPUT_TOKENS, min(MAX_OUTPUT_TOKENS, estimate))


# ── windowing ─────────────────────────────────────────────────────────
# Asking for one JSON object covering a whole resume is a bet that the model's
# entire output budget will cover it. That bet lost twice on a two-page CV: at
# 7,660 tokens and again at 15,320, where a JSON of roughly 6,000 tokens should
# have fit easily. The excess went to a reasoning model thinking out loud, and
# raising the number again would only move the failure.
#
# So the document is split at its own section headings and parsed a window at a
# time. Each call is small and bounded, a truncation costs one window instead of
# the document, and the ceiling stops depending on how talkative the model is.
# It also parses better: a model looking at one section is not holding two pages
# in its head.
#
# The split is deterministic, in code. Asking the model where to split would put
# judgment back in the extraction stage, which is the thing this design removes.

WINDOW_CHARS = 3500

# A heading is short, has no sentence punctuation, and is not a bullet. This is
# the same shape `io.extract` uses to stop a bullet swallowing what follows.
_HEADING = re.compile(r"^(?![-*•])[^.!?]{2,60}$")


def looks_like_heading(line: str) -> bool:
    """A section heading, as printed. Errs towards no — a missed split just
    makes a window longer, while a false split cuts a job from its bullets."""
    stripped = line.strip()
    if not stripped or not _HEADING.match(stripped):
        return False
    letters = [c for c in stripped if c.isalpha()]
    if not letters:
        return False
    # Printed headings are upper-case, or title-case and short.
    upper_ratio = sum(c.isupper() for c in letters) / len(letters)
    return upper_ratio > 0.7 or (stripped.istitle() and len(stripped.split()) <= 4)


def split_windows(text: str, window_chars: int = WINDOW_CHARS) -> list[str]:
    """Break the document at heading boundaries into parseable windows.

    A window never starts mid-section: the split points are headings, so an
    entry and its bullets always travel together. A single section longer than
    `window_chars` is left whole rather than cut — losing the tie between a job
    and its bullets is worse than one large call.
    """
    body = (text or "").strip()
    if len(body) <= window_chars:
        return [body] if body else []

    # Blocks: each begins at a heading and runs to just before the next.
    blocks: list[list[str]] = []
    for line in body.splitlines():
        if looks_like_heading(line) or not blocks:
            blocks.append([line])
        else:
            blocks[-1].append(line)

    windows: list[str] = []
    current: list[str] = []
    size = 0
    for block in blocks:
        chunk = "\n".join(block)
        if current and size + len(chunk) > window_chars:
            windows.append("\n".join(current))
            current, size = [], 0
        current.append(chunk)
        size += len(chunk) + 1
    if current:
        windows.append("\n".join(current))
    return [w for w in windows if w.strip()]


def merge(parts: list[RawResume]) -> RawResume:
    """Join per-window parses into one document.

    Sections are merged by heading, because a section that straddles a window
    boundary comes back once from each — and two "WORK EXPERIENCE" sections
    would normalize into `sec.experience` and `sec.experience.2`, splitting a
    single job history in the final document.
    """
    merged = RawResume()
    by_heading: dict[str, RawSection] = {}

    for part in parts:
        if not merged.contact.full_name and part.contact.full_name:
            merged.contact = part.contact
        for section in part.sections:
            key = " ".join(section.heading.lower().split())
            if (existing := by_heading.get(key)) is not None:
                existing.entries.extend(section.entries)
            else:
                by_heading[key] = section
                merged.sections.append(section)
    return merged


# ── the stage ─────────────────────────────────────────────────────────

def parse_resume(
    text: str,
    client: LLMClient,
    *,
    max_tokens: int | None = None,
    window_chars: int = WINDOW_CHARS,
) -> RawResume:
    """Extracted text in, `RawResume` out. One call per window, temperature 0.

    Returns an empty `RawResume` for empty input rather than raising: an empty
    document is a legitimate state, and `engine.parse_check` will report the
    loss if the input was not in fact empty.
    """
    body = (text or "").strip()
    if not body:
        return RawResume()

    windows = split_windows(body, window_chars)
    logger.info("Parsing %d characters in %d window(s)", len(body), len(windows))

    parts: list[RawResume] = []
    for n, window in enumerate(windows, start=1):
        budget = max_tokens or output_budget(window)
        logger.info("  window %d/%d: %d chars, %d-token ceiling",
                    n, len(windows), len(window), budget)
        draft = call_structured(
            client,
            system=SYSTEM,
            user=as_json({"resume_text": window}),
            schema_model=ResumeDraft,
            max_tokens=budget,
            temperature=0.0,
            stage="parse",
        )
        parts.append(to_raw(draft))

    raw = merge(parts)
    logger.info(
        "Parsed: %d sections, %d entries, %d bullets",
        len(raw.sections),
        sum(len(s.entries) for s in raw.sections),
        sum(len(e.bullets) for s in raw.sections for e in s.entries),
    )
    return raw
