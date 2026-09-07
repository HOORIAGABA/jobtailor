import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Anchor everything to the backend directory so the app works no matter
# which CWD starts it (the .env and ./data paths silently resolve
# relative to the working directory otherwise).
BASE_DIR = Path(__file__).resolve().parent.parent

IS_PROD = os.getenv("ENVIRONMENT", "development") == "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        extra="ignore",
    )

    # Database
    database_url: str = "sqlite:///./data/jobtailor.db"

    # Auth — required in production
    jwt_secret: str = "" if IS_PROD else "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 10080  # 7 days

    # Google OAuth
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://127.0.0.1:8000/auth/google/callback"

    # Gmail API (OAuth sending) — separate scoped credentials vs. login above.
    # Needs the gmail.send scope + a Web application client registered in
    # Google Cloud. Falls back to SMTP + App Password if not set / not granted.
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_redirect_uri: str = "http://127.0.0.1:8000/auth/gmail/callback"

    # LLM — must be configured to a real provider ("groq" or "openai_compatible")
    llm_provider: str = "groq"  # "groq" | "openai_compatible"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "llama-3.1-70b-versatile"

    # Resume parser LLM stack
    # Recommended: run a local vLLM/OpenAI-compatible server with Qwen3 or NuExtract 3.
    resume_parser_provider: str = "openai_compatible"
    resume_parser_base_url: str = "http://localhost:11434/v1"
    resume_parser_model: str = "llama3.1:8b-instruct-q4_K_M"
    resume_parser_api_key: str = ""

    # Email
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_app_password: str = ""


settings = Settings()

if IS_PROD and not settings.jwt_secret:
    raise RuntimeError("JWT_SECRET must be set in production")


def _resolve_sqlite_url(url: str) -> str:
    """sqlite:///./data/jobtailor.db -> absolute path anchored to BASE_DIR."""
    prefix = "sqlite:///"
    if url.startswith(prefix + "./"):
        rel = url[len(prefix + "./"):]
        return prefix + (BASE_DIR / rel).resolve().as_posix()
    return url


settings.database_url = _resolve_sqlite_url(settings.database_url)
