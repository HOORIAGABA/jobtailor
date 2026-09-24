"""apply + diff. Deterministic — no API key, no network."""
import pytest

from app.domain.models import Bullet, Item, ResumeDoc, Section
from app.domain.ops import (
    AskUser, DropBullet, FlagGap, PromoteItem, Reject, ReorderBullets,
    ReorderItems, RewriteBullet, SetSkills, SetSummary, SkillGroup,
)
from app.engine.apply import apply_ops
from app.engine.diff import build_diff


def _doc() -> ResumeDoc:
    return ResumeDoc(
        summary="Analyst.",
        sections=[
            Section(id="sec.experience", kind="experience", heading="EXPERIENCE", items=[
                Item(id="exp.1", title="Data Analyst", org="Acme", bullets=[
                    Bullet(id="exp.1.b.1", text="Worked on data pipelines"),
                    Bullet(id="exp.1.b.2", text="Built a dashboard saving 12 hours a week"),
                ]),
                Item(id="exp.2", title="Intern", org="Beta", bullets=[
                    Bullet(id="exp.2.b.1", text="Wrote SQL reports"),
                    Bullet(id="exp.2.b.2", text="Helped with migration"),
                ]),
            ]),
            Section(id="sec.skills", kind="skills", heading="SKILLS", items=[
                Item(id="skl.3", title="Languages",
                     bullets=[Bullet(id="skl.3.b.1", text="Python, SQL")]),
            ]),
        ],
        skill_inventory=["Python", "SQL"],
        seq=3,
    )


# ══ apply ═════════════════════════════════════════════════════════════

def test_base_is_never_mutated():
    """I1. Everything downstream depends on a stable left-hand side."""
    base = _doc()
    before = base.model_dump()
    apply_ops(base, [
        RewriteBullet(op_id="1", bullet_id="exp.1.b.1", text="Built Airflow pipelines"),
        SetSummary(op_id="2", text="Data engineer.", cites=["exp.1.b.1"]),
    ])
    assert base.model_dump() == before


def test_rewrite_replaces_text_and_keeps_the_id():
    out = apply_ops(_doc(), [
        RewriteBullet(op_id="1", bullet_id="exp.1.b.1", text="Built Airflow pipelines"),
    ])
    assert out.bullet("exp.1.b.1").text == "Built Airflow pipelines"


def test_reorder_bullets():
    out = apply_ops(_doc(), [
        ReorderBullets(op_id="1", item_id="exp.1", order=["exp.1.b.2", "exp.1.b.1"]),
    ])
    assert out.item("exp.1").bullet_ids() == ["exp.1.b.2", "exp.1.b.1"]


def test_reorder_items():
    out = apply_ops(_doc(), [
        ReorderItems(op_id="1", section_id="sec.experience", order=["exp.2", "exp.1"]),
    ])
    assert out.section("sec.experience").item_ids() == ["exp.2", "exp.1"]


def test_promote_moves_one_entry_to_the_top():
    out = apply_ops(_doc(), [
        PromoteItem(op_id="1", item_id="exp.2", rationale="most relevant"),
    ])
    assert out.section("sec.experience").item_ids() == ["exp.2", "exp.1"]


def test_promoting_the_first_item_is_a_no_op():
    out = apply_ops(_doc(), [PromoteItem(op_id="1", item_id="exp.1", rationale="x")])
    assert out.section("sec.experience").item_ids() == ["exp.1", "exp.2"]


def test_drop_removes_a_bullet():
    out = apply_ops(_doc(), [
        DropBullet(op_id="1", bullet_id="exp.2.b.2", reason="irrelevant"),
    ])
    assert out.item("exp.2").bullet_ids() == ["exp.2.b.1"]


def test_drop_refuses_to_empty_an_entry():
    """Defence in depth: the validator checks this too, but apply must not
    produce a bullet-less entry even if it is called directly."""
    doc = _doc()
    doc.item("exp.2").bullets = [Bullet(id="exp.2.b.1", text="only one")]
    out = apply_ops(doc, [DropBullet(op_id="1", bullet_id="exp.2.b.1", reason="x")])
    assert len(out.item("exp.2").bullets) == 1


