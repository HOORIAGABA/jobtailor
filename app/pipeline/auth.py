"""Sign in, connect Gmail, disconnect. The identity half of the product.

Two flows through one OAuth client, and the split between them is the design:

    sign in       openid email profile        at the front door
    connect       + gmail.send                at the approval step, later

**Why sending is not asked for at sign-in.** A consent screen asking to send
email on someone's behalf, shown before they have uploaded a resume, is a screen
people close. It is also untrue at that moment — nothing is being sent, and
nothing will be until a person has read a draft and pressed approve. Asking at
the point of use means the request has a visible reason, which is both the honest
design and the one that gets accepted. Google calls it incremental
authorisation; `include_granted_scopes` is what keeps the first grant alive
through the second.

**A user with no `oauth_tokens` row is normal, not broken.** Someone who wants
the tailored `.docx` and will send it themselves never connects Gmail. The
presence of a row is exactly what "Gmail connected" means.

**Disconnect revokes before it deletes.** Deleting the row alone leaves a live
grant on the person's Google account that this application can no longer see or
withdraw, so the button would be lying in the only direction that matters.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.db.models import OAuthToken, User
from app.domain.errors import UserError
from app.engine import secrets
from app.io import google

logger = logging.getLogger(__name__)

PROVIDER = "google"

# Refresh an access token this long BEFORE it expires, rather than after.
#
# Without a margin the test for "an expired token is refreshed" was a coin
# flip, and the coin was the clock: `Grant.expires_at` clamps a negative
# `expires_in` to zero, so the token expired at *exactly* now, and whether
# `expires_at > now` came out true depended on microsecond rounding between
# `datetime.fromtimestamp` and `datetime.now`. It failed on Windows and passed
# on Linux, intermittently.
#
# The margin is the right fix rather than a bigger epsilon, because the
# production version of that race is worse than a flaky test: a token with two
# seconds left passes the check, and then expires *during* the Gmail request.
# On the send path that costs an audit row, a failed run and a person wondering
# whether their application went out — for a token the system could have
# refreshed a minute earlier for free.
REFRESH_MARGIN = timedelta(seconds=60)


class NotConnected(UserError):
    """This user has never connected Gmail, so there is nothing to send with."""


def start(*, client_id: str, redirect_uri: str, connect: bool = False,
          login_hint: str = "") -> tuple[str, google.Challenge]:
    """Where to send the browser, and the one-time values to remember.

    `connect=True` is the second flow: the same client, plus `gmail.send`, with
    `include_granted_scopes` so the sign-in scopes survive.
    """
    challenge = google.new_challenge()
    scopes = (google.SIGN_IN_SCOPES + (google.SEND_SCOPE,) if connect
              else google.SIGN_IN_SCOPES)
    url = google.authorization_url(
        client_id=client_id, redirect_uri=redirect_uri, scopes=scopes,
        challenge=challenge, login_hint=login_hint, incremental=connect,
        # A refresh token is asked for only by the flow that needs one.
        offline=connect)
    return url, challenge


def callback(session: Session, *, client_id: str, client_secret: str,
             redirect_uri: str, code: str, verifier: str,
             fernet_key: str) -> tuple[User, bool]:
    """Complete either flow. Returns the user and whether Gmail is now connected.

    One function for both because the difference is entirely in the scopes that
    come back, and Google — not this code — decides what the person actually
    granted. A user can approve sign-in and decline sending on the same screen,
    and the only honest way to know is to read `scope` from the response.
    """
    grant = google.exchange_code(
        client_id=client_id, client_secret=client_secret,
        redirect_uri=redirect_uri, code=code, verifier=verifier)
    who = google.identity(grant.id_token, client_id=client_id)

    user = _upsert_user(session, who)
    connected = False
    if grant.grants(google.SEND_SCOPE):
        _store_grant(session, user, grant, fernet_key)
        connected = True
    else:
        # Sign-in only, and nothing is stored. The sign-in flow does not even
        # ask for a refresh token (`offline=False`), because nothing here needs
        # to act for the person while they are away — and a credential held for
        # a feature that does not exist is a liability with no upside. If one
        # arrives anyway, it is dropped on the floor here.
        logger.info("Signed in %s (sign-in scopes only)", who.email)

    session.flush()
    return user, connected


def access_token(session: Session, user: User, *, client_id: str,
                 client_secret: str, fernet_key: str) -> str:
    """A usable access token for sending, refreshing it if it has expired.

    The stored access token is used while it lasts, which keeps a send from
    making two network calls where one will do, and the refresh token is the
    thing that makes the whole arrangement work while the person is not looking.
    """
    row = connection(session, user)
    if row is None:
        raise NotConnected(
            "Gmail is not connected for this account. Connect it at the "
            "approval step — it is asked for there, not at sign-in."
        )

    if row.expires_at is not None and row.access_token_encrypted:
        expires_at = row.expires_at
        if expires_at.tzinfo is None:
            # SQLite gives back a naive datetime even for a timezone-aware
            # column. Comparing that to an aware `now` raises, and the raise
            # would happen inside a send.
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at > datetime.now(timezone.utc) + REFRESH_MARGIN:
            return secrets.decrypt(fernet_key, row.access_token_encrypted)

    refreshed = google.refresh(
        client_id=client_id, client_secret=client_secret,
        refresh_token=secrets.decrypt(fernet_key, row.refresh_token_encrypted))
    _store_grant(session, user, refreshed, fernet_key)
    session.flush()
    return refreshed.access_token


def connection(session: Session, user: User) -> OAuthToken | None:
    """Queried, not read off `user.tokens` — see `gate.message_of`."""
    return (session.query(OAuthToken)
            .filter(OAuthToken.user_id == user.id,
                    OAuthToken.provider == PROVIDER)
            .one_or_none())


def disconnect(session: Session, user: User, *, fernet_key: str) -> bool:
    """Revoke at Google, then delete. Returns whether there was anything to do.

    A revoke that fails still deletes the row: a stored credential this
    application has decided not to hold is worse kept than dropped, and the
    person can withdraw the grant from their Google account page. The failure is
    logged, not raised.
    """
    row = connection(session, user)
    if row is None:
        return False
    try:
        google.revoke(secrets.decrypt(fernet_key, row.refresh_token_encrypted))
    except secrets.CannotDecrypt:
        logger.warning("Could not decrypt a token to revoke it; deleting anyway")
    session.delete(row)
    session.flush()
    logger.info("Disconnected Gmail for %s", user.email)
    return True


def describe(session: Session, user: User) -> dict:
    """What `/api/auth/me` answers. Never includes a token, by construction.

    There is no code path in this application that returns a refresh token to a
    client. That is worth stating rather than assuming, because the natural way
    to write this function — serialise the row — would do exactly that.
    """
    row = connection(session, user)
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "gmail_connected": row is not None,
        "gmail_scopes": ((row.scopes or "").split() if row else []),
        "can_send": row is not None,
    }


# ── internals ─────────────────────────────────────────────────────────

def _upsert_user(session: Session, who: google.Identity) -> User:
    """Keyed on `sub`, not email.

    An address can be reassigned inside a Workspace domain and a person can
    change theirs; the `sub` claim is stable for the life of the account. Keying
    on email is how one person's runs end up under another person's login.
    """
    user = (session.query(User)
            .filter(User.google_sub == who.sub).one_or_none())
    if user is None:
        user = User(google_sub=who.sub, email=who.email, name=who.name)
        session.add(user)
        session.flush()
        logger.info("New user %s", who.email)
        return user

    # The address and name are refreshed on every sign-in, because they are
    # Google's to change and this row is a cache of them.
    if who.email:
        user.email = who.email
    if who.name:
        user.name = who.name
    return user


def _store_grant(session: Session, user: User, grant: google.Grant,
                 fernet_key: str) -> OAuthToken:
    """Write the tokens, encrypted, keeping a refresh token we already have.

    **The empty-refresh-token case is the bug this function exists to prevent.**
    Google returns a refresh token on the first grant and never again, so a
    refresh response carries none — and writing that empty value over the stored
    one would leave sending working for exactly one hour, then failing
    permanently, with no obvious cause.
    """
    row = connection(session, user)
    if row is None:
        row = OAuthToken(user_id=user.id, provider=PROVIDER,
                         refresh_token_encrypted=b"")
        session.add(row)

    if grant.refresh_token:
        row.refresh_token_encrypted = secrets.encrypt(fernet_key,
                                                      grant.refresh_token)
    row.access_token_encrypted = secrets.encrypt(fernet_key, grant.access_token)
    row.expires_at = datetime.fromtimestamp(grant.expires_at, tz=timezone.utc)
    # The union, not the latest response: an incremental grant reports only the
    # scopes just granted on some flows, and forgetting the earlier ones would
    # make a connected account look unconnected.
    # `or ""` because a column default applies at INSERT, not at construction:
    # on the row just added above, `scopes` is still None.
    row.scopes = " ".join(
        sorted(set((row.scopes or "").split()) | set(grant.scopes)))
    return row
