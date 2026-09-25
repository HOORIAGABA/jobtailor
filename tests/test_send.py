"""★ S11 — the only irreversible stage, tested without sending anything.

Every test here asserts against a real RFC-5322 message and puts nothing on the
wire. That property is the whole reason `ConsoleSender` exists: the dangerous
stage gets the same "runs in CI with no credentials" guarantee as the rest of
the project.

The order of the five steps is the design, so the order is what is tested —
particularly that the audit row exists *before* the provider is touched, which
is the only way a send that fails halfway leaves a trace.
"""
from __future__ import annotations

import email
from email import policy
from email.utils import parseaddr

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.db import models
from app.db.session import create_all, engine, reset, session_scope
from app.domain.errors import IllegalTransition, UserError
from app.engine import confirm
from app.io.mail import ConsoleSender, UnknownSender, sender_for
from app.io.mail.base import Attachment, OutgoingMessage, SendFailed
from app.pipeline import gate
from app.pipeline import send as service

SECRET = "a-long-random-development-secret"
TO = "careers@annova.com"
SUBJECT = "AI Engineer - A. Morgan"
BODY = "I build backend services in Python and FastAPI.\n\nA. Morgan"

DOCX = b"PK\x03\x04docx-bytes"
PDF = b"%PDF-1.4 pdf-bytes"


def _message(**over) -> OutgoingMessage:
    fields = dict(sender="a.morgan@example.com", sender_name="A. Morgan",
                  recipient=TO, subject=SUBJECT, body=BODY,
                  attachments=[Attachment("cv.docx", DOCX),
                               Attachment("cv.pdf", PDF)])
    fields.update(over)
    return OutgoingMessage(**fields)


def _parse(raw: bytes):
    return email.message_from_bytes(raw, policy=policy.default)


# ══ the message ═══════════════════════════════════════════════════════

