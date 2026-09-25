"""★ S9 over HTTP — preview, then approve or reject.

Two endpoints, and the pair is the point: the preview issues a token over the
message it is showing, and the decision has to carry that token back along with
whatever the person actually approved. See `engine.confirm` for why a race
between those two steps is the thing being defended against.

The secret comes from settings and is refused rather than defaulted — a shared
default signing key is a signature anyone can forge, and the failure would be
silent.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_session, owned_run
from app.config import Settings
from app.db.models import User
from app.domain.errors import IllegalTransition, UserError
from app.pipeline import gate as service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/runs", tags=["gate"])


class DecisionIn(BaseModel):
    """The person's answer, and the message it is an answer about.

    The recipient, subject and body are sent back rather than read from the row
    on purpose. Two reads of a mutable row separated by a network round trip are
    only *probably* the same value; passed as parameters and covered by the
    token, they are provably the ones that were approved. That is invariant I6.
    """
    decision: str = Field(description='"approve" or "reject".')
    recipient: str = ""
    subject: str = ""
    body: str = ""
    confirm_token: str = Field(
        default="", description="From the preview. Required to approve.")


def _secret() -> str:
    secret = Settings().confirm_token_secret
    if not secret:
        raise HTTPException(
            503,
            "CONFIRM_TOKEN_SECRET is not set, so an approval cannot be signed. "
            "Set it to any long random string — it binds a decision to the "
            "message that was shown.",
        )
    return secret


@router.get("/{run_id}/preview")
def preview(run_id: str,
            session: Session = Depends(get_session),
            user: User = Depends(current_user)) -> dict:
    """The diff, the gaps, the draft — and the token that binds a decision."""
    run = owned_run(run_id, session, user)
    try:
        return service.preview(session, run, _secret())
    except IllegalTransition as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{run_id}/decision")
def decide(run_id: str, payload: DecisionIn,
           session: Session = Depends(get_session),
           user: User = Depends(current_user)) -> dict:
    """Approve or reject. The only way out of `needs_review`.

    A rejection needs no token: refusing to send is not the dangerous
    direction, and requiring a signature to say no would let a stale preview
    trap a run with no way to close it.
    """
    run = owned_run(run_id, session, user)
    secret = _secret() if payload.decision == service.APPROVE else ""
    try:
        return service.decide(
            session, run, decision=payload.decision, secret=secret,
            recipient=payload.recipient, subject=payload.subject,
            body=payload.body, confirm_token=payload.confirm_token,
        )
    except service.StaleDecision as exc:
        raise HTTPException(409, str(exc)) from exc
    except IllegalTransition as exc:
        raise HTTPException(409, str(exc)) from exc
    except UserError as exc:
        raise HTTPException(400, str(exc)) from exc
