"""Module 3.8 (Sender). Uses plain SMTP + a Gmail App Password.

Default sends with a shared sender from .env, but callers can pass
per-user credentials (each user's OWN Gmail + App Password) so nothing
is hardcoded in the backend. Swap for the Gmail API + OAuth if you want
full delegated sending.
"""
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from app.config import settings


def send_email_with_attachment(
    to_email: str,
    subject: str,
    body: str,
    attachment_path: str,
    smtp_username: str | None = None,
    smtp_app_password: str | None = None,
) -> None:
    """Send an email with an attachment. smtp_username / smtp_app_password
    override the shared .env sender when supplied (per-user sending)."""
    username = smtp_username or settings.smtp_username
    app_password = smtp_app_password or settings.smtp_app_password

    if not username or not app_password:
        raise RuntimeError(
            "No SMTP credentials configured. Add your Gmail + App Password in "
            "your profile (account settings), or set SMTP_USERNAME / "
            "SMTP_APP_PASSWORD in .env as a fallback."
        )

    msg = MIMEMultipart()
    msg["From"] = username
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with open(attachment_path, "rb") as f:
        part = MIMEApplication(f.read(), Name=attachment_path.split("/")[-1])
    part["Content-Disposition"] = f'attachment; filename="{attachment_path.split("/")[-1]}"'
    msg.attach(part)

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            server.starttls()
            server.login(username, app_password)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise RuntimeError(
            "Gmail rejected the login. Double-check the App Password (it must be "
            "a 16-char Gmail App Password, not your normal password)."
        ) from e
    except (smtplib.SMTPException, OSError) as e:
        raise RuntimeError(f"SMTP send failed: {e}") from e