def test_the_attachment_content_type_comes_from_its_filename():
    """Hardcoding `application/pdf` is how a `.docx` arrives that the
    recipient cannot open, and the person who finds out is the recruiter."""
    parsed = _parse(_message().to_mime().as_bytes())
    by_name = {p.get_filename(): p.get_content_type()
               for p in parsed.iter_attachments()}
    assert by_name["cv.docx"] == (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document")
    assert by_name["cv.pdf"] == "application/pdf"


def test_an_unknown_extension_falls_back_rather_than_guessing():
    parsed = _parse(_message(
        attachments=[Attachment("notes.xyz", b"data")]).to_mime().as_bytes())
    assert [p.get_content_type() for p in parsed.iter_attachments()] == [
        "application/octet-stream"]


def test_the_body_is_plain_text_with_no_html_part():
    """A cold application email has no formatting worth the risk: an HTML part
    is one more thing to render wrong."""
    parsed = _parse(_message(attachments=[]).to_mime().as_bytes())
    assert parsed.get_content_type() == "text/plain"
    assert "<html" not in _message().to_mime().as_string().lower()


def test_the_message_comes_from_the_candidate_not_a_service():
    """A job application has to come from the person signing it: otherwise the
    recruiter's reply goes to a service and there is no copy in the candidate's
    own Sent folder to follow up from."""
    parsed = _parse(_message().to_mime().as_bytes())
    # Compared through the parser rather than as a string: a display name with
    # a full stop in it is legally quoted (`"A. Morgan" <...>`), and asserting
    # on the raw header would be asserting on a quoting rule, not on the From.
    assert parseaddr(parsed["From"]) == ("A. Morgan", "a.morgan@example.com")
    assert parsed["To"] == TO
    assert parsed["Subject"] == SUBJECT
    assert parsed["Message-ID"] and parsed["Date"]


def test_the_message_id_carries_the_senders_domain_not_the_machines():
    """`make_msgid()` with no argument uses the local hostname, which here is
    `localhost` or a container id — and `<...@localhost>` is a spam signal some
    receivers reject. Found in the console demo, on a run that looked fine."""
    parsed = _parse(_message().to_mime().as_bytes())
    assert parsed["Message-ID"].endswith("@example.com>")
    assert "localhost" not in parsed["Message-ID"]


def test_a_senderless_address_still_does_not_produce_localhost():
    parsed = _parse(_message(sender="nobody").to_mime().as_bytes())
    assert "localhost" not in parsed["Message-ID"]


def test_a_nameless_sender_is_a_bare_address_not_an_empty_display_name():
    parsed = _parse(_message(sender_name="").to_mime().as_bytes())
    assert parsed["From"] == "a.morgan@example.com"
    assert parseaddr(parsed["From"]) == ("", "a.morgan@example.com")


def test_the_hashes_move_when_anything_they_cover_moves():
    base = _message()
    assert base.body_hash() != _message(body=BODY + " ").body_hash()
    assert base.subject_hash() != _message(subject="Edited").subject_hash()
    assert base.attachment_hash() != _message(
        attachments=[Attachment("cv.docx", DOCX)]).attachment_hash()
    # Order is part of the identity: the same files in the other order is a
    # different envelope.
    assert base.attachment_hash() != _message(
        attachments=[Attachment("cv.pdf", PDF),
                     Attachment("cv.docx", DOCX)]).attachment_hash()


def test_renaming_a_file_changes_the_attachment_hash():
    assert (_message(attachments=[Attachment("cv.docx", DOCX)]).attachment_hash()
            != _message(
                attachments=[Attachment("resume.docx", DOCX)]).attachment_hash())


# ══ choosing a backend ════════════════════════════════════════════════

def test_the_default_backend_is_the_one_that_cannot_send():
    """A misconfiguration should mean "nothing happened", never "something went
    to a stranger"."""
    assert sender_for("").name == "console"
    assert sender_for("console").name == "console"


def test_a_misspelled_provider_is_refused_rather_than_defaulted():
    """Falling back to `console` on a typo would leave the person believing
    they had sent something."""
    with pytest.raises(UnknownSender, match="unknown mail provider"):
        sender_for("consle")


def test_a_planned_backend_says_it_is_not_built_yet():
    """`gmail_smtp` stays listed and unbuilt: an app password in a file works on
    a laptop and fails on every free host that blocks outbound 587."""
    with pytest.raises(UnknownSender, match="not built yet"):
        sender_for("gmail_smtp")


def test_the_gmail_backend_refuses_to_exist_without_a_token():
    """A sender that can be constructed without a credential is one that gets
    constructed without a credential."""
    with pytest.raises(SendFailed, match="connect Gmail"):
        sender_for("gmail_api")
    assert sender_for("gmail_api", access_token="ya29.x").name == "gmail_api"


def test_the_console_sender_returns_the_real_bytes_and_dispatches_nothing():
    sender = ConsoleSender()
    message = _message()
    provider_id = sender.send(message)

    assert provider_id.startswith("console-")
    assert len(sender.sent) == 1
    raw = sender.sent[0][1]
    assert _parse(raw)["To"] == TO
    assert len(list(_parse(raw).iter_attachments())) == 2


# ══ the five steps ════════════════════════════════════════════════════

@pytest.fixture
def db(tmp_path, monkeypatch):
    url = f"sqlite+pysqlite:///{tmp_path / 'send.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    reset(url)
    create_all(engine())
    yield
    reset()


def _approved(session, *, status: str = "approved",
              attachments: bool = True) -> models.Run:
    """A run that has been through the gate, with a message to send."""
    user = session.query(models.User).first()
    if user is None:
        user = models.User(google_sub="s", email="a.morgan@example.com",
                           name="A. Morgan")
        session.add(user)
        session.flush()
    resume = session.query(models.Resume).first()
    if resume is None:
        resume = models.Resume(user_id=user.id, filename="cv.pdf",
                               file_sha256="a" * 64, status="confirmed")
        session.add(resume)
        session.flush()
    run = models.Run(user_id=user.id, resume_id=resume.id, status=status)
    session.add(run)
    session.flush()
    session.add(models.Message(run_id=run.id, recipient=TO, subject=SUBJECT,
                               body=BODY))
    if attachments:
        session.add(models.Artifact(
            run_id=run.id, stage="resume_docx", filename="cv.docx",
            content_type="application/x", bytes=len(DOCX), blob=DOCX))
        session.add(models.Artifact(
            run_id=run.id, stage="resume_pdf", filename="cv.pdf",
            content_type="application/pdf", bytes=len(PDF), blob=PDF))
        # A stage that is NOT in ATTACH_STAGES, to prove the envelope is a
        # deliberate list rather than "everything on the run".
        session.add(models.Artifact(
            run_id=run.id, stage="diff_json", filename="diff.json",
            content_type="application/json", bytes=2, blob=b"{}"))
    session.flush()
    return run


def _token(run: models.Run) -> str:
    return confirm.issue(SECRET, run.id, TO, SUBJECT, BODY)


def _send(session, run, sender=None, **over):
    fields = dict(secret=SECRET, recipient=TO, subject=SUBJECT, body=BODY,
                  confirm_token=_token(run))
    fields.update(over)
    user = session.query(models.User).first()
    return service.send(session, run, user, sender or ConsoleSender(), **fields)


def _audit(session, run) -> list[tuple[str, str]]:
    message = gate.message_of(session, run)
    rows = (session.query(models.SendAudit)
            .filter(models.SendAudit.message_id == message.id)
            .order_by(models.SendAudit.attempted_at,
                      models.SendAudit.id).all())
    return [(r.outcome, r.recipient) for r in rows]


def test_a_send_closes_the_run_and_records_who_carried_it(db):
    with session_scope() as s:
        run = _approved(s)
        sender = ConsoleSender()
        out = _send(s, run, sender)

        assert run.status == "sent"
        assert out["provider"] == "console"
        assert out["provider_message_id"].startswith("console-")
        assert out["attachments"] == ["cv.docx", "cv.pdf"]
        # The honest field: nothing left the machine, and the API says so.
        assert out["dispatched"] is False

        message = gate.message_of(s, run)
        assert message.sent_at is not None
        assert message.provider == "console"
        assert len(sender.sent) == 1


def test_only_the_two_resume_renderings_go_in_the_envelope(db):
    """`.docx` for the ATS and `.pdf` for the human — a deliberate list, not
    every artifact the run happens to hold."""
    with session_scope() as s:
        run = _approved(s)
        sender = ConsoleSender()
        _send(s, run, sender)
        names = [p.get_filename() for p in _parse(sender.sent[0][1])
                 .iter_attachments()]
        assert names == ["cv.docx", "cv.pdf"]


def test_a_run_with_nothing_rendered_still_sends_the_letter(db):
    """A missing attachment is a worse email, not a reason to refuse one."""
    with session_scope() as s:
        run = _approved(s, attachments=False)
        out = _send(s, run)
        assert out["attachments"] == []
        assert run.status == "sent"


def test_the_sent_message_is_kept_as_an_artifact(db):
    """The record of what was actually sent, not a reconstruction of it."""
    with session_scope() as s:
        run = _approved(s)
        _send(s, run)
        stored = (s.query(models.Artifact)
                  .filter(models.Artifact.run_id == run.id,
                          models.Artifact.stage == service.EML_STAGE).one())
        assert stored.content_type == "message/rfc822"
        assert stored.bytes == len(stored.blob)
        assert _parse(stored.blob)["To"] == TO


# ── step 1: twice is a refusal, not a second email ────────────────────

def test_sending_twice_is_refused_by_the_column_not_by_luck(db):
    """A double-clicked button, a retried request and a resumed worker must all
    be unable to send twice — and a lock does not survive the process."""
    with session_scope() as s:
        run = _approved(s)
        sender = ConsoleSender()
        _send(s, run, sender)

        with pytest.raises(service.AlreadySent, match="already sent"):
            _send(s, run, sender)
        assert len(sender.sent) == 1


def test_the_idempotency_guard_is_checked_before_the_signature(db):
    """Order matters: a stale token on an already-sent run should report the
    send, not the token, because that is the question the person is asking."""
    with session_scope() as s:
        run = _approved(s)
        _send(s, run)
        with pytest.raises(service.AlreadySent):
            _send(s, run, confirm_token="garbage")


def test_a_run_that_was_never_approved_cannot_be_sent(db):
    for status in ("needs_review", "rejected", "tailoring", "created"):
        with session_scope() as s:
            run = _approved(s, status=status)
            with pytest.raises(IllegalTransition, match="only an approved run"):
                _send(s, run)
            assert run.status == status


# ── step 2: the token covers what is about to go out ──────────────────

def test_a_send_that_does_not_match_the_approval_is_refused(db):
    """Reading the message from the row would mean the value approved and the
    value sent are only *probably* the same."""
    with session_scope() as s:
        run = _approved(s)
        sender = ConsoleSender()
        with pytest.raises(UserError, match="does not match"):
            _send(s, run, sender, recipient="attacker@example.com",
                  confirm_token=_token(run))
        assert sender.sent == []
        assert run.status == "approved"
        assert gate.message_of(s, run).sent_at is None


def test_a_refused_send_writes_no_audit_row(db):
    """Nothing was attempted, so nothing is recorded. An audit that logs
    rejected requests as attempts is an audit nobody can read."""
    with session_scope() as s:
        run = _approved(s)
        with pytest.raises(UserError):
            _send(s, run, confirm_token="garbage")
        assert _audit(s, run) == []


# ── step 3: the audit exists before the provider is touched ───────────

class _Declines:
    """A provider that answers, and the answer is no. Nothing was delivered."""
    name = "declines"

    def __init__(self) -> None:
        self.calls = 0

    def send(self, message: OutgoingMessage) -> str:
        self.calls += 1
        raise SendFailed("550 mailbox unavailable")


class _Vanishes:
    """A provider that never answers. Delivery is unknown."""
    name = "vanishes"

    def __init__(self) -> None:
        self.calls = 0

    def send(self, message: OutgoingMessage) -> str:
        self.calls += 1
        raise TimeoutError("read timed out")


def test_a_refusal_leaves_the_approval_standing_so_it_can_be_retried(db):
    """The provider said no and delivered nothing. Sending the run back to
    `failed` — whose only exit is `tailoring` — would charge seven model calls
    for a rate limit."""
    with session_scope() as s:
        run = _approved(s)
        sender = _Declines()
        with pytest.raises(SendFailed, match="mailbox unavailable"):
            _send(s, run, sender)

        assert sender.calls == 1
        assert _audit(s, run) == [("attempted", TO), ("refused", TO)]
        assert run.status == "approved"
        assert "SendFailed" in run.error
        assert gate.message_of(s, run).sent_at is None

        # And the retry works, with the same token, once the cause is fixed.
        good = ConsoleSender()
        assert _send(s, run, good)["status"] == "sent"
        assert len(good.sent) == 1


def test_a_send_with_no_answer_is_not_retried_by_a_machine(db):
    """The recruiter may have the email and this database may not know it.
    Recording that as a refusal would be a guess dressed as a fact."""
    with session_scope() as s:
        run = _approved(s)
        sender = _Vanishes()
        with pytest.raises(SendFailed, match="unknown"):
            _send(s, run, sender)

        assert _audit(s, run) == [("attempted", TO), ("unknown", TO)]
        assert run.status == "failed"
        assert "TimeoutError" in run.error
        # Not marked sent — but `failed` is not sendable, so a person has to
        # look before anything happens again.
        assert gate.message_of(s, run).sent_at is None
        with pytest.raises(IllegalTransition):
            _send(s, run)


def test_a_successful_send_leaves_both_rows_in_order(db):
    with session_scope() as s:
        run = _approved(s)
        _send(s, run)
        assert _audit(s, run) == [("attempted", TO), ("sent", TO)]


def test_the_audit_stores_hashes_not_the_message(db):
    """The audit answers "was this sent, to whom, when, and was it the approved
    text" — and does not need a second copy of the body to do it."""
    with session_scope() as s:
        run = _approved(s)
        _send(s, run)
        rows = (s.query(models.SendAudit)
                .order_by(models.SendAudit.attempted_at).all())
        expected = _message(attachments=[Attachment("cv.docx", DOCX),
                                         Attachment("cv.pdf", PDF)])
        for row in rows:
            assert row.body_hash == expected.body_hash()
            assert row.subject_hash == expected.subject_hash()
            assert row.attachment_hash == expected.attachment_hash()
            assert BODY not in (row.detail or "")


def test_an_edited_approval_is_what_gets_sent(db):
    """The person edited the draft at the gate; the envelope carries the edit,
    and the token is what proves the two agree."""
    edited = "Short version. Happy to talk this week."
    with session_scope() as s:
        run = _approved(s)
        sender = ConsoleSender()
        _send(s, run, sender, recipient="hiring@annova.com", body=edited,
              confirm_token=confirm.issue(SECRET, run.id, "hiring@annova.com",
                                          SUBJECT, edited))
        parsed = _parse(sender.sent[0][1])
        assert parsed["To"] == "hiring@annova.com"
        assert edited in parsed.get_body().get_content()


# ── the preview ───────────────────────────────────────────────────────

def test_the_eml_preview_produces_the_message_without_sending_it(db):
    with session_scope() as s:
        run = _approved(s)
        user = s.query(models.User).first()
        raw = service.preview_eml(s, run, user, recipient=TO, subject=SUBJECT,
                                  body=BODY)
        assert _parse(raw)["To"] == TO
        assert len(list(_parse(raw).iter_attachments())) == 2
        assert gate.message_of(s, run).sent_at is None
        assert run.status == "approved"


# ══ over HTTP ═════════════════════════════════════════════════════════

DRAFT = {"recipient": TO, "subject": SUBJECT, "body": BODY, "cites": [],
         "problems": []}


@pytest.fixture
def api(tmp_path, monkeypatch) -> TestClient:
    url = f"sqlite+pysqlite:///{tmp_path / 'send_api.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DEV_USER_EMAIL", "dev@example.com")
    monkeypatch.setenv("CONFIRM_TOKEN_SECRET", SECRET)
    monkeypatch.setenv("MAIL_PROVIDER", "console")
    reset(url)
    create_all(engine())

    with session_scope() as s:
        user = models.User(google_sub="dev-local", email="dev@example.com",
                           name="Dev User")
        s.add(user)
        s.flush()
        resume = models.Resume(user_id=user.id, filename="cv.pdf",
                               file_sha256="b" * 64, status="confirmed")
        s.add(resume)
        s.flush()
        run = models.Run(user_id=user.id, resume_id=resume.id,
                         status="needs_review", outreach_json=dict(DRAFT),
                         accepted_json=[], diff_json={"changes": [], "gaps": []})
        s.add(run)
        s.flush()
        s.add(models.Artifact(run_id=run.id, stage="resume_docx",
                              filename="cv.docx",
                              content_type="application/x",
                              bytes=len(DOCX), blob=DOCX))

    yield TestClient(create_app(tmp_path / "runs", read_only=False))
    reset()


