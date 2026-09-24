"""Edit operations — the only way the resume ever changes.

    tailored = base + validated ops

The model never returns a resume. It returns a list of these, and each one is
validated in Python before anything is applied. That single choice is what makes
the system auditable: an op names its target, carries its reason, and cites the
evidence behind it, so the diff and the provenance trail fall out for free.

Design notes that are easy to undo by accident:

* **Flat and shallow.** Nine variants is already a lot of schema surface for a
  model to select from. No nested unions, no optional object fields.
* **No self-reported validation fields.** An earlier draft had the writer
  declare `numbers_used` / `terms_used`, which the validator then trusted. A
  model that under-declares evades the fabrication guard entirely. The validator
  extracts both from the text itself.
* **`rationale` is for the human**, not the model — it is what the diff shows
  on hover at the approval gate.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class _Op(BaseModel):
    """Common fields. `op_id` is assigned by the planner node, not the model."""
    op_id: str = ""


class SetSummary(_Op):
    op: Literal["set_summary"] = "set_summary"
    text: str
    cites: list[str] = Field(
        default_factory=list,
        description="Bullet ids supporting the claims made. Required — a summary "
                    "that cites nothing cannot be checked.",
    )
    rationale: str = ""


class SkillGroup(BaseModel):
    label: str = ""
    skills: list[str] = Field(default_factory=list)


class SetSkills(_Op):
    op: Literal["set_skills"] = "set_skills"
    groups: list[SkillGroup] = Field(default_factory=list)
    rationale: str = ""

    def all_skills(self) -> list[str]:
        return [s for g in self.groups for s in g.skills]


class ReorderItems(_Op):
    op: Literal["reorder_items"] = "reorder_items"
    section_id: str
    order: list[str] = Field(
        default_factory=list,
        description="Must be a permutation of the section's existing item ids — "
                    "so a reorder can never drop or duplicate an entry.",
    )


class ReorderBullets(_Op):
    op: Literal["reorder_bullets"] = "reorder_bullets"
    item_id: str
    order: list[str] = Field(default_factory=list)


class PromoteItem(_Op):
    """Move one item out of reverse-chronological order.

    Capped at one per section by the validator. Reverse-chronological is what
    ATS parsers and human readers both expect; breaking it needs a reason.
    """
    op: Literal["promote_item"] = "promote_item"
    item_id: str
    rationale: str
    cites: list[str] = Field(default_factory=list)


class RewriteBullet(_Op):
    """Emitted by the planner with `text=""` (a target), filled by the writer."""
    op: Literal["rewrite_bullet"] = "rewrite_bullet"
    bullet_id: str
    text: str = ""
    target_terms: list[str] = Field(default_factory=list)
    rationale: str = ""


class DropBullet(_Op):
    op: Literal["drop_bullet"] = "drop_bullet"
    bullet_id: str
    reason: str


class FlagGap(_Op):
    """Advisory. Never mutates the document — it surfaces in the UI."""
    op: Literal["flag_gap"] = "flag_gap"
    requirement: str
    severity: Literal["blocking", "major", "minor"] = "major"
    closest_evidence: list[str] = Field(default_factory=list)


class AskUser(_Op):
    """A question only the candidate can answer. Never mutates the document.

    Emitted when the planner wants a fact the resume doesn't state, and when the
    validator rejects a rewrite for introducing one. The question must be
    open-ended and must NOT name the term being fished for — naming it shows the
    candidate the answer that improves their resume.
    """
    op: Literal["ask_user"] = "ask_user"
    bullet_id: str = ""
    question: str


Op = Annotated[
    Union[
        SetSummary, SetSkills, ReorderItems, ReorderBullets,
        PromoteItem, RewriteBullet, DropBullet, FlagGap, AskUser,
    ],
    Field(discriminator="op"),
]


class OpList(BaseModel):
    """Wrapper so the whole union can be passed as one JSON schema to the model."""
    ops: list[Op] = Field(default_factory=list)


# ── Rejection ─────────────────────────────────────────────────────────

RejectCode = Literal[
    "fabricated_number",
    "unsupported_entity",
    "seniority_escalation",
    "keyword_stuffing",
    "term_density",
    "not_a_permutation",
    "unknown_id",
    "missing_citation",
    "too_many_promotions",
    "would_empty_item",
]


class Reject(BaseModel):
    """Why an op was refused. Counted as an eval metric, shown in the UI."""
    op_id: str
    op_kind: str
    code: RejectCode
    detail: str = ""
    ask_user: bool = False        # emit an AskUser instead of silently dropping


# Ops that never change the document — they route to the UI instead.
ADVISORY_OPS: frozenset[str] = frozenset({"flag_gap", "ask_user"})

# Deterministic application order. Ops must be ordered or results depend on the
# order the model happened to emit them in, which makes runs irreproducible.
APPLY_ORDER: tuple[str, ...] = (
    "reorder_items",
    "reorder_bullets",
    "rewrite_bullet",
    "drop_bullet",
    "promote_item",
    "set_skills",
    "set_summary",
)
