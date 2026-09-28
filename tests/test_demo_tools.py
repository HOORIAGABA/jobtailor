"""The two scripts that let the demo run through the real UI without a GPU."""
from __future__ import annotations

from app.engine.normalize import normalize
from app.engine.parse_check import check_coverage
from app.io.extract import extract_text
from scripts import demo_files, fake_model
from scripts.demo_seed import ANSWERS, RESUME


def test_the_fake_model_answers_every_stage_the_pipeline_asks_for():
    """Dispatch is by the schema's property names, exactly as the eval
    harness does it, so a renamed stage schema fails here before it fails on
    camera."""
    def ask(*keys):
        return fake_model.answer_for({"response_format": {
            "type": "json_schema",
            "json_schema": {"schema": {"properties": {k: {} for k in keys}}}}})

    assert ask("contact", "sections")["contact"]["full_name"] == "Priya Raman"
    assert ask("role_narrative", "role", "company") == ANSWERS["brief"]
    assert ask("ops") == ANSWERS["plan"]
    assert ask("bullets") == ANSWERS["written"]
    assert ask("subject", "body", "problems") == ANSWERS["outreach"]


def test_the_fake_parse_keeps_the_ids_the_scripted_plan_cites():
    """Item ids are a running counter across sections. The summary section
    goes LAST so `exp.1.b.2` and `prj.3.b.1` — what the plan cites — still
    exist after a live parse. Put it first and every refusal on the gate
    becomes `unknown_id`."""
    from app.agents.parse import ResumeDraft, to_raw

    doc = normalize(to_raw(ResumeDraft.model_validate(fake_model.parse_answer())))
    ids = {item.id for section in doc.sections for item in section.items}
    assert {"exp.1", "exp.2", "prj.3"} <= ids, ids
    assert doc.summary == RESUME.summary


def test_the_demo_docx_parses_back_to_the_fixture_cleanly(tmp_path):
    """The file a person uploads must match what the scripted parse answers,
    or the coverage check reports invented text and the confirm screen is a
    warning instead of a demo."""
    demo_files.write_resume(tmp_path / "cv.docx")
    text = extract_text((tmp_path / "cv.docx").read_bytes(), "cv.docx")
    from app.agents.parse import ResumeDraft, to_raw
    raw = to_raw(ResumeDraft.model_validate(fake_model.parse_answer()))
    coverage = check_coverage(text, raw)
    assert coverage.is_clean, (coverage.dropped, coverage.invented)
