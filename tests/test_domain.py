"""Domain layer tests. No API key, no database, no network."""
import pytest
from pydantic import TypeAdapter, ValidationError

from app.domain.ids import bullet_id, is_bullet_id, item_id, item_id_of_bullet, section_id
from app.domain.models import (
    Bullet, EvidenceIndex, EvidenceLink, Grounded, Item, JobBrief,
    RawResume, RawSection, RawEntry, ResumeDoc, Section, Term,
)
from app.domain.ops import (
    ADVISORY_OPS, APPLY_ORDER, Op, OpList, RewriteBullet, SetSkills, SkillGroup,
)


# ── ids ───────────────────────────────────────────────────────────────

def test_id_shapes():
    assert section_id("experience") == "sec.experience"
    assert section_id("custom", 3) == "sec.custom.3"
    assert item_id("experience", 1) == "exp.1"
    assert item_id("projects", 2) == "prj.2"
    assert bullet_id("exp.1", 4) == "exp.1.b.4"


def test_unknown_kind_falls_back_to_custom_prefix():
    assert item_id("publications", 1) == "cst.1"


def test_bullet_id_round_trip():
    bid = bullet_id(item_id("experience", 7), 2)
    assert bid == "exp.7.b.2"
    assert item_id_of_bullet(bid) == "exp.7"
    assert is_bullet_id(bid)
    assert not is_bullet_id("exp.7")


def test_item_id_of_bullet_rejects_malformed():
    with pytest.raises(ValueError):
        item_id_of_bullet("exp.7")


# ── document ──────────────────────────────────────────────────────────

def _doc() -> ResumeDoc:
    return ResumeDoc(
        summary="Engineer.",
        sections=[
            Section(id="sec.experience", kind="experience", heading="WORK EXPERIENCE", items=[
                Item(id="exp.1", title="Data Analyst", org="Acme", bullets=[
                    Bullet(id="exp.1.b.1", text="Worked on data pipelines"),
                    Bullet(id="exp.1.b.2", text="Built a dashboard saving 12 hours a week"),
                ]),
                Item(id="exp.2", title="Intern", org="Beta", bullets=[
                    Bullet(id="exp.2.b.1", text="Wrote SQL reports"),
                ]),
            ]),
            Section(id="sec.custom.1", kind="custom", heading="PUBLICATIONS"),
        ],
        skill_inventory=["Python", "SQL", "Airflow"],
        seq=2,
    )


def test_lookup_by_id():
    d = _doc()
    assert d.bullet("exp.1.b.2").text.startswith("Built a dashboard")
    assert d.item("exp.2").title == "Intern"
    assert d.section("sec.experience").kind == "experience"
    assert d.section_of_kind("custom").heading == "PUBLICATIONS"


def test_lookup_misses_return_none_not_raise():
    d = _doc()
    assert d.bullet("exp.9.b.1") is None
    assert d.bullet("not-a-bullet-id") is None
    assert d.item("exp.99") is None
    assert d.section("sec.nope") is None


def test_section_of_item():
    assert _doc().section_of_item("exp.2").id == "sec.experience"


def test_iteration_covers_everything():
    d = _doc()
    assert [i.id for i in d.all_items()] == ["exp.1", "exp.2"]
    assert len(list(d.all_bullets())) == 3


def test_text_of_resolves_any_id_level():
    d = _doc()
    assert d.text_of("exp.1.b.1") == "Worked on data pipelines"
    assert d.text_of("exp.1") == "Data Analyst Acme"
    assert d.text_of("sec.experience") == "WORK EXPERIENCE"
    assert d.text_of("nonsense") == ""


def test_next_seq_is_monotonic():
    """New entities must never reuse an id, even after deletions."""
    d = _doc()
    assert [d.next_seq(), d.next_seq(), d.next_seq()] == [3, 4, 5]
    assert d.seq == 5


def test_headings_are_preserved_verbatim():
    """An unmapped section keeps its own heading rather than being forced into a bucket."""
    s = _doc().section("sec.custom.1")
    assert s.kind == "custom" and s.heading == "PUBLICATIONS"


# ── raw shape ─────────────────────────────────────────────────────────

def test_raw_resume_does_not_classify():
    """The extraction schema mirrors the document: headings, no domain kinds."""
    raw = RawResume(sections=[
        RawSection(heading="WORK EXPERIENCE", entries=[RawEntry(title="Data Analyst")]),
    ])
    assert raw.sections[0].heading == "WORK EXPERIENCE"
    assert not hasattr(raw.sections[0], "kind")


# ── job side ──────────────────────────────────────────────────────────

def test_alias_forms_are_lowercased_and_deduped():
    t = Term(term="Kubernetes", aliases=["K8s", "kubernetes", " "])
    assert t.alias_forms() == ["k8s", "kubernetes"]


