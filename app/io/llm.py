"""The one place this system talks to a language model.

Everything above goes through `LLMClient`, so switching provider is an `.env`
change plus one adapter — never a refactor.

Three rules the previous version learned the hard way:

* **`max_tokens` is always set.** An unbounded generation on a free tier is how
  you turn one bad prompt into a rate-limit ban.
* **429 is retried with backoff.** Free tiers rate-limit constantly. Without
  this the pipeline dies on a transient error.
* **Every call is counted.** `BudgetedClient` enforces I7 (≤7 calls per run) in
  one place rather than trusting each stage to behave.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.domain.errors import BudgetExceeded, LLMRateLimited, LLMUnavailable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMResponse:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMClient(Protocol):
    """Narrow on purpose: one method, no streaming, no chat history.

    Every stage in this pipeline is a single request/response with a schema.
    A wider interface would invite stages to start holding conversations.
    """

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> LLMResponse: ...


# ── Google (Gemini) ───────────────────────────────────────────────────

class GoogleClient:
    """Gemini via `google-generativeai`.

    The model id comes from configuration and is never defaulted in code —
    a retired id must surface at startup through `probe()`, with its name in
    the message.
    """

    MAX_ATTEMPTS = 3

    def __init__(self, api_key: str, model: str, timeout: float = 45.0) -> None:
        if not api_key:
            raise LLMUnavailable("LLM_API_KEY is empty.")
        if not model:
            raise LLMUnavailable(
                "LLM_MODEL is empty. Copy the current model id from your "
                "provider's model list into .env."
            )
        self._api_key = api_key
        self._model_name = model
        self._timeout = timeout
        self._sdk = None

    def _genai(self):
        # Imported lazily so the package is not needed to run the test suite.
        if self._sdk is None:
            import google.generativeai as genai
            genai.configure(api_key=self._api_key)
            self._sdk = genai
        return self._sdk

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> LLMResponse:
        genai = self._genai()
        generation_config: dict[str, Any] = {
            "max_output_tokens": max_tokens,
            "temperature": temperature,
        }
        if schema is not None:
            generation_config["response_mime_type"] = "application/json"
            generation_config["response_schema"] = schema

        model = genai.GenerativeModel(
            model_name=self._model_name,
            system_instruction=system or None,
            generation_config=generation_config,
        )

        last: Exception | None = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                response = model.generate_content(
                    user, request_options={"timeout": self._timeout}
                )
                usage = getattr(response, "usage_metadata", None)
                return LLMResponse(
                    text=response.text or "",
                    prompt_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                    completion_tokens=getattr(usage, "candidates_token_count", 0) or 0,
                )
            except Exception as exc:                      # noqa: BLE001
                last = exc
                if not _is_rate_limit(exc) or attempt == self.MAX_ATTEMPTS:
                    break
                delay = _backoff(attempt, exc)
                asked = retry_after(exc)
                logger.warning(
                    "Rate limited, waiting %.0fs (%d/%d)%s",
                    delay, attempt, self.MAX_ATTEMPTS,
                    "" if asked is None else f" — provider asked for {asked:.0f}s",
                )
                time.sleep(delay)

        if last is not None and _is_rate_limit(last):
            wait = retry_after(last)
            raise LLMRateLimited(
                f"{self._model_name} is rate limited"
                + (f"; the provider asked for {wait:.0f}s between requests" if wait else "")
                + f". Gave up after {self.MAX_ATTEMPTS} attempts. "
                "Free tiers are usually limited per minute — either wait, or set "
                "LLM_MODEL to a model with a higher allowance."
            ) from last
        raise LLMUnavailable(
            f"Call to {self._model_name!r} failed: {last}. "
            "If the model was retired, update LLM_MODEL in .env."
        ) from last

    def probe(self) -> None:
        """One cheap call at startup so a dead model fails on boot (D-19)."""
        try:
            self.complete(system="", user="ok", max_tokens=8, temperature=0.0)
        except (LLMRateLimited, LLMUnavailable):
            raise
        except Exception as exc:                          # noqa: BLE001
            raise LLMUnavailable(
                f"Could not reach model {self._model_name!r}: {exc}"
            ) from exc


def _is_rate_limit(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(m in text for m in ("429", "rate limit", "resource_exhausted", "quota"))


# Providers say how long to wait. Gemini puts it in the error as
# "Please retry in 28.4s" and again as "retry_delay { seconds: 28 }".
_RETRY_AFTER_PATTERNS = (
    re.compile(r"retry in\s+([\d.]+)\s*s", re.IGNORECASE),
    re.compile(r"retry_delay\s*\{\s*seconds:\s*(\d+)", re.IGNORECASE),
    re.compile(r"retry[- ]after[\"']?\s*[:=]\s*[\"']?(\d+)", re.IGNORECASE),
)

# Never sleep longer than this, whatever the server says. A provider asking for
# an hour means the quota is gone, not that we should hang.
MAX_RETRY_WAIT = 75.0


def retry_after(exc: Exception) -> float | None:
    """Seconds the provider asked us to wait, if it said.

    Parsed from the message rather than from a provider-specific exception
    type, so this keeps working when the provider changes — and works for any
    other provider that states a delay.
    """
    text = str(exc)
    for pattern in _RETRY_AFTER_PATTERNS:
        if (m := pattern.search(text)) is not None:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def _backoff(attempt: int, exc: Exception | None = None) -> float:
    """How long to wait before retrying.

    Honour the provider's own number when it gives one. Guessing shorter is
    actively harmful on a per-minute quota: the early retry fails AND consumes
    another request from the same allowance. Only fall back to exponential
    backoff with jitter when the provider says nothing.
    """
    if exc is not None and (asked := retry_after(exc)) is not None:
        # A second of slack, because the window is measured server-side.
        return min(asked + 1.0, MAX_RETRY_WAIT)
    return min(2 ** attempt, 20) * (0.5 + random.random() / 2)


# ── Budget wrapper ────────────────────────────────────────────────────

@dataclass
class RunBudget:
    max_calls: int = 7
    max_tokens: int = 40_000
    calls: int = 0
    tokens: int = 0

    def record(self, response: LLMResponse) -> None:
        self.calls += 1
        self.tokens += response.total_tokens


class BudgetedClient:
    """Enforces I7 in one place, and records what every call cost.

    Exceeding the budget raises `BudgetExceeded`, which is an
    `InvariantViolation` rather than a warning: silently truncating a run
    produces a half-tailored resume that looks finished.
    """

    def __init__(self, inner: LLMClient, budget: RunBudget) -> None:
        self._inner = inner
        self.budget = budget
        self.log: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        stage: str = "",
    ) -> LLMResponse:
        if self.budget.calls >= self.budget.max_calls:
            raise BudgetExceeded(
                f"run already used {self.budget.calls} model calls "
                f"(limit {self.budget.max_calls}); refusing to start {stage or 'another'}"
            )

        started = time.monotonic()
        response = self._inner.complete(
            system=system, user=user, schema=schema,
            max_tokens=max_tokens, temperature=temperature,
        )
        self.budget.record(response)
        self.log.append({
            "stage": stage,
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
            "ms": round((time.monotonic() - started) * 1000),
        })

        if self.budget.tokens > self.budget.max_tokens:
            raise BudgetExceeded(
                f"run used {self.budget.tokens} tokens (limit {self.budget.max_tokens})"
            )
        return response


# ── Test double ───────────────────────────────────────────────────────

class ScriptedClient:
    """Returns canned responses in order. Lets the whole pipeline be tested
    without a key, a network, or non-determinism."""

    def __init__(self, responses: list[str | dict[str, Any] | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> LLMResponse:
        self.calls.append({
            "system": system, "user": user, "schema": schema,
            "max_tokens": max_tokens, "temperature": temperature,
        })
        if not self._responses:
            raise AssertionError("ScriptedClient ran out of responses")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        text = nxt if isinstance(nxt, str) else json.dumps(nxt)
        return LLMResponse(text=text, prompt_tokens=10, completion_tokens=20)


def build_client(settings) -> LLMClient:
    """Construct the configured provider. One switch, one place."""
    provider = (settings.llm_provider or "google").lower()
    if provider == "google":
        return GoogleClient(
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout=settings.llm_timeout_seconds,
        )
    raise LLMUnavailable(
        f"Unknown LLM_PROVIDER {provider!r}. Supported: google."
    )
