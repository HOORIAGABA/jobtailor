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

import re
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

# The development identity. Named here rather than in `api.deps` so that
# `check()` can look for it without importing the API layer — `config` sits
# below everything and must stay that way.
DEV_EMAIL_ENV = "DEV_USER_EMAIL"


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
    llm_base_url: str = ""           # required for any OpenAI-compatible provider
    llm_max_tokens: int = 2048
    # "low" | "medium" | "high", for providers that expose it (Groq and
    # Cerebras do, on the gpt-oss family). Left empty the field is not sent at
    # all, because a provider that does not know it may reject the request.
    #
    # Worth setting to "low" on any reasoning model: extraction has one
    # correct answer, and a model that thinks at length about it spends the
    # output budget before it starts writing. Measured: 15,320 tokens with no
    # finished JSON, against 2,312 from a non-reasoning model on the same
    # document.
    llm_reasoning_effort: str = ""

    # ── A second model, for the stages that need judgment ─────────────
    # The pipeline's six calls are not equal work. Three of them are the
    # parse: bulk transcription, one correct answer, and the easiest thing
    # here — a local 4B-8B model does it fine, for free, forever. The other
    # three are judgment, and the planner is the hardest single call in the
    # system: nine operation variants, a long instruction set, and a decision
    # about someone's career.
    #
    # Measured on llama3.1:8b locally: the parse produced a clean document,
    # and the planner returned NINE output tokens — an empty list. It is not
    # a big enough model for that call.
    #
    # Splitting the two solves the free-tier problem rather than working
    # around it. The stage that needs MANY calls runs locally and unmetered;
    # the stage that needs a CAPABLE model needs only one call per run, which
    # fits inside any free tier with room to spare.
    #
    # Any field left empty falls back to its LLM_* counterpart, so a single
    # model everywhere remains the default.
    smart_llm_provider: str = ""
    smart_llm_api_key: str = ""
    smart_llm_model: str = ""
    smart_llm_base_url: str = ""
    smart_llm_reasoning_effort: str = ""

    @property
    def has_smart_model(self) -> bool:
        return bool(self.smart_llm_model.strip())

    def for_role(self, role: str) -> "Settings":
        """A view of these settings for `main` or `smart`.

        Returns a copy with the LLM_* fields replaced by their SMART_*
        counterparts, falling back field by field — so setting only
        SMART_LLM_MODEL against the same provider works as expected.
        """
        if role != "smart" or not self.has_smart_model:
            return self
        return self.model_copy(update={
            "llm_provider": self.smart_llm_provider or self.llm_provider,
            "llm_api_key": self.smart_llm_api_key or self.llm_api_key,
            "llm_model": self.smart_llm_model,
            "llm_base_url": self.smart_llm_base_url or self.llm_base_url,
            "llm_reasoning_effort": self.smart_llm_reasoning_effort,
        })
    # 45s was fine for a hosted API and far too short for a local model.
    # Measured on llama3.1:8b through Ollama on a laptop GPU: prompt
    # processing ran at ~30 tok/s and generation at ~11.9 tok/s, so a single
    # parse window — 1,325 tokens in, ~700 out — takes about 103 seconds.
    # The prompt processing ALONE was 44s, which exhausted the old timeout
    # before the model had written a character.
    #
    # A generous ceiling costs a hosted provider nothing: it answers in
    # seconds either way, and the timeout only fires when something is wrong.
    llm_timeout_seconds: float = 300.0

    # ── Database ──────────────────────────────────────────────────────
    database_url: str = ""

    # ── Security ──────────────────────────────────────────────────────
    # Signs the session cookie. No default: an unsigned cookie is the same as
    # trusting the client to say who it is. `engine.authsession.issue` refuses.
    jwt_secret: str = ""
    # Encrypts OAuth refresh tokens at rest. No default, for the same reason a
    # signing key has none — a shared default is the same as not encrypting
    # while appearing to. `engine.secrets` refuses. Generate: python -m scripts.keys
    fernet_key: str = ""

    # ── Google sign-in and sending ────────────────────────────────────
    # One OAuth client covers both: `openid email profile` at sign-in, and
    # `gmail.send` requested later through incremental authorisation. See
    # `io.google` for why the send scope is asked for late and why it is the
    # only Gmail scope this application will ever request.
    google_client_id: str = ""
    google_client_secret: str = ""
    # Must match a redirect URI registered on the OAuth client, character for
    # character — a trailing slash is a `redirect_uri_mismatch`.
    google_redirect_uri: str = "http://localhost:8000/api/auth/google/callback"
    # Where the browser lands after a successful sign-in. The API and the UI are
    # different origins in development, so this cannot be a relative path.
    frontend_url: str = "http://localhost:3000"
    # Set `Secure` on the session cookie. Off in development because there is no
    # TLS on localhost and a Secure cookie would simply never be stored; on in
    # production, where sending a session cookie over plain HTTP is the whole
    # problem the flag exists for. Derived from `environment` rather than set
    # separately, so there is no way to deploy with it accidentally off.
    @property
    def secure_cookies(self) -> bool:
        return self.is_production
    # Signs the HMAC binding an approval (S9) to the message that was
    # previewed. No default, and `engine.confirm` refuses to issue a token
    # without one: a shared default secret is a signature anyone can forge, and
    # the failure would be silent. Missing, it surfaces as "cannot preview".
    confirm_token_secret: str = ""

    # ── Sending (S11) ─────────────────────────────────────────────────
    # "console" renders the message and dispatches nothing. It is the default
    # on purpose: sending is the only irreversible action here, so a
    # misconfiguration should mean "nothing happened", never "something went to
    # a stranger". Choosing a real backend is a deliberate act.
    mail_provider: str = "console"

    # ── Budget (I7) ───────────────────────────────────────────────────
    # Was 7, when parsing was one call. It is now one call per window of the
    # resume — three for a two-page CV, more for a longer one — so a 7-call
    # ceiling would fail a legitimate run as a budget violation. The point of
    # the budget is to catch a runaway loop, not to cap a document's length.
    max_llm_calls_per_run: int = 12
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


