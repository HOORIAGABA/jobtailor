"""The OpenAI-compatible adapter — httpx MockTransport, no network.

Most free providers speak this API, so one adapter covers OpenRouter, Groq,
Cerebras, Together, Ollama and the various gateways. What actually differs
between them is structured-output support, which is what most of these tests
are about.
"""
import json

import httpx
import pytest

from app.config import Settings, check
from app.domain.errors import LLMRateLimited, LLMUnavailable
from app.io.llm import OpenAICompatibleClient, build_client

BASE = "https://example.test/v1"


def _ok(content: str = '{"value":"ok"}', prompt=11, completion=22) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


def _client(handler, **kw) -> OpenAICompatibleClient:
    """Wire httpx.post at the module level to a mock transport."""
    import app.io.llm as llm

    transport = httpx.MockTransport(handler)

    def fake_post(url, headers=None, json=None, timeout=None):
        with httpx.Client(transport=transport) as c:
            return c.post(url, headers=headers, json=json)

    llm.httpx.post = fake_post            # type: ignore[assignment]
    return OpenAICompatibleClient(api_key="k", model="some/model",
                                  base_url=BASE, **kw)


@pytest.fixture(autouse=True)
def _restore_httpx():
    import app.io.llm as llm
    original = llm.httpx.post
    yield
    llm.httpx.post = original


# ══ the happy path ════════════════════════════════════════════════════

def test_a_normal_call_parses_text_and_usage():
    client = _client(lambda r: httpx.Response(200, json=_ok()))
    out = client.complete(system="s", user="u", max_tokens=64)
    assert out.text == '{"value":"ok"}'
    assert (out.prompt_tokens, out.completion_tokens) == (11, 22)
    assert out.total_tokens == 33


def test_the_request_has_the_shape_these_apis_expect():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_ok())

    _client(handler).complete(system="be brief", user="hello", max_tokens=99,
                              temperature=0.4)
    assert seen["model"] == "some/model"
    assert seen["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello"},
    ]
    assert seen["max_tokens"] == 99 and seen["temperature"] == 0.4
    assert seen["auth"] == "Bearer k"


def test_a_system_prompt_is_omitted_when_empty():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_ok())

    _client(handler).complete(system="", user="hello")
    assert [m["role"] for m in seen["messages"]] == ["user"]


# ══ structured output: the part providers disagree on ═════════════════

SCHEMA = {"type": "object", "properties": {"value": {"type": "string"}}}


def test_a_full_json_schema_is_tried_first():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_ok())

    _client(handler).complete(system="s", user="u", schema=SCHEMA)
    assert seen["response_format"]["type"] == "json_schema"
    assert seen["response_format"]["json_schema"]["schema"] == SCHEMA


def test_it_degrades_to_json_object_when_the_schema_is_rejected():
    """Many endpoints accept "give me JSON" but not a full schema."""
    attempts = []

    def handler(request):
        body = json.loads(request.content)
        kind = (body.get("response_format") or {}).get("type")
        attempts.append(kind)
        if kind == "json_schema":
            return httpx.Response(400, json={"error": "response_format not supported"})
        return httpx.Response(200, json=_ok())

    out = _client(handler).complete(system="s", user="u", schema=SCHEMA)
    assert attempts == ["json_schema", "json_object"]
    assert out.text == '{"value":"ok"}'


def test_it_degrades_all_the_way_to_plain_text():
    attempts = []

    def handler(request):
        body = json.loads(request.content)
        kind = (body.get("response_format") or {}).get("type")
        attempts.append(kind)
        if kind is not None:
            return httpx.Response(400, json={"error": "unsupported"})
        return httpx.Response(200, json=_ok())

    out = _client(handler).complete(system="s", user="u", schema=SCHEMA)
    assert attempts == ["json_schema", "json_object", None]
    assert out.text


def test_the_working_style_is_remembered():
    """Climb the ladder once per client, not once per call."""
    attempts = []

    def handler(request):
        body = json.loads(request.content)
        kind = (body.get("response_format") or {}).get("type")
        attempts.append(kind)
        if kind == "json_schema":
            return httpx.Response(400, json={"error": "nope"})
        return httpx.Response(200, json=_ok())

    client = _client(handler)
    client.complete(system="s", user="u", schema=SCHEMA)
    client.complete(system="s", user="u", schema=SCHEMA)
    assert attempts == ["json_schema", "json_object", "json_object"]


