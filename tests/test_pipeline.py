"""S0.1 through S7 composed, with a scripted model and no network.

The point of these tests is the *artifacts*. Until this module existed, the
stages that decide what happens to a resume — the plan, what the validator
refused, the diff — printed to a terminal and were gone. A test that only
checked return values would not have noticed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domain.models import ResumeDoc
from app.io.llm import BudgetedClient, RunBudget, ScriptedClient
from app.io.runlog import NullRunLog, RunLog
from app.pipeline.run import ingest, proof_checks, run, tailor

RESUME_TEXT = """\
A. MORGAN
a.morgan@example.com

WORK EXPERIENCE

Data Analyst
Acme Corp | 01/2022 - Present
- Worked on data pipelines for the reporting team
- Built a dashboard that reduced manual reporting by 12 hours a week
- Scheduled nightly jobs with Airflow and handled retries
- Responsible for weekly stakeholder reports

TECHNICAL SKILLS
Languages: Python, SQL
Tools: Airflow, Docker
"""

# A realistic posting. The old fixture was 160 characters, which
# `engine.posting` correctly calls a teaser rather than something to tailor
# against — the span offsets in BRIEF are taken from this text, so the phrase
# "own model training and serving pipelines" has to stay.
JOB_TEXT = (
    "Machine Learning Engineer at Nimbus\n"
    "\n"
    "About the role\n"
    "We run forecasting for a few hundred customers and the models behind it "
    "are retrained by hand, which is slow and error prone.\n"
    "\n"
    "What you will do\n"
    "- You will own model training and serving pipelines end to end\n"
    "- Take the nightly retraining job from a manual runbook to something "
    "that schedules itself and recovers from a failed source\n"
    "- Work with the platform team on how models reach production\n"
    "\n"
    "Requirements\n"
    "- Required: Python, PyTorch, Airflow\n"
    "- Comfortable owning a service rather than handing it over\n"
    "\n"
    "Nice to have: Kubernetes, experience with feature stores.\n"
)

PARSE = {
    "contact": {"full_name": "A. MORGAN", "email": "a.morgan@example.com", "phone": "", "location": "", "linkedin": "", "github": "", "website": ""},
    "sections": [
        {"heading": "WORK EXPERIENCE", "entries": [{
            "title": "Data Analyst", "org": "Acme Corp",
            "dates": "01/2022 - Present",
            "bullets": [
                "Worked on data pipelines for the reporting team",
                "Built a dashboard that reduced manual reporting by 12 hours a week",
                "Scheduled nightly jobs with Airflow and handled retries",
                "Responsible for weekly stakeholder reports",
            ]}]},
        {"heading": "TECHNICAL SKILLS", "entries": [
            {"title": "", "org": "", "dates": "", "bullets": ["Languages: Python, SQL"]},
            {"title": "", "org": "", "dates": "", "bullets": ["Tools: Airflow, Docker"]},
        ]},
    ],
}

BRIEF = {
    "company": "Nimbus", "role": "Machine Learning Engineer",
    "seniority": "", "recruiter_email": "",
    "role_narrative": "Owns the training and serving pipelines.",
    "problems_to_solve": [{
        "statement": "own model training and serving pipelines",
        "start": JOB_TEXT.index("own model"),
        "end": JOB_TEXT.index("own model") + len("own model training and serving pipelines"),
        "priority": "core",
    }],
    "success_signals": [], "hard_requirements": [], "tone": "pragmatic",
    "terms": [
        {"term": "Python", "aliases": [], "weight": 9, "kind": "skill", "required": True},
        {"term": "Airflow", "aliases": ["apache airflow"], "weight": 8,
         "kind": "tool", "required": True},
        {"term": "PyTorch", "aliases": [], "weight": 8, "kind": "tool", "required": True},
    ],
}

PLAN = {"ops": [
    {"op": "rewrite_bullet", "bullet_id": "exp.1.b.1", "text": "",
     "target_terms": ["Python"],
     "rationale": "The line buries what this job cares about."},
    {"op": "flag_gap", "requirement": "PyTorch", "severity": "blocking",
     "closest_evidence": []},
]}

WRITE = {"title": "", "org": "", "dates": "", "bullets": [
    {"bullet_id": "exp.1.b.1",
     "text": "Built and maintained data pipelines in Python for the reporting team"},
]}

# A rewrite that invents a number the original never had — the validator must
# refuse it, and the refusal must reach 08_validation.json.
WRITE_FABRICATING = {"title": "", "org": "", "dates": "", "bullets": [
    {"bullet_id": "exp.1.b.1",
     "text": "Built data pipelines in Python that cut latency by 47%"},
]}


def client(write=WRITE) -> BudgetedClient:
    return BudgetedClient(
        ScriptedClient([PARSE, BRIEF, PLAN, write]), RunBudget(max_calls=7)
    )


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def resume(tmp_path: Path) -> bytes:
    return RESUME_TEXT.encode()


# ── every stage lands on disk ─────────────────────────────────────────

EXPECTED_ARTIFACTS = [
    "00_extract.txt",
    "01_parse.json",
    "02_coverage.json",
    "03_normalize.json",
    "04_posting.json",       # S1.0 — what was stripped, and what is left
    "05_brief.json",
    "06_evidence.json",
    "07_plan.json",
    "08_written.json",
    "09_validation.json",
    "10_tailored.json",
    "11_diff.json",
    # S10 — the deliverable, plus the text an ATS would see. Written to the run
    # folder like every other stage, so a run that produced a bad resume leaves
    # the bad resume behind to look at.
    "12_resume_docx.docx",
    "13_resume_pdf.pdf",
    "14_resume_ats.txt",
]


def test_a_full_run_writes_every_stage(resume: bytes, tmp_path: Path):
    log = RunLog.create("morgan", root=tmp_path)
    run(resume, "a_morgan.txt", JOB_TEXT, client(), log=log)
    log.finish()

    written = sorted(p.name for p in log.directory.iterdir())
    assert written == sorted([*EXPECTED_ARTIFACTS, "manifest.json"])


def test_the_artifacts_are_numbered_in_pipeline_order(resume: bytes, tmp_path: Path):
    """So the folder reads top to bottom as the run happened."""
    log = RunLog.create("morgan", root=tmp_path)
    run(resume, "a_morgan.txt", JOB_TEXT, client(), log=log)
    manifest = read(log.finish())
    assert [a["file"] for a in manifest["artifacts"]] == EXPECTED_ARTIFACTS


# ── the stages that used to leave no trace ────────────────────────────

def test_the_plan_is_saved_with_what_was_dropped(resume: bytes, tmp_path: Path):
    log = RunLog.create("x", root=tmp_path)
    run(resume, "a.txt", JOB_TEXT, client(), log=log)

    plan = read(log.directory / "07_plan.json")
    assert [o["op"] for o in plan["operations"]] == ["rewrite_bullet", "flag_gap"]
    assert "dropped_before_validation" in plan


def test_a_rejection_is_saved_with_its_reason(resume: bytes, tmp_path: Path):
    """The single most important artifact: why the guard refused something.

    It used to exist only as a line in a terminal.
    """
    log = RunLog.create("x", root=tmp_path)
    run(resume, "a.txt", JOB_TEXT, client(WRITE_FABRICATING), log=log)

    validation = read(log.directory / "09_validation.json")
    assert validation["fabrication_count"] == 1
    rejection = validation["rejected"][0]
    assert rejection["code"] == "fabricated_number"
    assert "47" in rejection["detail"]


def test_the_diff_is_saved(resume: bytes, tmp_path: Path):
    log = RunLog.create("x", root=tmp_path)
    run(resume, "a.txt", JOB_TEXT, client(), log=log)

    diff = read(log.directory / "11_diff.json")
    change = diff["changes"][0]
    assert change["ref_id"] == "exp.1.b.1"
    assert "Python" in change["after"]
    assert change["rationale"]
    assert diff["gaps"][0]["requirement"] == "PyTorch"


def test_the_evidence_index_is_saved_with_the_gap_list(resume: bytes, tmp_path: Path):
    log = RunLog.create("x", root=tmp_path)
    run(resume, "a.txt", JOB_TEXT, client(), log=log)

    evidence = read(log.directory / "06_evidence.json")

    # One bullet genuinely mentions Airflow, so that term has evidence.
    airflow = [l for l in evidence["links"] if l["job_element_id"] == "term.airflow"]
    assert airflow and airflow[0]["strength"] == "strong"

    # Python and PyTorch do not: Python is only DECLARED in the skills section,
    # which is never evidence, and PyTorch is absent entirely. Only the second
    # is a gap — a declared skill with no bullet behind it is not reported as
    # missing, it simply has nothing demonstrating it.
    assert "PyTorch" in evidence["undemonstrated_terms"]
    assert "Python" not in evidence["undemonstrated_terms"]
    assert not [l for l in evidence["links"] if l["job_element_id"] == "term.python"]


# ── a failed run keeps what it got to ─────────────────────────────────

def test_a_crash_leaves_the_stages_that_explain_it(resume: bytes, tmp_path: Path):
    """Written as produced, not bundled at the end.

    A run that dies in S3 must still leave S0–S2 on disk — those are the files
    worth reading.
    """
    from app.domain.errors import SchemaValidationFailed

    log = RunLog.create("x", root=tmp_path)
    broken = BudgetedClient(
        ScriptedClient([PARSE, BRIEF, "not json", "still not json"]),
        RunBudget(max_calls=7),
    )
    with pytest.raises(SchemaValidationFailed):
        run(resume, "a.txt", JOB_TEXT, broken, log=log)
    manifest = read(log.finish("SchemaValidationFailed"))

    assert manifest["ok"] is False
    assert sorted(p.name for p in log.directory.iterdir()) == sorted([
        "00_extract.txt", "01_parse.json", "02_coverage.json",
        "03_normalize.json", "04_posting.json", "05_brief.json",
        "06_evidence.json", "manifest.json",
    ])


# ── the run itself ────────────────────────────────────────────────────

def test_the_two_halves_are_joined(resume: bytes):
    """There was no single call from a file to a diff before this."""
    state = run(resume, "a_morgan.txt", JOB_TEXT, client())
    assert isinstance(state.doc, ResumeDoc)
    assert state.diff is not None
    assert state.tailored is not None


def test_the_happy_path_is_four_calls(resume: bytes):
    budget = client()
    run(resume, "a.txt", JOB_TEXT, budget)
    assert budget.budget.calls == 4
    assert [c["stage"] for c in budget.log] == ["parse", "job_brief", "planner", "writer"]


def test_the_proof_checks_are_recorded(resume: bytes, tmp_path: Path):
    """"Did this regress" should be answerable from the run folders."""
    log = RunLog.create("x", root=tmp_path)
    run(resume, "a.txt", JOB_TEXT, client(), log=log)
    proof = read(log.finish())["proof"]

    assert proof["no_content_silently_lost"] is True
    assert proof["every_section_survived"] is True
    assert proof["every_change_attributable"] is True
    assert proof["parse_verified"] is True


def test_a_lossy_parse_shows_up_in_the_proof(resume: bytes, tmp_path: Path):
    lossy = json.loads(json.dumps(PARSE))
    lossy["sections"][0]["entries"][0]["bullets"] = ["Worked on data pipelines"]

    log = RunLog.create("x", root=tmp_path)
    bad = BudgetedClient(ScriptedClient([lossy, BRIEF, PLAN, WRITE]),
                         RunBudget(max_calls=7))
    run(resume, "a.txt", JOB_TEXT, bad, log=log)

    assert read(log.finish())["proof"]["parse_verified"] is False


def test_ingest_alone_makes_one_call(resume: bytes):
    one = BudgetedClient(ScriptedClient([PARSE]), RunBudget(max_calls=7))
    state = ingest(resume, "a.txt", one)
    assert one.budget.calls == 1
    assert state.doc.bullet("exp.1.b.2") is not None
    assert "Airflow" in state.doc.skill_inventory


def test_tailor_can_be_run_on_an_already_parsed_document(resume: bytes):
    """S0 is cached per resume; re-tailoring for a second job must not re-parse."""
    first = BudgetedClient(ScriptedClient([PARSE]), RunBudget(max_calls=7))
    state = ingest(resume, "a.txt", first)

    second = BudgetedClient(ScriptedClient([BRIEF, PLAN, WRITE]),
                            RunBudget(max_calls=7))
    tailored = tailor(state.doc, JOB_TEXT, second, state=state)
    assert second.budget.calls == 3
    assert tailored.diff is not None


def test_nothing_is_written_without_a_log(resume: bytes, tmp_path: Path):
    run(resume, "a.txt", JOB_TEXT, client(), log=NullRunLog())
    assert list(tmp_path.iterdir()) == []


def test_proof_checks_are_empty_on_an_unfinished_run():
    from app.pipeline.run import RunState
    assert proof_checks(RunState()) == {}


# ── a stub posting must fail loudly ───────────────────────────────────

def test_a_seven_character_job_posting_is_refused_before_any_call(resume: bytes):
    """One real run spent three calls on a 7-character job.txt.

    An empty posting yields an empty brief, an empty plan, and a result
    indistinguishable from a model that decided to change nothing.
    """
    from app.domain.errors import UserError
    from app.engine.normalize import normalize
    from app.domain.models import RawResume, RawSection, RawEntry

    doc = normalize(RawResume(sections=[RawSection(heading="EXPERIENCE",
        entries=[RawEntry(title="DE", bullets=["Built pipelines"])])]))
    spent = ScriptedClient([])                    # any call raises
    with pytest.raises(UserError) as excinfo:
        tailor(doc, "job.txt", spent)
    assert "characters" in str(excinfo.value)
    assert spent.calls == []


def test_a_real_posting_is_accepted(resume: bytes):
    assert len(JOB_TEXT) >= 120                   # the fixture is realistic
    run(resume, "a.txt", JOB_TEXT, client())


# ── the three grades reach the saved artifact ─────────────────────────

def test_term_standing_is_saved(resume: bytes, tmp_path: Path):
    log = RunLog.create("x", root=tmp_path)
    run(resume, "a.txt", JOB_TEXT, client(), log=log)

    standing = read(log.directory / "06_evidence.json")["term_standing"]
    assert "Airflow" in standing["demonstrated"]
    assert "Python" in standing["declared_only"]
    assert "PyTorch" in standing["not_found"]


# ── a failed parse keeps its evidence ─────────────────────────────────

class RamblingClient:
    """Answers with prose and then runs out of room, like a reasoning model."""

    def complete(self, *, system, user, schema=None, max_tokens=2048,
                 temperature=0.2, stage=""):
        from app.io.llm import LLMResponse
        return LLMResponse(
            text="We need to carefully consider the structure of this resume...",
            completion_tokens=max_tokens, finish_reason="length",
        )


def test_the_failed_response_is_written_to_the_run_folder(resume: bytes,
                                                          tmp_path: Path):
    """"The JSON was incomplete" cannot tell a model that rambled from one cut
    off mid-string. The response is the only thing that can."""
    from app.domain.errors import ResponseTruncated

    log = RunLog.create("x", root=tmp_path)
    with pytest.raises(ResponseTruncated):
        ingest(resume, "a.txt", RamblingClient(), log=log)
    log.finish("ResponseTruncated")

    saved = log.directory / "01_parse_failed_raw.txt"
    assert saved.exists()
    assert "carefully consider" in saved.read_text(encoding="utf-8")


def test_the_truncation_message_names_the_reasoning_suspect():
    from app.domain.errors import ResponseTruncated
    from app.agents.parse import parse_resume

    with pytest.raises(ResponseTruncated) as excinfo:
        parse_resume("EXPERIENCE\n- did a thing worth writing down\n" * 3,
                     RamblingClient())
    assert "reasoning" in str(excinfo.value)


def test_the_call_budget_allows_a_windowed_parse():
    """Three parse windows plus brief, plan and prose is six calls."""
    from app.config import Settings
    assert Settings().max_llm_calls_per_run >= 8


# ── two models, split by what each stage actually needs ───────────────
# Measured on llama3.1:8b locally: the parse produced a clean document and the
# planner returned NINE output tokens — an empty list. The parse needs MANY
# calls and little capability; the planner needs ONE call and a lot of it.

def test_the_judgment_stages_can_use_a_different_client(resume: bytes):
    local = BudgetedClient(ScriptedClient([PARSE]), RunBudget(max_calls=12))
    smart = BudgetedClient(ScriptedClient([BRIEF, PLAN, WRITE]),
                           RunBudget(max_calls=12))

    state = run(resume, "cv.txt", JOB_TEXT, local, smart_client=smart)

    assert local.budget.calls == 1          # the parse only
    assert smart.budget.calls == 3          # brief, plan, prose
    assert state.diff is not None


def test_one_client_still_does_everything(resume: bytes):
    """The default: a single model, unchanged behaviour."""
    single = client()
    run(resume, "cv.txt", JOB_TEXT, single)
    assert single.budget.calls == 4


def test_the_smart_profile_falls_back_field_by_field():
    from app.config import Settings

    settings = Settings(
        llm_provider="ollama", llm_model="llama3.1:8b",
        llm_base_url="http://localhost:11434/v1",
        smart_llm_model="gemini-2.5-flash-lite",
        smart_llm_provider="google", smart_llm_api_key="k",
    )
    assert settings.for_role("main").llm_model == "llama3.1:8b"
    assert settings.for_role("smart").llm_model == "gemini-2.5-flash-lite"
    assert settings.for_role("smart").llm_provider == "google"


def test_no_smart_model_means_the_same_settings():
    from app.config import Settings
    settings = Settings(llm_model="only-one")
    assert settings.for_role("smart") is settings
    assert not settings.has_smart_model


def test_a_smart_model_on_the_same_provider_inherits_its_base_url():
    from app.config import Settings
    settings = Settings(llm_provider="groq", llm_api_key="k",
                        llm_model="small", llm_base_url="https://x.test/v1",
                        smart_llm_model="big")
    smart = settings.for_role("smart")
    assert smart.llm_base_url == "https://x.test/v1"
    assert smart.llm_api_key == "k"
    assert smart.llm_model == "big"
