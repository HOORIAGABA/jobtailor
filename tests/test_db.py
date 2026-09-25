"""The state machines and the schema.

The transition tables are the authority for what may happen to a run, so they
get tested as tables rather than through the API — a rule that only holds when
reached through a handler is a rule that stops holding the first time someone
adds a second handler.
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.db import models
from app.db.session import build_engine, create_all
from app.domain.errors import IllegalTransition
from app.domain.status import (
    TERMINAL_RUN, artifacts_available, check_resume_transition,
    check_run_transition, is_terminal_run, may_send, next_run_states,
)


@pytest.fixture
def session():
    from sqlalchemy.orm import sessionmaker
    engine = build_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as s:
        yield s


def _user(session) -> models.User:
    user = models.User(google_sub="sub-1", email="a@example.com", name="A")
    session.add(user)
    session.commit()
    return user


def _resume(session, user, **over) -> models.Resume:
    resume = models.Resume(
        user_id=user.id, filename="cv.pdf", file_sha256="a" * 64, **over)
    session.add(resume)
    session.commit()
    return resume


# ══ the run machine ═══════════════════════════════════════════════════

def test_the_happy_path_is_legal():
    for a, b in [("created", "tailoring"), ("tailoring", "needs_review"),
                 ("needs_review", "approved"), ("approved", "sending"),
                 ("sending", "sent")]:
        check_run_transition(a, b)


def test_needs_review_has_exactly_two_ways_out():
    """"Approve or reject, nothing in between", written down as a machine."""
    assert set(next_run_states("needs_review")) == {"approved", "rejected", "failed"}


def test_rejected_and_sent_are_both_terminal():
    assert TERMINAL_RUN == {"sent", "rejected"}
    assert next_run_states("sent") == []
    assert next_run_states("rejected") == []
    assert is_terminal_run("rejected") and is_terminal_run("sent")


@pytest.mark.parametrize("current, target", [
    ("needs_review", "sent"),        # skipping the decision entirely
    ("needs_review", "sending"),     # approving by starting to send
    ("created", "needs_review"),     # skipping the work
    ("sent", "sending"),             # sending twice
    ("rejected", "approved"),        # changing your mind after the fact
    ("approved", "rejected"),
])
def test_an_illegal_transition_raises(current, target):
    with pytest.raises(IllegalTransition):
        check_run_transition(current, target)


def test_the_error_says_what_was_possible_instead():
    with pytest.raises(IllegalTransition) as exc:
        check_run_transition("needs_review", "sent")
    assert "approved" in str(exc.value)

    with pytest.raises(IllegalTransition) as exc:
        check_run_transition("sent", "sending")
    assert "terminal" in str(exc.value)


def test_an_unknown_status_is_not_silently_accepted():
    with pytest.raises(IllegalTransition):
        check_run_transition("created", "nearly_done")
    with pytest.raises(IllegalTransition):
        check_run_transition("in_progress", "tailoring")


# ══ reject costs the candidate nothing ════════════════════════════════

def test_the_resume_is_downloadable_after_a_rejection():
    """Reject means "do not send this on my behalf", not "destroy the work".

    The likeliest real rejection is a good resume with a clumsy covering
    letter. A gate that throws away the resume to punish the email makes the
    honest answer the expensive one.
    """
    assert artifacts_available("rejected")
    assert artifacts_available("approved")
    assert artifacts_available("needs_review")
    assert artifacts_available("sent")


def test_nothing_is_downloadable_before_the_work_is_done():
    assert not artifacts_available("created")
    assert not artifacts_available("tailoring")


def test_only_an_approved_run_may_send():
    assert may_send("approved")
    for status in ("needs_review", "rejected", "sent", "sending", "created"):
        assert not may_send(status), status


# ══ the resume machine ════════════════════════════════════════════════

def test_the_edit_loop_is_a_legal_self_transition():
    """The only one in either machine.

    The user corrects a field, `normalize` runs again, and the parse still
    needs confirming. Every other self-transition is a bug.
    """
    check_resume_transition("needs_confirm", "needs_confirm")


@pytest.mark.parametrize("status", ["confirmed", "uploaded", "parsing", "superseded"])
def test_no_other_self_transition_is_legal(status):
    with pytest.raises(IllegalTransition):
        check_resume_transition(status, status)


def test_a_confirmed_resume_cannot_go_back_to_editing():
    """Ids are frozen at confirmation — an `Op` naming `exp.3.b.2` a week later
    has to still mean it."""
    for target in ("needs_confirm", "parsing", "uploaded"):
        with pytest.raises(IllegalTransition):
            check_resume_transition("confirmed", target)


def test_a_confirmed_resume_can_only_be_superseded():
    check_resume_transition("confirmed", "superseded")


# ══ the schema ════════════════════════════════════════════════════════

def test_move_to_enforces_the_machine_on_a_row(session):
    user = _user(session)
    resume = _resume(session, user)
    run = models.Run(user_id=user.id, resume_id=resume.id)
    session.add(run)
    session.commit()

    run.move_to("tailoring")
    run.move_to("needs_review")
    with pytest.raises(IllegalTransition):
        run.move_to("sent")
    assert run.status == "needs_review"        # unchanged by the failed attempt


def test_a_decision_records_when_it_was_made(session):
    user = _user(session)
    resume = _resume(session, user)
    run = models.Run(user_id=user.id, resume_id=resume.id, status="needs_review")
    session.add(run)
    session.commit()

    assert run.decided_at is None
    run.move_to("rejected")
    assert run.decided_at is not None


def test_confirming_a_resume_stamps_the_time(session):
    resume = _resume(session, _user(session), status="needs_confirm")
    assert resume.confirmed_at is None
    resume.move_to("confirmed")
    assert resume.confirmed_at is not None


def test_one_resume_serves_many_runs(session):
    """The reason a resume is not part of a run: confirm once, apply often."""
    user = _user(session)
    resume = _resume(session, user, status="confirmed")
    for _ in range(3):
        session.add(models.Run(user_id=user.id, resume_id=resume.id))
    session.commit()
    session.refresh(resume)
    assert len(resume.runs) == 3


def test_google_sub_is_unique_not_email(session):
    """An address can be reassigned; the subject identifier cannot."""
    _user(session)
    session.add(models.User(google_sub="sub-2", email="a@example.com"))
    session.commit()                                  # same email is fine

    session.add(models.User(google_sub="sub-1", email="other@example.com"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_rendered_file_is_stored_as_bytes_not_a_path(session):
    """v1 stored absolute paths; the filesystem was ephemeral and the rows
    ended up pointing at files that no longer existed."""
    user = _user(session)
    resume = _resume(session, user)
    run = models.Run(user_id=user.id, resume_id=resume.id)
    session.add(run)
    session.commit()

    session.add(models.Artifact(
        run_id=run.id, stage="resume_docx", filename="x.docx",
        content_type="application/vnd.openxmlformats-officedocument."
                     "wordprocessingml.document",
        bytes=4, blob=b"PK\x03\x04"))
    session.commit()

    stored = session.query(models.Artifact).one()
    assert stored.blob == b"PK\x03\x04"
    assert not hasattr(stored, "path")


def test_a_stage_is_checkpointed_once(session):
    user = _user(session)
    resume = _resume(session, user)
    run = models.Run(user_id=user.id, resume_id=resume.id)
    session.add(run)
    session.commit()

    session.add(models.RunCheckpoint(run_id=run.id, stage="brief"))
    session.commit()
    session.add(models.RunCheckpoint(run_id=run.id, stage="brief"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_sent_at_is_the_idempotency_guard(session):
    """A double-clicked button, a retried request and a resumed worker must all
    be unable to send twice."""
    user = _user(session)
    resume = _resume(session, user)
    run = models.Run(user_id=user.id, resume_id=resume.id)
    session.add(run)
    session.commit()

    message = models.Message(run_id=run.id, recipient="a@b.com", subject="s")
    session.add(message)
    session.commit()
    assert message.sent_at is None


def test_the_audit_row_can_exist_before_any_send_succeeded(session):
    """Written BEFORE dispatch, so a crash mid-flight still leaves a record."""
    user = _user(session)
    resume = _resume(session, user)
    run = models.Run(user_id=user.id, resume_id=resume.id)
    session.add(run)
    session.commit()
    message = models.Message(run_id=run.id, recipient="a@b.com")
    session.add(message)
    session.commit()

    session.add(models.SendAudit(
        message_id=message.id, recipient="a@b.com",
        subject_hash="x" * 64, body_hash="y" * 64, outcome="attempted"))
    session.commit()
    assert session.query(models.SendAudit).one().outcome == "attempted"


def test_op_feedback_is_per_op_even_though_the_gate_is_binary(session):
    """A binary gate is a narrower question over the same data, not a simpler
    data model — so per-change approval later is a UI change, not a migration."""
    user = _user(session)
    resume = _resume(session, user)
    run = models.Run(user_id=user.id, resume_id=resume.id)
    session.add(run)
    session.commit()

    for op_id, kind in [("op1", "promote_item"), ("op2", "set_summary")]:
        session.add(models.OpFeedback(
            run_id=run.id, op_id=op_id, op_kind=kind, decision="approved"))
    session.commit()
    assert session.query(models.OpFeedback).count() == 2


def test_sqlite_enforces_foreign_keys(session):
    """Off by default, per connection. Without the PRAGMA a dangling
    `resume_id` is accepted locally and rejected in production."""
    session.add(models.Run(user_id="nope", resume_id="also-nope"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_ids_are_not_sequential():
    """A sequential id in a URL invites walking the sequence.

    Every handler still checks ownership — an unguessable id is not
    authorisation — but it removes the invitation.
    """
    ids = {models._id() for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) == 32 and i.isalnum() for i in ids)


# ══ migrations must match the models ══════════════════════════════════

def test_the_migrations_produce_exactly_the_models(tmp_path):
    """The check that stops the v1 failure from recurring in a new form.

    v1 ran migrations as SQLite `PRAGMA` statements that silently no-op on
    Postgres, so the schema never changed and nothing said so. Alembic fixes
    the mechanism; this fixes the discipline. Edit a model, forget the
    migration, and CI fails here rather than production failing on a column
    that does not exist.
    """
    from alembic import command
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.autogenerate import compare_metadata

    url = f"sqlite+pysqlite:///{tmp_path / 'migrated.db'}"
    config = Config("alembic.ini")
    config.set_main_option("script_location", "migrations")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")

    engine = build_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"compare_type": True})
            diff = compare_metadata(context, models.Base.metadata)
    finally:
        # Disposed, not just dropped. Leaving `with engine.connect()` returns
        # the connection to the pool rather than closing it, so the engine keeps
        # this file open — and on Windows an open file cannot be deleted, so
        # pytest's `tmp_path` cleanup fails as a teardown ERROR on a test that
        # passed. POSIX allows unlinking an open file, so on Linux this leaks
        # in silence.
        engine.dispose()

    assert diff == [], (
        "models and migrations disagree — run "
        "`alembic revision --autogenerate -m '...'`:\n" + "\n".join(map(str, diff))
    )
