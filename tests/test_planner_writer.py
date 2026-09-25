"""Planner and writer — scripted client, no key, no network."""
import pytest

from app.agents.planner import (
    MAX_OPS, build_payload, clean_ops, plan, rewrite_targets,
)
from app.agents.writer import WriterOutput, build_payload as writer_payload, write_bullets
from app.domain.models import (
    Bullet, EvidenceIndex, EvidenceLink, Item, JobBrief, Problem, ResumeDoc,
    Section, Term,
)
from app.domain.ops import (
    AskUser, DropBullet, FlagGap, PromoteItem, ReorderBullets, ReorderItems,
    RewriteBullet, SetSkills, SetSummary, SkillGroup,
)
from app.engine.evidence import compute_evidence
from app.engine.validator import grounding_corpus, validate
from app.io.llm import ScriptedClient


def _doc() -> ResumeDoc:
    return ResumeDoc(
        summary="Data analyst.",
        sections=[
            Section(id="sec.experience", kind="experience", heading="EXPERIENCE", items=[
                Item(id="exp.1", title="Data Analyst", org="Acme", bullets=[
                    Bullet(id="exp.1.b.1", text="Worked on data pipelines for the reporting team"),
                    Bullet(id="exp.1.b.2", text="Built a dashboard that saved 12 hours a week"),
                ]),
                Item(id="exp.2", title="Intern", org="Beta", bullets=[
                    Bullet(id="exp.2.b.1", text="Helped with a data migration"),
                ]),
            ]),
            Section(id="sec.skills", kind="skills", heading="SKILLS", items=[
                Item(id="skl.3", title="Tools",
                     bullets=[Bullet(id="skl.3.b.1", text="Python, SQL, Airflow")]),
            ]),
        ],
        skill_inventory=["Python", "SQL", "Airflow"],
        seq=3,
    )


def _brief() -> JobBrief:
    return JobBrief(
        role="Machine Learning Engineer", company="Nimbus", tone="pragmatic",
        role_narrative="Owns training and serving pipelines.",
        problems_to_solve=[Problem(
            statement="automate manual model retraining", source_span=(0, 1),
            priority="core")],
        terms=[Term(term="Python", weight=9, required=True),
               Term(term="Airflow", weight=8, required=True),
               Term(term="PyTorch", weight=10, required=True)],
        excerpt="Machine Learning Engineer at Nimbus...",
    )


def _index(doc, brief):
    return compute_evidence(brief, doc)


# ══ planner payload ═══════════════════════════════════════════════════

def test_payload_exposes_every_bullet_by_id():
    doc, brief = _doc(), _brief()
    payload = build_payload(brief, doc, _index(doc, brief), doc.skill_inventory)
    bullets = {
        bid
        for section in payload["resume"]["sections"]
        for item in section["items"]
        for bid in item["bullets"]
    }
    assert bullets == {b.id for b in doc.all_bullets()}


def test_payload_sends_the_brief_not_the_raw_posting():
    """The planner reasons from the brief; it gets an excerpt, not the full JD."""
    doc, brief = _doc(), _brief()
    payload = build_payload(brief, doc, _index(doc, brief), doc.skill_inventory)
    assert payload["brief"]["narrative"]
    assert payload["brief"]["posting_excerpt"] == brief.excerpt


def test_payload_includes_grounding_and_gaps():
    doc, brief = _doc(), _brief()
    payload = build_payload(brief, doc, _index(doc, brief), grounding_corpus(doc))
    assert "python" in payload["grounding"]
    assert any("PyTorch" in g for g in payload["gaps"])     # nothing evidences it


def test_payload_translates_evidence_ids_to_readable_text():
    doc, brief = _doc(), _brief()
    payload = build_payload(brief, doc, _index(doc, brief), doc.skill_inventory)
    for _bid, elements in payload["evidence"]["by_bullet"].items():
        assert all(not e.startswith(("problem.", "term.")) for e in elements)


# ══ cleaning the model's output ═══════════════════════════════════════

def test_ops_get_sequential_ids():
    ops = clean_ops([
        RewriteBullet(bullet_id="exp.1.b.1"),
        FlagGap(requirement="PyTorch"),
    ], _doc())
    assert [o.op_id for o in ops] == ["op1", "op2"]