def test_no_response_format_is_sent_when_no_schema_is_wanted():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_ok())

    _client(handler).complete(system="s", user="u")
    assert "response_format" not in seen


# ══ failures ══════════════════════════════════════════════════════════

def test_429_is_retried_then_reported_as_rate_limited(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, text="Rate limit reached. Please retry in 12s.")

    with pytest.raises(LLMRateLimited) as exc:
        _client(handler).complete(system="s", user="u")
    assert len(calls) == OpenAICompatibleClient.MAX_ATTEMPTS
    assert "12s" in str(exc.value)          # the provider's own number, surfaced


def test_a_400_without_a_schema_is_not_retried_forever():
    def handler(request):
        return httpx.Response(400, text="bad model id")

    with pytest.raises(LLMUnavailable) as exc:
        _client(handler).complete(system="s", user="u")
    assert "400" in str(exc.value) and "bad model id" in str(exc.value)


def test_a_401_says_so_rather_than_degrading():
    def handler(request):
        return httpx.Response(401, text="invalid api key")

    with pytest.raises(LLMUnavailable) as exc:
        _client(handler).complete(system="s", user="u", schema=SCHEMA)
    assert "401" in str(exc.value)


def test_an_unexpected_body_shape_is_reported_clearly():
    def handler(request):
        return httpx.Response(200, json={"nope": True})

    with pytest.raises(LLMUnavailable) as exc:
        _client(handler).complete(system="s", user="u")
    assert "Unexpected response shape" in str(exc.value)


def test_a_missing_base_url_fails_at_construction():
    with pytest.raises(LLMUnavailable) as exc:
        OpenAICompatibleClient(api_key="k", model="m", base_url="")
    assert "LLM_BASE_URL" in str(exc.value)


# ══ wiring ════════════════════════════════════════════════════════════

@pytest.mark.parametrize("provider", [
    "openai", "openai_compatible", "openrouter", "groq", "cerebras",
    "together", "ollama", "mistral", "custom",
])
def test_every_named_provider_builds_the_compatible_client(provider):
    client = build_client(Settings(
        llm_provider=provider, llm_api_key="k", llm_model="m", llm_base_url=BASE))
    assert isinstance(client, OpenAICompatibleClient)


def test_google_still_builds_the_google_client():
    from app.io.llm import GoogleClient
    client = build_client(Settings(
        llm_provider="google", llm_api_key="k", llm_model="gemini-2.5-flash-lite"))
    assert isinstance(client, GoogleClient)


def test_an_unknown_provider_names_the_alternatives():
    with pytest.raises(LLMUnavailable) as exc:
        build_client(Settings(llm_provider="acme", llm_api_key="k", llm_model="m"))
    assert "openrouter" in str(exc.value)


def test_config_requires_a_base_url_for_non_google_providers():
    problems = check(Settings(llm_provider="openai", llm_api_key="k", llm_model="m"))
    assert any("LLM_BASE_URL" in p for p in problems)
    assert check(Settings(llm_provider="openai", llm_api_key="k",
                          llm_model="m", llm_base_url=BASE)) == []


# ── test isolation ────────────────────────────────────────────────────
# These two pin the fix in tests/conftest.py. The test above passed on a clean
# checkout and in CI, and failed on any machine with a real `.env` — because
# `Settings()` loaded it and inherited a perfectly good LLM_BASE_URL. A failure
# that appears only once the project is configured correctly is the worst kind
# to leave unpinned, so the isolation itself is now asserted.

def test_settings_do_not_read_the_developers_dotenv():
    assert Settings.model_config.get("env_file") is None


def test_settings_are_empty_unless_a_test_sets_them():
    blank = Settings()
    assert blank.llm_api_key == ""
    assert blank.llm_model == ""
    assert blank.llm_base_url == ""


def test_ollama_does_not_need_a_key():
    """A local endpoint has nothing to authenticate against."""
    assert check(Settings(llm_provider="ollama", llm_model="llama3.1",
                          llm_base_url="http://localhost:11434/v1")) == []


def test_a_slash_in_a_model_id_is_valid():
    """Gateways namespace models: `antigravity/gemini-3.7-flash-medium`."""
    assert check(Settings(llm_provider="openai", llm_api_key="k",
                          llm_model="antigravity/gemini-3.7-flash-medium",
                          llm_base_url=BASE)) == []
