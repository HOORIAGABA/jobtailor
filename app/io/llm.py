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
import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.domain.errors import BudgetExceeded, LLMRateLimited, LLMUnavailable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMResponse:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Why the model stopped. "length" means the answer was CUT OFF at the
    # token ceiling, and it is the difference between a real diagnosis and a
    # wild goose chase: truncated JSON fails schema validation, so without
    # this field the system reports "the model would not follow the schema"
    # when the model was following it perfectly and simply ran out of room.
    # Measured: a 7,660-character resume was cut at exactly the 4,213-token
    # ceiling, twice, and reported itself as a schema problem both times.
    finish_reason: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def was_truncated(self) -> bool:
        return self.finish_reason.lower() in ("length", "max_tokens")


# One counter for the process. Ordering across two clients is the whole point,
# so it cannot live on a client.
_CALL_SEQUENCE = itertools.count()


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
                    finish_reason=_google_finish_reason(response),
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

        # Ordering and duration are two different questions, and only one of
        # them is a clock question.
        #
        # `started` alone was used for both, and on Windows `time.monotonic()`
        # advances in ~15.6 ms steps — so five calls that complete in
        # microseconds all record the SAME value, the merge sort finds every key
        # equal, and a stable sort leaves the entries grouped by client rather
        # than interleaved. The run log then claims an order the calls did not
        # happen in, which is worse than claiming none: someone reading it to
        # work out which stage failed first is reading fiction.
        #
        # A sequence number is exact on every platform and costs nothing.
        # `next()` on an `itertools.count` is a single C call, so it is atomic
        # under the GIL and needs no lock of its own.
        sequence = next(_CALL_SEQUENCE)
        started = time.monotonic()
        response = self._inner.complete(
            system=system, user=user, schema=schema,
            max_tokens=max_tokens, temperature=temperature,
        )
        self.budget.record(response)
        self.log.append({
            "stage": stage,
            # So two clients sharing a run can have their logs merged back
            # into the order the calls actually happened in.
            "seq": sequence,
            "started": started,
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
            "ms": round((time.monotonic() - started) * 1000),
        })

        if self.budget.tokens > self.budget.max_tokens:
            raise BudgetExceeded(
                f"run used {self.budget.tokens} tokens (limit {self.budget.max_tokens})"
            )
        return response


def merged_call_log(*clients: Any) -> list[dict[str, Any]]:
    """Every call from every client, in the order they happened.

    A run with a separate smart model has two `BudgetedClient`s sharing one
    `RunBudget`, so the budget counts both while `client.log` holds one. A
    manifest once said `calls: 5` beside three entries, and the two missing
    were the two that failed.

    `BudgetedClient` records a `seq` for exactly this, which is why the merge is
    a sort and not a concatenation. It lives here, beside the thing that writes
    those entries, because two callers needed it and the second one
    reimplemented it.

    Sorted on `seq`, with `started` as the fallback so a log read back from an
    older run — a seeded folder, a stored manifest — still merges. Those entries
    have no sequence number and never will.
    """
    seen: set[int] = set()
    entries: list[dict[str, Any]] = []
    for client in clients:
        log = getattr(client, "log", None)
        if log is None or id(client) in seen:
            continue
        seen.add(id(client))
        entries.extend(log)
    return sorted(entries,
                  key=lambda e: (e.get("seq", -1), e.get("started", 0.0)))


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


# ── OpenAI-compatible (almost everything else) ────────────────────────

