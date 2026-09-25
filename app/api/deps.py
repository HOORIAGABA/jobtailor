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
from sqlalchemy.exc import IntegrityError
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
        demo = _demo_user(request, session)
        if demo is not None:
            request.state.user_email = demo.email
            return demo

        raise HTTPException(
            401,
            "Not signed in. Sign in at /api/auth/google/start, or set "
            f"{DEV_EMAIL_ENV} to work locally without Google (development "
            f"only — it is ignored when ENVIRONMENT=production).",
        )

    user = _get_or_create_dev_user(session, email)
    request.state.user_email = user.email
    return user


def _demo_user(request: Request, session: Session) -> User | None:
    """The seeded demo account, on a read-only instance only.

    A public demo has a problem the local app does not: every run belongs to a
    user, and a visitor who has not signed in is nobody. Without this they see
    an empty list and conclude the product does nothing — which is a poor
    advertisement for a product whose whole pitch is that it shows its work.

    **The read-only check is what stops this being `DEV_USER_EMAIL` wearing a
    disguise.** On a writable instance it resolves nothing at all, so it can
    never be the thing that lets a stranger upload a resume, approve a draft or
    press send. On a read-only instance the middleware in `api.main` refuses
    every unsafe method anyway, so the worst an anonymous visitor can do with
    this identity is read the runs it was seeded with — which is the entire
    point of it.

    It also never creates anything. If the account has not been seeded, there is
    no demo and the caller gets its 401.
    """
    import os

    from app.api.main import DEMO_EMAIL_ENV

    if not getattr(request.app.state, "read_only", False):
        return None
    email = (os.environ.get(DEMO_EMAIL_ENV) or "").strip()
    if not email:
        return None
    return session.query(User).filter(User.email == email).first()


def _get_or_create_dev_user(session: Session, email: str) -> User:
    """Find the development user, creating it once across concurrent requests.

    **Committed, not merely flushed.** Identity is not part of the work the
    request is doing, so it must survive that work failing — and a flush alone
    does not: any request that raises rolls the session back, the row vanishes,
    and the NEXT request creates the user again with a NEW id. Anything keyed on
    the user id then sees a different person every time, which quietly defeated
    the rate limiter on exactly the endpoints most likely to fail.

    **And the insert races.** One page load fires several requests at once —
    `/api/auth/me`, `/api/capabilities`, the run — and on a cold database they
    all find no user, all insert, and all but one get

        IntegrityError: UNIQUE constraint failed: users.google_sub

    which surfaces as a 500 on whichever request lost. Found by loading the demo
    in a browser; a test that makes one request at a time can never see it.

    The unique constraint is doing its job, so the fix is to treat losing the
    race as the ordinary outcome it is: roll back and read the row the winner
    wrote.
    """
    user = session.query(User).filter(User.google_sub == DEV_SUB).one_or_none()
    if user is not None:
        return user

    session.add(User(google_sub=DEV_SUB, email=email,
                     name=email.split("@")[0]))
    try:
        session.commit()
        logger.info("Created the local development user %s", email)
    except IntegrityError:
        session.rollback()
        logger.debug("Lost the race to create the development user; reading it")

    found = session.query(User).filter(User.google_sub == DEV_SUB).one_or_none()
    if found is None:                                     # pragma: no cover
        raise HTTPException(500, "could not resolve the development user")
    return found


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
