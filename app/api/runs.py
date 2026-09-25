"""Runs over HTTP — start one, watch it, read it, download from it.

**These read the database, not the run folders.** They used to read folders,
which was right while there was no database and wrong the moment there was one:
two live sources for the same list is the bug class this project has an
invariant against. `scripts/seed.py` imports the folders into rows once, and
everything downstream reads rows.

**`POST` answers 202 and does not wait.** A run takes minutes — measured at 466
seconds — and no HTTP request survives that behind any proxy worth using. The
work goes to a background task and progress arrives on the SSE stream.

**The stream reads checkpoints, not an in-process queue.** A client that
reconnects sees every stage that finished, not every stage that finished while
it happened to be connected. That is the same property that makes a killed run
recoverable, and it is why `DbRunLog` commits per stage.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_session, owned_resume, owned_run
from app.api.limits import RUN, limiter
from app.config import Settings, check
from app.db.models import Run, User
from app.db.session import session_factory
from app.domain.errors import UserError
from app.io.cache import FileCache
from app.io.jobsource import load_job
from app.io.llm import BudgetedClient, RunBudget, build_client
from app.pipeline import runs as service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/runs", tags=["runs"])

# A stage takes tens of seconds, so a second is responsive without being a busy
# loop; a run that has produced nothing for half an hour is not going to.
POLL_SECONDS = 1.0
STREAM_TIMEOUT_SECONDS = 1800


# A generous ceiling on a pasted posting. The longest real one measured here is
# around 9,000 characters; 200,000 is room for a saved HTML page with its markup
# still attached. Without a bound this field is an unauthenticated-sized memory
# allocation from anyone who has signed in.
MAX_JOB_CHARS = 200_000


class RunIn(BaseModel):
    resume_id: str
    job: str = Field(
        max_length=MAX_JOB_CHARS,
        description="The posting, as pasted text or saved HTML. NOT a path: "
                    "this endpoint does not read files from the server.")
    hints: bool = Field(default=False, description="S2b — one extra call.")
    outreach: bool = Field(default=True, description="S8 — one extra call.")


@router.get("")
def list_runs(limit: int = Query(50, ge=1, le=200),
              session: Session = Depends(get_session),
              user: User = Depends(current_user)) -> list[dict]:
    rows = (session.query(Run)
            .filter(Run.user_id == user.id)
            .order_by(Run.created_at.desc())
            .limit(limit).all())
    return [service.summary(row) for row in rows]


@router.post("", status_code=202)
def start(payload: RunIn, request: Request,
          session: Session = Depends(get_session),
          user: User = Depends(current_user)) -> dict:
    """Create the run, hand the work off, answer at once."""
    if getattr(request.app.state, "read_only", False):
        raise HTTPException(
            403,
            "This instance serves stored runs only. Run JobTailor locally to "
            "tailor a new resume — it needs a model on the same machine.",
        )
    settings = Settings()
    if problems := check(settings):
        raise HTTPException(503, "No model configured: " + "; ".join(problems))

    limiter.take(user.id, RUN)
    try:
        resume = owned_resume(payload.resume_id, session, user)
        jd_text = load_job(payload.job)
        run = service.create(session, user, resume, jd_text)
    except UserError as exc:
        # Nothing was spent, so the allowance is not spent either.
        limiter.give_back(user.id, RUN)
        raise HTTPException(400, str(exc)) from exc
    except HTTPException:
        limiter.give_back(user.id, RUN)
        raise

    run_id = run.id
    session.commit()

    budget = RunBudget(max_calls=settings.max_llm_calls_per_run,
                       max_tokens=settings.max_tokens_per_run)
    client = BudgetedClient(build_client(settings), budget)
    smart = client
    if settings.has_smart_model:
        smart = BudgetedClient(build_client(settings, role="smart"), budget)

    # `to_thread` because the pipeline is synchronous and spends most of its
    # time blocked on a model. Inline, it would stop the server answering the
    # very requests the client uses to watch it.
    asyncio.get_running_loop().run_in_executor(
        None, _execute, run_id, client, smart, payload)

    return {"run_id": run_id, "events": f"/api/runs/{run_id}/events"}


def _execute(run_id: str, client, smart, payload: RunIn) -> None:
    service.execute(
        session_factory(), run_id, client, smart_client=smart,
        cache=FileCache(), semantic_hints=payload.hints,
        write_outreach=payload.outreach,
    )


@router.get("/{run_id}")
def get_run(run_id: str,
            session: Session = Depends(get_session),
            user: User = Depends(current_user)) -> dict:
    return service.detail(session, owned_run(run_id, session, user))


@router.get("/{run_id}/stages")
def list_stages(run_id: str,
                session: Session = Depends(get_session),
                user: User = Depends(current_user)) -> list[str]:
    return service.stages(owned_run(run_id, session, user))


@router.get("/{run_id}/stages/{stage}")
def get_stage(run_id: str, stage: str,
              session: Session = Depends(get_session),
              user: User = Depends(current_user)) -> Any:
    """One stage's output, raw. Every stage, including the unglamorous ones."""
    run = owned_run(run_id, session, user)
    payload = service.stage_payload(run, stage)
    if payload is None:
        raise HTTPException(404, f"run {run_id} has nothing for stage {stage!r}")
    return payload


@router.get("/{run_id}/file/{stage}")
def get_file(run_id: str, stage: str,
             session: Session = Depends(get_session),
             user: User = Depends(current_user)) -> Response:
    """The .docx, the .pdf, or the text an ATS would see.

    Available in `rejected` too — rejecting blocks sending, it does not destroy
    the work.
    """
    run = owned_run(run_id, session, user)
    artifact = service.file(session, run, stage)
    if artifact is None:
        raise HTTPException(
            404,
            f"no {stage!r} on this run"
            if run.status not in ("created", "tailoring") else
            f"this run is still {run.status} — nothing rendered yet",
        )
    return Response(
        content=artifact.blob, media_type=artifact.content_type,
        headers={"Content-Disposition":
                 f'attachment; filename="{artifact.filename}"'},
    )


@router.get("/{run_id}/events")
async def events(run_id: str,
                 session: Session = Depends(get_session),
                 user: User = Depends(current_user)) -> StreamingResponse:
    owned_run(run_id, session, user)          # 404 before opening a stream
    return StreamingResponse(
        _stage_events(run_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _stage_events(run_id: str) -> AsyncIterator[str]:
    """One message per completed stage, then a final result.

    Polls the checkpoint table rather than subscribing to anything in this
    process: the run may be executing in a different worker, and a client that
    reconnects must see what it missed.
    """
    seen: set[str] = set()
    waited = 0.0
    factory = session_factory()

    while waited < STREAM_TIMEOUT_SECONDS:
        with factory() as session:
            run = session.get(Run, run_id)
            if run is None:
                yield _sse("error", {"detail": f"no run {run_id}"})
                return
            for checkpoint in sorted(run.checkpoints,
                                     key=lambda c: c.created_at or 0):
                if checkpoint.stage in seen:
                    continue
                seen.add(checkpoint.stage)
                yield _sse("stage", {"stage": checkpoint.stage,
                                     "state": checkpoint.state_json or {}})
            status, error = run.status, run.error
            done = status in ("needs_review", "failed", "approved",
                              "rejected", "sent")
            payload = service.summary(run) if done else None

        if done:
            yield _sse("done" if status != "failed" else "error",
                       payload or {"detail": error})
            return

        await asyncio.sleep(POLL_SECONDS)
        waited += POLL_SECONDS

    yield _sse("error", {"detail": "the run produced nothing for 30 minutes"})


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
