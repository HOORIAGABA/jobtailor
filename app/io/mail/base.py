"""What a sender is, and the message it sends.

One protocol, three implementations: a console sender that writes the `.eml` and
dispatches nothing, SMTP with an app password, and the Gmail API. The console one
is not a stub — it is what makes this stage testable. **Every test of the send
path asserts against a real MIME message and sends no email**, which gives the
one genuinely dangerous stage in the system the same "runs in CI with no
credentials" property as everything else.

The MIME is built here rather than in each backend, so all three send byte-
identical messages and a bug in the headers is a bug in one place.

**The attachment's content type comes from its filename.** Hardcoding
`application/pdf` is how a `.docx` arrives that the recipient cannot open, and
the person who finds out is the recruiter.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Protocol

from app.domain.errors import JobTailorError

logger = logging.getLogger(__name__)

# Keyed on the extension, because that is what the renderer produced and what
# the recipient's mail client will look at.
MEDIA = {
    ".docx": ("application", "vnd.openxmlformats-officedocument."
                             "wordprocessingml.document"),
    ".pdf": ("application", "pdf"),
    ".txt": ("text", "plain"),
}
FALLBACK_MEDIA = ("application", "octet-stream")


class SendFailed(JobTailorError):
    """The provider refused, or could not be reached."""


@dataclass
class Attachment:
    filename: str
    data: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def media(self) -> tuple[str, str]:
        return MEDIA.get(Path(self.filename).suffix.lower(), FALLBACK_MEDIA)


@dataclass
class OutgoingMessage:
    """Everything needed to send, and nothing that decides whether to.

    `sender` is the candidate's own address. A job application has to come from
    the person signing it: a transactional provider sending "on behalf of" means
    the recruiter's reply goes to a service, the `From` domain does not match the
    signature, and the candidate has no copy in their own Sent folder to follow
    up from three days later.
    """
    sender: str
    sender_name: str
    recipient: str
    subject: str
    body: str
    attachments: list[Attachment] = field(default_factory=list)

    def to_mime(self) -> EmailMessage:
        """Plain text with attachments. No HTML part.

        A cold application email has no formatting worth the risk: an HTML part
        is one more thing to render wrong, and a plain-text-only message is the
        one shape that looks the same everywhere.
        """
        mime = EmailMessage()
        mime["From"] = (f"{self.sender_name} <{self.sender}>"
                        if self.sender_name else self.sender)
        mime["To"] = self.recipient
        mime["Subject"] = self.subject
        mime["Date"] = formatdate(localtime=True)
        # The domain has to be the sender's, not the machine's. `make_msgid()`
        # with no argument uses the local hostname, which on this stack is
        # `localhost` or a container id — and `<...@localhost>` is a spam signal
        # that some receivers reject outright. Measured in the console demo:
        # `Message-ID: <...@localhost>`, from a run that looked entirely fine.
        mime["Message-ID"] = make_msgid(domain=self._domain())
        mime.set_content(self.body)

        for attachment in self.attachments:
            maintype, subtype = attachment.media()
            mime.add_attachment(attachment.data, maintype=maintype,
                               subtype=subtype, filename=attachment.filename)
        return mime

    def _domain(self) -> str:
        """The sender address's domain, or a last resort that is not `localhost`."""
        _, _, domain = self.sender.rpartition("@")
        return domain.strip().lower() or "invalid"

    def body_hash(self) -> str:
        return hashlib.sha256(self.body.encode()).hexdigest()

    def subject_hash(self) -> str:
        return hashlib.sha256(self.subject.encode()).hexdigest()

    def attachment_hash(self) -> str:
        """One hash over every attachment, in order.

        The audit records what went out; a per-file list would be more precise
        and is not what the question "was this the document that was approved"
        needs.
        """
        digest = hashlib.sha256()
        for attachment in self.attachments:
            digest.update(attachment.filename.encode())
            digest.update(attachment.data)
        return digest.hexdigest()


class Sender(Protocol):
    """Dispatch a message and return the provider's id for it."""

    name: str

    def send(self, message: OutgoingMessage) -> str: ...
