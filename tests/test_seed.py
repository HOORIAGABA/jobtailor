"""The seeder — run folders into the database, zero model calls.

This is what makes the approval UI, the stage inspector and the downloads
buildable on a free tier, so the tests are about the awkward parts of a real
corpus rather than the happy path: folders of different vintages, folders
missing artifacts, and many runs sharing one resume.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import models
from app.db.session import create_all, engine, reset, session_scope
from scripts import seed as seeder

SHA_A = "a" * 64
SHA_B = "b" * 64


def _write(root: Path, run_id: str, *, ok: bool = True, sha: str = SHA_A,
           sections: int = 3, artifacts: dict[str, object] | None = None,
           started: str = "2026-09-20T10:00:00+00:00", **manifest) -> Path:
    """A run folder, as `RunLog` would have left it."""
    folder = root / run_id
    folder.mkdir(parents=True, exist_ok=True)
    listed: list[dict] = []

    payloads = {
        "extract": ("00_extract.txt", "A. MORGAN\nWORK EXPERIENCE\n"),
        "normalize": ("03_normalize.json", {
            "contact": {"full_name": "A. Morgan"}, "summary": "",
            "sections": [
                {"id": f"sec.{n}", "kind": "experience",
                 "heading": f"SECTION {n}", "items": []}
                for n in range(sections)
            ],
            "skill_inventory": ["Python", "SQL"], "seq": 0,
        }),
    }
    payloads.update(artifacts or {})

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
        "run_id": run_id, "ok": ok, "started": started, "seconds": 1.0,
        "input_name": "cv.pdf", "input_sha256": sha, "input_bytes": 1024,
        "artifacts": listed, **manifest,
    }), encoding="utf-8")
    return folder


@pytest.fixture
def db(tmp_path, monkeypatch):
    url = f"sqlite+pysqlite:///{tmp_path / 'seed.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    reset(url)
    create_all(engine())
    yield
    reset()


# ══ the ordering bug this corpus actually had ═════════════════════════

def test_the_newest_parse_wins_when_runs_share_a_resume(tmp_path, db):
    """Thirteen runs of one file, and the earliest had parsed zero sections.

    Seeding newest-first meant the last folder processed was the oldest, so a
    good document was overwritten by a known-bad one. Later runs used a better
    parser; theirs is the parse to keep.
    """
    runs = tmp_path / "runs"
    _write(runs, "2026-09-01T00-00-00_old", sections=0,
           started="2026-09-01T00:00:00+00:00")
    _write(runs, "2026-09-20T00-00-00_new", sections=6,
           started="2026-09-20T00:00:00+00:00")

    seeder.seed(runs, "dev@example.com")

    with session_scope() as s:
        resume = s.query(models.Resume).one()          # one file, one row
        assert len(resume.doc_json["sections"]) == 6


def test_a_missing_artifact_does_not_blank_out_an_earlier_one(tmp_path, db):
    """Folders are genuinely uneven — runs before S10 have no rendered files,
    runs that died early have no brief. Overwriting a real value with an
    absence loses information for no reason."""
    runs = tmp_path / "runs"
    _write(runs, "2026-09-01T00-00-00_has", started="2026-09-01T00:00:00+00:00",
           artifacts={"coverage": ("02_coverage.json",
                                   {"dropped": ["City: Springfield"],
                                    "invented": [], "structure": []})})
    _write(runs, "2026-09-20T00-00-00_lacks", started="2026-09-20T00:00:00+00:00")

    seeder.seed(runs, "dev@example.com")

    with session_scope() as s:
        resume = s.query(models.Resume).one()
        assert resume.coverage_json["dropped"] == ["City: Springfield"]


# ══ stage names, not filenames ════════════════════════════════════════

def test_the_brief_is_found_whatever_it_is_numbered(tmp_path, db):
    """The brief was `04_brief.json` before S1.0 was inserted and
    `05_brief.json` after. Anything keying off filenames breaks on half the
    corpus, so the manifest's stage name is what is looked up."""
    runs = tmp_path / "runs"
    _write(runs, "2026-09-01T00-00-00_old", sha=SHA_A,
           artifacts={"brief": ("04_brief.json",
                                {"role": "Data Analyst", "company": "Acme"})})
    _write(runs, "2026-09-20T00-00-00_new", sha=SHA_B,
           artifacts={"brief": ("05_brief.json",
                                {"role": "AI Engineer", "company": "Annova"})})

    seeder.seed(runs, "dev@example.com")

    with session_scope() as s:
        roles = {r.role for r in s.query(models.Run).all()}
        assert roles == {"Data Analyst", "AI Engineer"}


def test_the_role_falls_back_to_the_brief(tmp_path, db):
    """The manifest only started carrying role and company recently; older
    folders have them nowhere but the brief itself."""
    runs = tmp_path / "runs"
    _write(runs, "2026-09-01T00-00-00_a",
           artifacts={"brief": ("05_brief.json",
                                {"role": "AI Engineer", "company": "Annova"})})
    seeder.seed(runs, "dev@example.com")
    with session_scope() as s:
        run = s.query(models.Run).one()
        assert run.role == "AI Engineer" and run.company == "Annova"


# ══ where a seeded run lands ══════════════════════════════════════════

def test_a_completed_run_lands_in_needs_review(tmp_path, db):
    """Where it actually stopped. S9 does not exist, so nobody has decided —
    which is exactly the state the approval screen needs to render."""
    runs = tmp_path / "runs"
    _write(runs, "2026-09-20T00-00-00_done", ok=True,
           artifacts={"diff": ("11_diff.json", {"changes": [], "gaps": []})})
    seeder.seed(runs, "dev@example.com")
    with session_scope() as s:
        assert s.query(models.Run).one().status == "needs_review"


