"""Stage S2b — ask a model about the requirements lexical matching could not place.

`engine.evidence` matches a job term to a bullet when the bullet *names* it.
That is exact, checkable and free, and it is why a `strong` link means
something. It is also blind to a bullet that proves a capability without
naming it:

    "Built nightly jobs with dependency handling and automatic retries"
      -> Airflow: no link, reported as a gap

Measured on a real run: **16 links, all strong, zero hints** across 8
requirements and ~50 bullets. Everything that matched, matched by name.
Nothing was found by meaning.

This stage closes that hole with one model call, under three constraints that
keep it from undoing the design it is bolted onto.

**1. It may only produce `hint` links, never `strong`.**
A strong link is mechanically verifiable — the term is in the bullet, anyone
can check. An LLM link is an assertion. The existing rule already says a hint
"may inform the planner but must never be the sole justification for an edit",
and the validator's grounding corpus is built from the resume, not from this.
So the worst this stage can do is draw attention to a bullet; it cannot make a
claim legal.

**2. It only sees what lexical matching could not place.**
A job element that already has evidence is not sent. On a well-matched posting
this stage has nothing to do and the call is skipped entirely.

**3. Every id it returns is checked.**
A `bullet_id` that does not exist is dropped, and an element that was not
actually unmatched is dropped. The model cannot invent a link between things
that are not on the board.

**Why this is not the dense-retrieval decision in disguise.** That question —
embeddings or not — is about recall quality and cannot be answered without an
eval set. This is about a specific, measured hole and it carries its own
safety rail: the hint/strong distinction that already exists. Whether it
*improves* the gap list is still an eval question, and the answer belongs in
`evals/cases/` when that exists.

**Why the model does not do all of S2.** Term-to-bullet matching is retrieval,
not generation: it is decidable, it must be identical on every run, and the
model being asked is the same one that later proposes the edits. A model that
grades its own evidence can manufacture the justification for the change it
wants to make. The deterministic pass stays the floor.
"""
from __future__ import annotations

import logging
from typing import Iterable

from pydantic import BaseModel, Field

from app.agents.base import as_json, call_structured, prompt_version
from app.domain.models import EvidenceIndex, EvidenceLink, JobBrief, ResumeDoc
from app.engine.evidence import JobElement, evidence_bullets, job_elements
from app.io.llm import LLMClient

logger = logging.getLogger(__name__)

# Above this, a posting is asking for so much that a per-element pass is not
# the right tool and the payload stops being cheap.
MAX_ELEMENTS = 12
MAX_BULLETS = 60


class DraftHint(BaseModel):
    element_id: str = Field(
        description="The id of the requirement, exactly as given in `unmatched`."
    )
    bullet_id: str = Field(
        description="The id of the resume line, exactly as given in `bullets`."
    )
    why: str = Field(
        description="One short sentence naming what in the line does the work."
    )


class HintList(BaseModel):
    hints: list[DraftHint] = Field(
        description="Only genuine matches. An empty list is the right answer "
                    "when nothing in the resume touches these requirements."
    )


SYSTEM = """\
You are looking for resume lines that demonstrate a requirement WITHOUT naming
it.

Exact name matching has already run. Everything it could find is found. What
is left are the requirements nothing matched by name, and your job is to say
whether any line proves one anyway.

WHAT COUNTS
The line must describe work that genuinely demonstrates the requirement.

  "Built nightly jobs with dependency handling and automatic retries"
    demonstrates a scheduling/orchestration requirement. The work is the same
    work; only the tool is unnamed.

  "Deployed a model behind an HTTP endpoint with request validation"
    demonstrates API development.

WHAT DOES NOT COUNT
  - Adjacency. Working near a thing is not doing it.
  - Sharing a word. "data" in both is not evidence.
  - A skills line. Writing a word down is not demonstrating it.
  - Plausibility. "They probably used X" is a guess, not evidence.

If a requirement names a specific product and no line describes doing that kind
of work, say nothing about it. A missing requirement is a useful, honest
answer; a wrong match sends the candidate to rewrite a line around something
they never did.

These matches are treated as HINTS. They are never enough on their own to
justify changing a line, so the cost of a wrong one is wasted attention, and
the cost of a timid one is a requirement that looks unmet. Prefer being right
over being helpful.

Use the ids exactly as given. Return only the JSON object."""

