"""The Gmail backend, with no Gmail.

`httpx.post` is replaced, so these assert on the request that would have gone out
and on how each answer is classified. The classification is the point: a failure
that means *nothing was delivered* returns the run to `approved` for a retry, and
a failure that means *we do not know* must not be retried by a machine. Getting
that backwards either charges a full re-run for a rate limit or sends a second
copy of an email that already arrived.
"""
from __future__ import annotations

import base64
import email
from email import policy

import httpx
import pytest

from app.io.mail.base import Attachment, OutgoingMessage, SendFailed
from app.io.mail.gmail import MAX_RAW_BYTES, SEND_ENDPOINT, GmailSender

TOKEN = "ya29.a-fake-access-token"


def _message(**over) -> OutgoingMessage:
    fields = dict(sender="a.morgan@example.com", sender_name="A. Morgan",
                  recipient="careers@annova.ai", subject="AI Engineer",
                  body="Hello.", attachments=[Attachment("cv.pdf", b"%PDF-1.4")])
    fields.update(over)
    return OutgoingMessage(**fields)


class _Recorder:
    """Stands in for `httpx.post`, answering with a fixed response."""

    def __init__(self, status: int = 200, json_body: dict | None = None,
                 text: str = "", raises: Exception | None = None) -> None:
        self.status, self.json_body = status, json_body
        self.text, self.raises = text, raises
        self.calls: list[dict] = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.raises is not None:
            raise self.raises
        request = httpx.Request("POST", url)
        if self.json_body is not None:
            return httpx.Response(self.status, json=self.json_body,
                                  request=request)
        return httpx.Response(self.status, text=self.text, request=request)


@pytest.fixture
def post(monkeypatch):
    def install(recorder: _Recorder) -> _Recorder:
        monkeypatch.setattr(httpx, "post", recorder)
        return recorder
    return install


# ══ the request ═══════════════════════════════════════════════════════

def test_the_message_is_posted_as_base64url_to_the_send_endpoint(post):
    recorder = post(_Recorder(json_body={"id": "18f2c", "threadId": "18f2c"}))
    assert GmailSender(TOKEN).send(_message()) == "18f2c"

    call = recorder.calls[0]
    assert call["url"] == SEND_ENDPOINT
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"

    raw = base64.urlsafe_b64decode(call["json"]["raw"])
    parsed = email.message_from_bytes(raw, policy=policy.default)
    assert parsed["To"] == "careers@annova.ai"
    assert [p.get_filename() for p in parsed.iter_attachments()] == ["cv.pdf"]


def test_a_sender_cannot_be_built_without_a_token(post):
    """A sender that can be constructed without a credential is one that gets
    constructed without a credential."""
    with pytest.raises(SendFailed, match="connect Gmail"):
        GmailSender("")


def test_a_message_over_gmails_limit_is_refused_before_the_request(post):
    """Discovering this as a 413 would mean telling the person after they pressed
    send; the fix — a smaller PDF — is something to say before."""
    recorder = post(_Recorder())
    huge = _message(attachments=[Attachment("cv.pdf",
                                            b"x" * (MAX_RAW_BYTES + 1))])
    with pytest.raises(SendFailed, match="over Gmail's"):
        GmailSender(TOKEN).send(huge)
    assert recorder.calls == []


def test_an_acceptance_with_no_id_is_not_reported_as_an_id(post):
    """The mail has gone; the provider reference has not. The audit must not
    claim an id it does not have."""
    post(_Recorder(json_body={}))
    assert GmailSender(TOKEN).send(_message()) == "gmail-accepted-no-id"


# ══ failures that mean nothing was delivered ══════════════════════════

def test_expired_credentials_say_to_reconnect(post):
    post(_Recorder(403, json_body={"error": {
        "code": 403,
        "message": "Request had insufficient authentication scopes."}}))
    with pytest.raises(SendFailed, match="reconnect Gmail"):
        GmailSender(TOKEN).send(_message())


def test_the_providers_own_words_survive_into_the_error(post):
    """A person who reads "403" learns nothing; a person who reads the message
    knows what to do."""
    post(_Recorder(403, json_body={"error": {
        "message": "Request had insufficient authentication scopes."}}))
    with pytest.raises(SendFailed, match="insufficient authentication scopes"):
        GmailSender(TOKEN).send(_message())


def test_rate_limiting_says_the_approval_still_stands(post):
    post(_Recorder(429, json_body={"error": {
        "message": "User-rate limit exceeded."}}))
    with pytest.raises(SendFailed, match="approval still stands"):
        GmailSender(TOKEN).send(_message())


def test_an_error_body_that_is_not_json_still_produces_a_message(post):
    post(_Recorder(500, text="<html>Internal Server Error</html>"))
    with pytest.raises(SendFailed, match="500"):
        GmailSender(TOKEN).send(_message())


@pytest.mark.parametrize("failure", [
    httpx.ConnectError("no route to host"),
    httpx.ConnectTimeout("timed out connecting"),
])
def test_never_reaching_google_is_a_refusal_and_therefore_retryable(post,
                                                                   failure):
    """The request never arrived, so nothing was delivered — and `SendFailed` is
    what returns the run to `approved`."""
    post(_Recorder(raises=failure))
    with pytest.raises(SendFailed, match="could not reach Gmail"):
        GmailSender(TOKEN).send(_message())


# ══ failures that mean we do not know ═════════════════════════════════

@pytest.mark.parametrize("failure", [
    httpx.ReadTimeout("timed out reading"),
    httpx.RemoteProtocolError("server disconnected"),
])
def test_losing_the_answer_is_not_dressed_up_as_a_refusal(post, failure):
    """★ The message may have been accepted and the reply lost. Raising
    `SendFailed` here would tell `pipeline.send` that nothing was delivered, and
    a machine would retry — a second copy of an email the recruiter already has.
    So these propagate as themselves and the run fails for a person to look at."""
    post(_Recorder(raises=failure))
    with pytest.raises(type(failure)):
        GmailSender(TOKEN).send(_message())


def test_the_two_failure_kinds_do_not_overlap():
    """Stated as a test because the distinction lives in an except-clause order,
    which is exactly the kind of thing a later edit reorders by accident."""
    assert not issubclass(httpx.ReadTimeout, httpx.ConnectTimeout)
    assert not issubclass(httpx.RemoteProtocolError, httpx.ConnectError)
