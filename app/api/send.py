"""S11 over HTTP — the last door, and the only one that opens outward.

Two endpoints:

    GET  /api/runs/{id}/eml   the exact bytes that would go out
    POST /api/runs/{id}/send  dispatch, once

**`/eml` exists because "what will the recruiter actually receive" deserves a
real answer.** Not a rendering of the draft in a web page with the site's own
fonts — the RFC-5322 message, headers and attachments included, openable in a
mail client. It is also the honest way to demonstrate the product to someone
without emailing them.

**`/send` takes the message as parameters, exactly as `/decision` does.** The
token is recomputed over what is about to be sent rather than over a row that is
read a second time. See `pipeline.send` for why the two are not the same thing.

The sender comes from `MAIL_PROVIDER` and defaults to the one that cannot send.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_session, owned_run
from app.api.limits import SEND, limiter
from app.config import Settings
from app.db.models import User
from app.domain.errors import IllegalTransition, UserError
from app.io.google import GoogleError
from app.io.mail import (
    SendFailed,
    UnknownSender,
    needs_token,
    sender_for,
)
from app.pipeline import auth, gate, send as service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/runs", tags=["send"])


class SendIn(BaseModel):
    """What to send, and the proof it is what was approved."""
    recipient: str = ""
    subject: str = ""
    body: str = ""
    confirm_token: str = Field(
        default="",
        description="The token from the preview that was approved.")


def _secret() -> str:
    secret = Settings().confirm_token_secret
    if not secret:
        raise HTTPException(
            503,
            "CONFIRM_TOKEN_SECRET is not set, so a send cannot be checked "
            "against the approval. Set it to any long random string.",
        )
    return secret


def _sender(session: Session, user: User):
    """The backend this instance sends with, with a live token if it needs one.

    The token is minted here rather than inside the sender because refreshing it
    is a database write, and `io` does not touch the database. `needs_token`
    keeps the condition out of this function's body, so a third backend is a line
    in `io.mail`, not an `elif` here.
    """
    settings = Settings()
    provider = settings.mail_provider
    token = ""
    if needs_token(provider):
        try:
            token = auth.access_token(
                session, user,
                client_id=settings.google_client_id,
                client_secret=settings.google_client_secret,
                fernet_key=settings.fernet_key)
        except auth.NotConnected as exc:
            # 428: the request is well-formed and the person has to do something
            # first — connect Gmail. A 400 would read as "your request was
            # wrong", and a 401 as "sign in again", and both would send the UI
            # to the wrong screen.
            raise HTTPException(428, str(exc)) from exc
        except GoogleError as exc:
            raise HTTPException(
                502, f"{exc} Reconnect Gmail at /api/auth/google/start"
                     f"?connect=true") from exc
    try:
        return sender_for(provider, access_token=token)
    except UnknownSender as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/{run_id}/approved")
def approved(run_id: str,
             session: Session = Depends(get_session),
             user: User = Depends(current_user)) -> dict:
    """The approved message and a token for sending it.

    This is what makes the send survive a page reload: the browser does not have
    to hold the token from the approval, and a run left in `approved` with the
    tab closed is not stranded. See `pipeline.send.approved` for why reissuing
    here still admits exactly one message.
    """
    run = owned_run(run_id, session, user)
    try:
        return service.approved(session, run, secret=_secret())
    except service.AlreadySent as exc:
        raise HTTPException(409, str(exc)) from exc
    except IllegalTransition as exc:
        raise HTTPException(409, str(exc)) from exc
    except UserError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/{run_id}/eml")
def eml(run_id: str,
        session: Session = Depends(get_session),
        user: User = Depends(current_user)) -> Response:
    """The approved message as a `.eml` file. Sends nothing."""
    run = owned_run(run_id, session, user)
    message = gate.message_of(session, run)
    if message is None:
        raise HTTPException(
            409, "nothing has been approved on this run, so there is no "
                 "message to preview")
    raw = service.preview_eml(
        session, run, user, recipient=message.recipient,
        subject=message.subject, body=message.body)
    return Response(
        content=raw, media_type="message/rfc822",
        headers={"Content-Disposition":
                 f'attachment; filename="{run.id}.eml"'},
    )


@router.post("/{run_id}/send")
def dispatch(run_id: str, payload: SendIn,
             session: Session = Depends(get_session),
             user: User = Depends(current_user)) -> dict:
    """Send the approved message. Twice is a 409, not a second email."""
    run = owned_run(run_id, session, user)
    limiter.take(user.id, SEND)
    try:
        return service.send(
            session, run, user, _sender(session, user),
            secret=_secret(),
            recipient=payload.recipient, subject=payload.subject,
            body=payload.body, confirm_token=payload.confirm_token,
        )
    except service.AlreadySent as exc:
        # None of these refusals dispatched anything, so none of them should
        # cost the person a send from their hourly allowance.
        limiter.give_back(user.id, SEND)
        raise HTTPException(409, str(exc)) from exc
    except IllegalTransition as exc:
        limiter.give_back(user.id, SEND)
        raise HTTPException(409, str(exc)) from exc
    except UserError as exc:
        limiter.give_back(user.id, SEND)
        raise HTTPException(400, str(exc)) from exc
    except SendFailed as exc:
        # 502, not 500: this instance behaved correctly and the provider did
        # not. The distinction matters to whoever reads the logs at 2am.
        raise HTTPException(502, str(exc)) from exc