PROMPT_VERSION = prompt_version(SYSTEM)


def unmatched_elements(brief: JobBrief, index: EvidenceIndex) -> list[JobElement]:
    """The job elements the deterministic pass could not place."""
    unmatched = set(index.unmatched_elements)
    return [e for e in job_elements(brief) if e.id in unmatched]


def build_payload(elements: list[JobElement], doc: ResumeDoc) -> dict:
    return {
        "unmatched": [
            {"id": e.id, "kind": e.kind, "text": e.text}
            for e in elements[:MAX_ELEMENTS]
        ],
        "bullets": {
            b.id: b.text for b in list(evidence_bullets(doc))[:MAX_BULLETS]
        },
    }


def find_hints(
    brief: JobBrief,
    doc: ResumeDoc,
    index: EvidenceIndex,
    client: LLMClient,
    *,
    max_tokens: int = 1024,
    dropped: list[str] | None = None,
) -> list[EvidenceLink]:
    """One call, only for what lexical matching missed. Hints only.

    Returns the new links; the caller merges them. Returns `[]` without
    calling the model when there is nothing unmatched.
    """
    elements = unmatched_elements(brief, index)
    if not elements:
        logger.info("Nothing unmatched — skipping the hint pass")
        return []

    bullets = {b.id for b in evidence_bullets(doc)}
    if not bullets:
        return []

    allowed = {e.id for e in elements[:MAX_ELEMENTS]}
    logger.info("Asking about %d unmatched element(s) against %d bullet(s)",
                len(allowed), len(bullets))

    result = call_structured(
        client,
        system=SYSTEM,
        user=as_json(build_payload(elements, doc)),
        schema_model=HintList,
        max_tokens=max_tokens,
        temperature=0.0,
        stage="evidence_hints",
    )

    links: list[EvidenceLink] = []
    seen: set[tuple[str, str]] = set()
    for hint in result.hints:
        reason = _reject(hint, allowed, bullets, seen)
        if reason:
            logger.info("Dropped hint: %s", reason)
            if dropped is not None:
                dropped.append(reason)
            continue
        seen.add((hint.element_id, hint.bullet_id))
        links.append(EvidenceLink(
            job_element_id=hint.element_id,
            bullet_id=hint.bullet_id,
            similarity=0.0,          # not comparable to a lexical score
            lexical_hit=False,
            strength="hint",         # never strong — see the module docstring
        ))

    logger.info("Kept %d hint(s) of %d proposed", len(links), len(result.hints))
    return links


def _reject(hint: DraftHint, allowed: set[str], bullets: set[str],
            seen: set[tuple[str, str]]) -> str:
    """Why this hint cannot be used, or "" when it can."""
    if hint.bullet_id not in bullets:
        return f"no such bullet {hint.bullet_id!r}"
    if hint.element_id not in allowed:
        return f"{hint.element_id!r} was not unmatched"
    if (hint.element_id, hint.bullet_id) in seen:
        return f"duplicate {hint.element_id} <- {hint.bullet_id}"
    return ""


def merge(index: EvidenceIndex, hints: Iterable[EvidenceLink]) -> EvidenceIndex:
    """Fold hints into the index, clearing the elements they now cover.

    A new `EvidenceIndex` rather than a mutation, so the deterministic result
    stays intact in the run folder alongside the augmented one.
    """
    new = list(hints)
    if not new:
        return index
    covered = {link.job_element_id for link in new}
    return EvidenceIndex(
        links=[*index.links, *new],
        unmatched_elements=[e for e in index.unmatched_elements if e not in covered],
    )
