"""The token that binds a decision to what the person actually saw.

**The problem this solves is a race, not an attack.** Between the preview and
the approval, any of the recipient, the subject or the body could change — the
person edits the draft in another tab, a background job re-renders, a stale
client posts. Approving "the message" is meaningless if the message has moved,
and the failure mode is the worst kind: something goes out that nobody read.

So the preview issues an HMAC over exactly the four values that identify the
message, `/send` recomputes it from the values *supplied in the request*, and a
mismatch is a refusal rather than a surprise. That is invariant I6, and it is
why `/send` takes the recipient as a parameter instead of reading it from the
row: two reads of a mutable row separated by a network round trip are only
*probably* the same value, while a parameter covered by the token is provably
the one that was approved.

Pure and secretless by design: the caller passes the key. That keeps this in
`engine`, where it can be tested without configuration, and keeps the secret in
exactly one place — the settings object the API reads.

`compare_digest` rather than `==`, because a timing-distinguishable comparison
on a signature is a bad habit even where it is not yet exploitable.
"""
from __future__ import annotations

import hashlib
import hmac
import time

# Bumped if the payload's shape ever changes, so an old token cannot be
# mistaken for a valid one over a different set of fields.
VERSION = "v1"

# The four values that identify a message. Order is part of the contract.
_FIELDS = ("run_id", "recipient", "subject", "body")

# How long a preview stays approvable.
#
# Long enough to read a diff, a gap list and a covering letter properly, and to
# be interrupted while doing it. Short enough that a preview left open in a tab
# over a weekend cannot be approved on Monday against a draft whose run has
# since been re-executed — the token proves what was *shown*, and after a while
# "what was shown" stops being a thing anybody remembers reading.
#
# The expiry lives INSIDE the signed payload. An expiry beside a signature is a
# number the client edits.
DEFAULT_TTL_SECONDS = 2 * 60 * 60


def _payload(run_id: str, recipient: str, subject: str, body: str,
             expires_at: int) -> bytes:
    """Length-prefixed, so no combination of values can imitate another.

    Joining with a separator would let `recipient="a|b"` and `subject=""`
    produce the same bytes as `recipient="a"` and `subject="b"` — the classic
    canonicalisation bug. Prefixing each field with its length removes the
    ambiguity entirely.
    """
    parts = [VERSION, run_id, recipient, subject, body, str(expires_at)]
    return b"".join(
        f"{len(part.encode())}:".encode() + part.encode() for part in parts
    )


def issue(secret: str, run_id: str, recipient: str, subject: str, body: str,
          *, ttl_seconds: int = DEFAULT_TTL_SECONDS,
          now: float | None = None) -> str:
    """The token to hand to the preview screen. `v1.<expiry>.<mac>`.

    The expiry is carried in the clear AND covered by the signature, so the
    verifier can read it without a lookup table and cannot be lied to about it.
    """
    if not secret:
        raise ValueError("no signing secret: set CONFIRM_TOKEN_SECRET")
    expires_at = int((now if now is not None else time.time()) + ttl_seconds)
    digest = hmac.new(secret.encode(),
                      _payload(run_id, recipient, subject, body, expires_at),
                      hashlib.sha256)
    return f"{VERSION}.{expires_at}.{digest.hexdigest()}"


def matches(secret: str, token: str, run_id: str, recipient: str,
            subject: str, body: str, *, now: float | None = None) -> bool:
    """Does this token cover exactly these values, and is it still live?

    The signature is checked BEFORE the expiry, so a forged token and an old one
    are indistinguishable from the outside — and no value is trusted out of a
    token that did not verify.
    """
    if not token or not secret:
        return False
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != VERSION:
        return False
    try:
        expires_at = int(parts[1])
    except ValueError:
        return False

    expected = hmac.new(secret.encode(),
                        _payload(run_id, recipient, subject, body, expires_at),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, parts[2]):
        return False
    return (now if now is not None else time.time()) < expires_at


def expires_at(token: str) -> int:
    """When this token stops working, so the UI can say so before it does.

    Unverified — it is the token's own claim about itself, fine for showing a
    countdown and never for deciding anything.
    """
    parts = (token or "").split(".")
    try:
        return int(parts[1]) if len(parts) == 3 else 0
    except ValueError:
        return 0


def fingerprint(token: str) -> str:
    """What to store on the message row.

    The token itself is not kept: it is a credential for one decision, and a
    stored credential is one that can be replayed out of a database dump. The
    hash is enough to answer "was this the decision that was approved".
    """
    return hashlib.sha256((token or "").encode()).hexdigest()
