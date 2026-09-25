"""Shared request dependencies.

`current_user` resolves a signed session cookie, and falls back to a local
development identity **only when `DEV_USER_EMAIL` is set**. That fallback is
deliberately loud rather than convenient: the read-only public deployment never
sets it, so the public instance has no writable identity at all, and a default
that silently worked would be an authentication bypass with a comment promising
to fix it.

The order is cookie first, environment second. The other way round would mean a
developer with `DEV_USER_EMAIL` still set could not test as a real signed-in
user, and would not notice — every request would quietly be someone else.
"""
from __future__ import annotations

import logging
import os
from typing import Iterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import DEV_EMAIL_ENV, Settings
from app.db.models import Resume, Run, User
from app.db.session import session_scope
from app.engine import authsession

logger = logging.getLogger(__name__)

SESSION_COOKIE = "jt_session"
DEV_SUB = "dev-local"

# Re-exported: callers have always imported it from here, and `config` is where
# it now lives so that `config.check` can see it without importing this module.
__all__ = ["SESSION_COOKIE", "DEV_EMAIL_ENV", "DEV_SUB", "get_session",
           "current_user", "signed_in_user", "owned_resume", "owned_run"]


def get_session() -> Iterator[Session]:
    with session_scope() as session:
        yield session


def signed_in_user(request: Request, session: Session) -> User | None:
    """The user the session cookie attests to, or None. Never raises.

    A cookie that is absent, expired, forged or points at a deleted row all mean
    the same thing here — nobody is signed in — and returning None for all four
    is what lets the development fallback below stay a fallback.
    """
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    try:
        user_id = authsession.read(Settings().jwt_secret, cookie)
    except authsession.BadCookie as exc:
        logger.info("Ignoring a session cookie: %s", exc)
        return None
    return session.get(User, user_id)


def current_user(request: Request,
                 session: Session = Depends(get_session)) -> User:
    """The signed-in user, or 401."""
    user = signed_in_user(request, session)
    if user is not None:
        # Stashed so `/api/auth/google/start?connect=true` can pre-fill the
        # account chooser with the address that is already signed in.
        request.state.user_email = user.email
        return user

    email = (os.environ.get(DEV_EMAIL_ENV) or "").strip()

    # ★ The development identity is an authentication bypass, and calling it
    # that is the point. With it set, every unauthenticated request becomes one
    # named user — which is exactly what makes local work possible and exactly
    # what must never survive a deploy. Refusing it in production means the
    # variable can be left in a `.env` that gets copied to a server without
    # silently opening the door; the instance answers 401 instead.
    #
    # Checked here rather than at startup because the environment can change
    # under a running process, and because a bypass that is only checked once
    # is a bypass that a config reload can re-enable.
    if email and Settings().is_production:
        logger.error(
            "%s is set on a production instance and is being ignored. It would "
            "let any unauthenticated request act as %s.", DEV_EMAIL_ENV, email)
        email = ""

    if not email:
        raise HTTPException(
            401,
            "Not signed in. Sign in at /api/auth/google/start, or set "
            f"{DEV_EMAIL_ENV} to work locally without Google (development "
            f"only — it is ignored when ENVIRONMENT=production).",
        )

    user = session.query(User).filter(User.google_sub == DEV_SUB).one_or_none()
    if user is None:
        user = User(google_sub=DEV_SUB, email=email, name=email.split("@")[0])
        session.add(user)
        # Committed here, not merely flushed. Identity is not part of the work
        # the request is doing, so it must survive that work failing — and a
        # flush alone does not: any request that raises rolls the session back,
        # the row vanishes, and the NEXT request creates the user again with a
        # NEW id. Anything keyed on the user id then sees a different person
        # every time, which quietly defeated the rate limiter on exactly the
        # endpoints most likely to fail.
        session.commit()
        logger.info("Created the local development user %s", email)
    request.state.user_email = user.email
    return user


def owned_resume(resume_id: str, session: Session, user: User) -> Resume:
    """Fetch a resume, or 404 — never 403.

    A 403 confirms the row exists, which tells an enumerator that they found
    something. Ids are unguessable, but an unguessable id is not authorisation,
    so ownership is checked on every read and the failure is indistinguishable
    from absence.
    """
    resume = session.get(Resume, resume_id)
    if resume is None or resume.user_id != user.id:
        raise HTTPException(404, f"no resume {resume_id}")
    return resume


def owned_run(run_id: str, session: Session, user: User) -> Run:
    run = session.get(Run, run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(404, f"no run {run_id}")
    return run
