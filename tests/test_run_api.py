"""Runs over HTTP, backed by the database.

Every test here runs with **no model configured** — the rows come from
`scripts/seed.py`, which is the whole point of that script: the review screen,
the stage inspector and the downloads are all exercisable on a free tier,
because a run that already happened is as good a fixture as a run started now.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.db import models
from app.db.session import create_all, engine, reset, session_scope
from scripts import seed as seeder

SHA = "c" * 64


def _run_folder(root: Path, run_id: str, *, ok: bool = True, **extra) -> Path:
    """A finished run folder, as `RunLog` leaves one."""
    folder = root / run_id
    folder.mkdir(parents=True, exist_ok=True)
    payloads = {
        "extract": ("00_extract.txt", "A. MORGAN\nWORK EXPERIENCE\n"),
        "normalize": ("03_normalize.json", {
            "contact": {"full_name": "A. Morgan"}, "summary": "",
            "sections": [{"id": "sec.experience", "kind": "experience",
                          "heading": "WORK EXPERIENCE", "items": []}],
            "skill_inventory": ["Python"], "seq": 0}),
        "brief": ("05_brief.json", {"role": "AI Engineer", "company": "Annova",
                                    "source_hash": "deadbeef"}),
        "evidence": ("06_evidence.json", {
            "links": [], "unmatched_elements": ["req.0"],
            "term_standing": {"demonstrated": ["Python"], "declared_only": [],
                              "not_found": ["Airflow"]}}),
        "plan": ("07_plan.json", {"operations": [
            {"op_id": "op1", "op": "promote_item", "item_id": "exp.1",
             "rationale": "most relevant"}]}),
        "validation": ("09_validation.json", {
            "accepted": [{"op_id": "op1"}], "rejected": []}),
        "diff": ("11_diff.json", {"changes": [{"op_id": "op1"}], "gaps": [],
                                  "questions": [], "rejections": []}),
        "resume_docx": ("12_resume_docx.docx", b"PK\x03\x04fake"),
        "resume_pdf": ("13_resume_pdf.pdf", b"%PDF-1.4 fake"),
        "resume_ats": ("14_resume_ats.txt", "A. MORGAN\nWORK EXPERIENCE"),
    }
    listed = []
    for stage, (filename, content) in payloads.items():
        path = folder / filename
        if isinstance(content, (dict, list)):
            path.write_text(json.dumps(content), encoding="utf-8")
        elif isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(str(content), encoding="utf-8")
        listed.append({"stage": stage, "file": filename,
                       "bytes": path.stat().st_size})

    (folder / "manifest.json").write_text(json.dumps({
        "run_id": run_id, "ok": ok, "started": "2026-09-20T10:00:00+00:00",
        "input_name": "cv.pdf", "input_sha256": SHA, "input_bytes": 1024,
        "calls": 7, "tokens": 21486, "artifacts": listed,
        "proof": {"no_content_silently_lost": True, "parse_verified": False},
        "call_log": [{"stage": "brief", "prompt_tokens": 1, "completion_tokens": 2}],
        **extra,
    }), encoding="utf-8")
    return folder


@pytest.fixture
def api(tmp_path, monkeypatch) -> TestClient:
    url = f"sqlite+pysqlite:///{tmp_path / 'runs.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DEV_USER_EMAIL", "dev@example.com")
    reset(url)
    create_all(engine())

    runs = tmp_path / "runs"
    _run_folder(runs, "2026-09-20T10-00-00_ok")
    _run_folder(runs, "2026-09-19T10-00-00_bad", ok=False,
                error="ResponseTruncated: ran out of room")
    seeder.seed(runs, "dev@example.com")

    yield TestClient(create_app(tmp_path / "runs", read_only=False))
    reset()


def _first(api: TestClient, status: str = "needs_review") -> dict:
    return next(r for r in api.get("/api/runs").json() if r["status"] == status)


# ══ the list ══════════════════════════════════════════════════════════

def test_seeded_runs_are_served_from_the_database(api: TestClient):
    """The endpoints used to read folders. Two live sources for one list is
    the bug class this project has an invariant against."""
    rows = api.get("/api/runs").json()
    assert len(rows) == 2
    assert {r["status"] for r in rows} == {"needs_review", "failed"}


def test_a_list_row_says_which_job_it_was_for(api: TestClient):
    row = _first(api)
    assert row["role"] == "AI Engineer"
    assert row["company"] == "Annova"
    assert row["llm_calls"] == 7


def test_a_list_row_carries_the_proof_checks(api: TestClient):
    proof = _first(api)["proof"]
    assert proof["no_content_silently_lost"] is True
    assert proof["parse_verified"] is False


def test_a_failed_run_is_listed_with_its_reason(api: TestClient):
    """A run folder exists *because* a failure leaves the stages explaining it."""
    row = _first(api, "failed")
    assert "ResponseTruncated" in row["error"]
    assert row["can_download"] is False        # nothing was approved or rendered


# ══ the review screen ═════════════════════════════════════════════════

def test_the_detail_carries_everything_the_gate_needs(api: TestClient):
    """The gap list, the diff and the draft are what the person is being asked
    about, so they arrive together rather than as three round trips."""
    body = api.get(f"/api/runs/{_first(api)['id']}").json()

    assert body["brief"]["role"] == "AI Engineer"
    assert body["standing"]["not_found"] == ["Airflow"]
    assert len(body["ops"]) == 1
    assert len(body["accepted_ops"]) == 1
    assert body["diff"]["changes"]
    assert {a["stage"] for a in body["artifacts"]} == {
        "resume_docx", "resume_pdf", "resume_ats"}


def test_the_three_grades_reach_their_own_field(api: TestClient):
    """S2 produces them inside the evidence payload; lifting them to a column
    keeps one producer while letting the review screen read them directly."""
    body = api.get(f"/api/runs/{_first(api)['id']}").json()
    assert set(body["standing"]) == {"demonstrated", "declared_only", "not_found"}


def test_a_needs_review_run_may_not_send_yet(api: TestClient):
    body = api.get(f"/api/runs/{_first(api)['id']}").json()
    assert body["can_download"] is True
    assert body["may_send"] is False        # only `approved` may send


# ══ the inspector ═════════════════════════════════════════════════════

def test_every_stage_is_served(api: TestClient):
    """A stage the API hides is a stage nobody can check."""
    run_id = _first(api)["id"]
    stages = api.get(f"/api/runs/{run_id}/stages").json()
    for stage in ("brief", "evidence", "plan", "diff"):
        assert stage in stages
        assert api.get(f"/api/runs/{run_id}/stages/{stage}").status_code == 200


def test_validation_is_served_even_without_a_column(api: TestClient):
    body = api.get(f"/api/runs/{_first(api)['id']}/stages/validation").json()
    assert body["accepted"] and body["rejected"] == []


def test_an_unknown_stage_is_404(api: TestClient):
    assert api.get(
        f"/api/runs/{_first(api)['id']}/stages/nonsense").status_code == 404


# ══ the files ═════════════════════════════════════════════════════════

def test_the_rendered_files_download(api: TestClient):
    run_id = _first(api)["id"]
    docx = api.get(f"/api/runs/{run_id}/file/resume_docx")
    assert docx.status_code == 200
    assert docx.content.startswith(b"PK")
    assert "wordprocessingml" in docx.headers["content-type"]
    assert "attachment" in docx.headers["content-disposition"]
    assert api.get(f"/api/runs/{run_id}/file/resume_pdf").content[:5] == b"%PDF-"
    assert "MORGAN" in api.get(f"/api/runs/{run_id}/file/resume_ats").text


def test_downloads_survive_a_rejection(api: TestClient):
    """Rejecting blocks sending; it does not destroy the work.

    The likeliest real rejection is a good resume with a clumsy covering
    letter, and a gate that throws away the resume to punish the email makes
    the honest answer the expensive one.
    """
    run_id = _first(api)["id"]
    with session_scope() as s:
        s.get(models.Run, run_id).move_to("rejected")

    assert api.get(f"/api/runs/{run_id}").json()["can_download"] is True
    assert api.get(f"/api/runs/{run_id}/file/resume_docx").status_code == 200
    assert api.get(f"/api/runs/{run_id}").json()["may_send"] is False


def test_a_run_still_tailoring_has_nothing_to_download(api: TestClient):
    with session_scope() as s:
        user = s.query(models.User).one()
        resume = s.query(models.Resume).one()
        run = models.Run(user_id=user.id, resume_id=resume.id,
                         status="tailoring")
        s.add(run)
        s.flush()
        run_id = run.id

    response = api.get(f"/api/runs/{run_id}/file/resume_docx")
    assert response.status_code == 404
    assert "still tailoring" in response.json()["detail"]


# ══ ownership and refusals ════════════════════════════════════════════

def test_another_users_run_is_404_not_403(api: TestClient):
    """A 403 confirms the row exists, which tells an enumerator they found
    something."""
    with session_scope() as s:
        other = models.User(google_sub="someone-else", email="b@example.com")
        s.add(other)
        s.flush()
        resume = s.query(models.Resume).one()
        run = models.Run(user_id=other.id, resume_id=resume.id,
                         status="needs_review")
        s.add(run)
        s.flush()
        theirs = run.id

    assert api.get(f"/api/runs/{theirs}").status_code == 404
    assert api.get(f"/api/runs/{theirs}/stages/brief").status_code == 404
    assert api.get(f"/api/runs/{theirs}/file/resume_docx").status_code == 404


def test_an_unknown_run_is_404(api: TestClient):
    assert api.get("/api/runs/nope").status_code == 404
    assert api.get("/api/runs/nope/events").status_code == 404


def test_a_read_only_instance_refuses_to_start_a_run(tmp_path, monkeypatch):
    url = f"sqlite+pysqlite:///{tmp_path / 'ro.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DEV_USER_EMAIL", "dev@example.com")
    reset(url)
    create_all(engine())

    client = TestClient(create_app(tmp_path / "runs", read_only=True))
    response = client.post("/api/runs", json={"resume_id": "x", "job": "text"})
    assert response.status_code == 403
    assert "locally" in response.json()["detail"]
    reset()


def test_a_run_cannot_start_from_an_unconfirmed_resume(api: TestClient):
    """Tailoring an unconfirmed parse would build on a document the person has
    not agreed is theirs, and every later claim would inherit the doubt."""
    from app.domain.errors import UserError
    from app.pipeline import runs as service

    with session_scope() as s:
        user = s.query(models.User).one()
        resume = s.query(models.Resume).one()
        resume.status = "needs_confirm"
        with pytest.raises(UserError, match="confirm the parse"):
            service.create(s, user, resume, "a posting")


def test_an_empty_posting_is_refused(api: TestClient):
    from app.domain.errors import UserError
    from app.pipeline import runs as service

    with session_scope() as s:
        user = s.query(models.User).one()
        resume = s.query(models.Resume).one()
        with pytest.raises(UserError, match="no job posting"):
            service.create(s, user, resume, "   ")


# ══ the log that writes rows ══════════════════════════════════════════

def test_the_db_runlog_is_a_runlog(api: TestClient):
    """`RunLog` was already the seam where stage output leaves the pipeline, so
    persistence is a third implementation rather than an edit to fifteen
    stages."""
    from app.io.dblog import DbRunLog
    from app.io.runlog import RunLog

    public = {n for n in dir(RunLog) if not n.startswith("_")}
    assert public <= {n for n in dir(DbRunLog) if not n.startswith("_")}


def test_each_stage_commits_as_it_finishes(api: TestClient):
    """A process killed halfway must leave the stages that finished — holding
    one transaction for the whole run would discard exactly that."""
    from app.db.session import session_factory
    from app.io.dblog import DbRunLog

    with session_scope() as s:
        user = s.query(models.User).one()
        resume = s.query(models.Resume).one()
        run = models.Run(user_id=user.id, resume_id=resume.id,
                         status="tailoring")
        s.add(run)
        s.flush()
        run_id = run.id

    log = DbRunLog(run_id, session_factory())
    log.json("brief", {"role": "Data Analyst"})

    # A different session entirely: if the write had not committed, this sees
    # nothing.
    with session_scope() as s:
        run = s.get(models.Run, run_id)
        assert run.brief_json == {"role": "Data Analyst"}
        assert run.stage == "brief"
        assert [c.stage for c in run.checkpoints] == ["brief"]


def test_a_stage_written_twice_updates_its_checkpoint(api: TestClient):
    from app.db.session import session_factory
    from app.io.dblog import DbRunLog

    with session_scope() as s:
        user = s.query(models.User).one()
        resume = s.query(models.Resume).one()
        run = models.Run(user_id=user.id, resume_id=resume.id,
                         status="tailoring")
        s.add(run)
        s.flush()
        run_id = run.id

    log = DbRunLog(run_id, session_factory())
    log.json("brief", {"role": "one"})
    log.json("brief", {"role": "two"})

    with session_scope() as s:
        run = s.get(models.Run, run_id)
        assert run.brief_json == {"role": "two"}
        assert len(run.checkpoints) == 1


def test_a_note_with_no_column_is_dropped_not_stashed(api: TestClient):
    """A schemaless bag of whatever a stage felt like recording is how a
    manifest becomes untrustworthy."""
    from app.db.session import session_factory
    from app.io.dblog import DbRunLog

    with session_scope() as s:
        user = s.query(models.User).one()
        resume = s.query(models.Resume).one()
        run = models.Run(user_id=user.id, resume_id=resume.id,
                         status="tailoring")
        s.add(run)
        s.flush()
        run_id = run.id

    DbRunLog(run_id, session_factory()).note(calls=4, nonsense="x")

    with session_scope() as s:
        run = s.get(models.Run, run_id)
        assert run.llm_calls == 4
        assert not hasattr(run, "nonsense")


# ══ the two input limits ══════════════════════════════════════════════

def test_a_posting_cannot_name_a_file_on_the_server(tmp_path, monkeypatch):
    """★ `{"job": ".env"}` used to return the secrets file.

    `PurePosixPath(".env").suffix` is `""`, which `io.jobsource` treats as plain
    text, so the contents landed in `run.jd_text` and the stages endpoint served
    them back to any signed-in user. `load_job` now needs `allow_paths=True`,
    which only the command line passes.
    """
    from app.domain.errors import UserError
    from app.io.jobsource import load_job

    secrets = tmp_path / ".env"
    secrets.write_text("LLM_API_KEY=sk-real\nFERNET_KEY=abc\n", encoding="utf-8")
    with pytest.raises(UserError):
        load_job(str(secrets))


def test_the_posting_field_has_a_ceiling():
    """Without one, this field is a memory allocation sized by the caller."""
    from app.api.runs import MAX_JOB_CHARS, RunIn

    RunIn(resume_id="r", job="x" * MAX_JOB_CHARS)
    with pytest.raises(Exception):
        RunIn(resume_id="r", job="x" * (MAX_JOB_CHARS + 1))


# ══ starting a run over HTTP ══════════════════════════════════════════
#
# ★ Everything above starts a run by calling `pipeline.runs.create` or
# `pipeline.runs.execute` directly. Nothing exercised `POST /api/runs`, which is
# the only route a person has — and it answered 500 on every request, because
# the handler is `def` (so FastAPI runs it in a worker thread) and it called
# `asyncio.get_running_loop()`, which raises where there is no loop.
#
# The row was already committed by then, so each attempt also left an orphan run
# in `created` and spent a rate-limit token. 985 tests, all green, and the
# product's primary action was unreachable.

def test_posting_a_run_answers_202_and_does_not_500(api: TestClient, monkeypatch):
    """The regression test for the bug 985 other tests could not see."""
    from app.api import runs as route

    started: list[str] = []
    # The pipeline itself is not under test here: no model is configured in the
    # suite, and this asserts the handler's contract, not the tailoring.
    monkeypatch.setattr(route, "check", lambda _settings: [])
    monkeypatch.setattr(route, "build_client", lambda *a, **k: object())
    monkeypatch.setattr(route, "_execute",
                        lambda run_id, *a, **k: started.append(run_id))

    with session_scope() as s:
        resume = s.query(models.Resume).one()
        resume.status = "confirmed"
        resume_id = resume.id

    response = api.post("/api/runs",
                        json={"resume_id": resume_id, "job": "x" * 200})

    assert response.status_code == 202, response.text
    body = response.json()

    # The field is `run_id`, and the frontend read `id` — so a successful start
    # navigated to `/runs/undefined`. Asserted here so the contract is pinned on
    # both sides.
    assert "run_id" in body and body["run_id"]
    assert body["events"] == f"/api/runs/{body['run_id']}/events"


def test_a_started_run_reaches_the_worker(api: TestClient, monkeypatch):
    """The handler must actually hand the work off, not merely answer 202.

    A 202 with nothing behind it is the same failure wearing a success code.
    """
    from concurrent.futures import Future

    from app.api import runs as route

    handed: list[tuple] = []

    def fake_submit(fn, *args):
        handed.append(args)
        done: Future = Future()
        done.set_result(None)
        return done

    monkeypatch.setattr(route, "check", lambda _settings: [])
    monkeypatch.setattr(route, "build_client", lambda *a, **k: object())
    monkeypatch.setattr(route._RUNNERS, "submit", fake_submit)

    with session_scope() as s:
        resume = s.query(models.Resume).one()
        resume.status = "confirmed"
        resume_id = resume.id

    response = api.post("/api/runs",
                        json={"resume_id": resume_id, "job": "y" * 200})

    assert response.status_code == 202
    assert len(handed) == 1
    assert handed[0][0] == response.json()["run_id"]


def test_put_is_allowed_by_cors(api: TestClient):
    """`PUT /api/resumes/{id}/draft` is the edit loop for a corrected parse.

    It was missing from `allow_methods`, so the preflight answered "Disallowed
    CORS method" and saving a correction failed on every cross-origin
    deployment — including the documented development setup, UI on :3000 and
    API on :8000.
    """
    preflight = api.options(
        "/api/resumes/abc/draft",
        headers={"Origin": "http://localhost:3000",
                 "Access-Control-Request-Method": "PUT"},
    )
    assert preflight.status_code == 200, preflight.text


# ── the product path persists the verdict ────────────────────────────
#
# `validation` was logged by `tailor()` and stored by nothing: `DbRunLog` had
# no column for it, so it became a checkpoint and `accepted_json` /
# `rejected_json` were only ever written by the seed scripts. The accounting
# (`llm_calls`, `tokens`, `proof_json`) lived in `run()`, which the product
# path does not call. Result: every run started from the UI reported
# `accepted: 0, rejected: 0`, no model calls and an empty proof block — on a
# product whose whole claim is the refusal list.

def _execute_the_demo(api: TestClient) -> str:
    """Run the demo case through `pipeline.runs.execute`, the way the API
    worker does, and return the run id."""
    from datetime import datetime, timezone
    import hashlib

    from evals.harness import ScriptedClient
    from app.db.session import session_factory
    from app.engine.normalize import normalize
    from app.pipeline import runs as pipeline_runs
    from scripts import demo_seed

    document = normalize(demo_seed.RESUME)
    with session_scope() as s:
        user = s.query(models.User).one()
        resume = models.Resume(
            user_id=user.id, version=1, filename="demo.pdf",
            file_sha256=hashlib.sha256(b"demo").hexdigest(), file_bytes=4,
            extract_text="demo", raw_json=demo_seed.RESUME.model_dump(),
            doc_json=document.model_dump(),
            coverage_json={"dropped": [], "invented": [], "structure": []},
            status="confirmed", revision=1,
            confirmed_at=datetime.now(timezone.utc))
        s.add(resume)
        s.flush()
        run = models.Run(user_id=user.id, resume_id=resume.id,
                         status="created", jd_text=demo_seed.POSTING)
        s.add(run)
        s.flush()
        run_id = run.id

    # Wrapped exactly as `api/runs.py` wraps it: the budget is what counts
    # calls and tokens, so a bare scripted client would report nothing.
    from app.io.llm import BudgetedClient, RunBudget
    client = BudgetedClient(ScriptedClient(demo_seed.ANSWERS), RunBudget())
    pipeline_runs.execute(session_factory(), run_id, client)
    return run_id


def test_a_live_run_persists_the_validators_verdict(api: TestClient):
    run_id = _execute_the_demo(api)

    with session_scope() as s:
        run = s.get(models.Run, run_id)
        assert run.status == "needs_review", run.error
        assert run.accepted_json, "accepted operations were not stored"
        assert run.rejected_json, "the refusals were not stored"
        codes = {r["code"] for r in run.rejected_json}
        assert "fabricated_number" in codes, codes

    # And it reaches the screen: the list row and the detail both count them.
    row = next(r for r in api.get("/api/runs").json() if r["id"] == run_id)
    assert row["rejected"] >= 1 and row["accepted"] >= 1
    detail = api.get(f"/api/runs/{run_id}").json()
    assert detail["rejected_ops"] and detail["accepted_ops"]


def test_a_live_run_is_accounted_for(api: TestClient):
    """Calls, tokens and the proof block used to be written only by `run()`."""
    run_id = _execute_the_demo(api)
    with session_scope() as s:
        run = s.get(models.Run, run_id)
        assert run.llm_calls and run.llm_calls > 0
        assert run.tokens and run.tokens > 0
        assert run.proof_json and all(run.proof_json.values()), run.proof_json
        assert run.role and run.company


def test_a_failed_run_is_still_accounted_for(api: TestClient, monkeypatch):
    """What was spent before the failure is the most useful number a failed
    run has, and the proof block says which stages it got through."""
    from app.pipeline import runs as pipeline_runs

    def explode(*_, **__):
        raise RuntimeError("the planner fell over")

    monkeypatch.setattr("app.pipeline.run.plan", explode)
    run_id = _execute_the_demo(api)
    with session_scope() as s:
        run = s.get(models.Run, run_id)
        assert run.status == "failed"
        assert "fell over" in run.error
        assert run.llm_calls == 1          # the brief, before the plan
        assert run.proof_json == {}        # nothing after S3 exists to check
        assert run.brief_json              # what finished, stayed


def test_split_stage_columns_are_one_rule():
    """`columns_for` is the single place a stage maps to columns, so the seed
    scripts and the live log cannot disagree about where `validation` lives."""
    from app.io.dblog import columns_for

    assert columns_for("brief", {"role": "x"}) == {"brief_json": {"role": "x"}}
    assert columns_for("plan", {"operations": [1], "dropped": []}) == {
        "ops_json": [1]}
    assert columns_for("validation", {"accepted": [1], "rejected": [2],
                                      "fabrication_count": 1}) == {
        "accepted_json": [1], "rejected_json": [2]}
    assert columns_for("written", [1, 2]) == {}


def test_a_run_whose_setup_fails_is_failed_not_stranded(api: TestClient):
    """The setup block — load the resume, validate its document, move to
    `tailoring` — ran outside the handlers, so a `doc_json` that no longer
    validated raised out of the worker thread and left the run in `created`
    with no error on it. A run that never starts must still say why."""
    from app.db.session import session_factory
    from app.io.llm import BudgetedClient, RunBudget, ScriptedClient
    from app.pipeline import runs as pipeline_runs

    with session_scope() as s:
        user = s.query(models.User).one()
        resume = models.Resume(user_id=user.id, filename="broken.pdf",
                               file_sha256="b" * 64, status="confirmed",
                               doc_json={"sections": "not a list"})
        s.add(resume)
        s.flush()
        run = models.Run(user_id=user.id, resume_id=resume.id,
                         status="created", jd_text="x" * 200)
        s.add(run)
        s.flush()
        run_id = run.id

    pipeline_runs.execute(session_factory(), run_id,
                          BudgetedClient(ScriptedClient([]), RunBudget()))

    with session_scope() as s:
        run = s.get(models.Run, run_id)
        assert run.status == "failed"
        assert "ValidationError" in run.error