def test_set_summary():
    out = apply_ops(_doc(), [
        SetSummary(op_id="1", text="  Data engineer.  ", cites=["exp.1.b.1"]),
    ])
    assert out.summary == "Data engineer."


def test_set_skills_rebuilds_the_section_as_labelled_groups():
    out = apply_ops(_doc(), [
        SetSkills(op_id="1", groups=[
            SkillGroup(label="Languages", skills=["Python", "SQL"]),
            SkillGroup(label="Tools", skills=["Airflow"]),
        ]),
    ])
    section = out.section_of_kind("skills")
    assert [i.title for i in section.items] == ["Languages", "Tools"]
    assert section.items[1].bullets[0].text == "Airflow"


def test_set_skills_creates_the_section_when_absent():
    doc = _doc()
    doc.sections = [s for s in doc.sections if s.kind != "skills"]
    out = apply_ops(doc, [
        SetSkills(op_id="1", groups=[SkillGroup(label="Languages", skills=["Python"])]),
    ])
    assert out.section_of_kind("skills") is not None


def test_new_skill_items_get_fresh_ids():
    out = apply_ops(_doc(), [
        SetSkills(op_id="1", groups=[SkillGroup(label="Languages", skills=["Python"])]),
    ])
    ids = [i.id for i in out.all_items()]
    assert len(ids) == len(set(ids))
    assert out.seq > _doc().seq          # counter advanced, ids never reused


# ── ordering and robustness ───────────────────────────────────────────

def test_application_order_is_independent_of_emission_order():
    """Same ops, shuffled, must give the same document — otherwise runs are
    not reproducible and the diff depends on what the model emitted first."""
    ops = [
        RewriteBullet(op_id="1", bullet_id="exp.1.b.1", text="Built pipelines"),
        ReorderBullets(op_id="2", item_id="exp.1", order=["exp.1.b.2", "exp.1.b.1"]),
        SetSummary(op_id="3", text="Engineer.", cites=["exp.1.b.1"]),
    ]
    a = apply_ops(_doc(), ops)
    b = apply_ops(_doc(), list(reversed(ops)))
    assert a.model_dump() == b.model_dump()


def test_advisory_ops_change_nothing():
    base = _doc()
    out = apply_ops(base, [
        FlagGap(op_id="1", requirement="PyTorch", severity="blocking"),
        AskUser(op_id="2", bullet_id="exp.1.b.1", question="What was the result?"),
    ])
    assert out.model_dump() == base.model_dump()


def test_stale_reference_is_skipped_not_raised():
    """A rewrite targeting a bullet an earlier drop removed must not crash."""
    out = apply_ops(_doc(), [
        DropBullet(op_id="1", bullet_id="exp.2.b.2", reason="x"),
        RewriteBullet(op_id="2", bullet_id="exp.2.b.2", text="ghost"),
    ])
    assert out.item("exp.2").bullet_ids() == ["exp.2.b.1"]


def test_empty_op_list_returns_an_equal_document():
    base = _doc()
    assert apply_ops(base, []).model_dump() == base.model_dump()


def test_reapplying_a_subset_is_how_revert_works():
    """The approval gate reverts by re-applying the remaining ops to the base,
    never by undoing an edit in place."""
    keep = RewriteBullet(op_id="1", bullet_id="exp.1.b.1", text="Built pipelines")
    drop = SetSummary(op_id="2", text="Engineer.", cites=["exp.1.b.1"])
    full = apply_ops(_doc(), [keep, drop])
    reverted = apply_ops(_doc(), [keep])
    assert full.summary == "Engineer."
    assert reverted.summary == "Analyst."                 # back to the original
    assert reverted.bullet("exp.1.b.1").text == "Built pipelines"


# ══ diff ══════════════════════════════════════════════════════════════

def test_rewrite_change_carries_before_after_and_reason():
    base = _doc()
    op = RewriteBullet(op_id="1", bullet_id="exp.1.b.1",
                       text="Built Airflow pipelines", rationale="surface orchestration")
    tailored = apply_ops(base, [op])
    d = build_diff(base, tailored, [op])
    c = d.changes[0]
    assert c.before == "Worked on data pipelines"
    assert c.after == "Built Airflow pipelines"
    assert c.rationale == "surface orchestration"
    assert c.ref_id == "exp.1.b.1"
    assert c.label == "Data Analyst · Acme"               # where, in human terms


