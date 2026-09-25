"""S0.4 over HTTP — upload, look at the parse, fix it, confirm it.

Four endpoints, and the interesting one is `PUT /draft`, which is the only
write in this system that is guaranteed to cost nothing: it re-derives the
document from the user's correction without going near the parser. See
`pipeline.resumes` for why re-parsing an edit would both cost three model calls
and discard the edit.

`POST /` does the reading inline rather than in the background. A parse is
seconds to a couple of minutes — slower than a request should be, faster than a
run — so this is the one place where the honest options are both bad and the
lesser one is a slow request. The alternative is a second progress stream for a
step with three sub-steps, which is more machinery than the wait deserves; if
parse latency grows this becomes a `202` like `POST /runs`.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_session, owned_resume
from app.api.limits import UPLOAD, limiter
from app.config import Settings, check
from app.db.models import Resume, User
from app.domain.errors import (
    IllegalTransition, JobTailorError, ModelOutputError, UserError,
)
from app.domain.models import RawResume
from app.io.cache import FileCache
from app.io.llm import BudgetedClient, RunBudget, build_client
from app.pipeline import resumes as service
from app.pipeline.resumes import MAX_RESUME_BYTES

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/resumes", tags=["resumes"])


class DraftIn(BaseModel):
    """A corrected parse, plus the revision it was written from.

    `revision` is not optional by accident. Two tabs editing the same parse
    would otherwise silently lose one set of corrections, and the person who
    lost them would have no way to know.
    """
    raw: RawResume
    revision: int = Field(description="The revision this draft was edited from.")


def _client():
    """The model client for a parse, or a clear refusal.

    Checked rather than attempted: the read-only deployment has no model
    configured, and "the parse failed" is a much worse message than "this
    instance cannot parse".
    """
    settings = Settings()
    if problems := check(settings):
        raise HTTPException(
            503,
            "This instance has no model configured, so it cannot read a "
            "resume: " + "; ".join(problems),
        )
    return BudgetedClient(
        build_client(settings),
        RunBudget(max_calls=settings.max_llm_calls_per_run,
                  max_tokens=settings.max_tokens_per_run),
    )


@router.get("")
def list_resumes(session: Session = Depends(get_session),
                 user: User = Depends(current_user)) -> list[dict]:
    """Newest first. Superseded versions included — a run may still point at one."""
    rows = (
        session.query(Resume)
        .filter(Resume.user_id == user.id)
        .order_by(Resume.created_at.desc())
        .all()
    )
    return [service.summary(row) for row in rows]


UPLOAD_CHUNK = 1024 * 1024


async def _read_capped(file: UploadFile) -> bytes:
    """Read the body, but stop at the limit instead of after it.

    `await file.read()` with no argument pulls the whole request into memory
    first and lets `resumes.create` reject it afterwards — which is a check that
    runs one step too late. A 2 GB upload is then 2 GB of RAM in a container
    that has far less, and the process dies before it can refuse anything. On a
    free host with one worker that is the whole service, from one request, with
    no authentication beyond having signed in.

    Reading in chunks and stopping one byte over the limit costs nothing and
    turns a crash into a 413.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(UPLOAD_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESUME_BYTES:
            raise HTTPException(
                413,
                f"That file is larger than the {MAX_RESUME_BYTES // (1024 * 1024)} MB "
                f"limit. A resume is a few hundred kilobytes; something else is "
                f"going on with this file.",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("", status_code=201)
async def upload(file: UploadFile = File(...),
                 session: Session = Depends(get_session),
                 user: User = Depends(current_user)) -> dict:
    """S0.1–S0.3. Leaves the resume in `needs_confirm`."""
    # Before the body is read, so an oversized upload cannot be used to spend
    # memory repeatedly for free.
    limiter.take(user.id, UPLOAD)
    try:
        data = await _read_capped(file)
    except HTTPException:
        limiter.give_back(user.id, UPLOAD)
        raise
    try:
        resume = service.create(session, user.id, file.filename or "resume", data)
    except UserError as exc:
        raise HTTPException(400, str(exc)) from exc

    try:
        service.read(session, resume, data, _client(), cache=FileCache())
    except ModelOutputError as exc:
        # The row survives in `failed` with the reason on it, so the UI can
        # offer a retry that re-parses from the stored text rather than making
        # the user find the file again.
        session.flush()
        raise HTTPException(
            502,
            f"The model could not structure this resume: {exc}. "
            f"You can retry, or upload a different file.",
        ) from exc
    except JobTailorError as exc:
        session.flush()
        raise HTTPException(400, str(exc)) from exc

    return _detail(resume)


@router.get("/{resume_id}")
def get_resume(resume_id: str,
               session: Session = Depends(get_session),
               user: User = Depends(current_user)) -> dict:
    return _detail(owned_resume(resume_id, session, user))


@router.put("/{resume_id}/draft")
def put_draft(resume_id: str, payload: DraftIn,
              session: Session = Depends(get_session),
              user: User = Depends(current_user)) -> dict:
    """The user's correction. Costs nothing — the parser is not involved."""
    resume = owned_resume(resume_id, session, user)
    try:
        service.apply_draft(session, resume, payload.raw, payload.revision)
    except service.StaleEdit as exc:
        raise HTTPException(409, str(exc)) from exc
    except IllegalTransition as exc:
        raise HTTPException(409, str(exc)) from exc
    return _detail(resume)


@router.post("/{resume_id}/reparse")
def reparse(resume_id: str,
            session: Session = Depends(get_session),
            user: User = Depends(current_user)) -> dict:
    """Ask the model again, after a failure. Costs model calls — hence a
    separate endpoint from `PUT /draft`, which never does."""
    resume = owned_resume(resume_id, session, user)
    try:
        service.reparse(session, resume, _client())
    except JobTailorError as exc:
        session.flush()
        raise HTTPException(502, str(exc)) from exc
    return _detail(resume)


@router.post("/{resume_id}/confirm")
def confirm(resume_id: str,
            session: Session = Depends(get_session),
            user: User = Depends(current_user)) -> dict:
    """★ S0.4. Ids are frozen from here.

    Any previously confirmed version of the same file is superseded, so exactly
    one version of a resume is current while older runs keep pointing at the
    version they actually used.
    """
    resume = owned_resume(resume_id, session, user)
    try:
        service.confirm(session, resume)
    except UserError as exc:
        raise HTTPException(400, str(exc)) from exc
    except IllegalTransition as exc:
        raise HTTPException(409, str(exc)) from exc

    superseded = (
        session.query(Resume)
        .filter(Resume.user_id == user.id,
                Resume.file_sha256 == resume.file_sha256,
                Resume.status == "confirmed",
                Resume.id != resume.id)
        .all()
    )
    for older in superseded:
        service.supersede(session, older)

    body = _detail(resume)
    body["superseded"] = [row.id for row in superseded]
    return body


def _detail(resume: Resume) -> dict:
    """Summary plus the editable draft.

    The draft is the whole `RawResume` because that is what `PUT /draft` takes
    back — a form that renders one shape and submits another is a form with a
    translation bug waiting in it.
    """
    body = service.summary(resume)
    draft = service.draft(resume)
    body["raw"] = draft.model_dump() if draft else None
    body["doc"] = resume.doc_json
    return body