class OpenAICompatibleClient:
    """Any endpoint speaking OpenAI's `/chat/completions`.

    That is most of them: OpenRouter, Groq, Cerebras, Together, Ollama, and the
    various gateways that proxy other providers. One adapter, so picking a
    different free tier is an `.env` change.

    Structured output is the part that actually differs between them, so it
    degrades in three steps rather than assuming (see `_response_format`).
    """

    MAX_ATTEMPTS = 3

    def __init__(self, api_key: str, model: str, base_url: str,
                 timeout: float = 45.0, reasoning_effort: str = "") -> None:
        if not model:
            raise LLMUnavailable("LLM_MODEL is empty.")
        if not base_url:
            raise LLMUnavailable(
                "LLM_BASE_URL is required for an OpenAI-compatible provider "
                "(e.g. https://openrouter.ai/api/v1)."
            )
        self._api_key = api_key
        self._model_name = model
        self._url = base_url.strip().rstrip("/") + "/chat/completions"
        self._timeout = timeout
        # Reasoning models emit a thinking channel BEFORE the answer, and on
        # most gateways those tokens come out of `max_tokens`. Measured on one
        # 7,660-character resume: gpt-oss-120b-medium spent 15,320 tokens and
        # never finished the JSON, while a non-reasoning model did the same
        # work in 2,312. Extraction has one correct answer — there is nothing
        # there worth thinking about — so the effort is worth turning down.
        #
        # Sent only when set, because a provider that does not know the field
        # may reject the whole request.
        self._reasoning_effort = (reasoning_effort or "").strip().lower()
        # Remembered per client so the ladder below is climbed once, not once
        # per call.
        self._schema_mode: str | None = None

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> LLMResponse:
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": user}
        ]
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        last: Exception | None = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            for mode in self._schema_modes(schema):
                body: dict[str, Any] = {
                    "model": self._model_name,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                }
                if fmt := _response_format(mode, schema):
                    body["response_format"] = fmt
                if self._reasoning_effort:
                    body["reasoning_effort"] = self._reasoning_effort

                try:
                    resp = httpx.post(self._url, headers=headers, json=body,
                                      timeout=self._timeout)
                except Exception as exc:                     # noqa: BLE001
                    last = exc
                    break                                     # network: retry, don't downgrade

                if resp.status_code == 429:
                    last = LLMRateLimited(_body_text(resp) or "rate limited")
                    break                                     # retry, don't downgrade

                if resp.status_code == 400 and mode != "none":
                    # The endpoint rejected this structured-output style. Try
                    # the next one down rather than giving up on the provider.
                    logger.info("%s rejected response_format=%s; degrading",
                                self._model_name, mode)
                    continue

                if resp.status_code == 404:
                    # "model not found" almost never means the model is gone.
                    # It means the request went to the wrong endpoint — and
                    # with a split main/smart configuration the base URL is
                    # INHERITED when SMART_LLM_BASE_URL is left empty, so a
                    # hosted model id gets asked of a local Ollama.
                    raise LLMUnavailable(
                        f"{self._url} has no model named {self._model_name!r}.\n"
                        f"  The model id and the endpoint have to match. If "
                        f"this is the judgment model, SMART_LLM_BASE_URL is "
                        f"empty, so it inherited LLM_BASE_URL above — set it "
                        f"to the endpoint that actually serves "
                        f"{self._model_name!r}.\n"
                        f"  For a local Ollama model, `ollama list` shows the "
                        f"exact tags it has."
                    )

                if resp.status_code >= 400:
                    raise LLMUnavailable(
                        f"{self._model_name} returned {resp.status_code}: "
                        f"{_body_text(resp)[:300]}"
                    )

                self._schema_mode = mode                      # remember what worked
                return _parse_openai_response(resp.json())

            if attempt == self.MAX_ATTEMPTS or not _is_rate_limit(last or Exception()):
                break
            delay = _backoff(attempt, last)
            logger.warning("Rate limited, waiting %.0fs (%d/%d)",
                           delay, attempt, self.MAX_ATTEMPTS)
            time.sleep(delay)

        if last is not None and _is_rate_limit(last):
            wait = retry_after(last)
            raise LLMRateLimited(
                f"{self._model_name} is rate limited"
                + (f"; the provider asked for {wait:.0f}s" if wait else "")
                + f". Gave up after {self.MAX_ATTEMPTS} attempts."
            ) from last
        if isinstance(last, httpx.TimeoutException):
            raise LLMUnavailable(
                f"Call to {self._model_name!r} at {self._url} timed out after "
                f"{self._timeout:.0f}s.\n"
                f"  A local model is slow, not broken: an 8B model on a laptop "
                f"GPU needs roughly 100 seconds for one parse window. Raise "
                f"LLM_TIMEOUT_SECONDS, or run a smaller model.\n"
                f"  If it is far slower than that, Ollama is probably on the "
                f"CPU — `ollama ps` shows a PROCESSOR column."
            ) from last
        raise LLMUnavailable(
            f"Call to {self._model_name!r} at {self._url} failed: {last}"
        ) from last

    def _schema_modes(self, schema: dict[str, Any] | None) -> list[str]:
        """Structured-output styles to try, best first.

        Endpoints vary: some support a full JSON schema, some only "give me
        JSON", some neither. Probing in order and remembering the winner beats
        assuming — and beats a per-provider capability table that goes stale.
        """
        if schema is None:
            return ["none"]
        if self._schema_mode:
            return [self._schema_mode]
        return ["json_schema", "json_object", "none"]

    def probe(self) -> None:
        self.complete(system="", user="ok", max_tokens=8, temperature=0.0)