@pytest.mark.parametrize("op", [
    RewriteBullet(bullet_id="exp.9.b.9"),
    DropBullet(bullet_id="nope.1.b.1", reason="x"),
    ReorderBullets(item_id="exp.99", order=[]),
    ReorderItems(section_id="sec.nope", order=[]),
    PromoteItem(item_id="exp.99", rationale="x"),
])
def test_unresolvable_targets_are_dropped(op):
    """Kept out of the rejection list so it shows refusals on substance, not typos."""
    assert clean_ops([op], _doc()) == []


def test_advisory_ops_survive_without_a_target():
    ops = clean_ops([FlagGap(requirement="PyTorch"), AskUser(question="what result?")], _doc())
    assert len(ops) == 2


def test_plan_is_capped():
    many = [FlagGap(requirement=f"r{i}") for i in range(50)]
    assert len(clean_ops(many, _doc())) == MAX_OPS


def test_rewrite_targets_are_identified():
    ops = clean_ops([
        RewriteBullet(bullet_id="exp.1.b.1"),
        SetSummary(text="x", cites=["exp.1.b.1"]),
        RewriteBullet(bullet_id="exp.1.b.2"),
    ], _doc())
    assert [o.bullet_id for o in rewrite_targets(ops)] == ["exp.1.b.1", "exp.1.b.2"]


# ══ the planner stage ═════════════════════════════════════════════════

def _plan_response() -> dict:
    return {"ops": [
        {"op": "rewrite_bullet", "bullet_id": "exp.1.b.1",
         "target_terms": ["Airflow", "Python"], "rationale": "vague opener"},
        {"op": "reorder_bullets", "item_id": "exp.1",
         "order": ["exp.1.b.2", "exp.1.b.1"]},
        {"op": "set_summary", "text": "Analyst building data pipelines.",
         "cites": ["exp.1.b.1"], "rationale": "lead with the job's core work"},
        {"op": "flag_gap", "requirement": "PyTorch", "severity": "blocking"},
        {"op": "ask_user", "bullet_id": "exp.1.b.1",
         "question": "What did you use to schedule and retry these jobs?"},
    ]}


def test_planner_returns_typed_operations():
    doc, brief = _doc(), _brief()
    client = ScriptedClient([_plan_response()])
    ops = plan(brief, doc, _index(doc, brief), doc.skill_inventory, client)
    assert [o.op for o in ops] == [
        "rewrite_bullet", "reorder_bullets", "set_summary", "flag_gap", "ask_user"]


def test_planner_emits_rewrites_without_prose():
    """The planner names targets; the writer supplies the words."""
    doc, brief = _doc(), _brief()
    client = ScriptedClient([_plan_response()])
    ops = plan(brief, doc, _index(doc, brief), doc.skill_inventory, client)
    assert rewrite_targets(ops)[0].text == ""


def test_planner_uses_low_temperature():
    doc, brief = _doc(), _brief()
    client = ScriptedClient([_plan_response()])
    plan(brief, doc, _index(doc, brief), doc.skill_inventory, client)
    assert client.calls[0]["temperature"] == 0.2


# ══ the writer stage ══════════════════════════════════════════════════

def _targets(doc):
    return [RewriteBullet(op_id="op1", bullet_id="exp.1.b.1",
                          target_terms=["Airflow", "Python"], rationale="vague")]


def test_writer_only_sees_the_targeted_lines():
    doc = _doc()
    payload = writer_payload(_targets(doc), doc, _brief())
    assert [b["bullet_id"] for b in payload["bullets"]] == ["exp.1.b.1"]
    assert payload["bullets"][0]["original"] == "Worked on data pipelines for the reporting team"


def test_writer_payload_carries_context_and_tone():
    doc = _doc()
    payload = writer_payload(_targets(doc), doc, _brief())
    entry = payload["bullets"][0]
    assert entry["entry"] == "Data Analyst at Acme"
    assert "Built a dashboard that saved 12 hours a week" in entry["sibling_bullets"]
    assert payload["tone"] == "pragmatic"


