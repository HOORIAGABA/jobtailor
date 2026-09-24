"""Environment configuration. One file, no profile switches.

The stack is identical in development and production — same model, same
libraries, same database engine. Only the values here differ.

**There is no model id default.** This project has been broken twice by a
provider retiring a hardcoded model (`llama-3.1-70b-versatile` was
decommissioned; `llama-3.1-8b-instant` moved behind Enterprise pricing). A
missing or retired model must fail at startup with a message naming it, not
400 three minutes into a pipeline run.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        extra="ignore",
        # `model_` is a pydantic namespace; ours are LLM_* so there is no clash,
        # but be explicit rather than relying on it.
        protected_namespaces=("settings_",),
    )

    environment: str = "development"

    # ── LLM ───────────────────────────────────────────────────────────
    llm_provider: str = "google"
    llm_api_key: str = ""
    llm_model: str = ""              # NO DEFAULT. See the module docstring.
    llm_max_tokens: int = 2048
    llm_timeout_seconds: float = 45.0

    # ── Database ──────────────────────────────────────────────────────
    database_url: str = ""

    # ── Security ──────────────────────────────────────────────────────
    jwt_secret: str = ""
    fernet_key: str = ""
    confirm_token_secret: str = ""

    # ── Budget (I7) ───────────────────────────────────────────────────
    max_llm_calls_per_run: int = 7
    max_tokens_per_run: int = 40_000

    # ── Optional ──────────────────────────────────────────────────────
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    cors_origins: str = "http://localhost:3000"

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @field_validator("llm_max_tokens")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("LLM_MAX_TOKENS must be positive")
        return v


class ConfigError(RuntimeError):
    """Configuration is wrong. Raised at startup, never at request time."""


def check(settings: Settings) -> list[str]:
    """Return the list of configuration problems. Empty means good to go.

    Separated from raising so tests and a `/health` endpoint can inspect the
    same answer, and so every problem is reported at once rather than one per
    restart.
    """
    problems: list[str] = []

    if not settings.llm_api_key:
        problems.append("LLM_API_KEY is empty — get one from Google AI Studio.")
    if not settings.llm_model:
        problems.append(
            "LLM_MODEL is empty. There is deliberately no default: copy the "
            "current model id from your provider's model list into .env. "
            "(Two previous defaults were retired by their provider.)"
        )
    if settings.is_production:
        for name in ("database_url", "jwt_secret", "fernet_key", "confirm_token_secret"):
            if not getattr(settings, name):
                problems.append(f"{name.upper()} must be set in production.")
    return problems


def require_valid(settings: Settings) -> None:
    if problems := check(settings):
        raise ConfigError(
            "Configuration is incomplete:\n  - " + "\n  - ".join(problems)
        )


settings = Settings()
