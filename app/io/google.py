"""Google OAuth, over HTTPS, with the scope decisions written down.

**One scope at sign-in: `openid email profile`.** Nothing else, ever. Asking for
`gmail.send` on the consent screen a person sees before they have uploaded
anything is how an application gets refused at the first screen, and it is also
dishonest — at that moment nothing is being sent. `gmail.send` is requested
later, at the approval step, through incremental authorisation (S9 → S11).

**`gmail.send` and nothing adjacent.** Google classifies `gmail.send` as
*sensitive*; every other Gmail scope — `gmail.compose`, `gmail.readonly`,
`gmail.modify`, `mail.google.com` — is *restricted*, and a single restricted
scope reclassifies the whole application: a CASA security assessment, roughly six
weeks of review, and an annual renewal. `gmail.compose` looks like the natural
choice for "write an email" and is the expensive trap, because it can also read
drafts. So the send scope is the only one, and adding a second Gmail scope is a
project-level decision rather than a line of code.

**The ID token is decoded, not verified.** It arrives in the body of a response
to a request this process made directly to `oauth2.googleapis.com` over TLS with
the client secret. Google's own documentation says verification is unnecessary in
exactly this case, and the alternative — fetching JWKS, caching it, and verifying
RS256 — is more code, a network dependency in the login path, and a signature
library reading the algorithm out of the token. `_claims` still checks `iss`,
`aud` and `exp`, because those are cheap and catch a misconfigured client id.
A token arriving by any other route would need real verification; none does.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets as _secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.domain.errors import JobTailorError

logger = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

ISSUERS = ("https://accounts.google.com", "accounts.google.com")

# Sign-in. `openid` for the `sub` claim, which is the stable identifier; `email`
# and `profile` for a name to print and an address to send from.
SIGN_IN_SCOPES = ("openid", "email", "profile")

# Sending. One scope, requested later. See the module docstring.
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"

TIMEOUT = 20.0
# Tolerance on `exp`, for the clock skew between this machine and Google's.
CLOCK_SKEW_SECONDS = 60


class GoogleError(JobTailorError):
    """Google refused, or could not be reached."""


class NotConfigured(GoogleError):
    """No client id and secret, so there is nothing to sign in to."""


@dataclass
class Grant:
    """What came back from the token endpoint.

    `refresh_token` is present only on the FIRST exchange for a given grant, and
    only because `access_type=offline` was asked for. A later exchange for the
    same grant returns none, and a caller that overwrote a stored one with the
    empty string would silently break sending — so `pipeline.auth` keeps the old
    value when this is empty, and that is not an optimisation.
    """
    access_token: str
    refresh_token: str
    expires_in: int
    scopes: tuple[str, ...]
    id_token: str

    @property
    def expires_at(self) -> float:
        return time.time() + max(self.expires_in, 0)

    def grants(self, scope: str) -> bool:
        return scope in self.scopes


@dataclass
class Identity:
    """The person, as Google describes them."""
    sub: str
    email: str
    name: str
    email_verified: bool


@dataclass
class Challenge:
    """The one-time values that tie a callback to the request that started it.

    `state` defends against CSRF: without it, an attacker can complete a login
    flow in the victim's browser and land them in the attacker's account.
    `verifier` is PKCE — if an authorization code leaks (a proxy log, a referrer,
    a shared machine), it is useless without the verifier, which never travels
    through the browser's address bar.
    """
    state: str
    verifier: str

    @property
    def challenge(self) -> str:
        digest = hashlib.sha256(self.verifier.encode()).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def new_challenge() -> Challenge:
    return Challenge(state=_secrets.token_urlsafe(24),
                     verifier=_secrets.token_urlsafe(64))


def authorization_url(*, client_id: str, redirect_uri: str,
                      scopes: tuple[str, ...], challenge: Challenge,
                      login_hint: str = "",
                      incremental: bool = False,
                      offline: bool = False) -> str:
    """Where to send the browser.

    `incremental=True` sets `include_granted_scopes`, so asking for
    `gmail.send` later keeps the sign-in scopes rather than replacing them — the
    whole point of asking late.

    `offline=True` asks for a refresh token, and brings `prompt=consent` with it.
    The two travel together because Google issues a refresh token on the FIRST
    grant only: re-connecting without `prompt=consent` returns a grant with no
    refresh token, and sending would then work for exactly one hour before
    failing permanently. It is off for plain sign-in, where nothing needs to act
    for the person while they are away — so signing in does not re-show the
    consent screen every time, and no long-lived credential is minted for a
    feature that does not exist.
    """
    if not client_id:
        raise NotConfigured(
            "GOOGLE_CLIENT_ID is not set. Create an OAuth client in the Google "
            "Cloud console (APIs & Services -> Credentials -> Create "
            "credentials -> OAuth client ID -> Web application) and put the id "
            "and secret in .env."
        )
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": challenge.state,
        "code_challenge": challenge.challenge,
        "code_challenge_method": "S256",
    }
    if offline:
        params["access_type"] = "offline"
        params["prompt"] = "consent"
    if incremental:
        params["include_granted_scopes"] = "true"
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def exchange_code(*, client_id: str, client_secret: str, redirect_uri: str,
                  code: str, verifier: str) -> Grant:
    """Trade the authorization code for tokens."""
    return _grant_from(_post(TOKEN_ENDPOINT, {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
    }))


def refresh(*, client_id: str, client_secret: str, refresh_token: str) -> Grant:
    """A fresh access token from a stored refresh token.

    A `400 invalid_grant` here is the expected failure, not a bug: the user
    revoked access, changed their password, or the application is still in
    Testing mode, where refresh tokens expire after seven days. The caller has
    to turn that into "reconnect your account", so the message says so.
    """
    try:
        grant = _grant_from(_post(TOKEN_ENDPOINT, {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }))
    except GoogleError as exc:
        if "invalid_grant" in str(exc):
            raise GoogleError(
                "Google has withdrawn this authorisation — the account was "
                "disconnected, the password changed, or (in Testing mode) the "
                "refresh token passed its seven-day expiry. Reconnect Gmail."
            ) from exc
        raise
    # A refresh never returns a new refresh token, so the caller must keep the
    # one it has. Saying so here prevents the "stored empty and broke sending"
    # bug at the only place it could be introduced.
    return grant


def revoke(token: str) -> None:
    """Withdraw the grant at Google, before deleting the row locally.

    Order matters and this is the half that is easy to skip: deleting the row
    alone leaves a live grant on the user's Google account that this application
    can no longer see, so "disconnect" would be a lie in the only direction that
    matters. A failure here is logged and not raised — the row is deleted
    either way, because leaving it would be worse.
    """
    if not token:
        return
    try:
        response = httpx.post(REVOKE_ENDPOINT, data={"token": token},
                              timeout=TIMEOUT)
        if response.status_code >= 400:
            logger.warning("Revoke refused (%s): %s", response.status_code,
                           response.text[:200])
    except httpx.HTTPError as exc:
        logger.warning("Could not reach Google to revoke: %s", exc)


def identity(id_token: str, *, client_id: str = "") -> Identity:
    """Who signed in. See the module docstring on why this is not verified."""
    claims = _claims(id_token, client_id=client_id)
    sub = str(claims.get("sub") or "")
    if not sub:
        raise GoogleError("Google's response carried no subject identifier")
    return Identity(
        sub=sub,
        email=str(claims.get("email") or ""),
        name=str(claims.get("name") or ""),
        # Keyed on `sub`, not email, so an unverified address is recorded as
        # unverified rather than refused: the account is still uniquely that
        # account. The caller decides whether to send from it.
        email_verified=bool(claims.get("email_verified")),
    )


# ── internals ─────────────────────────────────────────────────────────

def _post(url: str, data: dict[str, str]) -> dict:
    try:
        response = httpx.post(url, data=data, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise GoogleError(f"could not reach Google: {exc}") from exc
    if response.status_code >= 400:
        # The body carries `error` and `error_description`, which are the only
        # useful part of a failed OAuth exchange. Passing them through is the
        # difference between twenty minutes of guessing and one glance.
        raise GoogleError(
            f"Google refused ({response.status_code}): {response.text[:300]}")
    try:
        return response.json()
    except ValueError as exc:
        raise GoogleError("Google's response was not JSON") from exc


def _grant_from(payload: dict) -> Grant:
    access = str(payload.get("access_token") or "")
    if not access:
        raise GoogleError("Google's response carried no access token")
    return Grant(
        access_token=access,
        refresh_token=str(payload.get("refresh_token") or ""),
        expires_in=int(payload.get("expires_in") or 0),
        scopes=tuple(str(payload.get("scope") or "").split()),
        id_token=str(payload.get("id_token") or ""),
    )


def _claims(id_token: str, *, client_id: str = "") -> dict:
    parts = (id_token or "").split(".")
    if len(parts) != 3:
        raise GoogleError("the ID token is not a JWT")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except Exception as exc:                                  # noqa: BLE001
        raise GoogleError("the ID token's payload could not be read") from exc
    if not isinstance(claims, dict):
        raise GoogleError("the ID token's payload is not an object")

    if str(claims.get("iss") or "") not in ISSUERS:
        raise GoogleError(f"the ID token was not issued by Google: "
                          f"{claims.get('iss')!r}")
    if client_id and str(claims.get("aud") or "") != client_id:
        # Catches the commonest console mistake — the id from a different
        # OAuth client, or from a different project entirely.
        raise GoogleError(
            "the ID token was issued for a different client id than "
            "GOOGLE_CLIENT_ID — check which OAuth client you copied."
        )
    expiry = float(claims.get("exp") or 0)
    if expiry and time.time() > expiry + CLOCK_SKEW_SECONDS:
        raise GoogleError("the ID token has expired")
    return claims