def test_writer_fills_in_the_text():
    doc = _doc()
    client = ScriptedClient([{"bullets": [
        {"bullet_id": "exp.1.b.1",
         "text": "Built and maintained Airflow pipelines in Python for the reporting team"}
    ]}])
    ops = write_bullets(_targets(doc), doc, _brief(), client)
    assert ops[0].text.startswith("Built and maintained Airflow")


def test_writer_uses_high_temperature():
    """The one stage that wants variety rather than determinism."""
    doc = _doc()
    client = ScriptedClient([{"bullets": []}])
    write_bullets(_targets(doc), doc, _brief(), client)
    assert client.calls[0]["temperature"] == 0.6


def test_an_unchanged_rewrite_is_dropped():
    """A rewrite that returns the original is not a change to review."""
    doc = _doc()
    client = ScriptedClient([{"bullets": [
        {"bullet_id": "exp.1.b.1", "text": "Worked on data pipelines for the reporting team"}
    ]}])
    assert write_bullets(_targets(doc), doc, _brief(), client) == []


def test_a_skipped_bullet_is_dropped():
    doc = _doc()
    client = ScriptedClient([{"bullets": []}])
    assert write_bullets(_targets(doc), doc, _brief(), client) == []


def test_non_rewrite_ops_pass_through_untouched():
    doc = _doc()
    ops = [FlagGap(op_id="op1", requirement="PyTorch"),
           SetSummary(op_id="op2", text="x", cites=["exp.1.b.1"])]
    client = ScriptedClient([])          # no rewrites, so no call is made
    assert write_bullets(ops, doc, _brief(), client) == ops


def test_writer_makes_exactly_one_call_for_many_bullets():
    doc = _doc()
    targets = [
        RewriteBullet(op_id="op1", bullet_id="exp.1.b.1"),
        RewriteBullet(op_id="op2", bullet_id="exp.1.b.2"),
        RewriteBullet(op_id="op3", bullet_id="exp.2.b.1"),
    ]
    client = ScriptedClient([{"bullets": [
        {"bullet_id": "exp.1.b.1", "text": "Built Airflow pipelines in Python"},
        {"bullet_id": "exp.1.b.2", "text": "Shipped a dashboard saving 12 hours weekly"},
        {"bullet_id": "exp.2.b.1", "text": "Supported a data migration between systems"},
    ]}])
    ops = write_bullets(targets, doc, _brief(), client)
    assert len(ops) == 3 and len(client.calls) == 1


# ══ planner + writer + validator together ═════════════════════════════

def test_a_bad_plan_is_caught_downstream_and_the_run_survives():
    """The point of the whole arrangement: the model can be wrong safely."""
    doc, brief = _doc(), _brief()
    planner = ScriptedClient([{"ops": [
        {"op": "rewrite_bullet", "bullet_id": "exp.1.b.1",
         "target_terms": ["Airflow"], "rationale": "surface orchestration"},
        {"op": "rewrite_bullet", "bullet_id": "exp.1.b.2",
         "target_terms": [], "rationale": "quantify"},
        {"op": "rewrite_bullet", "bullet_id": "exp.2.b.1",
         "target_terms": [], "rationale": "strengthen"},
        # Keeps every existing skill, so the only thing wrong with it is the
        # one the corpus cannot support.
        {"op": "set_skills", "groups": [
            {"label": "ML", "skills": ["Python", "SQL", "Airflow", "PyTorch"]}]},
    ]}])
    ops = plan(brief, doc, _index(doc, brief), grounding_corpus(doc), planner)

    writer = ScriptedClient([{"bullets": [
        {"bullet_id": "exp.1.b.1",
         "text": "Built Airflow pipelines in Python for the reporting team"},
        {"bullet_id": "exp.1.b.2",
         "text": "Built a dashboard that saved 624 hours annually"},   # derived number
        {"bullet_id": "exp.2.b.1",
         "text": "Drove a data migration end-to-end"},                 # escalation
    ]}])
    ops = write_bullets(ops, doc, brief, writer)

    result = validate(ops, doc)
    codes = {r.op_kind + ":" + r.code for r in result.rejected}

    assert len(result.accepted) == 1                       # only the good rewrite
    assert "rewrite_bullet:fabricated_number" in codes
    assert "rewrite_bullet:seniority_escalation" in codes
    assert "set_skills:unsupported_entity" in codes
    assert result.fabrication_count == 2


