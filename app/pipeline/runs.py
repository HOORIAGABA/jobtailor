"""A run as a row: create it, execute it, read it back.

The stages are unchanged. What this adds is the part a gate needs — the run
stops being a call stack and becomes something that survives the request that
started it, so a decision arriving three days later still has something to
decide about.

**A run starts from a CONFIRMED resume.** Not from an upload: S0.1–S0.3 already
happened and a person already looked at the result. That is why the resume is a
separate entity — one confirmed parse serves every application made from it,
and a second job against the same resume costs nothing in S0.

**Execution writes as it goes**, through `io.dblog.DbRunLog`, which is a
`RunLog` like any other. A process killed halfway leaves the stages that
finished, which is what makes the progress stream honest and a failure
explicable. v1 put this work on a daemon thread and lost a run when a free-tier
host spun down after fifteen minutes; the fix was never a sturdier thread.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Iterable

from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Artifact, Capability, Resume, Run, RunCheckpoint, User
from app.domain.errors import JobTailorError, UserError
from app.domain.models import ResumeDoc
from app.io.cache import Cache, NullCache
from app.io.dblog import DbRunLog
from app.io.llm import LLMClient
from app.pipeline.run import tailor

logger = logging.getLogger(__name__)

MAX_POSTING_CHARS = 60_000


def create(session: Session, user: User, resume: Resume, jd_text: str) -> Run:
    """A new application. Refuses an unconfirmed resume.

    The refusal matters: tailoring an unconfirmed parse would silently build on
    a document the person has not agreed is theirs, and every later claim —
    "this bullet evidences that requirement" — would inherit the doubt.
    """
    if resume.status != "confirmed":
        raise UserError(
            f"this resume is {resume.status!r}; confirm the parse before "
            f"applying it to a job"
        )
    text = (jd_text or "").strip()
    if not text:
        raise UserError("no job posting given")
    if len(text) > MAX_POSTING_CHARS:
        raise UserError(
            f"the posting is {len(text):,} characters; the limit is "
            f"{MAX_POSTING_CHARS:,}"
        )

    run = Run(
        user_id=user.id, resume_id=resume.id, jd_text=text,
        jd_sha256=hashlib.sha256(text.encode()).hexdigest(),
        status="created",
    )
    session.add(run)
    session.flush()
    logger.info("Run %s created from resume %s", run.id, resume.id)
    return run


def execute(factory: sessionmaker[Session], run_id: str,
            client: LLMClient, *, smart_client: LLMClient | None = None,
            cache: Cache | None = None, semantic_hints: bool = False,
            write_outreach: bool = True) -> None:
    """S1–S8 and S10, writing each stage as it completes.

    Takes a session factory rather than a session because this runs in a worker
    thread; each write is its own short transaction, which is also what makes
    each one durable the moment its stage finishes.

    Never raises. A failed run is a run in `failed` with the reason on it —
    raising here would lose that, because nobody is awaiting this call.
    """
    with factory() as session:
        run = session.get(Run, run_id)
        if run is None:
            logger.error("No run %s to execute", run_id)
            return
        resume = session.get(Resume, run.resume_id)
        doc = ResumeDoc.model_validate(resume.doc_json)
        jd_text = run.jd_text
        confirmed = _confirmed_capabilities(session, run.user_id)
        run.move_to("tailoring")
        session.commit()

    log = DbRunLog(run_id, factory)
    try:
        tailor(
            doc, jd_text, smart_client or client,
            log=log, cache=cache or NullCache(),
            confirmed_capabilities=confirmed,
            semantic_hints=semantic_hints,
            write_outreach=write_outreach,
        )
    except JobTailorError as exc:
        _fail(factory, run_id, exc, log)
        return
    except Exception as exc:                              # noqa: BLE001
        logger.exception("Run %s failed unexpectedly", run_id)
        _fail(factory, run_id, exc, log)
        return

    with factory() as session:
        run = session.get(Run, run_id)
        if run is not None:
            run.move_to("needs_review")
            session.commit()
    logger.info("Run %s is ready for review", run_id)


def _fail(factory, run_id: str, exc: Exception, log: DbRunLog) -> None:
    """Record why, including the model's own words when there were any."""
    raw = getattr(exc, "raw", "")
    if raw:
        # The response IS the evidence: without it, "the JSON was incomplete"
        # cannot distinguish a model that rambled from one cut off mid-string.
        log.text(f"{getattr(exc, 'stage', 'model')}_response", raw)
    with factory() as session:
        run = session.get(Run, run_id)
        if run is not None:
            run.error = f"{type(exc).__name__}: {exc}"
            run.status = "failed"
            session.commit()


