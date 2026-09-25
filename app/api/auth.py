"""Sign in with Google, connect Gmail, sign out, disconnect.

Five endpoints:

    GET    /api/auth/google/start      redirect to Google's consent screen
    GET    /api/auth/google/callback   Google returns here; sets the cookie
    GET    /api/auth/me                who am I, and is Gmail connected
    POST   /api/auth/logout            clear the cookie
    DELETE /api/auth/google            revoke at Google, then delete the row

**`state` and the PKCE verifier live in short-lived HttpOnly cookies.** They have
to survive a round trip through Google and be unreadable by page scripts, and
they are single-use per flow — which is a cookie, not a database table. Comparing
the `state` in the callback against the one in the cookie is what makes a
forged callback fail: without it, an attacker can complete a login in the
victim's browser and leave them signed into the attacker's account.

**The session cookie is HttpOnly, SameSite=Lax, and Secure in production.**
HttpOnly so a script cannot read it; Lax because the callback is a cross-site
top-level GET and `Strict` would drop the cookie on exactly that navigation;
Secure off on localhost only, where there is no TLS and a Secure cookie would
simply never be stored.

**No endpoint here returns a token.** Not the access token, not the refresh
token, not the ID token. `/api/auth/me` answers with booleans and scope names.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import SESSION_COOKIE, current_user, get_session
from app.config import Settings
from app.db.models import User
from app.engine import authsession
from app.io import google
from app.pipeline import auth as service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

STATE_COOKIE = "jt_oauth_state"
VERIFIER_COOKIE = "jt_oauth_verifier"
FLOW_COOKIE = "jt_oauth_flow"
# Long enough to read a consent screen, short enough that an abandoned flow does
# not leave a usable value in the browser for the rest of the day.
FLOW_TTL_SECONDS = 600


def _settings() -> Settings:
    settings = Settings()
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(
            503,
            "Google sign-in is not configured. Create an OAuth client (Google "
            "Cloud console -> APIs & Services -> Credentials -> Create "
            "credentials -> OAuth client ID -> Web application), add "
            f"{settings.google_redirect_uri} as an authorised redirect URI, and "
            "put GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env.",
        )
    return settings


def _fernet_key(settings: Settings) -> str:
    if not settings.fernet_key:
        raise HTTPException(
            503,
            "FERNET_KEY is not set, so a refresh token cannot be stored "
            "encrypted — and it will not be stored any other way. Generate one "
            "with `python -m scripts.keys`.",
        )
    return settings.fernet_key


def _set_flow_cookies(response: Response, challenge: google.Challenge,
                      *, connect: bool, secure: bool) -> None:
    for name, value in ((STATE_COOKIE, challenge.state),
                        (VERIFIER_COOKIE, challenge.verifier),
                        (FLOW_COOKIE, "connect" if connect else "signin")):
        response.set_cookie(name, value, max_age=FLOW_TTL_SECONDS,
                            httponly=True, samesite="lax", secure=secure,
                            path="/api/auth")


def _clear_flow_cookies(response: Response) -> None:
    for name in (STATE_COOKIE, VERIFIER_COOKIE, FLOW_COOKIE):
        response.delete_cookie(name, path="/api/auth")


@router.get("/google/start")
def start(request: Request, connect: bool = False) -> RedirectResponse:
    """Send the browser to Google.

    `connect=true` is the second flow — the same client plus `gmail.send`,
    requested at the approval step rather than at the front door.
    """
    settings = _settings()
    if connect:
        _fernet_key(settings)          # refuse before the round trip, not after

    hint = ""
    if connect:
        # Pre-fills the account chooser with the address already signed in, so
        # the person does not connect Gmail on a different Google account than
        # the one holding their runs — which would look like nothing happened.
        try:
            with_session = request.state.user_email
            hint = with_session or ""
        except AttributeError:
            hint = ""

    url, challenge = service.start(
        client_id=settings.google_client_id,
        redirect_uri=settings.google_redirect_uri,
        connect=connect, login_hint=hint)

    response = RedirectResponse(url, status_code=307)
    _set_flow_cookies(response, challenge, connect=connect,
                      secure=settings.secure_cookies)
    return response


@router.get("/google/callback")
def callback(request: Request, code: str = "", state: str = "",
             error: str = "",
             session: Session = Depends(get_session)) -> RedirectResponse:
    """Google returns here. Sets the session cookie and lands on the UI.

    Errors redirect with a query parameter rather than rendering JSON: this URL
    is reached by a browser navigation, and a person who declined consent should
    see the application again, not a stack of braces.
    """
    settings = _settings()

    if error:
        # `access_denied` is the person pressing "cancel", which is not a fault.
        logger.info("Google returned an error at the callback: %s", error)
        return _back(settings, f"?auth_error={error}")

    expected_state = request.cookies.get(STATE_COOKIE) or ""
    verifier = request.cookies.get(VERIFIER_COOKIE) or ""
    if not code or not state or not expected_state or state != expected_state:
        # One message for a missing code, a missing cookie and a mismatched
        # state: they are all "this callback did not come from a flow this
        # server started", and distinguishing them tells an attacker which half
        # they got right.
        logger.warning("Refused a callback whose state did not match")
        return _back(settings, "?auth_error=bad_state")

    try:
        user, connected = service.callback(
            session,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            redirect_uri=settings.google_redirect_uri,
            code=code, verifier=verifier,
            fernet_key=settings.fernet_key)
    except google.GoogleError as exc:
        logger.warning("Google sign-in failed: %s", exc)
        return _back(settings, "?auth_error=exchange_failed")

    was_connecting = (request.cookies.get(FLOW_COOKIE) or "") == "connect"
    landing = "?gmail=connected" if connected else (
        "?gmail=declined" if was_connecting else "?signed_in=1")

    response = _back(settings, landing)
    response.set_cookie(
        SESSION_COOKIE,
        authsession.issue(settings.jwt_secret, user.id),
        max_age=authsession.DEFAULT_TTL_SECONDS,
        httponly=True, samesite="lax", secure=settings.secure_cookies, path="/")
    _clear_flow_cookies(response)
    logger.info("Signed in %s (gmail connected: %s)", user.email, connected)
    return response


@router.get("/me")
def me(session: Session = Depends(get_session),
       user: User = Depends(current_user)) -> dict:
    """Who is signed in, and what they can do. No tokens, ever."""
    return service.describe(session, user)


@router.post("/logout")
def logout(response: Response) -> dict:
    """Clear the cookie. Does not touch the Gmail grant.

    Signing out and disconnecting Gmail are different intentions, and collapsing
    them would mean every sign-out costs the person a consent screen next time.
    """
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"signed_out": True}


@router.delete("/google")
def disconnect(session: Session = Depends(get_session),
               user: User = Depends(current_user)) -> dict:
    """Revoke the Gmail grant at Google, then delete the stored tokens."""
    settings = Settings()
    removed = service.disconnect(session, user,
                                 fernet_key=settings.fernet_key)
    return {"disconnected": removed, "gmail_connected": False}


def _back(settings: Settings, query: str) -> RedirectResponse:
    return RedirectResponse(f"{settings.frontend_url.rstrip('/')}/{query}",
                            status_code=303)
