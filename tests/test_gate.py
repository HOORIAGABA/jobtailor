"""★ S9 — the human gate, and the token that makes an approval mean something.

The tests split three ways: the token as pure arithmetic, the decision as a
service, and the pair over HTTP. All run with no model and no network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.db import models
from app.db.session import create_all, engine, reset, session_scope
from app.domain.errors import IllegalTransition, UserError
from app.engine import confirm
from app.pipeline import gate as service

SECRET = "a-long-random-development-secret"
DRAFT = {
    "recipient": "careers@annova.com",
    "recipient_candidates": ["careers@annova.com", "noreply@annova.com"],
    "subject": "AI Engineer - A. Morgan",
    "body": "I build backend services in Python and FastAPI.",
    "problems": [],
    "cites": ["exp.1.b.1"],
}


# ══ the token ═════════════════════════════════════════════════════════

def test_a_token_covers_exactly_the_message_it_was_issued_for():
    token = confirm.issue(SECRET, "run1", "a@b.com", "Subject", "Body")
    assert confirm.matches(SECRET, token, "run1", "a@b.com", "Subject", "Body")


@pytest.mark.parametrize("run_id, to, subject, body", [
    ("run2", "a@b.com", "Subject", "Body"),          # a different run
    ("run1", "other@b.com", "Subject", "Body"),      # recipient changed
    ("run1", "a@b.com", "Edited", "Body"),           # subject changed
    ("run1", "a@b.com", "Subject", "Rewritten"),     # body changed
])
def test_any_change_invalidates_the_token(run_id, to, subject, body):
    """The problem is a race, not an attack: between the preview and the
    approval the draft could be edited in another tab, and something going out
    that nobody read is the worst available outcome."""
    token = confirm.issue(SECRET, "run1", "a@b.com", "Subject", "Body")
    assert not confirm.matches(SECRET, token, run_id, to, subject, body)


def test_another_secret_does_not_verify():
    token = confirm.issue(SECRET, "run1", "a@b.com", "S", "B")
    assert not confirm.matches("different-secret", token, "run1", "a@b.com",
                               "S", "B")


def test_fields_cannot_be_shuffled_past_the_signature():
    """Length-prefixed, so no combination of values imitates another.

    Joining with a separator would let recipient="a|b", subject="" produce the
    same bytes as recipient="a", subject="b" — the classic canonicalisation bug.
    """
    a = confirm.issue(SECRET, "r", "a|b", "", "body")
    b = confirm.issue(SECRET, "r", "a", "b", "body")
    assert a != b


def test_issuing_without_a_secret_refuses_rather_than_signing_with_nothing():
    """A shared default signing key is a signature anyone can forge, and the
    failure would be silent."""
    with pytest.raises(ValueError, match="CONFIRM_TOKEN_SECRET"):
        confirm.issue("", "run1", "a@b.com", "S", "B")
    assert not confirm.matches("", "anything", "run1", "a@b.com", "S", "B")


def test_an_empty_or_malformed_token_never_verifies():
    for token in ("", "garbage", "v1.", "v1.deadbeef"):
        assert not confirm.matches(SECRET, token, "r", "a@b.com", "S", "B")


def test_the_stored_fingerprint_is_not_the_token():
    """A stored credential is one that can be replayed out of a database dump."""
    token = confirm.issue(SECRET, "r", "a@b.com", "S", "B")
    assert confirm.fingerprint(token) != token
    assert len(confirm.fingerprint(token)) == 64


# ══ the decision ══════════════════════════════════════════════════════

@pytest.fixture
def db(tmp_path, monkeypatch):
    url = f"sqlite+pysqlite:///{tmp_path / 'gate.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    reset(url)
    create_all(engine())
    yield
    reset()


def _run(session, status: str = "needs_review", **over) -> models.Run:
    """A run awaiting review. Reuses the user and resume so the helper can be
    called more than once inside one session."""
    user = session.query(models.User).first()
    if user is None:
        user = models.User(google_sub="s", email="a@example.com")
        session.add(user)
        session.flush()
    resume = session.query(models.Resume).first()
    if resume is None:
        resume = models.Resume(user_id=user.id, filename="cv.pdf",
                               file_sha256="a" * 64, status="confirmed")
        session.add(resume)
        session.flush()
    run = models.Run(
        user_id=user.id, resume_id=resume.id, status=status,
        outreach_json=dict(DRAFT),
        accepted_json=[{"op_id": "op1", "op": "promote_item"},
                       {"op_id": "op2", "op": "set_summary"}],
        diff_json={"changes": [{"op_id": "op1"}], "gaps": [{"requirement": "X"}],
                   "questions": [], "rejections": []},
        standing_json={"demonstrated": ["Python"], "declared_only": [],
                       "not_found": ["Airflow"]},
        **over)
    session.add(run)
    session.flush()
    return run


def test_the_preview_shows_what_the_person_is_deciding_about(db):
    with session_scope() as s:
        body = service.preview(s, _run(s), SECRET)
    assert body["recipient"] == "careers@annova.com"
    assert body["changes"] and body["gaps"]
    assert body["standing"]["not_found"] == ["Airflow"]
    assert body["confirm_token"]


def test_a_run_not_awaiting_review_cannot_be_previewed(db):
    with session_scope() as s:
        with pytest.raises(IllegalTransition):
            service.preview(s, _run(s, status="tailoring"), SECRET)


def test_approving_moves_the_run_and_records_the_message(db):
    with session_scope() as s:
        run = _run(s)
        token = service.preview(s, run, SECRET)["confirm_token"]
        out = service.decide(s, run, decision="approve", secret=SECRET,
                             recipient=DRAFT["recipient"],
                             subject=DRAFT["subject"], body=DRAFT["body"],
                             confirm_token=token)

        assert run.status == "approved"
        assert out["may_send"] is True
        message = service.message_of(s, run)
        assert message.recipient == "careers@annova.com"
        assert message.sent_at is None          # approval is not sending
        assert message.confirm_token_hash and message.confirm_token_hash != token


def test_an_edited_message_is_approved_as_edited(db):
    """★ The draft is editable, and the token from the preview is the only one a
    browser has.

    This test failed on the first build of the gate, and the failure was the
    design: the token was checked against the SUBMITTED text, so the only
    submission that could verify was one identical to the preview. A person who
    fixed a clumsy sentence was told their approval did not match — true, and
    impossible to act on, because no client can compute an HMAC it has no key
    for. The check now covers the draft the server showed, which is the thing
    that could have moved without them noticing.
    """
    with session_scope() as s:
        run = _run(s)
        token = service.preview(s, run, SECRET)["confirm_token"]

        edited_body = "I build backend services. Happy to talk this week."
        service.decide(s, run, decision="approve", secret=SECRET,
                       recipient="hiring@annova.com",
                       subject="Application: AI Engineer", body=edited_body,
                       confirm_token=token)
        message = service.message_of(s, run)
        assert message.recipient == "hiring@annova.com"
        assert message.subject == "Application: AI Engineer"
        assert message.body == edited_body


def test_an_approval_of_a_draft_that_has_since_moved_is_refused(db):
    """The race the token exists for: the person is approving a message the
    server no longer holds. Something going out that nobody read is the worst
    available outcome."""
    with session_scope() as s:
        run = _run(s)
        token = service.preview(s, run, SECRET)["confirm_token"]

        # A re-run, a background re-render, another tab — the draft changes
        # under the open preview.
        run.outreach_json = {**DRAFT, "body": "A completely different letter."}
        s.flush()

        with pytest.raises(service.StaleDecision, match="changed after it was "
                                                       "shown"):
            service.decide(s, run, decision="approve", secret=SECRET,
                           recipient=DRAFT["recipient"],
                           subject=DRAFT["subject"], body=DRAFT["body"],
                           confirm_token=token)
        assert run.status == "needs_review"      # unchanged by the refusal


def test_approving_without_a_recipient_is_refused(db):
    with session_scope() as s:
        run = _run(s)
        token = service.preview(s, run, SECRET)["confirm_token"]
        with pytest.raises(UserError, match="no recipient"):
            service.decide(s, run, decision="approve", secret=SECRET,
                           recipient="  ", subject="S", body="B",
                           confirm_token=token)


# ══ rejecting costs the candidate nothing ═════════════════════════════

def test_rejecting_needs_no_token(db):
    """Refusing to send is not the dangerous direction, and requiring a
    signature to say no would let a stale preview trap a run."""
    with session_scope() as s:
        run = _run(s)
        out = service.decide(s, run, decision="reject", secret="")
        assert run.status == "rejected"
        assert out["can_download"] is True
        assert out["may_send"] is False


def test_a_rejected_run_keeps_its_files(db):
    """The likeliest real rejection is a good resume with a clumsy covering
    letter."""
    from app.domain.status import artifacts_available, may_send
    with session_scope() as s:
        run = _run(s)
        service.decide(s, run, decision="reject", secret="")
    assert artifacts_available("rejected") is True
    assert may_send("rejected") is False


def test_a_decision_is_final(db):
    with session_scope() as s:
        run = _run(s)
        service.decide(s, run, decision="reject", secret="")
        with pytest.raises(IllegalTransition):
            service.decide(s, run, decision="approve", secret=SECRET)


def test_anything_other_than_approve_or_reject_is_refused(db):
    with session_scope() as s:
        for decision in ("maybe", "", "APPROVE", "partially"):
            with pytest.raises(UserError):
                service.decide(s, _run(s), decision=decision, secret=SECRET)


# ══ per-op feedback under a binary gate ═══════════════════════════════

def test_each_op_gets_a_row_even_though_one_answer_was_given(db):
    """A binary gate is a narrower question over the same data, not a simpler
    data model — so per-change approval later is a UI change, not a migration."""
    with session_scope() as s:
        run = _run(s)
        service.decide(s, run, decision="reject", secret="")
        rows = s.query(models.OpFeedback).filter(
            models.OpFeedback.run_id == run.id).all()
        assert {r.op_id for r in rows} == {"op1", "op2"}
        assert {r.decision for r in rows} == {"rejected"}
        assert {r.op_kind for r in rows} == {"promote_item", "set_summary"}


# ══ over HTTP ═════════════════════════════════════════════════════════

@pytest.fixture
def api(tmp_path, monkeypatch) -> TestClient:
    url = f"sqlite+pysqlite:///{tmp_path / 'gate_api.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DEV_USER_EMAIL", "dev@example.com")
    monkeypatch.setenv("CONFIRM_TOKEN_SECRET", SECRET)
    reset(url)
    create_all(engine())

    with session_scope() as s:
        user = models.User(google_sub="dev-local", email="dev@example.com")
        s.add(user)
        s.flush()
        resume = models.Resume(user_id=user.id, filename="cv.pdf",
                               file_sha256="b" * 64, status="confirmed")
        s.add(resume)
        s.flush()
        run = models.Run(user_id=user.id, resume_id=resume.id,
                         status="needs_review", outreach_json=dict(DRAFT),
                         accepted_json=[{"op_id": "op1", "op": "promote_item"}],
                         diff_json={"changes": [], "gaps": []})
        s.add(run)
        s.flush()
        s.add(models.Artifact(run_id=run.id, stage="resume_docx",
                              filename="cv.docx", content_type="application/x",
                              bytes=4, blob=b"PK\x03\x04"))

    yield TestClient(create_app(tmp_path / "runs", read_only=False))
    reset()


def _run_id(api: TestClient) -> str:
    return api.get("/api/runs").json()[0]["id"]


def test_the_whole_gate_over_http(api: TestClient):
    run_id = _run_id(api)
    preview = api.get(f"/api/runs/{run_id}/preview").json()
    assert preview["recipient"] == "careers@annova.com"
    assert preview["confirm_token"]

    approved = api.post(f"/api/runs/{run_id}/decision", json={
        "decision": "approve",
        "recipient": preview["recipient"],
        "subject": preview["subject"],
        "body": preview["body"],
        "confirm_token": preview["confirm_token"],
    })
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["may_send"] is True
    assert api.get(f"/api/runs/{run_id}").json()["status"] == "approved"


def test_correcting_the_address_at_the_gate_is_allowed(api: TestClient):
    """The recipient field is editable because postings put the wrong address in
    as often as the right one. Refusing an edit here was the bug, not the
    feature."""
    run_id = _run_id(api)
    preview = api.get(f"/api/runs/{run_id}/preview").json()
    response = api.post(f"/api/runs/{run_id}/decision", json={
        "decision": "approve", "recipient": "hiring.manager@annova.com",
        "subject": preview["subject"], "body": preview["body"],
        "confirm_token": preview["confirm_token"],
    })
    assert response.status_code == 200
    ready = api.get(f"/api/runs/{run_id}/approved").json()
    assert ready["recipient"] == "hiring.manager@annova.com"


def test_an_approval_carrying_a_forged_token_is_409(api: TestClient):
    run_id = _run_id(api)
    api.get(f"/api/runs/{run_id}/preview")
    response = api.post(f"/api/runs/{run_id}/decision", json={
        "decision": "approve", "recipient": "a@b.com", "subject": "S",
        "body": "B", "confirm_token": "v1." + "0" * 64,
    })
    assert response.status_code == 409
    assert api.get(f"/api/runs/{run_id}").json()["status"] == "needs_review"


def test_approving_with_no_token_at_all_is_409(api: TestClient):
    run_id = _run_id(api)
    response = api.post(f"/api/runs/{run_id}/decision",
                        json={"decision": "approve", "recipient": "a@b.com"})
    assert response.status_code == 409


def test_rejecting_over_http_keeps_the_download(api: TestClient):
    run_id = _run_id(api)
    assert api.post(f"/api/runs/{run_id}/decision",
                    json={"decision": "reject"}).status_code == 200
    body = api.get(f"/api/runs/{run_id}").json()
    assert body["status"] == "rejected"
    assert body["can_download"] is True
    assert body["may_send"] is False
    assert api.get(f"/api/runs/{run_id}/file/resume_docx").status_code == 200


def test_previewing_a_decided_run_is_409(api: TestClient):
    run_id = _run_id(api)
    api.post(f"/api/runs/{run_id}/decision", json={"decision": "reject"})
    assert api.get(f"/api/runs/{run_id}/preview").status_code == 409


def test_another_users_run_cannot_be_decided(api: TestClient):
    with session_scope() as s:
        other = models.User(google_sub="else", email="b@example.com")
        s.add(other)
        s.flush()
        resume = s.query(models.Resume).first()
        run = models.Run(user_id=other.id, resume_id=resume.id,
                         status="needs_review", outreach_json=dict(DRAFT))
        s.add(run)
        s.flush()
        theirs = run.id

    assert api.get(f"/api/runs/{theirs}/preview").status_code == 404
    assert api.post(f"/api/runs/{theirs}/decision",
                    json={"decision": "reject"}).status_code == 404


def test_without_a_secret_the_gate_refuses_rather_than_signing_nothing(
        api: TestClient, monkeypatch):
    monkeypatch.setenv("CONFIRM_TOKEN_SECRET", "")
    response = api.get(f"/api/runs/{_run_id(api)}/preview")
    assert response.status_code == 503
    assert "CONFIRM_TOKEN_SECRET" in response.json()["detail"]


# ══ the token expires ═════════════════════════════════════════════════

def test_a_token_stops_working_after_its_ttl():
    """A preview left open over a weekend should not be approvable on Monday
    against a draft whose run has since been re-executed."""
    token = confirm.issue(SECRET, "run1", "a@b.com", "S", "B",
                          ttl_seconds=3600, now=0)
    assert confirm.matches(SECRET, token, "run1", "a@b.com", "S", "B", now=3599)
    assert not confirm.matches(SECRET, token, "run1", "a@b.com", "S", "B",
                               now=3601)


def test_the_expiry_cannot_be_extended_by_editing_the_token():
    """It is inside the signed payload. An expiry beside a signature is a
    number the client edits."""
    token = confirm.issue(SECRET, "run1", "a@b.com", "S", "B",
                          ttl_seconds=60, now=0)
    version, expiry, mac = token.split(".")
    stretched = ".".join([version, str(int(expiry) + 100_000), mac])
    assert not confirm.matches(SECRET, stretched, "run1", "a@b.com", "S", "B",
                               now=100)


def test_a_forged_token_and_an_expired_one_are_indistinguishable():
    """The signature is checked before the expiry, so nothing is leaked about
    which half an attacker got right."""
    expired = confirm.issue(SECRET, "r", "a@b.com", "S", "B",
                            ttl_seconds=1, now=0)
    forged = confirm.issue("wrong-secret", "r", "a@b.com", "S", "B")
    assert confirm.matches(SECRET, expired, "r", "a@b.com", "S", "B",
                           now=10_000) is False
    assert confirm.matches(SECRET, forged, "r", "a@b.com", "S", "B") is False


def test_the_preview_says_when_it_expires(db):
    """So the screen can warn before it refuses, not after someone has spent
    ten minutes rewriting the letter."""
    import time

    with session_scope() as s:
        body = service.preview(s, _run(s), SECRET)
    assert body["confirm_token_expires_at"] > time.time()
    assert body["confirm_token_expires_at"] == confirm.expires_at(
        body["confirm_token"])


def test_an_expired_preview_cannot_be_approved(db, monkeypatch):
    with session_scope() as s:
        run = _run(s)
        token = confirm.issue(SECRET, run.id, DRAFT["recipient"],
                              DRAFT["subject"], DRAFT["body"],
                              ttl_seconds=1, now=0)
        with pytest.raises(service.StaleDecision):
            service.decide(s, run, decision="approve", secret=SECRET,
                           recipient=DRAFT["recipient"],
                           subject=DRAFT["subject"], body=DRAFT["body"],
                           confirm_token=token)
        assert run.status == "needs_review"