def test_summary_change_carries_its_citations():
    base = _doc()
    op = SetSummary(op_id="1", text="Data engineer.", cites=["exp.1.b.1", "exp.1.b.2"])
    d = build_diff(base, apply_ops(base, [op]), [op])
    assert d.changes[0].cites == ["exp.1.b.1", "exp.1.b.2"]


def test_no_op_produces_no_row():
    """A rewrite that changes nothing must not appear as a reviewable change."""
    base = _doc()
    op = RewriteBullet(op_id="1", bullet_id="exp.1.b.1", text="Worked on data pipelines")
    d = build_diff(base, apply_ops(base, [op]), [op])
    assert d.changes == [] and d.is_empty()


def test_reorder_is_described_by_content_not_by_ids():
    base = _doc()
    op = ReorderBullets(op_id="1", item_id="exp.1", order=["exp.1.b.2", "exp.1.b.1"])
    d = build_diff(base, apply_ops(base, [op]), [op])
    c = d.changes[0]
    assert "Built a dashboard" in c.after and "Worked on data" in c.before
    assert c.before != c.after


def test_drop_is_shown_as_removed():
    base = _doc()
    op = DropBullet(op_id="1", bullet_id="exp.2.b.2", reason="not relevant")
    d = build_diff(base, apply_ops(base, [op]), [op])
    assert d.changes[0].after == "(removed)"
    assert d.changes[0].rationale == "not relevant"


def test_skills_change_renders_as_a_readable_line():
    base = _doc()
    op = SetSkills(op_id="1", groups=[
        SkillGroup(label="Languages", skills=["Python", "SQL"]),
        SkillGroup(label="Tools", skills=["Airflow"]),
    ])
    d = build_diff(base, apply_ops(base, [op]), [op])
    assert d.changes[0].before == "Languages: Python, SQL"
    assert "Tools: Airflow" in d.changes[0].after


def test_gaps_and_questions_are_separated_from_changes():
    base = _doc()
    ops = [
        FlagGap(op_id="1", requirement="PyTorch", severity="blocking"),
        AskUser(op_id="2", bullet_id="exp.1.b.1", question="What was the result?"),
    ]
    d = build_diff(base, apply_ops(base, ops), ops)
    assert d.changes == []
    assert d.gaps[0].requirement == "PyTorch"
    assert d.questions[0].context == "Worked on data pipelines"   # asks in context


def test_rejections_are_surfaced_to_the_reviewer():
    d = build_diff(_doc(), _doc(), [], rejections=[
        Reject(op_id="9", op_kind="rewrite_bullet", code="fabricated_number",
               detail="introduces ['40']", ask_user=True),
    ])
    assert d.rejections[0].code == "fabricated_number"


def test_counts_summarize_the_review():
    base = _doc()
    ops = [
        RewriteBullet(op_id="1", bullet_id="exp.1.b.1", text="Built pipelines"),
        RewriteBullet(op_id="2", bullet_id="exp.2.b.1", text="Wrote reporting SQL"),
        SetSummary(op_id="3", text="Engineer.", cites=["exp.1.b.1"]),
    ]
    d = build_diff(base, apply_ops(base, ops), ops)
    assert d.counts() == {"rewrite_bullet": 2, "set_summary": 1}


def test_every_change_is_attributable_to_an_op():
    """The product claim, asserted: no change appears without an op id."""
    base = _doc()
    ops = [
        RewriteBullet(op_id="a", bullet_id="exp.1.b.1", text="Built pipelines"),
        ReorderBullets(op_id="b", item_id="exp.2", order=["exp.2.b.2", "exp.2.b.1"]),
        SetSummary(op_id="c", text="Engineer.", cites=["exp.1.b.1"]),
    ]
    d = build_diff(base, apply_ops(base, ops), ops)
    assert {c.op_id for c in d.changes} == {"a", "b", "c"}
    assert all(c.ref_id for c in d.changes)
