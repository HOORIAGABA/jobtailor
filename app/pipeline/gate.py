"""★ S9 — the human gate. The product's core guarantee, as code.

Everything before this point is a machine reading a document and proposing
changes. This is where a person says yes, and it is the reason the rest is
allowed to be imperfect: the validator has never refused anything on a live run
and the tailoring quality is unproven, but nothing leaves the machine until
somebody has read the diff, the gap list and the message.

**Approve or reject. Nothing in between.** One decision over the whole plan, by
design — see `SDD-AMENDMENT-06`. The per-op data is still recorded in
`op_feedback`, so per-change approval later is a UI change rather than a
migration: a binary gate is a narrower question over the same data, not a
simpler data model.

**Rejecting blocks sending. It does not destroy the work.** The likeliest real
rejection is a good resume with a clumsy covering letter, and a gate that threw
away the resume to punish the email would make the honest answer the expensive
one. `domain.status.artifacts_available` includes `rejected` for exactly this.

**Nothing here sends.** S11 does, and only from `approved`, and only with a
token that covers the message it is about to send.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Message, OpFeedback, Run
from app.domain.errors import IllegalTransition, UserError
from app.domain.status import may_send
from app.engine.confirm import expires_at, fingerprint, issue, matches

logger = logging.getLogger(__name__)

APPROVE = "approve"
REJECT = "reject"
DECISIONS = (APPROVE, REJECT)


class StaleDecision(UserError):
    """The decision does not cover the message that was previewed.

    Between preview and decision the recipient, subject or body changed. This
    is a refusal rather than a send of whichever version happened to be current,
    because the alternative is something going out that nobody read.
    """


def preview(session: Session, run: Run, secret: str) -> dict[str, Any]:
    """What the gate screen shows, plus the token that binds a decision to it.

    The recipient, subject and body come from the outreach draft and are
    editable — so the token is issued over the *current* values and the decision
    must carry back whatever the person actually approved, edits included.
    """
    if run.status != "needs_review":
        raise IllegalTransition(
            f"this run is {run.status!r}; only a run awaiting review can be "
            f"previewed for a decision"
        )

    draft = run.outreach_json or {}
    recipient, subject, body = _draft_shown(run)
    # Issued once and read once. Issuing twice — for the token and again for
    # its expiry — would give two different expiry values whenever the clock
    # ticked between the calls.
    token = issue(secret, run.id, recipient, subject, body)

    return {
        "run_id": run.id,
        "recipient": recipient,
        "recipient_candidates": list(draft.get("recipient_candidates") or []),
        "subject": subject,
        "body": body,
        # The draft's own complaints, so the screen can show them beside the
        # text rather than making the person guess why a claim looks shaky.
        "problems": list(draft.get("problems") or []),
        "cites": list(draft.get("cites") or []),
        "changes": (run.diff_json or {}).get("changes", []),
        "gaps": (run.diff_json or {}).get("gaps", []),
        "questions": (run.diff_json or {}).get("questions", []),
        "rejections": (run.diff_json or {}).get("rejections", []),
        "standing": run.standing_json or {},
        "confirm_token": token,
        # So the screen can say "this preview expires in 1h 52m" rather than
        # discovering it as a refusal after someone has spent ten minutes
        # rewriting the letter.
        "confirm_token_expires_at": expires_at(token),
        "can_download": True,
        # Honest about what happens next: this instance may have no way to send.
        "will_send": True,
    }


def decide(session: Session, run: Run, *, decision: str, secret: str,
           recipient: str = "", subject: str = "", body: str = "",
           confirm_token: str = "") -> dict[str, Any]:
    """Record the person's answer. The only way out of `needs_review`.

    A rejection needs no token: refusing to send cannot be the dangerous
    direction, and demanding a signature to say "no" would mean a stale preview
    could trap a run in `needs_review` with no way to close it.

    **An approval's token is checked against the draft this server is still
    holding, not against the text being submitted.** That is a correction to
    how this was first built, and the bug it fixes was not subtle: the token was
    verified against the submitted recipient, subject and body, so the only
    submission that could ever verify was one identical to the preview — and the
    draft is editable. A person who fixed a clumsy sentence got a 409 telling
    them their approval did not match the message that was previewed, which was
    both true and useless, because no client can compute an HMAC it has no key
    for. The feature and the check were in direct contradiction and the check
    won.

    So the two halves are separated:

        preview -> decision   the token proves the draft has not moved under
                              the person since the server showed it to them.
                              That is the race `engine.confirm` was written for.
        decision -> send      `pipeline.send` issues a token over the stored
                              message row, so what goes out is what was
                              approved, edits included.

    The submitted text needs no signature of its own: the person typed it and
    read it, and it travels in the same request as the decision about it. The
    thing that could have changed without them noticing is the draft, and that
    is what is checked.
    """
    if decision not in DECISIONS:
        raise UserError(f"decision must be {APPROVE!r} or {REJECT!r}")
    if run.status != "needs_review":
        raise IllegalTransition(
            f"this run is {run.status!r}; it is not awaiting a decision"
        )

    _record_op_feedback(session, run, decision)

    if decision == REJECT:
        run.move_to("rejected")
        logger.info("Run %s rejected — nothing will be sent, files stay", run.id)
        return {"status": run.status, "may_send": False, "can_download": True}

    shown = _draft_shown(run)
    if not matches(secret, confirm_token, run.id, *shown):
        raise StaleDecision(
            "the draft changed after it was shown to you, so this approval is "
            "about a message that no longer exists. Reload the preview, read "
            "it again, and approve that."
        )
    if not recipient.strip():
        raise UserError(
            "no recipient — a message cannot be approved for sending without "
            "an address"
        )

    message = _upsert_message(session, run, recipient, subject, body,
                              confirm_token)
    run.move_to("approved")
    logger.info("Run %s approved: %s to %s", run.id, message.id, recipient)

    return {
        "status": run.status,
        "message_id": message.id,
        "may_send": may_send(run.status),
        "can_download": True,
    }


def message_of(session: Session, run: Run) -> Message | None:
    """The run's message, newest first.

    Queried rather than read off `run.messages`: the relationship is loaded
    once, so a read immediately after `_upsert_message` inserted a row sees the
    collection as it was before — empty. A function whose whole job is "what is
    there now" cannot be built on a cached view.
    """
    return (session.query(Message)
            .filter(Message.run_id == run.id)
            .order_by(Message.created_at.desc())
            .first())


# ── internals ─────────────────────────────────────────────────────────

def _draft_shown(run: Run) -> tuple[str, str, str]:
    """The draft as the preview renders it. One reader, used by both sides.

    `preview` issues the token over these three values and `decide` recomputes
    it over them. Two separate expressions of "the draft" would drift, and the
    symptom of the drift would be every approval failing.
    """
    draft = run.outreach_json or {}
    return (str(draft.get("recipient") or ""),
            str(draft.get("subject") or ""),
            str(draft.get("body") or ""))


def _upsert_message(session: Session, run: Run, recipient: str, subject: str,
                    body: str, token: str) -> Message:
    """The approved message, as its own row.

    Its own row rather than fields on the run because `sent_at` is the
    idempotency guard, and a guard belongs on the thing being guarded. The
    token is stored only as a hash — it is a credential for one decision, and a
    stored credential is one that can be replayed out of a database dump.
    """
    message = message_of(session, run)
    if message is None:
        message = Message(run_id=run.id)
        session.add(message)
    message.recipient = recipient.strip()
    message.subject = subject.strip()
    message.body = body
    message.confirm_token_hash = fingerprint(token)
    session.flush()
    return message


def _record_op_feedback(session: Session, run: Run, decision: str) -> None:
    """Per op, even though the person answered once for all of them.

    Costs nothing now and is what makes per-change approval a UI change later
    rather than a migration.
    """
    existing = {row.op_id for row in
                session.query(OpFeedback).filter(OpFeedback.run_id == run.id)}
    for op in (run.accepted_json or []):
        op_id = str(op.get("op_id") or "") if isinstance(op, dict) else ""
        if not op_id or op_id in existing:
            continue
        session.add(OpFeedback(
            run_id=run.id, op_id=op_id,
            op_kind=str(op.get("op", "")) if isinstance(op, dict) else "",
            decision="approved" if decision == APPROVE else "rejected",
        ))
    session.flush()
