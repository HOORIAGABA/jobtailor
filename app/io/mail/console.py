"""A sender that produces the exact message and dispatches nothing.

**This is the default, and it is not a placeholder.** Sending is the one
irreversible action in the system, so the useful default is the one that cannot
do it — a misconfiguration should mean "nothing happened", never "something went
to a stranger". Choosing a real backend is therefore a deliberate act of
configuration rather than the consequence of forgetting to set something.

It is also what makes S11 testable. The `.eml` it returns is the real RFC-5322
message, headers and attachments included, so a test can assert on the bytes
that *would* have gone out. The dangerous stage gets the same property as
everything else in this project: it runs in CI with no credentials.

The `.eml` is stored as an artifact on the run, so it is downloadable through the
same endpoint as the resume and openable in any mail client.
"""
from __future__ import annotations

import logging
import uuid

from app.io.mail.base import OutgoingMessage

logger = logging.getLogger(__name__)


class ConsoleSender:
    """Renders the message, logs a summary, returns a fake provider id."""

    name = "console"

    def __init__(self) -> None:
        self.sent: list[tuple[OutgoingMessage, bytes]] = []

    def send(self, message: OutgoingMessage) -> str:
        raw = message.to_mime().as_bytes()
        self.sent.append((message, raw))

        logger.warning(
            "NOT SENT (console sender): %d bytes to %s — %r, %d attachment(s)",
            len(raw), message.recipient, message.subject,
            len(message.attachments),
        )
        return f"console-{uuid.uuid4().hex[:16]}"

    def render(self, message: OutgoingMessage) -> bytes:
        """The `.eml` without recording a send. For a preview."""
        return message.to_mime().as_bytes()
