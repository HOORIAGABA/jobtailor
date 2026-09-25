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

from app.domain.errors import ResponseTruncated, SchemaValidationFailed
from app.io.llm import LLMClient
from app.io.schema import to_provider_schema

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# The most any single call may ask for, however large the input. Beyond this a
# document needs splitting, not a bigger number — and some providers reject a
# request whose ceiling exceeds what the model can emit.
HARD_TOKEN_CEILING = 16384

# Two different failures, two separate allowances. They shared one budget once
# and a real run died of it:
#
#   attempt 1  ceiling 2048  schema mismatch (terms missing `kind`)
#   attempt 2  ceiling 2048  cut off at 2048 -> raise
#
# The ceiling was never raised. A schema retry spent the allowance that
# truncation needed, so the one fix that was guaranteed to help — a bigger
# number — was the one thing never tried. They are counted apart now, because
# "the model wrote the wrong shape" and "the model ran out of room" have
# nothing to do with each other.
MAX_SCHEMA_RETRIES = 1
MAX_TRUNCATION_RETRIES = 2
# A stop, not a policy: a provider that ignores the schema AND rambles could
# otherwise cost 6 calls out of a 12-call budget.
MAX_ATTEMPTS = 4

# Kept short and imperative on purpose. A long apologetic correction reads as
# an invitation to deliberate, and on a reasoning model deliberation is spent
# from the same budget as the answer — which is how a stage that succeeded in
# 528 tokens failed at 2048 the next day, on the same posting.
_RETRY_NOTE = (
    "\n\nYour previous reply was rejected:\n{error}\n"
    "Fix only those fields. Do not explain. Return the JSON object and nothing "
    "else."
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

    Two failure modes, each with its own allowance (see `MAX_SCHEMA_RETRIES`
    and `MAX_TRUNCATION_RETRIES`): a wrong shape is answered by restating the
    error, running out of room is answered by a bigger ceiling. Neither is
    answered by a partial object — a half-parsed plan is worse than a visible
    error.
    """
    provider_schema = to_provider_schema(schema_model)
    kwargs: dict[str, Any] = {
        "system": system, "schema": provider_schema,
        "max_tokens": max_tokens, "temperature": temperature,
    }
    if stage:
        kwargs["stage"] = stage          # BudgetedClient records it; others ignore

    name = stage or schema_model.__name__
    attempt_user = user
    last_error = ""
    ceiling = max_tokens
    schema_retries = 0
    truncation_retries = 0

    for attempt in range(1, MAX_ATTEMPTS + 1):
        kwargs["max_tokens"] = ceiling
        try:
            response = _complete(client, user=attempt_user, **kwargs)
        except TypeError:
            kwargs.pop("stage", None)     # a client without the stage kwarg
            response = _complete(client, user=attempt_user, **kwargs)

        # Truncation is not a schema problem, and treating it as one sends you
        # to the prompt when the fix is a number. Retrying with the SAME
        # ceiling is guaranteed to fail the same way, so the ceiling is what
        # changes — and only if the provider told us it ran out of room.
        if response.was_truncated:
            logger.warning(
                "%s was cut off at %d tokens (finish_reason=%r)",
                name, ceiling, response.finish_reason,
            )
            room_left = ceiling < HARD_TOKEN_CEILING
            if truncation_retries < MAX_TRUNCATION_RETRIES and room_left:
                truncation_retries += 1
                ceiling = min(ceiling * 2, HARD_TOKEN_CEILING)
                logger.info("Retrying %s with a %d-token ceiling", name, ceiling)
                continue
            raise ResponseTruncated(
                f"{name} ran out of output room: the model was still writing "
                f"when it hit {ceiling} tokens, so the JSON is incomplete. "
                f"The raw response is saved in the run folder — if it begins "
                f"with reasoning rather than JSON, the model is spending the "
                f"budget thinking, so use a non-reasoning model for this "
                f"stage. Otherwise the input is too long for one call.",
                raw=response.text,
                stage=name,
            )

        try:
            return schema_model.model_validate_json(_strip(response.text))
        except (ValidationError, ValueError) as exc:
            last_error = str(exc)[:600]
            logger.warning("Schema mismatch from %s (attempt %d): %s",
                           name, attempt, last_error)
            if schema_retries >= MAX_SCHEMA_RETRIES:
                break
            schema_retries += 1
            attempt_user = user + _RETRY_NOTE.format(error=last_error)
            # A restated error makes the reply LONGER, not shorter: the model
            # now has corrections to apply on top of the object it already
            # struggled to finish. Raising the ceiling alongside the note is
            # what stops a schema retry from arriving as a truncation.
            ceiling = min(max(ceiling, max_tokens * 2), HARD_TOKEN_CEILING)

    raise SchemaValidationFailed(
        f"{name} did not return valid {schema_model.__name__} after "
        f"{attempt} attempt(s): {last_error}",
        raw=response.text,
        stage=name,
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