# ══ diagnosing an empty plan ══════════════════════════════════════════

def test_dropped_operations_report_their_reason():
    """A plan that silently shrinks to nothing looks identical to a model that
    said nothing — and those need opposite fixes. So reasons come back."""
    reasons: list[str] = []
    kept = clean_ops([
        RewriteBullet(bullet_id="exp.1.b.1"),      # real
        RewriteBullet(bullet_id="made.up.b.9"),    # invented id
        ReorderBullets(item_id="nope.7", order=[]),
    ], _doc(), dropped=reasons)

    assert len(kept) == 1
    assert len(reasons) == 2
    assert "made.up.b.9" in reasons[0] and "rewrite_bullet" in reasons[0]


def test_plan_surfaces_drops_to_the_caller():
    doc, brief = _doc(), _brief()
    client = ScriptedClient([{"ops": [
        {"op": "rewrite_bullet", "bullet_id": "totally.invented.b.1"},
        {"op": "flag_gap", "requirement": "PyTorch"},
    ]}])
    reasons: list[str] = []
    ops = plan(brief, doc, _index(doc, brief), doc.skill_inventory, client,
               dropped=reasons)

    assert [o.op for o in ops] == ["flag_gap"]
    assert "totally.invented.b.1" in reasons[0]


# ── the wrapper threw away a working plan ─────────────────────────────
# Real run: the planner returned a promote_item and an ask_user about a
# voice-agent pipeline, and OpList rejected the lot over punctuation. The
# retry then produced an empty list, and the run reported "the model produced
# no operations". The planner had been working the whole time.

from app.domain.ops import OP_NAMES, OpList          # noqa: E402

FLAG = {"op": "flag_gap", "requirement": "PyTorch", "severity": "major"}


def test_the_documented_shape_still_works():
    assert [o.op for o in OpList.model_validate({"ops": [FLAG]}).ops] == ["flag_gap"]


def test_a_bare_list_is_accepted():
    """No `ops` key — the exact shape that was rejected."""
    assert [o.op for o in OpList.model_validate([FLAG]).ops] == ["flag_gap"]


def test_the_tagged_encoding_is_accepted():
    """`{"promote_item": {...}}` instead of `{"op": "promote_item", ...}`."""
    parsed = OpList.model_validate([
        {"promote_item": {"item_id": "exp.2", "rationale": "most relevant",
                          "cites": []}},
        {"ask_user": {"bullet_id": "exp.1.b.1",
                      "question": "What did you use to build the pipeline?"}},
    ])
    assert [o.op for o in parsed.ops] == ["promote_item", "ask_user"]
    assert parsed.ops[0].item_id == "exp.2"
    assert "pipeline" in parsed.ops[1].question


def test_both_forms_can_be_mixed():
    parsed = OpList.model_validate([FLAG, {"promote_item": {
        "item_id": "exp.1", "rationale": "r", "cites": []}}])
    assert [o.op for o in parsed.ops] == ["flag_gap", "promote_item"]


def test_an_unknown_tag_still_fails():
    """A coercion, not a repair — it accepts two spellings of the union and
    goes no further."""
    with pytest.raises(Exception):
        OpList.model_validate([{"not_an_operation": {"x": 1}}])


def test_a_dict_that_merely_has_one_key_is_left_alone():
    with pytest.raises(Exception):
        OpList.model_validate([{"bullet_id": "exp.1.b.1"}])


def test_an_op_carrying_its_own_op_field_is_untouched():
    """Belt and braces: an object that already declares `op` is not reshaped
    even if it happens to have one other key."""
    assert OpList.model_validate([{"op": "flag_gap", "requirement": "X",
                                   "severity": "minor"}]).ops[0].requirement == "X"


def test_the_op_names_come_from_the_union():
    """Derived, so a tenth operation cannot leave this one short."""
    assert len(OP_NAMES) == 9
    assert "rewrite_bullet" in OP_NAMES
    assert "flag_gap" in OP_NAMES


def test_an_empty_plan_is_still_an_empty_plan():
    assert OpList.model_validate([]).ops == []
    assert OpList.model_validate({"ops": []}).ops == []