def _approve(api: TestClient) -> tuple[str, dict]:
    run_id = api.get("/api/runs").json()[0]["id"]
    preview = api.get(f"/api/runs/{run_id}/preview").json()
    body = {"recipient": preview["recipient"], "subject": preview["subject"],
            "body": preview["body"],
            "confirm_token": preview["confirm_token"]}
    assert api.post(f"/api/runs/{run_id}/decision",
                    json={"decision": "approve", **body}).status_code == 200
    return run_id, body


def test_gate_then_send_over_http(api: TestClient):
    run_id, approved = _approve(api)
    response = api.post(f"/api/runs/{run_id}/send", json=approved)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "sent"
    assert payload["provider"] == "console"
    assert payload["dispatched"] is False
    assert api.get(f"/api/runs/{run_id}").json()["status"] == "sent"


def test_sending_the_same_run_twice_over_http_is_409(api: TestClient):
    run_id, approved = _approve(api)
    assert api.post(f"/api/runs/{run_id}/send", json=approved).status_code == 200
    second = api.post(f"/api/runs/{run_id}/send", json=approved)
    assert second.status_code == 409
    assert "already sent" in second.json()["detail"]


def test_sending_something_other_than_what_was_approved_is_400(api: TestClient):
    run_id, approved = _approve(api)
    response = api.post(f"/api/runs/{run_id}/send",
                        json={**approved, "recipient": "attacker@example.com"})
    assert response.status_code == 400
    assert api.get(f"/api/runs/{run_id}").json()["status"] == "approved"


