"""The whole engine, end to end, with no model and no network.

    RawResume ──normalize──► ResumeDoc ──validate──► accepted / rejected
                                   └──apply──► TailoredDoc ──diff──► review

This is the test that shows why the deterministic layer is worth having: every
guarantee the product advertises is asserted here, in 0.1 seconds, without an
API key. In the real pipeline a model supplies the operations; here they are
written by hand, which is exactly the point — the guarantees do not depend on
the model behaving.

Scenario: a data analyst applying for a Machine Learning Engineer role that
wants Python, Airflow and PyTorch.
"""
from app.domain.models import Contact, RawEntry, RawResume, RawSection
from app.domain.ops import (
    AskUser, FlagGap, PromoteItem, ReorderBullets, RewriteBullet, SetSkills,
    SetSummary, SkillGroup,
)
from app.engine.apply import apply_ops
from app.engine.diff import build_diff
from app.engine.normalize import normalize
from app.engine.validator import validate


def _raw() -> RawResume:
    return RawResume(
        contact=Contact(full_name="R. Khan", email="r@example.com"),
        sections=[
            RawSection(heading="WORK EXPERIENCE", entries=[
                RawEntry(
                    title="Data Analyst", org="Acme Corp", dates="Jan 2022 - Present",
                    bullets=[
                        "Worked on data pipelines for the reporting team",
                        "Built a dashboard that reduced manual reporting by 12 hours a week",
                    ],
                ),
                RawEntry(
                    title="Intern", org="Beta Labs", dates="2021",
                    bullets=["Helped with a data migration", "Wrote SQL reports"],
                ),
            ]),
            RawSection(heading="TECHNICAL SKILLS", entries=[
                RawEntry(bullets=["Languages: Python, SQL", "Tools: Airflow, Docker"]),
            ]),
            RawSection(heading="PUBLICATIONS", entries=[
                RawEntry(title="A short paper on forecasting"),
            ]),
        ],
    )


def _plan(doc) -> list:
    """What a planner + writer would produce for this job.

    Deliberately mixed: four good operations and three the validator must
    refuse, so the test proves the guard fires rather than assuming it.
    """
    exp = doc.section_of_kind("experience")
    analyst, intern = exp.items[0], exp.items[1]
    b1, b2 = analyst.bullets[0].id, analyst.bullets[1].id

    return [
        # -- legitimate --------------------------------------------------
        SetSummary(op_id="o1",
                   text="Data analyst building production pipelines in Python and Airflow.",
                   cites=[b1, b2], rationale="lead with the role's core work"),
        RewriteBullet(op_id="o2", bullet_id=b1,
                      text="Built and maintained Airflow ingestion pipelines in Python "
                           "for the reporting team",
                      target_terms=["Airflow", "Python"],
                      rationale="Python/Airflow were claimed but never demonstrated"),
        ReorderBullets(op_id="o3", item_id=analyst.id, order=[b2, b1]),
        SetSkills(op_id="o4", groups=[
            SkillGroup(label="Languages", skills=["Python", "SQL"]),
            SkillGroup(label="Tools", skills=["Airflow", "Docker"]),
        ]),
        FlagGap(op_id="o5", requirement="PyTorch", severity="blocking"),

        # -- must be refused ---------------------------------------------
        RewriteBullet(op_id="x1", bullet_id=b2,
                      text="Built a dashboard that saved 624 hours annually",
                      rationale="quantify"),                    # derived number
        RewriteBullet(op_id="x2", bullet_id=intern.bullets[0].id,
                      text="Drove the data migration end-to-end",
                      rationale="strengthen"),                  # escalation
        SetSkills(op_id="x3", groups=[SkillGroup(label="ML", skills=["PyTorch"])]),
    ]


