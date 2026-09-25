"""Apply validated operations to produce the tailored document. No LLM.

    tailored = apply_ops(base, accepted)

Two properties this module exists to guarantee:

**The base is never mutated.** Everything works on a deep copy. That is what
makes the diff possible (a stable left-hand side) and what makes the approval
gate cheap: when a reviewer reverts one change, the caller re-applies the
remaining ops to the same base rather than trying to undo anything.

**Application order is fixed.** Ops are applied in `APPLY_ORDER`, never in the
order the model happened to emit them. Otherwise a rewrite landing before or
after a reorder could give different documents from the same op list, and runs
would not be reproducible.
"""
from __future__ import annotations

from app.domain.ids import bullet_id, item_id, item_id_of_bullet
from app.domain.models import Bullet, Item, ResumeDoc, Section
from app.domain.ops import (
    ADVISORY_OPS, APPLY_ORDER, DropBullet, Op, PromoteItem, ReorderBullets,
    ReorderItems, RewriteBullet, SetSkills, SetSummary,
)
from app.domain.ids import section_id
from app.engine.normalize import existing_skills, skill_key


def apply_ops(base: ResumeDoc, ops: list[Op]) -> ResumeDoc:
    """Return a new document with `ops` applied. `base` is left untouched.

    Ops that reference ids which no longer resolve are skipped rather than
    raising: the validator already rejected unknown ids, so anything unresolved
    here was made stale by an earlier op in the same batch (a bullet dropped
    before a rewrite targeting it, say). Skipping keeps the run alive; the diff
    simply shows one fewer change.
    """
    doc = base.model_copy(deep=True)

    by_kind: dict[str, list[Op]] = {}
    for op in ops:
        if op.op in ADVISORY_OPS:
            continue                      # flag_gap / ask_user never mutate
        by_kind.setdefault(op.op, []).append(op)

    for kind in APPLY_ORDER:
        for op in by_kind.get(kind, []):
            _APPLIERS[kind](doc, op)

    return doc


# ── individual appliers ───────────────────────────────────────────────

def _reorder_items(doc: ResumeDoc, op: ReorderItems) -> None:
    section = doc.section(op.section_id)
    if section is None:
        return
    index = {i.id: i for i in section.items}
    if set(op.order) != set(index):
        return                            # not a permutation; validator caught it
    section.items = [index[i] for i in op.order]


def _reorder_bullets(doc: ResumeDoc, op: ReorderBullets) -> None:
    item = doc.item(op.item_id)
    if item is None:
        return
    index = {b.id: b for b in item.bullets}
    if set(op.order) != set(index):
        return
    item.bullets = [index[b] for b in op.order]


def _rewrite_bullet(doc: ResumeDoc, op: RewriteBullet) -> None:
    bullet = doc.bullet(op.bullet_id)
    if bullet is None or not (op.text or "").strip():
        return
    # The id is deliberately kept: provenance survives the rewrite, so the diff
    # can still point at the line this came from.
    bullet.text = op.text.strip()


def _drop_bullet(doc: ResumeDoc, op: DropBullet) -> None:
    try:
        item = doc.item(item_id_of_bullet(op.bullet_id))
    except ValueError:
        return
    if item is None or len(item.bullets) <= 1:
        return                            # never leave an entry with no bullets
    item.bullets = [b for b in item.bullets if b.id != op.bullet_id]


def _promote_item(doc: ResumeDoc, op: PromoteItem) -> None:
    """Move one entry to the top of its section.

    Reverse-chronological is the default order, so this is the sanctioned way
    to break it — and the validator allows at most one per section.
    """
    section = doc.section_of_item(op.item_id)
    if section is None:
        return
    item = next((i for i in section.items if i.id == op.item_id), None)
    if item is None or section.items[0].id == item.id:
        return
    section.items.remove(item)
    section.items.insert(0, item)


def _set_skills(doc: ResumeDoc, op: SetSkills) -> None:
    """Rewrite the skills section as the grouped version.

    Each group becomes one entry printed as `Label: a, b, c` — the shape a
    resume actually uses. A skills section is created if the document had none.

    **The resume's own spelling wins.** The planner reads a normalised,
    lowercased inventory, so it proposes `python fastapi` and `n8n` where the
    page says `Python FastAPI`. Echoing that back would quietly downcase a
    candidate's resume, so every proposed skill that already exists is restored
    to the form the document prints. Only genuinely new skills keep the
    planner's spelling.

    This is presentation, not permission: the validator has already decided
    which skills may appear (`_check_set_skills`).
    """
    groups = [g for g in op.groups if g.skills]
    if not groups:
        return

    section = doc.section_of_kind("skills")
    if section is None:
        section = Section(id=section_id("skills"), kind="skills", heading="SKILLS")
        doc.sections.append(section)

    as_written = existing_skills(doc)

    items: list[Item] = []
    for group in groups:
        iid = item_id("skills", doc.next_seq())
        skills = [as_written.get(skill_key(s), s) for s in group.skills]
        items.append(Item(
            id=iid,
            title=group.label,
            bullets=[Bullet(id=bullet_id(iid, 1), text=", ".join(skills))],
        ))
    section.items = items


def _set_summary(doc: ResumeDoc, op: SetSummary) -> None:
    if (op.text or "").strip():
        doc.summary = op.text.strip()


_APPLIERS = {
    "reorder_items": _reorder_items,
    "reorder_bullets": _reorder_bullets,
    "rewrite_bullet": _rewrite_bullet,
    "drop_bullet": _drop_bullet,
    "promote_item": _promote_item,
    "set_skills": _set_skills,
    "set_summary": _set_summary,
}

# A mutating op without an applier would be silently ignored, so fail at import.
assert set(_APPLIERS) == set(APPLY_ORDER), (
    f"applier/APPLY_ORDER mismatch: {set(_APPLIERS) ^ set(APPLY_ORDER)}"
)
