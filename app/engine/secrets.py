"""Encryption at rest for the one thing here worth stealing.

A Google refresh token does not expire on its own. Whoever holds one can mint
access tokens for as long as the grant stands, and this application's grant
includes `gmail.send` — so a leaked database row is the ability to send email as
the candidate, indefinitely, from an address recruiters trust. That is a
materially worse breach than a leaked resume, and it is the reason this file
exists rather than a plain `Text` column.

**Fernet, not a hand-rolled scheme.** AES-128-CBC with an HMAC over the
ciphertext and a timestamp, from `cryptography`, which is the library that
everything else in this stack already depends on transitively. The alternative —
`AES` plus a home-made MAC — is the classic way to ship an encryption bug.

**There is no default key, and `encrypt` refuses without one.** A default would
mean every deployment shares it, which is the same as not encrypting while
looking as though you did. The failure is loud and at the point of use.

This module is pure: bytes in, bytes out, no database, no network, no
configuration. It is `engine` for that reason, and the key is passed in by the
caller rather than read here, so the same function is testable with a throwaway
key and there is exactly one place that decides where the real one comes from.
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class BadKey(ValueError):
    """The key is missing or not a valid Fernet key."""


class CannotDecrypt(ValueError):
    """The ciphertext does not verify under this key.

    Either the key changed or the bytes were altered. Both mean the same thing
    to the caller — this token is unusable and the user has to reconnect — and
    neither is a reason to guess.
    """


def new_key() -> str:
    """A fresh key, to paste into `FERNET_KEY`. Printed by `scripts/keys.py`."""
    return Fernet.generate_key().decode()


def _cipher(key: str) -> Fernet:
    if not (key or "").strip():
        raise BadKey(
            "no encryption key: set FERNET_KEY. Generate one with "
            "`python -m scripts.keys`. Without it a refresh token would be "
            "stored in plaintext, and a refresh token is the ability to send "
            "mail as the user until they revoke it."
        )
    try:
        return Fernet(key.strip().encode())
    except Exception as exc:                                  # noqa: BLE001
        raise BadKey(
            "FERNET_KEY is not a valid key: it must be 32 url-safe base64 "
            "encoded bytes. Generate one with `python -m scripts.keys`."
        ) from exc


def encrypt(key: str, plaintext: str) -> bytes:
    """Encrypt a token for storage. Empty input encrypts to empty output.

    Empty rather than raising because "this user has no access token yet" is a
    normal state and should not need a nullable column and a branch at every
    call site.
    """
    if plaintext == "":
        return b""
    return _cipher(key).encrypt(plaintext.encode())


def decrypt(key: str, ciphertext: bytes | None) -> str:
    """Recover a token, or raise. Never returns a partial or a placeholder."""
    if not ciphertext:
        return ""
    try:
        return _cipher(key).decrypt(ciphertext).decode()
    except InvalidToken as exc:
        raise CannotDecrypt(
            "a stored token could not be decrypted — FERNET_KEY has changed, "
            "or the row was altered. The user has to reconnect their account."
        ) from exc