def test_sending_a_run_that_was_not_approved_is_409(api: TestClient):
    run_id = api.get("/api/runs").json()[0]["id"]
    response = api.post(f"/api/runs/{run_id}/send",
                        json={"recipient": TO, "subject": SUBJECT,
                              "body": BODY, "confirm_token": "x"})
    assert response.status_code == 409


def test_the_eml_is_downloadable_after_approval(api: TestClient):
    run_id, _ = _approve(api)
    response = api.get(f"/api/runs/{run_id}/eml")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("message/rfc822")
    assert run_id in response.headers["content-disposition"]
    assert _parse(response.content)["To"] == TO


def test_there_is_no_eml_before_anything_is_approved(api: TestClient):
    run_id = api.get("/api/runs").json()[0]["id"]
    assert api.get(f"/api/runs/{run_id}/eml").status_code == 409


def test_another_users_run_cannot_be_sent(api: TestClient):
    with session_scope() as s:
        other = models.User(google_sub="else", email="b@example.com")
        s.add(other)
        s.flush()
        resume = s.query(models.Resume).first()
        run = models.Run(user_id=other.id, resume_id=resume.id,
                         status="approved")
        s.add(run)
        s.flush()
        s.add(models.Message(run_id=run.id, recipient=TO, subject=SUBJECT,
                             body=BODY))
        theirs = run.id

    # 404, never 403 — a 403 confirms the row exists.
    assert api.post(f"/api/runs/{theirs}/send",
                    json={"recipient": TO, "subject": SUBJECT, "body": BODY,
                          "confirm_token": "x"}).status_code == 404
    assert api.get(f"/api/runs/{theirs}/eml").status_code == 404


