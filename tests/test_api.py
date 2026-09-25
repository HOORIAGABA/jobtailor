"""The run-folder archive, and the app's own setup reporting.

The run ENDPOINTS moved to `test_run_api.py` when they started reading the
database. What is left here is `api.archive`, which is still load-bearing:
`scripts/seed.py` imports folders through it, so the path-safety and
manifest-tolerance rules below still guard real behaviour.

No mocks: the fixture runs the pipeline with a `ScriptedClient`, so these
tests exercise the same folder shape a real run produces.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.archive import Archive, RunNotFound
from app.api.main import create_app


# ── a real run folder ─────────────────────────────────────────────────

@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    """One complete run, produced by the pipeline, with no API key."""
    from tests.test_pipeline import JOB_TEXT, client
    from app.io.runlog import RunLog
    from app.pipeline.run import run

    resume = (
        b"A. MORGAN\na.morgan@example.com\n\nWORK EXPERIENCE\n\n"
        b"Data Analyst\nAcme Corp | 01/2022 - Present\n"
        b"- Built reporting pipelines in Python\n"
        b"- Responsible for weekly stakeholder reports\n\n"
        b"TECHNICAL SKILLS\nLanguages: Python, SQL\nTools: Airflow, Docker\n"
    )
    root = tmp_path / "runs"
    log = RunLog.create("morgan", root=root)
    run(resume, "a_morgan.txt", JOB_TEXT, client(), log=log)
    log.finish()
    return root


# ── capabilities ══════════════════════════════════════════════════════

def test_the_instance_says_what_it_can_do():
    """A frontend that guessed from the hostname would be wrong the first time
    someone ran it somewhere else."""
    body = TestClient(create_app(Path("runs"), read_only=True)) \
        .get("/api/capabilities").json()
    assert body["read_only"] is True
    assert body["can_start_runs"] is False
    # Every stage is built now, including the send — with the console backend,
    # which the capability says plainly rather than implying a wire.
    assert body["stages_not_built"] == []
    assert "S11 send" in " ".join(body["stages_built"])
    assert "sends nothing" in body["mail_provider"]
    assert "S9 approve" in body["stages_built"]      # as of step 6
    assert "S8 outreach" in body["stages_built"]


# ── listing and detail ════════════════════════════════════════════════

# ── the files ═════════════════════════════════════════════════════════

# ── the archive refuses to leave its directory ════════════════════════

@pytest.mark.parametrize("run_id", [
    "../secrets", "..", ".", "a/b", "a\\b", ".hidden", "",
])
def test_a_run_id_cannot_escape_the_runs_directory(runs_dir: Path, run_id: str):
    """`run_id` arrives from a URL, and `../` is the oldest file-serving bug.

    This project already declined to fetch a user-supplied URL for the same
    class of reason: a value from outside does not choose which file is read.
    """
    with pytest.raises(RunNotFound):
        Archive(runs_dir).detail(run_id)


def test_an_artifact_name_cannot_escape_either(runs_dir: Path):
    archive = Archive(runs_dir)
    run_id = archive.run_ids()[0]
    with pytest.raises(RunNotFound):
        archive._file(run_id, "../manifest.json")


# ── the archive itself ════════════════════════════════════════════════

def test_a_folder_with_no_manifest_is_not_listed(runs_dir: Path):
    """No manifest means the run is still going or was killed — it has no
    consistent story to tell yet."""
    (runs_dir / "2026-01-01T00-00-00_partial").mkdir()
    (runs_dir / "2026-01-01T00-00-00_partial" / "00_extract.txt").write_text("x", encoding="utf-8")
    assert "2026-01-01T00-00-00_partial" not in Archive(runs_dir).run_ids()


def test_one_unreadable_folder_does_not_break_the_list(runs_dir: Path):
    broken = runs_dir / "2026-01-01T00-00-00_broken"
    broken.mkdir()
    (broken / "manifest.json").write_text("{not json", encoding="utf-8")
    summaries = Archive(runs_dir).summaries()
    assert len(summaries) == 1                       # the good one survives


def test_runs_are_newest_first(runs_dir: Path):
    for name in ("2020-01-01T00-00-00_old", "2030-01-01T00-00-00_new"):
        folder = runs_dir / name
        folder.mkdir()
        (folder / "manifest.json").write_text(json.dumps(
            {"run_id": name, "ok": True, "artifacts": []}), encoding="utf-8")
    ids = Archive(runs_dir).run_ids()
    assert ids[0].startswith("2030")
    assert ids[-1].startswith("2020")


def test_a_failed_run_is_still_served(runs_dir: Path):
    """A run folder exists *because* a failure leaves the stages that explain it."""
    folder = runs_dir / "2026-06-01T00-00-00_failed"
    folder.mkdir()
    (folder / "manifest.json").write_text(json.dumps({
        "run_id": "2026-06-01T00-00-00_failed", "ok": False,
        "error": "ResponseTruncated: ran out of room", "artifacts": [],
    }), encoding="utf-8")
    summary = Archive(runs_dir).summary("2026-06-01T00-00-00_failed")
    assert summary.ok is False
    assert "ResponseTruncated" in summary.error


def test_the_archive_does_not_cache(runs_dir: Path):
    """A run folder is immutable once finished, so re-reading is always current.
    A cache would be the one way to make this wrong."""
    archive = Archive(runs_dir)
    first = archive.run_ids()
    (runs_dir / "2027-01-01T00-00-00_later").mkdir()
    (runs_dir / "2027-01-01T00-00-00_later" / "manifest.json").write_text(
        json.dumps({"run_id": "later", "ok": True, "artifacts": []}), encoding="utf-8")
    assert len(archive.run_ids()) == len(first) + 1


def test_runlog_is_still_write_only():
    """The reader is a separate type on purpose — see `api.archive`.

    This asserts the invariant did not get quietly relaxed to make the API
    easier: nothing was added to `RunLog`.
    """
    from app.io.runlog import RunLog
    assert not [n for n in dir(RunLog)
                if n.startswith(("load", "read", "open", "restore"))]


# ══ the root route is the setup check ═════════════════════════════════

def test_the_root_says_what_is_missing(tmp_path, monkeypatch):
    """A bare 404 at the root is technically right and tells a person nothing.

    This is the first URL anyone opens, so it answers "is the database
    migrated" and "do I have an identity" — both of which otherwise surface
    later as a confusing 500 or a 401 on a form submit.
    """
    from app.db import session as db_session

    monkeypatch.delenv("DEV_USER_EMAIL", raising=False)
    db_session.reset(f"sqlite+pysqlite:///{tmp_path / 'empty.db'}")
    body = TestClient(create_app(tmp_path / "runs")).get("/").json()

    assert body["service"] == "JobTailor"
    assert "alembic upgrade head" in body["setup"]["database"]
    assert "DEV_USER_EMAIL" in body["setup"]["identity"]
    db_session.reset()


def test_the_root_names_the_powershell_trap(tmp_path, monkeypatch):
    """`set NAME=value` is cmd.exe. In PowerShell it sets a shell variable the
    process cannot see, so every write 401s with no clue why."""
    from app.db import session as db_session

    monkeypatch.delenv("DEV_USER_EMAIL", raising=False)
    db_session.reset(f"sqlite+pysqlite:///{tmp_path / 'e.db'}")
    identity = TestClient(create_app(tmp_path / "runs")).get("/").json()["setup"]["identity"]
    assert "$env:" in identity
    db_session.reset()


def test_the_root_reports_a_ready_database(tmp_path, monkeypatch):
    from app.db import session as db_session
    from app.db.session import create_all

    monkeypatch.setenv("DEV_USER_EMAIL", "dev@example.com")
    db_session.reset(f"sqlite+pysqlite:///{tmp_path / 'ready.db'}")
    create_all(db_session.engine())
    body = TestClient(create_app(tmp_path / "runs")).get("/").json()

    assert body["setup"]["database"] == "ready"
    assert body["setup"]["identity"].startswith("local development user")
    # And it says which secrets sign-in is still missing, rather than only
    # failing later at the redirect.
    assert "GOOGLE_CLIENT_ID" in body["setup"]["google_sign_in"]
    assert "/api/resumes" in body["endpoints"]
    db_session.reset()


# ══ the deployment footguns ═══════════════════════════════════════════

def test_the_development_identity_is_ignored_in_production(tmp_path, monkeypatch):
    """★ With DEV_USER_EMAIL set, every unauthenticated request becomes one
    named user. That is what makes local work possible and what must never
    survive a deploy."""
    from app.db import session as db_session
    from app.db.session import create_all

    monkeypatch.setenv("DEV_USER_EMAIL", "dev@example.com")
    db_session.reset(f"sqlite+pysqlite:///{tmp_path / 'prod.db'}")
    create_all(db_session.engine())
    client = TestClient(create_app(tmp_path / "runs", read_only=False))

    # Development: the identity resolves.
    monkeypatch.setenv("ENVIRONMENT", "development")
    assert client.get("/api/auth/me").status_code == 200

    # Production: the same variable, the same request, refused.
    monkeypatch.setenv("ENVIRONMENT", "production")
    response = client.get("/api/auth/me")
    assert response.status_code == 401
    assert "development only" in response.json()["detail"]
    db_session.reset()


def test_production_startup_complains_about_a_dev_identity(monkeypatch):
    """Ignoring it at request time is the safety net. It should not be in the
    environment at all, and startup is where that gets said."""
    from app.config import Settings, check

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DEV_USER_EMAIL", "dev@example.com")
    problems = " ".join(check(Settings()))
    assert "DEV_USER_EMAIL is set on a production instance" in problems


def test_a_cors_wildcard_is_refused_with_the_reason(monkeypatch):
    """It does not open a hole — browsers reject a credentialed response
    carrying `*`. It silently breaks sign-in, which is a worse failure than a
    refusal because it looks like an auth bug three files away."""
    from app.config import Settings, check

    monkeypatch.setenv("CORS_ORIGINS", "*")
    problems = " ".join(check(Settings()))
    assert "CORS_ORIGINS=* cannot work" in problems
    assert "session cookie" in problems


def test_a_deployment_can_declare_itself_read_only(tmp_path, monkeypatch):
    """A hosted instance must be able to say "stored runs only" without relying
    on the absence of a model to imply it — a stray LLM_API_KEY would otherwise
    turn a public URL into something that spends a quota per visitor."""
    from app.api.main import READ_ONLY_ENV

    monkeypatch.setenv(READ_ONLY_ENV, "true")
    assert create_app(tmp_path).state.read_only is True

    monkeypatch.setenv(READ_ONLY_ENV, "false")
    assert create_app(tmp_path).state.read_only is False

    # The argument still wins, so tests are not at the mercy of the environment.
    monkeypatch.setenv(READ_ONLY_ENV, "true")
    assert create_app(tmp_path, read_only=False).state.read_only is False


def test_an_unset_flag_falls_back_to_whether_a_model_is_reachable(tmp_path,
                                                                  monkeypatch):
    """So a laptop works with no configuration at all."""
    from app.api.main import READ_ONLY_ENV

    monkeypatch.delenv(READ_ONLY_ENV, raising=False)
    assert create_app(tmp_path).state.read_only is True     # no model here
