"""Unified LLM client.

Supports two providers, switchable via `LLM_PROVIDER` in `.env`:
  - "groq"              : Groq's OpenAI-compatible API.
  - "openai_compatible" : any OpenAI-compatible endpoint (vLLM,
                          local servers, Ollama-compatible gateways, etc).

The resume parser can use its own model stack through dedicated
`RESUME_PARSER_*` settings so it can be pinned to Qwen3 or NuExtract 3
without changing tailoring/outreach behavior.
"""
import json
import logging
import re

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def _post_chat_completion(provider: str, base_url: str, model: str, api_key: str, system: str, user: str, json_mode: bool) -> str:
    url = f"{base_url.strip().rstrip('/')}/chat/completions"
    headers = {}
    if provider == "groq":
        if not api_key:
            raise RuntimeError("Groq requires LLM_API_KEY in .env")
        headers["Authorization"] = f"Bearer {api_key}"
    elif api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body = {
        "model": model.strip(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    logger.debug("POST %s model=%s json_mode=%s", url, model, json_mode)
    resp = httpx.post(url, headers=headers, json=body, timeout=300)
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError:
        if json_mode:
            logger.debug("json_mode failed, retrying without response_format")
            body.pop("response_format", None)
            resp = httpx.post(url, headers=headers, json=body, timeout=300)
            resp.raise_for_status()
        else:
            raise
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    logger.debug("LLM response length: %d chars", len(content))
    return content


def call_llm(
    system: str,
    user: str,
    json_mode: bool = True,
    provider: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> str:
    provider = (provider or settings.llm_provider).strip()

    if provider == "mock":
        raise RuntimeError(
            "LLM_PROVIDER='mock' is not supported. Configure a real LLM provider "
            "(groq or openai_compatible) in backend/.env."
        )

    base_url = (base_url or settings.llm_base_url).strip()
    model = (model or settings.llm_model).strip()
    api_key = settings.llm_api_key if api_key is None else api_key

    if provider == "groq" and not api_key:
        raise RuntimeError(
            "LLM_PROVIDER is set to groq but LLM_API_KEY is empty. Set it in .env."
        )

    return _post_chat_completion(provider, base_url, model, api_key or "", system, user, json_mode)


def call_resume_parser_llm(system: str, user: str, json_mode: bool = True) -> str:
    stack = (settings.resume_parser_provider or "openai_compatible").strip().lower()

    provider = (settings.resume_parser_provider or settings.llm_provider).strip()
    base_url = (settings.resume_parser_base_url or settings.llm_base_url).strip()
    model = (settings.resume_parser_model or settings.llm_model).strip()
    api_key = settings.resume_parser_api_key or settings.llm_api_key

    if stack == "qwen3" and not settings.resume_parser_model:
        model = "Qwen/Qwen3-8B-Instruct"
    elif stack == "nuextract3" and not settings.resume_parser_model:
        model = "NuExtract-3"

    logger.info("Resume parser calling %s %s (model=%s)", provider, base_url, model)
    result = call_llm(
        system=system,
        user=user,
        json_mode=json_mode,
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=api_key,
    )
    logger.info("Resume parser LLM returned %d chars", len(result))
    return result


def extract_json(text: str) -> dict:
    """LLMs sometimes wrap JSON in prose or code fences — strip that off."""
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()

    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                return json.loads(text[start : i + 1])

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    return json.loads(text)