def _response_format(mode: str, schema: dict[str, Any] | None) -> dict | None:
    if mode == "json_schema" and schema is not None:
        return {
            "type": "json_schema",
            "json_schema": {"name": "response", "schema": schema, "strict": False},
        }
    if mode == "json_object":
        return {"type": "json_object"}
    return None


def _google_finish_reason(response) -> str:
    """Google names it MAX_TOKENS on the candidate, not `finish_reason`."""
    try:
        reason = response.candidates[0].finish_reason
    except (AttributeError, IndexError, TypeError):
        return ""
    name = getattr(reason, "name", None) or str(reason)
    return "length" if "MAX_TOKENS" in name.upper() else name.lower()


def _parse_openai_response(data: dict) -> LLMResponse:
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMUnavailable(f"Unexpected response shape: {str(data)[:200]}") from exc
    usage = data.get("usage") or {}
    try:
        finish = str(data["choices"][0].get("finish_reason") or "")
    except (KeyError, IndexError, TypeError):
        finish = ""
    return LLMResponse(
        text=text,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        finish_reason=finish,
    )


def _body_text(resp) -> str:
    try:
        return resp.text
    except Exception:                                          # noqa: BLE001
        return ""


# ── construction ──────────────────────────────────────────────────────

# Anything not "google" is assumed to speak OpenAI's API, because nearly
# everything does. Listing the names people actually type avoids a pointless
# "unknown provider" wall.
_OPENAI_COMPATIBLE = frozenset({
    "openai", "openai_compatible", "openrouter", "groq", "cerebras",
    "together", "ollama", "mistral", "deepseek", "fireworks", "custom",
})


def build_client(settings, role: str = "main") -> LLMClient:
    """Construct the configured provider. One switch, one place.

    `role="smart"` selects the SMART_LLM_* settings when they are configured,
    so the judgment stages can run on a different model from the parse. See
    `Settings.for_role`.
    """
    if role != "main" and hasattr(settings, "for_role"):
        settings = settings.for_role(role)
    provider = (settings.llm_provider or "google").strip().lower()

    if provider == "google":
        return GoogleClient(
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout=settings.llm_timeout_seconds,
        )

    if provider in _OPENAI_COMPATIBLE:
        return OpenAICompatibleClient(
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_seconds,
            reasoning_effort=settings.llm_reasoning_effort,
        )

    raise LLMUnavailable(
        f"Unknown LLM_PROVIDER {provider!r}. Use 'google', or any "
        f"OpenAI-compatible provider with LLM_BASE_URL set: "
        f"{', '.join(sorted(_OPENAI_COMPATIBLE))}."
    )