def test_engine_end_to_end():
    # ── S0.3 normalize ────────────────────────────────────────────────
    doc = normalize(_raw())

    assert [s.kind for s in doc.sections] == ["experience", "skills", "custom"]
    assert doc.section_of_kind("custom").heading == "PUBLICATIONS"   # nothing lost
    assert [s.lower() for s in doc.skill_inventory] == ["python", "sql", "airflow", "docker"]
    assert doc.section_of_kind("experience").items[0].date_start == "2022-01"

    # ── S5 validate ───────────────────────────────────────────────────
    result = validate(_plan(doc), doc)

    assert {op.op_id for op in result.accepted} == {"o1", "o2", "o3", "o4", "o5"}
    by_op = {r.op_id: r.code for r in result.rejected}
    assert by_op == {
        "x1": "fabricated_number",        # 624 is correct arithmetic, still invented
        "x2": "seniority_escalation",     # "helped" -> "drove"
        "x3": "unsupported_entity",       # PyTorch appears nowhere
    }
    assert result.fabrication_count == 2
    assert any(r.ask_user for r in result.rejected)   # asks rather than silently dropping

    # ── S6 apply ──────────────────────────────────────────────────────
    before = doc.model_dump()
    tailored = apply_ops(doc, result.accepted)
    assert doc.model_dump() == before                 # I1: base untouched

    analyst = tailored.section_of_kind("experience").items[0]
    assert analyst.bullets[0].text.startswith("Built a dashboard")      # reordered
    assert "Airflow" in analyst.bullets[1].text                         # rewritten
    assert tailored.summary.startswith("Data analyst building")

    # nothing was fabricated into the document
    rendered = " ".join(b.text for b in tailored.all_bullets()) + tailored.summary
    assert "624" not in rendered
    assert "PyTorch" not in rendered
    assert "Drove" not in rendered

    # nothing was lost from the document
    assert len(list(tailored.all_bullets())) == len(list(doc.all_bullets()))
    assert [s.heading for s in tailored.sections] == [s.heading for s in doc.sections]

    # ── S7 diff ───────────────────────────────────────────────────────
    review = build_diff(doc, tailored, result.accepted, result.rejected)

    assert review.counts() == {
        "set_summary": 1, "rewrite_bullet": 1, "reorder_bullets": 1, "set_skills": 1,
    }
    assert all(c.op_id and c.ref_id for c in review.changes)   # every change attributable
    assert review.gaps[0].requirement == "PyTorch"
    assert len(review.rejections) == 3

    rewrite = next(c for c in review.changes if c.op_kind == "rewrite_bullet")
    assert rewrite.before == "Worked on data pipelines for the reporting team"
    assert rewrite.label == "Data Analyst · Acme Corp"
    assert rewrite.rationale


def test_reverting_one_change_at_the_gate():
    """The approval gate reverts by re-applying the survivors to the base."""
    doc = normalize(_raw())
    accepted = validate(_plan(doc), doc).accepted

    kept = [op for op in accepted if op.op_id != "o1"]      # reviewer rejects the summary
    tailored = apply_ops(doc, kept)

    assert tailored.summary == doc.summary                  # summary reverted
    analyst = tailored.section_of_kind("experience").items[0]
    assert "Airflow" in analyst.bullets[1].text             # other edits survive


def test_a_question_reaches_the_reviewer_in_context():
    doc = normalize(_raw())
    b = doc.section_of_kind("experience").items[0].bullets[0].id
    ops = [AskUser(op_id="q1", bullet_id=b,
                   question="What did you use to schedule and retry these jobs?")]
    review = build_diff(doc, apply_ops(doc, ops), ops)

    q = review.questions[0]
    assert q.context == "Worked on data pipelines for the reporting team"
    # open-ended: the question must not name the term it is fishing for
    assert "airflow" not in q.question.lower()


def test_promotion_is_capped_at_one_per_section():
    doc = normalize(_raw())
    exp = doc.section_of_kind("experience")
    result = validate([
        PromoteItem(op_id="p1", item_id=exp.items[1].id, rationale="more relevant"),
        PromoteItem(op_id="p2", item_id=exp.items[0].id, rationale="also relevant"),
    ], doc)

    assert len(result.accepted) == 1
    assert result.rejected[0].code == "too_many_promotions"

    tailored = apply_ops(doc, result.accepted)
    assert tailored.section_of_kind("experience").items[0].id == exp.items[1].id
