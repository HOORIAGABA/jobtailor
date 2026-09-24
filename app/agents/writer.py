"""Stage S4 — write the prose for the bullets the planner targeted.

The only stage that writes bullet text, and the only one where temperature is
high. Everything else in this system wants determinism; this wants good
sentences.

The schema is the thinnest in the project on purpose: one string per bullet. A
large nested schema costs attention and, under constrained decoding, restricts
token choice exactly where nuance matters.

It notably does NOT ask the writer to declare which numbers or terms it used.
An earlier design did, and fed those declarations to the fabrication guard —
which a model can walk straight past by under-declaring. The validator extracts
both from the text itself.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.agents.base import as_json, call_structured, prompt_version
from app.domain.models import JobBrief, ResumeDoc
from app.domain.ops import Op, RewriteBullet
from app.io.llm import LLMClient

logger = logging.getLogger(__name__)


class BulletDraft(BaseModel):
    bullet_id: str
    text: str = Field(description="The rewritten line. One sentence, no bullet marker.")


class WriterOutput(BaseModel):
    bullets: list[BulletDraft] = Field(default_factory=list)


SYSTEM = """\
You rewrite individual resume lines. Nothing else.

For each line you are given: the original text, the entry it belongs to, the
terms it should surface, and why a rewrite was requested.

THE ONE RULE
Every fact in your rewrite must be in the original line. You may reword,
restructure, sharpen and lead with what matters. You may not add.

Specifically:
- No number that is not already in the original. Not a derived one either:
  if the original says "12 hours a week", you may not write "624 hours a year".
  That is arithmetic the candidate never claimed.
- No tool, framework, employer, team size or scope that is not already there.
- No promotion. If the original says "helped with", you may not write "led" or
  "drove" or "owned". Same work, same seniority.
- No invented outcome. "Improved performance" is not licensed by "worked on
  performance".

WHAT MAKES A GOOD REWRITE
- Lead with the action and the thing built, not with a preamble.
  "Worked on data pipelines for the reporting team"
   → "Built and maintained data pipelines feeding the reporting team"
- Surface the requested terms ONLY where the original already supports them.
  If the original clearly describes Airflow work and the term is Airflow, name
  it. If it doesn't, leave the term out — a term dropped into an otherwise
  unchanged sentence is keyword stuffing, and it is rejected.
- Keep the candidate's voice. You are tightening their sentence, not
  replacing it with a template. Ten lines that all follow the same shape read
  worse than ten ordinary ones.
- One sentence. No trailing period is needed. No bullet character.
- Match the register in `tone`: "scrappy" means plain and direct, "formal"
  means measured, "unclear" means write neutrally.

IF YOU CANNOT IMPROVE IT
Return the original text unchanged. That is a valid and useful answer. A
rewrite that only shuffles words wastes the candidate's review time, and one
that reaches for a fact you do not have will be rejected anyway.

Return one entry per line you were given, keeping the same bullet_id.
Return only the JSON object."""

PROMPT_VERSION = prompt_version(SYSTEM)


def build_payload(targets: list[Op], doc: ResumeDoc, brief: JobBrief) -> dict:
    """Only the targeted lines and their immediate context — never the full doc."""
    items = []
    for op in targets:
        bullet = doc.bullet(op.bullet_id)
        if bullet is None:
            continue
        item = doc.item(_item_of(op.bullet_id))
        items.append({
            "bullet_id": op.bullet_id,
            "original": bullet.text,
            "entry": f"{item.title} at {item.org}" if item else "",
            "sibling_bullets": [
                b.text for b in (item.bullets if item else []) if b.id != op.bullet_id
            ],
            "target_terms": list(op.target_terms),
            "why": op.rationale,
        })

    return {
        "role": brief.role,
        "tone": brief.tone,
        "bullets": items,
    }


def _item_of(bullet_id: str) -> str:
    from app.domain.ids import item_id_of_bullet
    try:
        return item_id_of_bullet(bullet_id)
    except ValueError:
        return ""


def write_bullets(
    ops: list[Op],
    doc: ResumeDoc,
    brief: JobBrief,
    client: LLMClient,
    *,
    max_tokens: int = 1536,
) -> list[Op]:
    """Fill in `text` on every RewriteBullet. One batched call.

    Returns the full op list with rewrites completed. A rewrite the writer
    skipped, or returned unchanged, is dropped — an operation that changes
    nothing should not reach the reviewer as a change.
    """
    targets = [op for op in ops if isinstance(op, RewriteBullet)]
    if not targets:
        return ops

    result = call_structured(
        client,
        system=SYSTEM,
        user=as_json(build_payload(targets, doc, brief)),
        schema_model=WriterOutput,
        max_tokens=max_tokens,
        temperature=0.6,          # the one stage that wants variety
        stage="writer",
    )
    drafts = {d.bullet_id: (d.text or "").strip() for d in result.bullets}

    out: list[Op] = []
    filled = skipped = 0
    for op in ops:
        if not isinstance(op, RewriteBullet):
            out.append(op)
            continue
        text = drafts.get(op.bullet_id, "")
        origin = doc.bullet(op.bullet_id)
        if not text or (origin and text == origin.text.strip()):
            skipped += 1
            continue                      # unchanged: not a change to review
        op.text = text
        out.append(op)
        filled += 1

    logger.info("Writer: %d bullets rewritten, %d left unchanged", filled, skipped)
    return out