def test_without_a_secret_sending_is_refused_rather_than_unchecked(
        api: TestClient, monkeypatch):
    run_id, approved = _approve(api)
    monkeypatch.setenv("CONFIRM_TOKEN_SECRET", "")
    response = api.post(f"/api/runs/{run_id}/send", json=approved)
    assert response.status_code == 503
    assert api.get(f"/api/runs/{run_id}").json()["status"] == "approved"


def test_a_misspelled_mail_provider_is_503_not_a_silent_console_send(
        api: TestClient, monkeypatch):
    run_id, approved = _approve(api)
    monkeypatch.setenv("MAIL_PROVIDER", "gmial_api")
    response = api.post(f"/api/runs/{run_id}/send", json=approved)
    assert response.status_code == 503
    assert "unknown mail provider" in response.json()["detail"]
    assert api.get(f"/api/runs/{run_id}").json()["status"] == "approved"


def test_choosing_gmail_without_connecting_it_is_428_not_a_failed_run(
        api: TestClient, monkeypatch):
    """428 Precondition Required: the request is fine and the person has to do
    something first. A 400 would read as "your request was wrong" and a 401 as
    "sign in again", and both would send the UI to the wrong screen."""
    run_id, approved = _approve(api)
    monkeypatch.setenv("MAIL_PROVIDER", "gmail_api")
    response = api.post(f"/api/runs/{run_id}/send", json=approved)
    assert response.status_code == 428
    assert "approval step" in response.json()["detail"]
    # Nothing was attempted, so the approval survives for a retry after
    # connecting.
    assert api.get(f"/api/runs/{run_id}").json()["status"] == "approved"


