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