# API model ids are lowercase and hyphenated: `gemini-2.5-flash-lite`,
# `openai/gpt-oss-20b`. Providers show a DISPLAY NAME in their console —
# "Gemini 2.5 Flash Lite" — and pasting that yields a cryptic 400
# ("unexpected model name format") on the first call, minutes later.
_MODEL_ID = re.compile(r"^[a-z0-9]+(?:[-._/:][a-z0-9]+)*$")


def looks_like_model_id(value: str) -> bool:
    return bool(_MODEL_ID.match((value or "").strip()))


def suggest_model_id(display_name: str) -> str:
    """Best guess at the API id behind a console display name.

    "Gemini 2.5 Flash Lite" -> "gemini-2.5-flash-lite"

    A suggestion, not a promise — the id still has to exist on the account,
    which only the provider's model list can confirm.
    """
    slug = re.sub(r"\s+", "-", (display_name or "").strip().lower())
    return re.sub(r"-+", "-", slug).strip("-")


def check(settings: Settings) -> list[str]:
    """Return the list of configuration problems. Empty means good to go.

    Separated from raising so tests and a `/health` endpoint can inspect the
    same answer, and so every problem is reported at once rather than one per
    restart.
    """
    problems: list[str] = []

    provider = (settings.llm_provider or "google").strip().lower()

    if not settings.llm_api_key and provider != "ollama":
        problems.append(f"LLM_API_KEY is empty (provider {provider!r}).")
    if provider != "google" and not settings.llm_base_url:
        problems.append(
            f"LLM_BASE_URL is required for provider {provider!r} — an "
            "OpenAI-compatible endpoint, e.g. https://openrouter.ai/api/v1"
        )
    if not settings.llm_model:
        problems.append(
            "LLM_MODEL is empty. There is deliberately no default: copy the "
            "current model id from your provider's model list into .env. "
            "(Two previous defaults were retired by their provider.)"
        )
    elif not looks_like_model_id(settings.llm_model):
        problems.append(
            f"LLM_MODEL={settings.llm_model!r} looks like a display name, not an "
            f"API model id. Providers show a friendly name in the console but the "
            f"API wants the id — lowercase and hyphenated. "
            f"Try: LLM_MODEL={suggest_model_id(settings.llm_model)}"
        )
    # A split configuration inherits the base URL field by field, which is
    # right when both models sit behind one endpoint and wrong the moment they
    # do not. The common mistake: set SMART_LLM_MODEL to a hosted model, leave
    # SMART_LLM_BASE_URL empty, and the request goes to the local Ollama —
    # which answers 404, naming a model it was never going to have.
    if settings.has_smart_model and not settings.smart_llm_base_url.strip():
        inherited = settings.llm_base_url.strip().lower()
        local = any(host in inherited for host in ("localhost", "127.0.0.1"))
        vendored = "/" in settings.smart_llm_model
        if local and vendored:
            problems.append(
                f"SMART_LLM_MODEL={settings.smart_llm_model!r} looks like a "
                f"hosted model, but SMART_LLM_BASE_URL is empty so it will be "
                f"requested from {settings.llm_base_url} — a local server. "
                f"Set SMART_LLM_BASE_URL to the endpoint that serves it."
            )

    # A wildcard origin and a session cookie cannot coexist. Starlette sends
    # `Access-Control-Allow-Origin: *`, and every browser refuses a credentialed
    # response carrying `*` — so this does not open a hole, it silently breaks
    # sign-in and every screen reports "not signed in" with a 200 in the network
    # tab. That is a worse failure than a refusal, because it looks like an auth
    # bug and the cause is three files away.
    if "*" in settings.cors_origin_list:
        problems.append(
            "CORS_ORIGINS=* cannot work now that requests carry a session "
            "cookie: browsers reject a credentialed response whose "
            "Access-Control-Allow-Origin is '*', so every request would look "
            "like 'not signed in'. List the UI's origin instead, e.g. "
            "CORS_ORIGINS=http://localhost:3000"
        )

    if settings.is_production:
        for name in ("database_url", "jwt_secret", "fernet_key", "confirm_token_secret"):
            if not getattr(settings, name):
                problems.append(f"{name.upper()} must be set in production.")
        import os

        if (os.environ.get(DEV_EMAIL_ENV) or "").strip():
            problems.append(
                f"{DEV_EMAIL_ENV} is set on a production instance. It is "
                f"ignored at request time, but it should not be in this "
                f"environment at all — remove it from the deployment config."
            )
    return problems


def require_valid(settings: Settings) -> None:
    if problems := check(settings):
        raise ConfigError(
            "Configuration is incomplete:\n  - " + "\n  - ".join(problems)
        )


settings = Settings()
