"""One call path for every agent.

Each stage asks for a typed answer and gets one, or the run fails with a clear
error. No stage parses text, strips code fences, or repairs JSON — all of that
belongs to the shape problem, and the shape problem is solved once, here.

The previous version had `extract_json`, a lenient loader, an invalid-escape
repair regex, two "your output did not conform" reminder prompts and a
`_find_resume_dict` that went hunting for a resume inside whatever came back.
Schema-constrained generation plus one typed retry replaces all of it.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.domain.errors import SchemaValidationFailed
from app.io.llm import LLMClient
from app.io.schema import to_provider_schema

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_RETRY_NOTE = (
    "\n\nYour previous reply did not match the required schema:\n{error}\n"
    "Return only a JSON object matching the schema exactly."
)


def prompt_version(prompt: str) -> str:
    """Short hash of a prompt, recorded with eval results.

    Prompts are the least version-controlled part of an LLM system and the most
    likely cause of a quality shift. Hashing them makes "the numbers moved"
    traceable to "the prompt changed".
    """
    return hashlib.sha256(prompt.encode()).hexdigest()[:8]


def call_structured(
    client: LLMClient,
    *,
    system: str,
    user: str,
    schema_model: type[T],
    max_tokens: int = 2048,
    temperature: float = 0.2,
    stage: str = "",
) -> T:
    """Ask for `schema_model` and return a validated instance.

    One retry, and only when the failure is a shape problem worth restating.
    A second failure raises rather than falling back to a partial object — a
    half-parsed plan is worse than a visible error.
    """
    provider_schema = to_provider_schema(schema_model)
    kwargs: dict[str, Any] = {
        "system": system, "schema": provider_schema,
        "max_tokens": max_tokens, "temperature": temperature,
    }
    if stage:
        kwargs["stage"] = stage          # BudgetedClient records it; others ignore

    attempt_user = user
    last_error = ""

    for attempt in (1, 2):
        try:
            response = _complete(client, user=attempt_user, **kwargs)
        except TypeError:
            kwargs.pop("stage", None)     # a client without the stage kwarg
            response = _complete(client, user=attempt_user, **kwargs)

        try:
            return schema_model.model_validate_json(_strip(response.text))
        except (ValidationError, ValueError) as exc:
            last_error = str(exc)[:600]
            logger.warning("Schema mismatch from %s (attempt %d): %s",
                           stage or schema_model.__name__, attempt, last_error)
            attempt_user = user + _RETRY_NOTE.format(error=last_error)

    raise SchemaValidationFailed(
        f"{stage or schema_model.__name__} did not return valid "
        f"{schema_model.__name__} after 2 attempts: {last_error}"
    )


def _complete(client: LLMClient, **kwargs):
    return client.complete(**kwargs)


def _strip(text: str) -> str:
    """Remove a markdown fence if the provider added one.

    Schema-constrained output should never be fenced, but providers differ and
    a two-line guard is cheaper than a failed run.
    """
    body = (text or "").strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1] if "\n" in body else body
        if body.endswith("```"):
            body = body[: -3]
    return body.strip()


def as_json(value: Any) -> str:
    """Compact JSON for prompt payloads — no indentation to waste tokens."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
