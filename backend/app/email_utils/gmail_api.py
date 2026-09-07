"""
Optional Gmail API (OAuth) sender.

This is the "no App Password needed" path the user wants:
  1. User clicks "Connect Gmail" -> redirected to Google consent with the
     `gmail.send` scope.
  2. They authorize once -> we receive and store their refresh token.
  3. On Send, we exchange the refresh token for an access token and call the
     Gmail API `users.messages.send`, so mail genuinely goes out from the
     user's own Gmail with NO password ever entered.

It needs Gmail API credentials in .env (GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET)
and the Gmail API enabled for the project. If those are not configured, or
the user has not connected Gmail yet, we fall back to SMTP + App Password.

The refresh token is stored in the user's `oauth_tokens` JSON under the
"gmail" key.
"""
import base64
import json
import logging
from email.message import EmailMessage

from fastapi import HTTPException

logger = logging.getLogger(__name__)

# Scope required to send mail on the user's behalf.
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"

# The <credentials> are loaded lazily from config so importing this module
# never crashes when GMAIL_* are unset (the app still runs with SMTP).
from app.config import settings  # noqa: E402


def gmail_configured() -> bool:
    return bool(settings.gmail_client_id and settings.gmail_client_secret)


def _flow():
    from google_auth_oauthlib.flow import Flow
    return Flow.from_client_config(
        {
            "web": {
                "client_id": settings.gmail_client_id,
                "client_secret": settings.gmail_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                # redirect_uris must match what's registered in Google Cloud
                "redirect_uris": [settings.gmail_redirect_uri],
            }
        },
        scopes=[GMAIL_SCOPE],
        redirect_uri=settings.gmail_redirect_uri,
    )


def build_authorization_url() -> str:
    """Start: return the Google consent URL for the gmail.send scope."""
    if not gmail_configured():
        raise HTTPException(
            status_code=503,
            detail="Gmail API not configured. Set GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET in backend/.env and enable the Gmail API.",
        )
    flow = _flow()
    return flow.authorization_url(prompt="consent")[0]


def exchange_callback_code(code: str) -> dict:
    """Callback /auth/gmail/callback: swap the auth code for a refresh token."""
    if not gmail_configured():
        raise HTTPException(status_code=503, detail="Gmail API not configured.")
    try:
        flow = _flow()
        flow.fetch_token(code=code)
    except Exception as e:
        logger.warning("Gmail token exchange failed: %s", e)
        raise HTTPException(status_code=400, detail=f"Failed to authorize Gmail: {e}")
    token = flow.credentials
    return {
        "refresh_token": token.refresh_token,
        "access_token": token.token,
        "token_uri": token.token_uri,
        "client_id": token.client_id,
        "client_secret": token.client_secret,
    }


def store_gmail_token(user, db, token: dict) -> None:
    """Persist the (opaque) Gmail credentials on the user row."""
    tokens = json.loads(user.oauth_tokens or "{}")
    tokens["gmail"] = token
    user.oauth_tokens = json.dumps(tokens)
    if db:
        db.add(user)
        db.commit()


def get_gmail_token(user) -> dict | None:
    tokens = json.loads(user.oauth_tokens or "{}")
    return tokens.get("gmail")


def _credentials_from(token: dict):
    from google.oauth2.credentials import Credentials
    return Credentials(
        token=token.get("access_token"),
        refresh_token=token.get("refresh_token"),
        token_uri=token.get("token_uri") or "https://oauth2.googleapis.com/token",
        client_id=token.get("client_id") or settings.gmail_client_id,
        client_secret=token.get("client_secret") or settings.gmail_client_secret,
        scopes=[GMAIL_SCOPE],
    )


def send_via_gmail_api(user, to_email: str, subject: str, body: str, attachment_path: str) -> bool:
    """Send via the Gmail API using the user's connected Gmail account.
    Returns True on success; raises RuntimeError with a clear message on any
    failure. Also refreshes the stored access token when needed."""
    token = get_gmail_token(user)
    if not token or not token.get("refresh_token"):
        raise RuntimeError(
            "Gmail not connected. In your profile, click 'Connect Gmail' and "
            "authorize it once, then Send again."
        )

    creds = _credentials_from(token)

    # Build the raw RFC822 message with the file attached.
    msg = EmailMessage()
    msg["From"] = user.email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    attachment_name = attachment_path.split("/")[-1].split("\\")[-1]
    with open(attachment_path, "rb") as f:
        msg.add_attachment(
            f.read(),
            maintype="application",
            subtype="pdf",
            filename=attachment_name,
        )
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    from googleapiclient.discovery import build

    try:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
    except Exception as e:
        # Catch "invalid_grant" (token revoked/scoped changed) to prompt re-connect.
        if "invalid_grant" in str(e) or "unauthorized_client" in str(e):
            raise RuntimeError(
                "Your Gmail connection expired or was revoked. Reconnect Gmail in "
                "your profile, then Send again."
            ) from e
        raise RuntimeError(f"Gmail API send failed: {e}") from e

    # Persist the refreshed access token so we don't re-auth every time.
    try:
        store_gmail_token(user, None, {
            "refresh_token": token["refresh_token"],
            "access_token": creds.token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
        })
    except Exception:
        pass

    return True
