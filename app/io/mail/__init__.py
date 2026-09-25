"""Senders, and the choice between them.

Three backends are planned and one exists. The factory is here rather than in
the API layer so that "which sender is this instance using" has exactly one
answer, and so adding the Gmail one later is a line in `_BACKENDS` rather than
an `if` somewhere in a route.

**The default is `console`, and an unknown name is refused rather than falling
back.** Sending is the only irreversible thing this system does. A typo in
`MAIL_PROVIDER` that quietly resolved to a real backend would be the worst
possible failure mode; a typo that quietly resolved to `console` would be the
second worst, because the person would believe they had sent something. So:
default to the one that cannot send, and refuse anything unrecognised loudly.
"""
from __future__ import annotations

from app.io.mail.base import (
    Attachment,
    OutgoingMessage,
    SendFailed,
    Sender,
)
from app.io.mail.console import ConsoleSender
from app.io.mail.gmail import GmailSender

__all__ = [
    "Attachment", "OutgoingMessage", "SendFailed", "Sender",
    "ConsoleSender", "GmailSender", "sender_for", "needs_token",
    "AVAILABLE", "PLANNED", "UnknownSender",
]

AVAILABLE = ("console", "gmail_api")

# Named so the error message can tell the difference between "you misspelled it"
# and "that one is not built yet", which are different problems with different
# fixes. `gmail_smtp` stays listed and unbuilt on purpose: an app password in a
# file works on a laptop and fails on every free host that blocks outbound 587.
PLANNED = ("gmail_smtp",)

# Which backends need an OAuth access token passed in. The API layer asks this
# rather than matching on the name itself, so adding a backend does not mean
# editing a condition in a route.
_NEEDS_TOKEN = frozenset({"gmail_api"})


class UnknownSender(RuntimeError):
    """`MAIL_PROVIDER` names a backend that does not exist here."""


def needs_token(name: str) -> bool:
    return _normalise(name) in _NEEDS_TOKEN


def sender_for(name: str, *, access_token: str = "") -> Sender:
    """Resolve a provider name to a sender. Empty means `console`."""
    key = _normalise(name)
    if key == "console":
        return ConsoleSender()
    if key == "gmail_api":
        return GmailSender(access_token)
    if key in PLANNED:
        raise UnknownSender(
            f"mail provider {key!r} is not built yet. Available: "
            f"{', '.join(AVAILABLE)}."
        )
    raise UnknownSender(
        f"unknown mail provider {key!r}. Available: {', '.join(AVAILABLE)}."
    )


def _normalise(name: str) -> str:
    return (name or "console").strip().lower()
