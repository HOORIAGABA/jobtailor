"""The tables. SQLite on a laptop, Postgres when hosted, one model set.

**Why a database at all**, when fifteen stages have run happily without one: a
human gate means the run has to survive the request that started it. Until S0.4
and S9, every run was a call stack — start it, finish it, return. A gate means
persist, return, wait an unbounded time, accept a decision that arrives
separately, resume. That is a row.

Three decisions worth stating, because each one undoes a specific v1 failure.

**Artifacts are bytes in a row, not paths on disk.** v1 stored absolute paths to
rendered files; free-tier filesystems are ephemeral and the files stopped
existing on the next restart, leaving rows pointing at nothing. `render()`
already returns bytes rather than paths for exactly this reason, so there is
nothing to change upstream — the bytes go straight in.

**Status is a plain string, validated by `domain.status`.** Not a database enum:
a native enum needs a migration to add a state, and this machine will grow. The
authority is the transition table in the domain layer, which the API, the
pipeline and this module all call. An enum here plus rules in the route handlers
would be two authorities, and the one that wins is whichever file was edited
last.

**A resume is not part of a run.** `runs.resume_id` points at a confirmed
resume, so one resume serves many applications and the parse is confirmed once
rather than once per job. On a three-calls-a-day tier that is three model calls
saved per application after the first.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer,
    LargeBinary, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.domain.status import check_resume_transition, check_run_transition


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id() -> str:
    """A short opaque id.

    Not an autoincrementing integer: these appear in URLs, and a sequential id
    in a URL invites walking the sequence to read someone else's run. Every
    handler still checks ownership — an unguessable id is not authorisation —
    but it removes the invitation.
    """
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


# ── people ────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    # The Google subject identifier, not the email. An address can be
    # reassigned inside a Workspace domain and a person can change theirs; the
    # `sub` claim is stable for the life of the account. Keying on email is how
    # one person's runs end up under another person's login.
    google_sub: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320))
    name: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now)

    resumes: Mapped[list["Resume"]] = relationship(back_populates="user")
    runs: Mapped[list["Run"]] = relationship(back_populates="user")


class OAuthToken(Base):
    """One row per (user, provider, scope set).

    `gmail.send` is requested incrementally, at the approval step rather than at
    sign-in, so a user may exist with no row here — that is the normal state for
    someone who only wants the tailored .docx. The presence of a row is what
    "Gmail connected" means, and deleting it is what "disconnect" means, after
    revoking upstream.

    Tokens are stored encrypted. The column names say so, so that a plaintext
    write is visible in review rather than only at the breach.
    """
    __tablename__ = "oauth_tokens"
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_user_provider"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32), default="google")
    scopes: Mapped[str] = mapped_column(Text, default="")
    refresh_token_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    access_token_encrypted: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now)


# ── the resume ────────────────────────────────────────────────────────

class Resume(Base):
    """An uploaded file, its parse, and the user's corrections to that parse.

    `raw_json` is the editable hypothesis — a `RawResume`, headings verbatim, no
    ids. `doc_json` is the `ResumeDoc` that `normalize` builds from it.

    The edit loop rewrites `raw_json` and re-derives `doc_json` **without
    touching the parser**: the bytes have not changed, so re-parsing would
    return the same mistake and discard the correction. `normalize` is pure and
    free, and is also what has to re-run, since it assigns the ids.

    Ids are frozen at `confirmed`, not before. That is what lets an
    `EvidenceLink` or an `Op` name `exp.3.b.2` a week later and mean it.
    """
    __tablename__ = "resumes"
    __table_args__ = (
        UniqueConstraint("user_id", "file_sha256", "version",
                         name="uq_resume_version"),
        Index("ix_resume_user_status", "user_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)

    filename: Mapped[str] = mapped_column(String(255))
    # The bytes are hashed, not kept. A resume is personal data and the parse is
    # cached on this hash, so the file itself is needed exactly once.
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    file_bytes: Mapped[int] = mapped_column(Integer, default=0)
    # The extracted text, kept; the file itself is not. Two reasons, and
    # neither is caching: a re-parse after a failure works from this rather
    # than needing the upload again, and the coverage check compares the parse
    # against it — so after an edit the user can be told "3 of the 5 missing
    # lines are now in". Keeping the bytes would be a second copy of personal
    # data that nothing needs.
    extract_text: Mapped[str] = mapped_column(Text, default="")

    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    doc_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # `ParseCoverage.dropped` is the edit screen, not a debug field: it names
    # the exact source lines the parse lost, which is the only kind of
    # confirmation prompt anyone completes.
    coverage_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="uploaded", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    # Bumped on every edit so `PUT /draft` can refuse a stale write from a
    # second tab instead of silently overwriting the first one.
    revision: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="resumes")
    runs: Mapped[list["Run"]] = relationship(back_populates="resume")

    def move_to(self, target: str) -> None:
        check_resume_transition(self.status, target)
        self.status = target
        if target == "confirmed":
            self.confirmed_at = _now()


# ── the run ───────────────────────────────────────────────────────────

class Run(Base):
    """One job application.

    Every stage's output is a column rather than a blob of `RunState`, because
    the API serves them individually and a UI that has to download the whole
    run to show the gap list is a UI that feels slow for no reason.
    """
    __tablename__ = "runs"
    __table_args__ = (Index("ix_run_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    resume_id: Mapped[str] = mapped_column(ForeignKey("resumes.id"), index=True)

    status: Mapped[str] = mapped_column(String(32), default="created", index=True)
    stage: Mapped[str] = mapped_column(String(32), default="")
    error: Mapped[str] = mapped_column(Text, default="")

    jd_text: Mapped[str] = mapped_column(Text, default="")
    # The brief is cached on this hash, so a second resume against the same
    # posting costs no model call in S1.
    jd_sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    role: Mapped[str] = mapped_column(String(255), default="")
    company: Mapped[str] = mapped_column(String(255), default="")

    brief_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    index_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    standing_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    ops_json: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    accepted_json: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    rejected_json: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    tailored_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    diff_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    outreach_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    proof_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    call_log_json: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)

    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now)

    user: Mapped[User] = relationship(back_populates="runs")
    resume: Mapped[Resume] = relationship(back_populates="runs")
    checkpoints: Mapped[list["RunCheckpoint"]] = relationship(
        back_populates="run", cascade="all, delete-orphan")
    artifacts: Mapped[list["Artifact"]] = relationship(
        back_populates="run", cascade="all, delete-orphan")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="run", cascade="all, delete-orphan")

    def move_to(self, target: str) -> None:
        check_run_transition(self.status, target)
        self.status = target
        if target in ("approved", "rejected"):
            self.decided_at = _now()


class RunCheckpoint(Base):
    """One row per completed stage. The resume-after-crash record.

    Also the source for the progress stream: a client reconnecting reads the
    checkpoints and sees every stage that finished, not only the ones that
    happened while it was connected. v1 put in-flight work on a daemon thread
    and lost a run when a free-tier host spun down after fifteen minutes idle;
    the fix is not a sturdier thread, it is that the work leaves a trail.
    """
    __tablename__ = "run_checkpoints"
    __table_args__ = (
        UniqueConstraint("run_id", "stage", name="uq_checkpoint_stage"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    stage: Mapped[str] = mapped_column(String(64))
    state_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now)

    run: Mapped[Run] = relationship(back_populates="checkpoints")


class Artifact(Base):
    """A rendered file, as bytes.

    v1 stored absolute paths. The host restarted, the filesystem was ephemeral,
    and the rows pointed at files that no longer existed. `io.render` returns
    bytes rather than paths for this reason, so this is where they land.
    """
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("run_id", "stage", name="uq_artifact_stage"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    stage: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(128))
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    blob: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now)

    run: Mapped[Run] = relationship(back_populates="artifacts")


# ── sending ───────────────────────────────────────────────────────────

class Message(Base):
    """The draft, then the sent thing.

    `sent_at` is the idempotency guard and the reason this is a row rather than
    a field on the run: a double-clicked button, a retried request and a resumed
    worker must all be unable to send twice, and the check is
    `sent_at IS NOT NULL`.

    `confirm_token_hash` binds the decision to what the person actually saw. The
    token is an HMAC over `run_id|recipient|subject|body`; between preview and
    send any of those could change, and approving "the message" is meaningless
    if the message has moved. A mismatch is a 409, not a surprise send.
    """
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)

    recipient: Mapped[str] = mapped_column(String(320), default="")
    subject: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    confirm_token_hash: Mapped[str] = mapped_column(String(64), default="")

    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    provider: Mapped[str] = mapped_column(String(32), default="")
    provider_message_id: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now)

    run: Mapped[Run] = relationship(back_populates="messages")


class SendAudit(Base):
    """Append-only. Written BEFORE dispatch, never updated.

    Written first so a crash mid-dispatch still leaves a row saying an attempt
    happened. An audit written afterwards cannot record the sends that failed
    halfway, which are exactly the ones worth knowing about.

    Hashes, not contents: the audit answers "was this sent, to whom, when" and
    does not need a second copy of the message body to do it.
    """
    __tablename__ = "send_audit"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"), index=True)
    recipient: Mapped[str] = mapped_column(String(320))
    subject_hash: Mapped[str] = mapped_column(String(64))
    body_hash: Mapped[str] = mapped_column(String(64))
    attachment_hash: Mapped[str] = mapped_column(String(64), default="")
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                   default=_now)
    outcome: Mapped[str] = mapped_column(String(32), default="attempted")
    detail: Mapped[str] = mapped_column(Text, default="")


# ── what the candidate has told us ────────────────────────────────────

class Capability(Base):
    """A capability the candidate confirmed in answer to an `ask_user`.

    `grounding_corpus(doc, confirmed)` already takes this as its second source.
    The rule it encodes: inference may propose an entry, only a person may add
    one — so `source` records which, and nothing writes here without a human.
    """
    __tablename__ = "capabilities"
    __table_args__ = (UniqueConstraint("user_id", "term", name="uq_user_term"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    term: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(64), default="asked")
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                   default=_now)


class OpFeedback(Base):
    """Per-op decisions, recorded even when the decision was made in bulk.

    The gate is binary — approve or reject the whole plan — but the data is kept
    per operation. That costs nothing now and makes per-change approval a UI
    change later rather than a migration. **A binary gate is a narrower question
    over the same data, not a simpler data model.**
    """
    __tablename__ = "op_feedback"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    op_id: Mapped[str] = mapped_column(String(64))
    op_kind: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now)


__all__ = [
    "Base", "User", "OAuthToken", "Resume", "Run", "RunCheckpoint",
    "Artifact", "Message", "SendAudit", "Capability", "OpFeedback",
]
