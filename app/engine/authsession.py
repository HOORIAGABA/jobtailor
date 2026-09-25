"""The signed-in cookie: who this request is, and until when.

A session cookie has to survive a restart and a second process, so it cannot be
server-side state in a dictionary. The two ways to do that are a random id in a
`sessions` table and a signed value the client carries. This is the second one,
because the only fact being carried is a user id that the database will be asked
about anyway on every request — a `sessions` table would add a write per sign-in
and a read per request to learn something the cookie already says.

**Signed, not encrypted.** The user id is not a secret; forging one is the
threat. So: HMAC-SHA256 over the id and an expiry, with the expiry *inside* the
signed payload. An expiry outside the signature is a number the client edits.

**No JWT.** A JWT here would mean a parser that accepts an `alg` field from the
attacker, which is the single most reliably exploited misfeature in web
authentication — `alg: none` and RS256-to-HS256 confusion are both attacks on a
library reading the algorithm out of the token. One fixed algorithm, chosen by
this file, cannot be negotiated down. It also drops a dependency.

Pure, like `engine.confirm`: the caller passes the key, so this is testable with
a throwaway secret and the real one lives in exactly one place.
"""
from __future__ import annotations

import hashlib
import hmac
import time

# Bumped if the payload's shape changes, so an old cookie cannot be read as a
# valid one over different fields.
VERSION = "v1"

# Long enough that a person is not signed out mid-application, short enough that
# a stolen cookie is not a permanent credential. A refresh on use would be
# better and is a later change: the shape here already carries an expiry.
DEFAULT_TTL_SECONDS = 14 * 24 * 60 * 60


class BadCookie(ValueError):
    """The cookie is absent, malformed, forged or expired.

    One error for all four on purpose. A caller that distinguished "expired"
    from "forged" would be telling an attacker which of the two they achieved,
    and the correct response to every one of them is the same: sign in again.
    """


def _payload(user_id: str, expires_at: int) -> bytes:
    """Length-prefixed, for the same reason as `engine.confirm._payload`.

    Without it a user id ending in a digit and a shorter expiry could produce
    the same bytes as a different pair.
    """
    parts = [VERSION, user_id, str(expires_at)]
    return b"".join(
        f"{len(part.encode())}:".encode() + part.encode() for part in parts
    )


def issue(secret: str, user_id: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS,
          now: float | None = None) -> str:
    """`v1.<user_id>.<expiry>.<mac>` — everything the server needs, signed."""
    if not secret:
        raise ValueError(
            "no signing secret: set JWT_SECRET. Without it a session cookie "
            "would be unsigned, which is the same as trusting the client to "
            "say who it is."
        )
    if not user_id or "." in user_id:
        # The separator is a full stop, so an id containing one would make the
        # token ambiguous to parse. Ids here are hex, so this is a guard against
        # a future change rather than a live case.
        raise ValueError(f"cannot sign a session for user id {user_id!r}")
    expires_at = int((now if now is not None else time.time()) + ttl_seconds)
    mac = hmac.new(secret.encode(), _payload(user_id, expires_at),
                   hashlib.sha256).hexdigest()
    return f"{VERSION}.{user_id}.{expires_at}.{mac}"


def read(secret: str, cookie: str, *, now: float | None = None) -> str:
    """The user id the cookie attests to, or raise `BadCookie`.

    Checks the signature BEFORE the expiry, so an attacker learns nothing from
    the ordering, and never returns a value from a token it could not verify.
    """
    if not secret:
        raise BadCookie("no signing secret is configured")
    parts = (cookie or "").split(".")
    if len(parts) != 4 or parts[0] != VERSION:
        raise BadCookie("not a session cookie")

    _, user_id, expiry_text, mac = parts
    try:
        expires_at = int(expiry_text)
    except ValueError as exc:
        raise BadCookie("not a session cookie") from exc

    expected = hmac.new(secret.encode(), _payload(user_id, expires_at),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, mac):
        raise BadCookie("this session cookie was not signed by this server")
    if (now if now is not None else time.time()) >= expires_at:
        raise BadCookie("this session has expired")
    return user_id