def test_the_instance_says_which_backend_it_would_use(api: TestClient):
    body = api.get("/api/capabilities").json()
    assert "sends nothing" in body["mail_provider"]
    assert body["stages_not_built"] == []


# ══ the whole path, with Gmail selected and connected ═════════════════

def _connect_gmail(key: str) -> None:
    """Give the signed-in user a Gmail grant, as the OAuth callback would."""
    from datetime import datetime, timedelta, timezone

    from app.engine import secrets as crypto

    with session_scope() as s:
        user = s.query(models.User).first()
        s.add(models.OAuthToken(
            user_id=user.id, provider="google",
            scopes="openid email profile "
                   "https://www.googleapis.com/auth/gmail.send",
            refresh_token_encrypted=crypto.encrypt(key, "1//refresh"),
            access_token_encrypted=crypto.encrypt(key, "ya29.live"),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))


def test_gate_then_gmail_send_over_http(api: TestClient, monkeypatch):
    """The full irreversible path, with the one irreversible call replaced.

    What this proves is the wiring: the access token is minted from the stored
    grant, handed to the Gmail backend, and the provider's own id is what lands
    in the message row — not a console placeholder.
    """
    import httpx

    from app.engine import secrets as crypto

    key = crypto.new_key()
    monkeypatch.setenv("FERNET_KEY", key)
    monkeypatch.setenv("MAIL_PROVIDER", "gmail_api")
    _connect_gmail(key)

    sent: list[dict] = []

    def fake_post(url, **kwargs):
        sent.append({"url": url, **kwargs})
        return httpx.Response(200, json={"id": "18f2cafe"},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    run_id, approved = _approve(api)
    payload = api.post(f"/api/runs/{run_id}/send", json=approved).json()

    assert payload["status"] == "sent"
    assert payload["provider"] == "gmail_api"
    assert payload["provider_message_id"] == "18f2cafe"
    # The honest field, on the backend that really does put mail on the wire.
    assert payload["dispatched"] is True

    # The live access token was used, not the refresh token and not a placeholder.
    assert sent[0]["headers"]["Authorization"] == "Bearer ya29.live"

    with session_scope() as s:
        run = s.get(models.Run, run_id)
        message = gate.message_of(s, run)
        assert message.provider == "gmail_api"
        assert message.provider_message_id == "18f2cafe"
        assert _audit(s, run) == [("attempted", TO), ("sent", TO)]


def test_a_gmail_refusal_leaves_the_approval_standing_over_http(api: TestClient,
                                                               monkeypatch):
    import httpx

    from app.engine import secrets as crypto

    key = crypto.new_key()
    monkeypatch.setenv("FERNET_KEY", key)
    monkeypatch.setenv("MAIL_PROVIDER", "gmail_api")
    _connect_gmail(key)

    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(
        429, json={"error": {"message": "User-rate limit exceeded."}},
        request=httpx.Request("POST", url)))

    run_id, approved = _approve(api)
    response = api.post(f"/api/runs/{run_id}/send", json=approved)

    # 502: this instance behaved correctly and the provider did not.
    assert response.status_code == 502
    assert "rate limiting" in response.json()["detail"]
    # And the run is sendable again the moment the limit clears.
    assert api.get(f"/api/runs/{run_id}").json()["status"] == "approved"
    assert api.post(f"/api/runs/{run_id}/send",
                    json=approved).status_code == 502