JD = "We need someone to own model retraining, which is currently manual and slow."


def test_grounded_verify_accepts_a_faithful_span():
    span = (JD.index("own model retraining"), JD.index("manual and slow") + len("manual and slow"))
    g = Grounded(statement="own model retraining which is manual", source_span=span)
    assert g.verify(JD)


def test_grounded_verify_rejects_out_of_range_span():
    assert not Grounded(statement="anything", source_span=(0, 10_000)).verify(JD)
    assert not Grounded(statement="anything", source_span=(50, 10)).verify(JD)


def test_grounded_verify_rejects_a_span_that_says_something_else():
    """The anti-hallucination check: cite a real span, but not for this claim."""
    g = Grounded(statement="deploy Kubernetes clusters across regions", source_span=(0, 20))
    assert not g.verify(JD)


def test_grounded_verify_rejects_stopword_only_statement():
    assert not Grounded(statement="the and of", source_span=(0, 20)).verify(JD)


def test_job_brief_defaults_are_honest():
    """Empty is a valid answer — no field is fabricated to satisfy a minimum."""
    b = JobBrief()
    assert b.recruiter_email == "" and b.terms == [] and b.problems_to_solve == []
    assert b.tone == "unclear"              # the enum escape hatch


# ── evidence ──────────────────────────────────────────────────────────

def test_evidence_index_has_no_scalar_score():
    """Guards D-11: a maximizable score must not exist to be optimized."""
    fields = set(EvidenceIndex.model_fields)
    assert fields == {"links", "unmatched_elements"}
    for banned in ("score", "coverage", "coverage_score", "total"):
        assert banned not in fields


def test_evidence_lookup_helpers():
    idx = EvidenceIndex(links=[
        EvidenceLink(job_element_id="p1", bullet_id="exp.1.b.1", lexical_hit=True, strength="strong"),
        EvidenceLink(job_element_id="p1", bullet_id="exp.1.b.2", similarity=0.61),
        EvidenceLink(job_element_id="p2", bullet_id="exp.2.b.1", similarity=0.58),
    ])
    assert len(idx.for_element("p1")) == 2
    assert idx.for_bullet("exp.2.b.1")[0].job_element_id == "p2"


# ── ops ───────────────────────────────────────────────────────────────

_adapter = TypeAdapter(Op)


def test_discriminator_selects_the_right_variant():
    op = _adapter.validate_python({"op": "rewrite_bullet", "bullet_id": "exp.1.b.1"})
    assert isinstance(op, RewriteBullet) and op.text == ""


def test_unknown_op_kind_is_rejected():
    with pytest.raises(ValidationError):
        _adapter.validate_python({"op": "delete_everything"})


def test_op_list_round_trips_through_json_schema():
    """OpList is what gets handed to the model as a response schema."""
    schema = OpList.model_json_schema()
    assert "ops" in schema["properties"]
    parsed = OpList.model_validate({"ops": [
        {"op": "reorder_bullets", "item_id": "exp.1", "order": ["exp.1.b.2", "exp.1.b.1"]},
        {"op": "flag_gap", "requirement": "PyTorch", "severity": "blocking"},
    ]})
    assert [o.op for o in parsed.ops] == ["reorder_bullets", "flag_gap"]


def test_writer_cannot_self_report_validation_fields():
    """W10: fields the validator trusts must not be model-supplied.

    `numbers_used` / `terms_used` were removed — a model that under-declares
    would otherwise walk straight past the fabrication guard.
    """
    fields = set(RewriteBullet.model_fields)
    assert "numbers_used" not in fields
    assert "terms_used" not in fields
    # extra keys are ignored rather than silently trusted
    op = RewriteBullet.model_validate(
        {"op": "rewrite_bullet", "bullet_id": "exp.1.b.1",
         "text": "Cut latency 40%", "numbers_used": []}
    )
    assert not hasattr(op, "numbers_used")


def test_set_skills_flattens_groups():
    op = SetSkills(groups=[
        SkillGroup(label="Languages", skills=["Python", "SQL"]),
        SkillGroup(label="Tools", skills=["Airflow"]),
    ])
    assert op.all_skills() == ["Python", "SQL", "Airflow"]


def test_advisory_ops_never_mutate():
    assert ADVISORY_OPS == {"flag_gap", "ask_user"}
    assert not (ADVISORY_OPS & set(APPLY_ORDER))


def test_apply_order_covers_every_mutating_op():
    """A new mutating op must be given a position, or application is undefined."""
    all_kinds = {
        f.default
        for variant in Op.__origin__.__args__          # type: ignore[attr-defined]
        for name, f in variant.model_fields.items() if name == "op"
    }
    assert set(APPLY_ORDER) == all_kinds - ADVISORY_OPS