def test_a_failed_run_is_seeded_too(tmp_path, db):
    """A run folder exists *because* a failure leaves the stages that explain
    it — those are worth rendering."""
    runs = tmp_path / "runs"
    _write(runs, "2026-09-20T00-00-00_bad", ok=False,
           error="ResponseTruncated: ran out of room")
    seeder.seed(runs, "dev@example.com")
    with session_scope() as s:
        run = s.query(models.Run).one()
        assert run.status == "failed"
        assert "ResponseTruncated" in run.error


def test_a_folder_that_died_before_normalize_is_skipped_with_a_reason(
        tmp_path, db):
    runs = tmp_path / "runs"
    folder = runs / "2026-09-20T00-00-00_early"
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps({
        "run_id": "2026-09-20T00-00-00_early", "ok": False,
        "input_sha256": SHA_A, "artifacts": []}), encoding="utf-8")

    report = seeder.seed(runs, "dev@example.com")
    assert report["runs"] == 0
    assert "died before S0.3" in report["skipped"][0]


def test_an_incomplete_folder_is_named_as_such(tmp_path, db):
    """Different from an early death: the manifest lists the artifact and the
    file is not there, which means the folder is damaged."""
    runs = tmp_path / "runs"
    folder = _write(runs, "2026-09-20T00-00-00_gone")
    (folder / "03_normalize.json").unlink()

    report = seeder.seed(runs, "dev@example.com")
    assert "could not be read" in report["skipped"][0]


# ══ files, checkpoints, idempotence ═══════════════════════════════════

def test_rendered_files_are_stored_as_bytes(tmp_path, db):
    runs = tmp_path / "runs"
    _write(runs, "2026-09-20T00-00-00_files", artifacts={
        "resume_docx": ("12_resume_docx.docx", b"PK\x03\x04fake"),
        "resume_pdf": ("13_resume_pdf.pdf", b"%PDF-1.4 fake"),
        "resume_ats": ("14_resume_ats.txt", "A. MORGAN\nWORK EXPERIENCE"),
    })
    seeder.seed(runs, "dev@example.com")

    with session_scope() as s:
        by_stage = {a.stage: a for a in s.query(models.Artifact).all()}
        assert by_stage["resume_docx"].blob.startswith(b"PK")
        assert "wordprocessingml" in by_stage["resume_docx"].content_type
        assert by_stage["resume_pdf"].content_type == "application/pdf"
        assert "A. MORGAN" in by_stage["resume_ats"].blob.decode()


def test_a_run_from_before_s10_seeds_with_nothing_to_download(tmp_path, db):
    """Accurate rather than a gap to paper over."""
    runs = tmp_path / "runs"
    _write(runs, "2026-09-01T00-00-00_pre_s10")
    seeder.seed(runs, "dev@example.com")
    with session_scope() as s:
        assert s.query(models.Run).one().status == "needs_review"
        assert s.query(models.Artifact).count() == 0


def test_every_stage_becomes_a_checkpoint(tmp_path, db):
    """So the progress stream has something to replay."""
    runs = tmp_path / "runs"
    _write(runs, "2026-09-20T00-00-00_ck", artifacts={
        "brief": ("05_brief.json", {"role": "X"}),
        "diff": ("11_diff.json", {"changes": []}),
    })
    seeder.seed(runs, "dev@example.com")
    with session_scope() as s:
        stages = {c.stage for c in s.query(models.RunCheckpoint).all()}
        assert stages == {"extract", "normalize", "brief", "diff"}


def test_seeding_twice_updates_rather_than_duplicates(tmp_path, db):
    """Ids are derived from the folder name and the file hash."""
    runs = tmp_path / "runs"
    _write(runs, "2026-09-20T00-00-00_a", sha=SHA_A)
    _write(runs, "2026-09-20T00-00-01_b", sha=SHA_B)

    first = seeder.seed(runs, "dev@example.com")
    second = seeder.seed(runs, "dev@example.com")

    assert first["runs"] == second["runs"] == 2
    with session_scope() as s:
        assert s.query(models.Run).count() == 2
        assert s.query(models.Resume).count() == 2
        assert s.query(models.RunCheckpoint).count() == 4      # not doubled


def test_reset_clears_first(tmp_path, db):
    runs = tmp_path / "runs"
    _write(runs, "2026-09-20T00-00-00_a")
    seeder.seed(runs, "dev@example.com")
    seeder.seed(runs, "dev@example.com", reset=True)
    with session_scope() as s:
        assert s.query(models.Run).count() == 1


def test_the_seeded_user_is_the_one_the_api_resolves(tmp_path, db):
    """A seeder that loaded data nobody could see would be worse than none."""
    from app.api.deps import DEV_SUB
    runs = tmp_path / "runs"
    _write(runs, "2026-09-20T00-00-00_a")
    seeder.seed(runs, "dev@example.com")
    with session_scope() as s:
        assert s.query(models.User).one().google_sub == DEV_SUB


def test_an_empty_directory_is_not_an_error(tmp_path, db):
    runs = tmp_path / "runs"
    runs.mkdir()
    report = seeder.seed(runs, "dev@example.com")
    assert report == {"folders": 0, "runs": 0, "resumes": set(),
                      "files": 0, "checkpoints": 0, "skipped": []}