def _confirmed_capabilities(session: Session, user_id: str) -> tuple[str, ...]:
    """The capability ledger. Inference may propose; only a person may add."""
    return tuple(
        row.term for row in
        session.query(Capability).filter(Capability.user_id == user_id).all()
    )


# ── reading ───────────────────────────────────────────────────────────

def summary(run: Run) -> dict[str, Any]:
    """A list row. No JSON blobs — this is rendered fifty at a time."""
    from app.domain.status import artifacts_available

    return {
        "id": run.id,
        "status": run.status,
        "stage": run.stage,
        "role": run.role,
        "company": run.company,
        "resume_id": run.resume_id,
        "llm_calls": run.llm_calls,
        "tokens": run.tokens,
        "error": run.error,
        "proof": run.proof_json or {},
        "created_at": run.created_at.isoformat() if run.created_at else "",
        "decided_at": run.decided_at.isoformat() if run.decided_at else "",
        "changes": len(run.diff_json.get("changes", [])) if run.diff_json else 0,
        "accepted": len(run.accepted_json or []),
        "rejected": len(run.rejected_json or []),
        "can_download": artifacts_available(run.status),
    }


def detail(session: Session, run: Run) -> dict[str, Any]:
    """Everything the review screen needs, in one response.

    The gap list, the diff and the outreach draft are what the person is being
    asked about, so they arrive together rather than as three round trips.
    """
    from app.domain.status import artifacts_available, may_send

    return {
        **summary(run),
        "brief": run.brief_json,
        "standing": run.standing_json,   # S2's three grades
        "evidence": run.index_json,
        "ops": run.ops_json or [],
        "accepted_ops": run.accepted_json or [],
        "rejected_ops": run.rejected_json or [],
        "diff": run.diff_json,
        "outreach": run.outreach_json,
        "call_log": run.call_log_json or [],
        "artifacts": [
            {"stage": a.stage, "filename": a.filename,
             "content_type": a.content_type, "bytes": a.bytes}
            for a in run.artifacts
        ],
        "checkpoints": [
            {"stage": c.stage,
             "at": c.created_at.isoformat() if c.created_at else ""}
            for c in sorted(run.checkpoints, key=lambda c: c.created_at or 0)
        ],
        "may_send": may_send(run.status),
        "can_download": artifacts_available(run.status),
    }


def file(session: Session, run: Run, stage: str) -> Artifact | None:
    """A rendered file, if this run's state allows downloading it.

    `rejected` allows it. Rejecting means "do not send this on my behalf", not
    "destroy the work" — the likeliest real rejection is a good resume with a
    clumsy covering letter.
    """
    from app.domain.status import artifacts_available

    if not artifacts_available(run.status):
        return None
    return next((a for a in run.artifacts if a.stage == stage), None)


def stages(run: Run) -> list[str]:
    return sorted({c.stage for c in run.checkpoints})


def stage_payload(run: Run, stage: str) -> Any:
    """One stage's output, for the inspector.

    Every stage is served, including the ones the UI has no special view for:
    a stage the API hides is a stage nobody can check, and "shows its work" has
    to be literally true.
    """
    from app.io.dblog import STAGE_COLUMNS

    if column := STAGE_COLUMNS.get(stage):
        return getattr(run, column)
    if stage == "validation":
        return {"accepted": run.accepted_json or [],
                "rejected": run.rejected_json or []}
    if stage == "proof":
        return run.proof_json
    checkpoint = next((c for c in run.checkpoints if c.stage == stage), None)
    return checkpoint.state_json if checkpoint else None
