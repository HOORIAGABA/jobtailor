"""Turn a set of applied operations into something a human can review. No LLM.

This is the module the product's central claim rests on: *every change is
attributable*. It costs almost nothing to write, because the ops already carry
`ref_id`, `rationale` and `cites` — the attribution falls out of the data model
rather than being reconstructed afterwards.

The diff is **structural, not textual**. Diffing two rendered resumes line by
line would drown a reordered section in noise. Diffing by id says the true
thing: "bullet `exp.1.b.1` was rewritten, because …, supported by `exp.1.b.2`".
"""
from __future__ import annotations

from typing import Sequence

from pydantic import BaseModel, Field

from app.domain.ids import item_id_of_bullet
from app.domain.models import ResumeDoc
from app.domain.ops import (
    AskUser, DropBullet, FlagGap, Op, PromoteItem, Reject, ReorderBullets,
    ReorderItems, RewriteBullet, SetSkills, SetSummary,
)

_SNIP = 60


class Change(BaseModel):
    """One reviewable edit. `before`/`after` are display strings."""
    op_id: str
    op_kind: str
    ref_id: str
    label: str = ""                       # where in the resume, for a heading
    before: str = ""
    after: str = ""
    rationale: str = ""
    cites: list[str] = Field(default_factory=list)


class Gap(BaseModel):
    requirement: str
    severity: str
    closest_evidence: list[str] = Field(default_factory=list)


class Question(BaseModel):
    bullet_id: str = ""
    context: str = ""                     # the bullet being asked about
    question: str = ""


class Diff(BaseModel):
    """The whole review payload for the approval gate."""
    changes: list[Change] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    rejections: list[Reject] = Field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.changes:
            out[c.op_kind] = out.get(c.op_kind, 0) + 1
        return out

    def is_empty(self) -> bool:
        return not self.changes


# ── labels ────────────────────────────────────────────────────────────

def _snip(text: str, limit: int = _SNIP) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _label_for(doc: ResumeDoc, ref_id: str) -> str:
    """Where in the resume this change lands, in words a person recognizes."""
    if (item := doc.item(ref_id)) is not None:
        return " · ".join(p for p in (item.title, item.org) if p)
    if (section := doc.section(ref_id)) is not None:
        return section.heading
    if doc.bullet(ref_id) is not None:
        try:
            owner = doc.item(item_id_of_bullet(ref_id))
        except ValueError:
            owner = None
        if owner is not None:
            return " · ".join(p for p in (owner.title, owner.org) if p)
    return ""


def _order_summary(doc: ResumeDoc, ids: Sequence[str]) -> str:
    return "  →  ".join(_snip(doc.text_of(i), 28) or i for i in ids)


def _skills_line(doc: ResumeDoc) -> str:
    section = doc.section_of_kind("skills")
    if section is None:
        return ""
    parts = []
    for item in section.items:
        body = ", ".join(b.text for b in item.bullets)
        parts.append(f"{item.title}: {body}" if item.title else body)
    return " | ".join(parts)


# ── build ─────────────────────────────────────────────────────────────

def build_diff(
    base: ResumeDoc,
    tailored: ResumeDoc,
    ops: Sequence[Op],
    rejections: Sequence[Reject] = (),
) -> Diff:
    """Describe what changed, why, and what was refused.

    `base` supplies the "before" text and the human-readable labels; `tailored`
    supplies the "after". Ops that produced no visible change are skipped, so
    the reviewer never sees a no-op row.
    """
    diff = Diff(rejections=list(rejections))

    for op in ops:
        if isinstance(op, FlagGap):
            diff.gaps.append(Gap(
                requirement=op.requirement,
                severity=op.severity,
                closest_evidence=list(op.closest_evidence),
            ))
            continue

        if isinstance(op, AskUser):
            diff.questions.append(Question(
                bullet_id=op.bullet_id,
                context=base.text_of(op.bullet_id),
                question=op.question,
            ))
            continue

        change = _change_for(op, base, tailored)
        if change is not None:
            diff.changes.append(change)

    return diff


def _change_for(op: Op, base: ResumeDoc, tailored: ResumeDoc) -> Change | None:
    def make(ref_id: str, before: str, after: str, **kw) -> Change | None:
        if before == after:
            return None                   # nothing visibly changed
        return Change(
            op_id=op.op_id, op_kind=op.op, ref_id=ref_id,
            label=_label_for(base, ref_id), before=before, after=after, **kw
        )

    if isinstance(op, RewriteBullet):
        origin = base.bullet(op.bullet_id)
        current = tailored.bullet(op.bullet_id)
        if origin is None or current is None:
            return None
        return make(op.bullet_id, origin.text, current.text,
                    rationale=op.rationale)

    if isinstance(op, DropBullet):
        origin = base.bullet(op.bullet_id)
        if origin is None or tailored.bullet(op.bullet_id) is not None:
            return None                   # the drop was skipped
        return make(op.bullet_id, origin.text, "(removed)", rationale=op.reason)

    if isinstance(op, ReorderBullets):
        item = base.item(op.item_id)
        if item is None:
            return None
        return make(op.item_id,
                    _order_summary(base, item.bullet_ids()),
                    _order_summary(base, op.order))

    if isinstance(op, ReorderItems):
        section = base.section(op.section_id)
        if section is None:
            return None
        return make(op.section_id,
                    _order_summary(base, section.item_ids()),
                    _order_summary(base, op.order))

    if isinstance(op, PromoteItem):
        section = base.section_of_item(op.item_id)
        after_section = tailored.section_of_item(op.item_id)
        if section is None or after_section is None:
            return None
        return make(op.item_id,
                    _order_summary(base, section.item_ids()),
                    _order_summary(base, after_section.item_ids()),
                    rationale=op.rationale, cites=list(op.cites))

    if isinstance(op, SetSummary):
        return make("summary", base.summary, tailored.summary,
                    rationale=op.rationale, cites=list(op.cites))

    if isinstance(op, SetSkills):
        return make("skills", _skills_line(base), _skills_line(tailored),
                    rationale=op.rationale)

    return None