def test_a_lost_answer_from_gmail_fails_the_run_rather_than_retrying(
        api: TestClient, monkeypatch):
    """The recruiter may have the email. `failed` leads only back to `tailoring`,
    so no machine sends a second copy."""
    import httpx

    from app.engine import secrets as crypto

    key = crypto.new_key()
    monkeypatch.setenv("FERNET_KEY", key)
    monkeypatch.setenv("MAIL_PROVIDER", "gmail_api")
    _connect_gmail(key)

    def times_out(url, **kwargs):
        raise httpx.ReadTimeout("timed out reading")

    monkeypatch.setattr(httpx, "post", times_out)

    run_id, approved = _approve(api)
    response = api.post(f"/api/runs/{run_id}/send", json=approved)
    assert response.status_code == 502
    assert "unknown" in response.json()["detail"]
    assert api.get(f"/api/runs/{run_id}").json()["status"] == "failed"

    with session_scope() as s:
        run = s.get(models.Run, run_id)
        assert _audit(s, run) == [("attempted", TO), ("unknown", TO)]


# ══ surviving a page reload ═══════════════════════════════════════════

def test_the_approved_message_can_be_fetched_again_with_a_fresh_token(db):
    """A run left in `approved` with the tab closed must not be stranded.

    The token defends against a race between preview and decision, where the
    text is still moving. After the decision it is fixed in a row, so a token
    issued over that row admits exactly one message — the approved one.
    """
    with session_scope() as s:
        run = _approved(s)
        out = service.approved(s, run, secret=SECRET)
        assert out["recipient"] == TO
        assert out["attachments"] == ["cv.docx", "cv.pdf"]

        sender = ConsoleSender()
        result = _send(s, run, sender, confirm_token=out["confirm_token"])
        assert result["status"] == "sent"


def test_the_reissued_token_still_refuses_a_different_message(db):
    """Otherwise this endpoint would be a way around the whole gate."""
    with session_scope() as s:
        run = _approved(s)
        token = service.approved(s, run, secret=SECRET)["confirm_token"]
        with pytest.raises(UserError, match="does not match"):
            _send(s, run, recipient="attacker@example.com",
                  confirm_token=token)


def test_there_is_nothing_to_fetch_before_a_decision(db):
    with session_scope() as s:
        with pytest.raises(IllegalTransition):
            service.approved(s, _approved(s, status="needs_review"),
                             secret=SECRET)


def test_fetching_after_sending_reports_the_send_rather_than_a_token(db):
    with session_scope() as s:
        run = _approved(s)
        _send(s, run)
        with pytest.raises(service.AlreadySent):
            service.approved(s, run, secret=SECRET)


def test_the_approved_message_over_http_round_trips_into_a_send(api: TestClient):
    run_id, _ = _approve(api)
    fetched = api.get(f"/api/runs/{run_id}/approved").json()
    assert fetched["recipient"] == TO
    assert fetched["confirm_token"]

    # Exactly as a reloaded page would: nothing carried over from the approval.
    sent = api.post(f"/api/runs/{run_id}/send", json={
        "recipient": fetched["recipient"], "subject": fetched["subject"],
        "body": fetched["body"], "confirm_token": fetched["confirm_token"]})
    assert sent.status_code == 200
    assert api.get(f"/api/runs/{run_id}/approved").status_code == 409
