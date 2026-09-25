"""Import run folders into the database. **Zero model calls.**

    python scripts/seed.py
    python scripts/seed.py --runs runs --email you@example.com --reset

This is what makes the rest of the product testable on a free tier. Every run
folder already on disk contains what a `Resume` row and a `Run` row need — the
parse, the coverage report, the normalized document, the brief, the evidence,
the plan, what the validator accepted, the diff, the outreach draft and the
rendered files. Loading them turns runs that really happened into fixtures, so
the approval UI, the stage inspector and the file downloads can all be built
and exercised **with no model configured at all**.

Two properties worth being deliberate about.

**It reads through `api.archive`**, not with its own directory walk. Old runs
number their artifacts differently from new ones — the brief was `04_brief.json`
before S1.0 was inserted and is `05_brief.json` after — so anything keying off
filenames breaks on half the corpus. The manifest records a *stage* name per
artifact and `Archive` looks them up by it.

**It is idempotent.** Ids are derived from the run folder name and the input
file's hash, so re-running updates rather than duplicating, and every run of the
same resume attaches to one `Resume` row — which is also the shape the product
has in production, where one confirmed resume serves many applications.

A completed run lands in `needs_review`, because that is where it actually
stopped: S9 does not exist yet, so nobody has decided. That is precisely the
state the approval screen needs to render.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.archive import Archive, RunDetail, RunNotFound
from app.db import models
from app.db.session import create_all, engine, session_scope
from app.io.dblog import standing_of

logger = logging.getLogger("seed")

# The stages a Resume row is built from, and the stages a Run row is built from.
# Named rather than positional for the reason in the module docstring.
RESUME_STAGES = ("parse", "coverage", "normalize", "extract")
FILE_STAGES = ("resume_docx", "resume_pdf", "resume_ats")

_MEDIA = {
    ".docx": "application/vnd.openxmlformats-officedocument."
             "wordprocessingml.document",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
}


def _id(*parts: str) -> str:
    """A deterministic 32-hex id, so re-seeding updates instead of duplicating."""
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def _json(archive: Archive, run_id: str, stage: str) -> Any | None:
    try:
        return archive.stage_json(run_id, stage)
    except (RunNotFound, ValueError):
        return None


def _text(archive: Archive, run_id: str, stage: str) -> str:
    try:
        return archive.stage_text(run_id, stage)
    except (RunNotFound, ValueError, UnicodeDecodeError):
        return ""


# ── the user ──────────────────────────────────────────────────────────

def ensure_user(session, email: str) -> models.User:
    """The fixture identity, matching the one `api.deps` resolves locally.

    Same `google_sub` as the development user, so seeded runs appear under the
    identity the API will hand a browser — a seeder that loaded data nobody
    could see would be worse than no seeder.
    """
    from app.api.deps import DEV_SUB

    user = (session.query(models.User)
            .filter(models.User.google_sub == DEV_SUB).one_or_none())
    if user is None:
        user = models.User(google_sub=DEV_SUB, email=email,
                           name=email.split("@")[0])
        session.add(user)
        session.flush()
        logger.info("Created user %s", email)
    return user


# ── the resume ────────────────────────────────────────────────────────

class NoDocument(Exception):
    """This folder cannot produce a `Resume` row, with the reason why."""


def upsert_resume(session, user: models.User, archive: Archive,
                  detail: RunDetail) -> models.Resume:
    """One `Resume` per input file, confirmed.

    Confirmed because it demonstrably was: a run used it. Raises `NoDocument`
    when the folder has no normalized document to attach anything to.
    """
    manifest = detail.manifest
    run_id = detail.summary.run_id
    digest = str(manifest.get("input_sha256") or "")
    doc_json = _json(archive, run_id, "normalize")
    if not digest or not doc_json:
        # Two different situations, worth telling apart: the manifest not
        # listing `normalize` means the run died before S0.3, and listing it
        # while the file is unreadable means the folder is incomplete.
        raise NoDocument(
            "manifest lists a normalize artifact but it could not be read"
            if detail.artifact("normalize") is not None
            else "died before S0.3, no document to attach"
        )

    resume_id = _id("resume", digest)
    resume = session.get(models.Resume, resume_id)
    if resume is None:
        resume = models.Resume(id=resume_id, user_id=user.id, version=1)
        session.add(resume)

    # Newest wins, but only where the newest folder actually HAS the artifact.
    # A later run whose folder is missing `coverage` must not blank out a
    # coverage report an earlier run recorded — the seeder merges the corpus,
    # and overwriting a real value with an absence loses information for no
    # reason. Folders are genuinely uneven: runs from before S10 have no
    # rendered files, runs that died early have no brief.
    resume.filename = str(manifest.get("input_name") or resume.filename
                          or "resume.pdf")
    resume.file_sha256 = digest
    resume.file_bytes = int(manifest.get("input_bytes") or 0) or resume.file_bytes
    resume.extract_text = _text(archive, run_id, "extract") or resume.extract_text
    resume.raw_json = _json(archive, run_id, "parse") or resume.raw_json
    resume.coverage_json = (_json(archive, run_id, "coverage")
                            or resume.coverage_json)
    resume.doc_json = doc_json
    # Set directly rather than through `move_to`: the machine describes how a
    # resume gets to `confirmed` from a live parse, and this one is being
    # restored rather than walked there.
    resume.status = "confirmed"
    resume.confirmed_at = resume.confirmed_at or _stamp(manifest)
    session.flush()
    return resume


# ── the run ───────────────────────────────────────────────────────────

def upsert_run(session, user: models.User, resume: models.Resume,
               archive: Archive, detail: RunDetail) -> models.Run:
    summary, manifest = detail.summary, detail.manifest
    run_id = _id("run", summary.run_id)

    run = session.get(models.Run, run_id)
    if run is None:
        run = models.Run(id=run_id, user_id=user.id, resume_id=resume.id)
        session.add(run)

    validation = _json(archive, summary.run_id, "validation") or {}
    plan = _json(archive, summary.run_id, "plan") or {}

    brief = _json(archive, summary.run_id, "brief") or {}

    run.resume_id = resume.id
    run.jd_sha256 = str(brief.get("source_hash") or "")
    # The manifest only started carrying these today, so older folders have
    # them nowhere but the brief itself. Reading both keeps every fixture
    # showing which job it was for instead of half of them saying "(no role)".
    run.role = summary.role or str(brief.get("role") or "")
    run.company = summary.company or str(brief.get("company") or "")
    run.brief_json = brief or None
    evidence = _json(archive, summary.run_id, "evidence")
    run.index_json = evidence
    # Same rule as a live run, from the same function — see `io.dblog`.
    run.standing_json = standing_of("evidence", evidence)
    run.ops_json = plan.get("operations") if isinstance(plan, dict) else plan
    run.accepted_json = validation.get("accepted")
    run.rejected_json = validation.get("rejected")
    run.tailored_json = _json(archive, summary.run_id, "tailored")
    run.diff_json = _json(archive, summary.run_id, "diff")
    run.outreach_json = _json(archive, summary.run_id, "outreach")
    run.proof_json = manifest.get("proof")
    run.llm_calls = summary.calls
    run.tokens = summary.tokens
    run.call_log_json = manifest.get("call_log")
    run.error = summary.error
    run.created_at = _stamp(manifest) or run.created_at

    # Where the run actually stopped. A completed run has not been decided —
    # S9 does not exist — so `needs_review` is the truth, not a placeholder.
    run.status = "needs_review" if summary.ok else "failed"
    run.stage = _last_stage(detail)
    session.flush()
    return run


def seed_checkpoints(session, run: models.Run, detail: RunDetail) -> int:
    """One per artifact, so the progress stream has something to replay."""
    existing = {c.stage for c in run.checkpoints}
    added = 0
    for artifact in detail.artifacts:
        if artifact.stage in existing or not artifact.stage:
            continue
        session.add(models.RunCheckpoint(
            run_id=run.id, stage=artifact.stage,
            state_json={"file": artifact.filename, "bytes": artifact.bytes}))
        added += 1
    session.flush()
    return added


def seed_files(session, run: models.Run, archive: Archive,
               detail: RunDetail) -> list[str]:
    """The rendered .docx, .pdf and ATS text, as bytes in rows.

    Runs from before S10 have none of these. Those still seed as complete runs
    with nothing to download, which is accurate rather than a gap to paper over.
    """
    stored = {a.stage for a in run.artifacts}
    added: list[str] = []
    for stage in FILE_STAGES:
        if stage in stored or detail.artifact(stage) is None:
            continue
        try:
            blob, filename = archive.stage_bytes(detail.summary.run_id, stage)
        except (RunNotFound, OSError):
            continue
        session.add(models.Artifact(
            run_id=run.id, stage=stage, filename=filename,
            content_type=_media(filename), bytes=len(blob), blob=blob))
        added.append(stage)
    session.flush()
    return added


# ── the whole job ─────────────────────────────────────────────────────

def seed(runs_dir: Path, email: str, *, reset: bool = False) -> dict[str, Any]:
    archive = Archive(runs_dir)
    # OLDEST first. `run_ids()` is newest-first for a UI list, and seeding in
    # that order was a real bug: every run of the same file maps to one
    # `Resume` row, so the last folder processed wins — and that was the
    # oldest one. On this corpus thirteen runs share a single resume and the
    # earliest of them had parsed zero sections, so a good document was being
    # overwritten by a known-bad one. Later runs used a better parser; their
    # parse is the one to keep.
    run_ids = list(reversed(archive.run_ids()))
    report: dict[str, Any] = {
        "folders": len(run_ids), "runs": 0, "resumes": set(),
        "files": 0, "checkpoints": 0, "skipped": [],
    }
    if not run_ids:
        logger.warning("No finished runs in %s — a folder needs a manifest.json",
                       runs_dir)
        return report

    with session_scope() as session:
        if reset:
            # Children first: the FKs are enforced, which is the point of the
            # PRAGMA, so a naive delete order fails loudly rather than orphaning.
            for table in (models.SendAudit, models.Message, models.OpFeedback,
                          models.Artifact, models.RunCheckpoint, models.Run,
                          models.Resume):
                session.query(table).delete()
            session.flush()
            logger.info("Cleared existing runs and resumes")

        user = ensure_user(session, email)

        for folder in run_ids:
            try:
                detail = archive.detail(folder)
            except (RunNotFound, ValueError) as exc:
                report["skipped"].append(f"{folder}: unreadable manifest ({exc})")
                continue

            try:
                resume = upsert_resume(session, user, archive, detail)
            except NoDocument as exc:
                report["skipped"].append(f"{folder}: {exc}")
                continue

            run = upsert_run(session, user, resume, archive, detail)
            report["runs"] += 1
            report["resumes"].add(resume.id)
            report["checkpoints"] += seed_checkpoints(session, run, detail)
            report["files"] += len(seed_files(session, run, archive, detail))

    report["resumes"] = len(report["resumes"])
    return report


# ── internals ─────────────────────────────────────────────────────────

def _media(filename: str) -> str:
    return _MEDIA.get(Path(filename).suffix.lower(), "application/octet-stream")


def _stamp(manifest: dict[str, Any]):
    from datetime import datetime
    raw = str(manifest.get("started") or "")
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _last_stage(detail: RunDetail) -> str:
    return detail.artifacts[-1].stage if detail.artifacts else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, default=Path("runs"))
    parser.add_argument("--email", default=os.environ.get("DEV_USER_EMAIL", ""))
    parser.add_argument("--reset", action="store_true",
                        help="delete existing runs and resumes first")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s")

    if not args.email:
        print("Give an address: --email you@example.com, or set DEV_USER_EMAIL.")
        return 2
    if not args.runs.is_dir():
        print(f"No such directory: {args.runs}")
        return 2

    create_all(engine())          # harmless when Alembic has already run
    report = seed(args.runs, args.email, reset=args.reset)

    print(f"\n  {report['runs']} run(s) from {report['folders']} folder(s)")
    print(f"  {report['resumes']} resume(s), {report['files']} rendered file(s), "
          f"{report['checkpoints']} checkpoint(s)")
    for note in report["skipped"]:
        print(f"  skipped  {note}")
    if report["runs"]:
        print("\n  Seeded runs sit in `needs_review` — where they actually "
              "stopped.")
        print("  GET /api/runs serves them from the database.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
