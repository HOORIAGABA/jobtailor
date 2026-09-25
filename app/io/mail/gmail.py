"""Sending through the Gmail API. One scope, one endpoint, one POST.

**Why the API and not SMTP.** Many free hosts block outbound 587 and 465
outright — Render and Vercel among them — so an SMTP sender works on a laptop
and fails silently on the only deployment that is free. The Gmail API is an
HTTPS POST to port 443, which every host allows. It also needs no password in a
file: the credential is an OAuth access token minted from a refresh token the
person can revoke from their Google account page, which is a materially better
thing to be storing.

**The message goes out from the person's own account, and Google enforces it.**
`users/me/messages/send` sends as the authenticated user, so the `From` header
cannot be spoofed — and a copy lands in their real Sent folder, which is what
makes following up three days later possible at all. A transactional provider
sending "on behalf of" gives none of that.

**Two kinds of failure, kept apart.** Anything that means *nothing was
delivered* is raised as `SendFailed`, which returns the run to `approved` so the
same approval can be retried. Anything that means *we do not know* — a read
timeout, a connection dropped mid-request — is allowed to propagate as itself, so
`pipeline.send` records it as unknown and refuses to retry automatically. Getting
this backwards would either charge a full re-run for a rate limit or send a
second copy of an email that already arrived.
"""
from __future__ import annotations

import base64
import logging

import httpx

from app.io.mail.base import OutgoingMessage, SendFailed

logger = logging.getLogger(__name__)

SEND_ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
TIMEOUT = 30.0

# Gmail's own ceiling on a single message, attachments included, after base64
# expansion (~33%). Checked here rather than discovered as a 413, because the
# fix — a smaller PDF — is something to tell the person before they press send.
MAX_RAW_BYTES = 35 * 1024 * 1024


class GmailSender:
    """Dispatches through the Gmail API as the token's owner."""

    name = "gmail_api"

    def __init__(self, access_token: str) -> None:
        if not access_token:
            raise SendFailed(
                "no Gmail access token — connect Gmail at the approval step")
        self._token = access_token

    def send(self, message: OutgoingMessage) -> str:
        raw = message.to_mime().as_bytes()
        if len(raw) > MAX_RAW_BYTES:
            raise SendFailed(
                f"the message is {len(raw) // (1024 * 1024)} MB, over Gmail's "
                f"{MAX_RAW_BYTES // (1024 * 1024)} MB limit. The attachments "
                f"are the size — a smaller PDF is the fix."
            )

        payload = {"raw": base64.urlsafe_b64encode(raw).decode()}
        try:
            response = httpx.post(
                SEND_ENDPOINT, json=payload, timeout=TIMEOUT,
                headers={"Authorization": f"Bearer {self._token}"})
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # The request never reached Google, so nothing was delivered. That
            # is a refusal, and retryable.
            raise SendFailed(f"could not reach Gmail: {exc}") from exc
        # Every other httpx error — a read timeout, a dropped connection — is
        # deliberately NOT caught. The message may have been accepted and the
        # answer lost, and `pipeline.send` is the place that knows what to do
        # with "unknown": record it and stop.

        if response.status_code >= 400:
            raise SendFailed(_why(response))

        try:
            message_id = str(response.json().get("id") or "")
        except ValueError:
            message_id = ""
        if not message_id:
            # Accepted with no id. The mail has gone; the provider reference has
            # not. Not a failure — but the audit should not claim an id it does
            # not have.
            logger.warning("Gmail accepted the message but returned no id")
            return "gmail-accepted-no-id"
        return message_id


def _why(response: httpx.Response) -> str:
    """Turn Gmail's error body into something a person can act on.

    The raw body is a nested JSON object whose useful part is one string. A
    person who reads "403" learns nothing; a person who reads "Request had
    insufficient authentication scopes" knows to reconnect.
    """
    detail = ""
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            detail = str(error.get("message") or "")
    except ValueError:
        detail = response.text[:200]

    if response.status_code in (401, 403):
        return (f"Gmail refused the credentials ({response.status_code}): "
                f"{detail or 'no detail'}. The authorisation was withdrawn or "
                f"never included the send scope — reconnect Gmail and try "
                f"again.")
    if response.status_code == 429:
        return (f"Gmail is rate limiting this account: {detail or 'no detail'}. "
                f"Nothing was sent; the approval still stands, so try again "
                f"shortly.")
    return f"Gmail refused ({response.status_code}): {detail or 'no detail'}"
