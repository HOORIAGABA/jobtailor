"""S11 — dispatch, once, auditably.

Five steps in a fixed order, and the order is the design:

    1  refuse if `sent_at` is already set                     -> 409
    2  recompute the HMAC over the SUPPLIED recipient/subject/body
    3  write the audit row  BEFORE  dispatching
    4  dispatch
    5  record the provider id, status = sent  (terminal)

**Step 1 is the idempotency guard.** A double-clicked button, a retried request
and a resumed worker must all be unable to send twice, and the check is a column
rather than a lock because a lock does not survive the process.

**Step 2 is why `/send` takes the message as parameters.** Reading it from the
row would mean the value that was approved and the value that is sent are only
*probably* the same — two reads of a mutable row separated by a network round
trip. Passed in and covered by the token, they are provably identical. That is
invariant I6.

**Step 3 is the one people get wrong.** An audit written after a successful
dispatch cannot record the sends that failed halfway — which are exactly the ones
worth knowing about, because those are the cases where the recruiter may have the
email and the database may not know it. So the row goes in first, with outcome
`attempted`, and is a *second* row on completion rather than an update: the table
is append-only, and a row that can be edited is not an audit.

**Step 4 distinguishes two failures that look alike and are not.** A provider
that answers "no" has delivered nothing, so the run returns to `approved` and
the same approval can be retried. A provider that never answers may well have
delivered it, so the run fails and a person reads the audit. Collapsing the two
into one `failed` either charges a full re-run for a rate limit, or lets a
machine send a second copy of an email that already arrived.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import Artifact, Message, Run, SendAudit, User
from app.domain.errors import IllegalTransition, UserError
from app.domain.status import may_send
from app.engine.confirm import expires_at, issue, matches
from app.io.mail.base import Attachment, OutgoingMessage, SendFailed, Sender

logger = logging.getLogger(__name__)

# What goes in the envelope. `.docx` is the more reliably parsed of the two by
# an ATS, and the PDF is what a human opens and sees identically everywhere —
# both is the common shape of a real application and neither is redundant.
ATTACH_STAGES = ("resume_docx", "resume_pdf")

# The rendered message, kept on the run so it is downloadable and openable in a
# mail client — the record of what was actually sent, not a reconstruction.
EML_STAGE = "sent_eml"


class AlreadySent(UserError):
    """This message has been sent. `sent_at` is the guard."""


def send(session: Session, run: Run, user: User, sender: Sender, *,
         secret: str, recipient: str, subject: str, body: str,
         confirm_token: str) -> dict:
    """Step 1 through 5. Returns what happened, or raises before dispatching."""
    message = _message_of(session, run)

    # 1 — idempotency, and it genuinely is first.
    #
    # It has to precede the status check, not follow it: a successful send moves
    # the run to `sent`, so a status check running first answers a retry with
    # "this run is 'sent'; only an approved run may be sent" — technically true
    # and the wrong answer to the question being asked. The person double-
    # clicked a button and needs to know their email went out once, not to be
    # told about a state machine.
    if message is not None and message.sent_at is not None:
        raise AlreadySent(
            f"already sent at {message.sent_at.isoformat()} "
            f"(provider id {message.provider_message_id or 'unknown'})"
        )

    if not may_send(run.status):
        raise IllegalTransition(
            f"this run is {run.status!r}; only an approved run may be sent"
        )
    if message is None:
        raise UserError("nothing approved to send")

    # 2 — the token covers what is about to go out, not what is in the row.
    if not matches(secret, confirm_token, run.id, recipient, subject, body):
        raise UserError(
            "this send does not match the message that was approved — reopen "
            "the preview, approve again, and send that"
        )

    outgoing = OutgoingMessage(
        sender=user.email,
        sender_name=user.name or "",
        recipient=recipient.strip(),
        subject=subject.strip(),
        body=body,
        attachments=_attachments(session, run),
    )

    # 3 — the audit row exists before the provider is touched.
    _audit(session, message, outgoing, "attempted")
    run.move_to("sending")
    session.commit()

    # 4 — dispatch. Two kinds of failure, and they are not the same failure.
    try:
        provider_id = sender.send(outgoing)
    except SendFailed as exc:
        # The provider ANSWERED, and the answer was no: a bad address, a revoked
        # token, a quota. Nothing was delivered, so the run goes back to
        # `approved` and the same approval can be sent again once the cause is
        # fixed. The alternative — `failed`, which leads only to `tailoring` —
        # would charge seven model calls for a rate limit.
        _audit(session, message, outgoing, "refused", detail=str(exc)[:500])
        run.move_to("approved")
        run.error = f"{type(exc).__name__}: {exc}"
        session.commit()
        raise
    except Exception as exc:                              # noqa: BLE001
        # We never heard back: a timeout, a dropped connection, a crash. The
        # recruiter MAY have the email and this database does not know. That is
        # not a state a machine may retry out of, so the run fails and a person
        # reads the audit. Recording it as `refused` would be a guess dressed as
        # a fact.
        _audit(session, message, outgoing, "unknown", detail=str(exc)[:500])
        run.move_to("failed")
        run.error = f"{type(exc).__name__}: {exc}"
        session.commit()
        raise SendFailed(
            f"{sender.name} did not answer: {exc}. Whether the message was "
            f"delivered is unknown — check the recipient's side before "
            f"retrying."
        ) from exc

    # 5 — record, and close the run.
    message.sent_at = datetime.now(timezone.utc)
    message.provider = sender.name
    message.provider_message_id = provider_id
    _audit(session, message, outgoing, "sent", detail=provider_id)
    _store_eml(session, run, outgoing)
    run.move_to("sent")
    session.commit()

    logger.info("Run %s sent via %s to %s (%s)", run.id, sender.name,
                recipient, provider_id)
    return {
        "status": run.status,
        "provider": sender.name,
        "provider_message_id": provider_id,
        "sent_at": message.sent_at.isoformat(),
        "attachments": [a.filename for a in outgoing.attachments],
        # Says plainly that nothing left the machine, when nothing did.
        "dispatched": sender.name != "console",
    }


def approved(session: Session, run: Run, *, secret: str) -> dict:
    """What was approved, and a token that covers exactly it.

    **Why reissuing a token here does not weaken I6.** The invariant is "what is
    sent is what was approved", and the `messages` row *is* what was approved:
    `gate._upsert_message` writes it inside the decision and nothing afterwards
    touches it. A token issued over that row therefore admits exactly one
    message — the approved one — and `/send` still refuses anything else.

    The alternative was keeping the token from the approval in the browser, and
    that fails the first time someone closes the tab: the run sits in `approved`
    with no way to send it, which is a product that loses work to a page reload.
    The token defends against a race between *preview and decision*, where the
    text is still moving. After the decision it is not moving any more.
    """
    # Same order as `send`: the send is reported before the status, because a
    # person asking "can I send this" after it has gone needs to hear that it
    # has gone, not a sentence about which states are sendable.
    message = _message_of(session, run)
    if message is not None and message.sent_at is not None:
        raise AlreadySent(
            f"already sent at {message.sent_at.isoformat()} "
            f"(provider id {message.provider_message_id or 'unknown'})")
    if not may_send(run.status):
        raise IllegalTransition(
            f"this run is {run.status!r}; only an approved run can be sent")
    if message is None:
        raise UserError("nothing approved to send")

    token = issue(secret, run.id, message.recipient, message.subject,
                  message.body)
    return {
        "recipient": message.recipient,
        "subject": message.subject,
        "body": message.body,
        "attachments": [a.filename for a in _attachments(session, run)],
        "confirm_token": token,
        "confirm_token_expires_at": expires_at(token),
    }


def preview_eml(session: Session, run: Run, user: User, *, recipient: str,
                subject: str, body: str) -> bytes:
    """The exact bytes that would be sent, without sending.

    Useful before the first real send, and the only honest way to answer "what
    will the recruiter actually receive".
    """
    return OutgoingMessage(
        sender=user.email, sender_name=user.name or "",
        recipient=recipient.strip(), subject=subject.strip(), body=body,
        attachments=_attachments(session, run),
    ).to_mime().as_bytes()


# ── internals ─────────────────────────────────────────────────────────

def _message_of(session: Session, run: Run) -> Message | None:
    return (session.query(Message)
            .filter(Message.run_id == run.id)
            .order_by(Message.created_at.desc()).first())


def _artifacts(session: Session, run: Run) -> dict[str, Artifact]:
    """Queried, not read off `run.artifacts`.

    The relationship is loaded once per session, so a run whose files were
    rendered earlier in the same session sees the collection as it was before —
    and an envelope silently missing its resume is the exact failure this stage
    cannot be allowed to have. Same lesson as `gate.message_of`.
    """
    return {a.stage: a for a in
            session.query(Artifact).filter(Artifact.run_id == run.id)}


def _attachments(session: Session, run: Run) -> list[Attachment]:
    by_stage = _artifacts(session, run)
    return [
        Attachment(filename=by_stage[stage].filename, data=by_stage[stage].blob)
        for stage in ATTACH_STAGES if stage in by_stage
    ]


def _audit(session: Session, message: Message, outgoing: OutgoingMessage,
           outcome: str, detail: str = "") -> None:
    """Append-only. Hashes, not contents.

    The audit answers "was this sent, to whom, when, and was it the approved
    text" — and does not need a second copy of the body to do it. A new row per
    outcome rather than an update, because a row that can be edited is not an
    audit.

    `seq` is counted rather than timed. See `SendAudit`: these rows land
    milliseconds apart and a 15.6 ms clock cannot separate them, which left the
    order to a random id. Counting the rows already there is exact everywhere
    and survives a restart.
    """
    written = (session.query(SendAudit)
               .filter(SendAudit.message_id == message.id).count())
    session.add(SendAudit(
        message_id=message.id,
        seq=written + 1,
        recipient=outgoing.recipient,
        subject_hash=outgoing.subject_hash(),
        body_hash=outgoing.body_hash(),
        attachment_hash=outgoing.attachment_hash(),
        outcome=outcome,
        detail=detail,
    ))
    session.flush()


def _store_eml(session: Session, run: Run, outgoing: OutgoingMessage) -> None:
    raw = outgoing.to_mime().as_bytes()
    # Queried for the same reason as the attachments, and here it also matters
    # for correctness rather than completeness: `(run_id, stage)` is unique, so
    # a stale read that missed an existing row would make a retry an
    # IntegrityError instead of an update.
    existing = _artifacts(session, run).get(EML_STAGE)
    if existing is not None:
        existing.blob, existing.bytes = raw, len(raw)
        return
    session.add(Artifact(
        run_id=run.id, stage=EML_STAGE, filename="sent_message.eml",
        content_type="message/rfc822", bytes=len(raw), blob=raw))
    session.flush()
