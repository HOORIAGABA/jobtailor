"""LLM plumbing and the JobBrief stage — with a scripted client, no network."""
import pytest
from pydantic import BaseModel, Field
from typing import Literal, Optional

from app.agents.base import call_structured, prompt_version
from app.agents.job_brief import (
    JobBriefDraft, DraftGrounded, DraftProblem, DraftTerm, MemoryBriefCache,
    build_job_brief, hash_jd, to_brief, verify_email,
)
from app.config import Settings, check
from app.domain.errors import BudgetExceeded, LLMUnavailable, SchemaValidationFailed
from app.io.llm import BudgetedClient, RunBudget, ScriptedClient, build_client
from app.io.schema import required_paths, to_provider_schema
from app.domain.models import JobBrief


# ══ config ════════════════════════════════════════════════════════════

def test_no_model_default_exists():
    """D-19. Two providers retired a hardcoded model id; there is no third time."""
    assert Settings.model_fields["llm_model"].default == ""


def test_check_names_the_missing_variable():
    problems = check(Settings(llm_api_key="", llm_model=""))
    assert any("LLM_API_KEY" in p for p in problems)
    assert any("LLM_MODEL" in p for p in problems)


def test_production_requires_secrets():
    problems = check(Settings(environment="production", llm_api_key="k", llm_model="m"))
    assert any("JWT_SECRET" in p for p in problems)
    assert check(Settings(llm_api_key="k", llm_model="m")) == []   # dev is lenient


def test_unknown_provider_fails_loudly():
    with pytest.raises(LLMUnavailable):
        build_client(Settings(llm_provider="acme", llm_api_key="k", llm_model="m"))


# ══ schema conversion ═════════════════════════════════════════════════

class _Inner(BaseModel):
    name: str
    span: tuple[int, int]


class _Outer(BaseModel):
    title: str = ""
    kind: Literal["a", "b"] = "a"
    only: Literal["fixed"] = "fixed"
    note: Optional[str] = None
    items: list[_Inner] = Field(default_factory=list)


def test_refs_are_inlined():
    """Providers reject $ref/$defs; a nested model must be expanded in place."""
    s = to_provider_schema(_Outer)
    assert "$defs" not in s and "$ref" not in str(s)
    assert s["properties"]["items"]["items"]["properties"]["name"]["type"] == "string"


def test_tuple_becomes_a_plain_array():
    """`tuple[int, int]` emits prefixItems, which the subset does not include."""
    inner = to_provider_schema(_Outer)["properties"]["items"]["items"]
    span = inner["properties"]["span"]
    assert span["type"] == "array" and span["items"]["type"] == "integer"
    assert "prefixItems" not in str(span)


def test_optional_becomes_nullable_not_anyof():
    note = to_provider_schema(_Outer)["properties"]["note"]
    assert note.get("nullable") is True and "anyOf" not in note


def test_literals_become_enums():
    s = to_provider_schema(_Outer)
    assert s["properties"]["kind"]["enum"] == ["a", "b"]
    assert s["properties"]["only"]["enum"] == ["fixed"]      # const -> enum


def test_unsupported_keys_are_dropped():
    assert "title" not in to_provider_schema(_Outer)
    assert "default" not in str(to_provider_schema(_Outer))


def test_the_real_draft_schema_converts():
    s = to_provider_schema(JobBriefDraft)
    assert "$ref" not in str(s)
    assert "problems_to_solve" in s["properties"]
    assert required_paths(s) is not None


# ══ call_structured ═══════════════════════════════════════════════════

class _Tiny(BaseModel):
    value: str


def test_valid_response_parses():
    client = ScriptedClient([{"value": "ok"}])
    assert call_structured(client, system="s", user="u", schema_model=_Tiny).value == "ok"


def test_markdown_fence_is_tolerated():
    client = ScriptedClient(['```json\n{"value": "ok"}\n```'])
    assert call_structured(client, system="s", user="u", schema_model=_Tiny).value == "ok"


def test_one_retry_with_the_error_restated():
    client = ScriptedClient(["not json at all", {"value": "second try"}])
    out = call_structured(client, system="s", user="u", schema_model=_Tiny)
    assert out.value == "second try"
    assert len(client.calls) == 2
    assert "did not match the required schema" in client.calls[1]["user"]


def test_two_failures_raise_rather_than_returning_partial():
    client = ScriptedClient(["garbage", "still garbage"])
    with pytest.raises(SchemaValidationFailed):
        call_structured(client, system="s", user="u", schema_model=_Tiny)


def test_schema_and_max_tokens_are_always_sent():
    client = ScriptedClient([{"value": "ok"}])
    call_structured(client, system="s", user="u", schema_model=_Tiny, max_tokens=512)
    assert client.calls[0]["schema"] is not None
    assert client.calls[0]["max_tokens"] == 512


def test_prompt_version_is_stable_and_changes_with_the_prompt():
    assert prompt_version("abc") == prompt_version("abc")
    assert prompt_version("abc") != prompt_version("abc ")


# ══ budget ════════════════════════════════════════════════════════════

def test_budget_blocks_the_eighth_call():
    client = BudgetedClient(ScriptedClient([{"value": "x"}] * 10), RunBudget(max_calls=3))
    for _ in range(3):
        client.complete(system="", user="u", stage="s")
    with pytest.raises(BudgetExceeded):
        client.complete(system="", user="u", stage="s")


def test_budget_tracks_tokens_and_logs_each_stage():
    budget = RunBudget(max_calls=5)
    client = BudgetedClient(ScriptedClient([{"v": 1}, {"v": 2}]), budget)
    client.complete(system="", user="u", stage="job_brief")
    client.complete(system="", user="u", stage="planner")
    assert budget.calls == 2 and budget.tokens == 60
    assert [e["stage"] for e in client.log] == ["job_brief", "planner"]


def test_token_ceiling_is_enforced():
    client = BudgetedClient(ScriptedClient([{"v": 1}]), RunBudget(max_tokens=10))
    with pytest.raises(BudgetExceeded):
        client.complete(system="", user="u")


# ══ job brief ═════════════════════════════════════════════════════════

JD = (
    "Nimbus is hiring a Machine Learning Engineer. "                      # 0-58
    "You will own model retraining, which is currently manual and slow. "
    "Required: Python, PyTorch and Airflow. Nice to have: Kubernetes. "
    "Send your application to careers@nimbus.example."
)


def _span(needle: str) -> tuple[int, int]:
    start = JD.index(needle)
    return start, start + len(needle)


def _draft(**over) -> JobBriefDraft:
    s, e = _span("own model retraining, which is currently manual and slow")
    base = dict(
        company="Nimbus", role="Machine Learning Engineer",
        recruiter_email="careers@nimbus.example",
        role_narrative="Owns the retraining pipeline end to end.",
        problems_to_solve=[DraftProblem(
            statement="own model retraining which is manual and slow",
            start=s, end=e, priority="core")],
        tone="pragmatic",
        terms=[
            DraftTerm(term="PyTorch", aliases=["torch"], weight=10, required=True),
            DraftTerm(term="Python", weight=9, required=True),
            DraftTerm(term="pytorch", weight=3),          # duplicate, different case
        ],
    )
    base.update(over)
    return JobBriefDraft(**base)


# ── the two code-enforced guarantees ──────────────────────────────────

def test_a_real_email_survives():
    assert verify_email("careers@nimbus.example", JD) == "careers@nimbus.example"


def test_an_invented_email_is_discarded():
    """The prompt says never derive one from a domain. This is what enforces it."""
    assert verify_email("hiring@nimbus.example", JD) == ""
    assert verify_email("recruiting@nimbus.com", JD) == ""
    assert verify_email("", JD) == ""


def test_a_grounded_problem_is_kept():
    brief = to_brief(_draft(), JD)
    assert len(brief.problems_to_solve) == 1
    assert brief.problems_to_solve[0].priority == "core"


def test_an_ungrounded_problem_is_dropped():
    """A span that points somewhere real but says something else must not pass."""
    s, e = _span("Nimbus is hiring")
    brief = to_brief(_draft(problems_to_solve=[DraftProblem(
        statement="deploy Kubernetes clusters across three regions", start=s, end=e)]), JD)
    assert brief.problems_to_solve == []


def test_an_out_of_range_span_is_dropped():
    brief = to_brief(_draft(problems_to_solve=[DraftProblem(
        statement="anything", start=0, end=99_999)]), JD)
    assert brief.problems_to_solve == []


# ── normalisation ─────────────────────────────────────────────────────

def test_terms_are_deduped_case_insensitively():
    brief = to_brief(_draft(), JD)
    assert [t.term for t in brief.terms] == ["PyTorch", "Python"]


def test_term_weights_are_clamped():
    brief = to_brief(_draft(terms=[DraftTerm(term="Go", weight=99)]), JD)
    assert brief.terms[0].weight == 10


def test_unknown_enum_values_fall_back_safely():
    brief = to_brief(_draft(tone="jazzy", terms=[DraftTerm(term="Go", kind="vibes")]), JD)
    assert brief.tone == "unclear"            # the escape hatch, not a crash
    assert brief.terms[0].kind == "skill"


def test_aliases_exclude_the_term_itself():
    brief = to_brief(_draft(terms=[
        DraftTerm(term="Kubernetes", aliases=["k8s", "kubernetes", "  "])]), JD)
    assert brief.terms[0].aliases == ["k8s"]


def test_code_owns_excerpt_and_hash():
    """Fields the code sets are not in the schema the model sees."""
    assert "excerpt" not in JobBriefDraft.model_fields
    assert "source_hash" not in JobBriefDraft.model_fields
    brief = to_brief(_draft(), JD)
    assert brief.source_hash == hash_jd(JD) and brief.excerpt


def test_empty_lists_are_accepted_not_padded():
    """A vague posting should yield a thin brief, not invented requirements."""
    brief = to_brief(JobBriefDraft(role="Engineer"), JD)
    assert brief.problems_to_solve == [] and brief.terms == []
    assert brief.role == "Engineer"


# ── the stage ─────────────────────────────────────────────────────────

def test_build_job_brief_end_to_end():
    client = ScriptedClient([_draft().model_dump()])
    brief = build_job_brief(JD, client)
    assert brief.role == "Machine Learning Engineer"
    assert brief.recruiter_email == "careers@nimbus.example"
    assert [t.term for t in brief.terms] == ["PyTorch", "Python"]
    assert client.calls[0]["temperature"] == 0.3


def test_the_cache_avoids_a_second_call():
    cache = MemoryBriefCache()
    client = ScriptedClient([_draft().model_dump()])
    first = build_job_brief(JD, client, cache)
    second = build_job_brief(JD, client, cache)     # would raise if it called again
    assert second.source_hash == first.source_hash
    assert len(client.calls) == 1


def test_whitespace_does_not_defeat_the_cache():
    assert hash_jd(JD) == hash_jd("  " + JD + "\n")


def test_empty_posting_short_circuits():
    client = ScriptedClient([])                     # any call would raise
    assert build_job_brief("   ", client) == JobBrief(source_hash=hash_jd(""))


def test_an_address_ending_a_sentence_still_verifies():
    """Regression: the regex swallowed the full stop, so a real address at the
    end of a sentence — the most common placement — failed verification."""
    from app.agents.job_brief import emails_in
    assert emails_in("Apply to careers@acme.com.") == {"careers@acme.com"}
    assert verify_email("careers@acme.com", "Apply to careers@acme.com.") == "careers@acme.com"


# ══ retry behaviour ═══════════════════════════════════════════════════

def test_retry_after_reads_the_providers_own_number():
    """Gemini states the wait twice; either spelling must be understood."""
    from app.io.llm import retry_after
    gemini = (
        "429 You exceeded your current quota. * Quota exceeded for metric: "
        "generate_content_free_tier_requests, limit: 5, model: gemini-3.8-flash "
        "Please retry in 28.424272419s."
    )
    assert retry_after(Exception(gemini)) == pytest.approx(28.42, abs=0.01)
    assert retry_after(Exception("violations { } retry_delay { seconds: 28 }")) == 28.0
    assert retry_after(Exception("Retry-After: 30")) == 30.0
    assert retry_after(Exception("something else entirely")) is None


def test_backoff_honours_the_provider_instead_of_guessing_shorter():
    """The bug this fixes: we waited 1.7s when the server asked for 28.

    On a per-minute quota an early retry does double damage — it fails, and it
    spends another request from the same allowance.
    """
    from app.io.llm import _backoff
    exc = Exception("Please retry in 28.4s")
    assert _backoff(1, exc) == pytest.approx(29.4, abs=0.01)
    assert _backoff(1, exc) > _backoff(1)          # longer than the guess


def test_backoff_is_capped_even_if_the_provider_asks_for_an_hour():
    from app.io.llm import MAX_RETRY_WAIT, _backoff
    assert _backoff(1, Exception("Please retry in 3600s")) == MAX_RETRY_WAIT


def test_backoff_falls_back_to_exponential_when_nothing_is_stated():
    from app.io.llm import _backoff
    assert 0 < _backoff(1) <= 2
    assert _backoff(3) > _backoff(1) / 2           # grows, jitter aside


# ══ model id format ═══════════════════════════════════════════════════

def test_a_display_name_is_rejected_before_any_call_is_made():
    """The third model-id problem in this project, caught locally this time.

    Pasting "Gemini 2.5 Flash Lite" from the console produced a 400 from
    Google reading "unexpected model name format" — minutes into a run, with a
    gRPC traceback. It is a config error and belongs at startup.
    """
    from app.config import check
    problems = check(Settings(llm_api_key="k", llm_model="Gemini 2.5 Flash Lite"))
    assert len(problems) == 1
    assert "display name" in problems[0]
    assert "gemini-2.5-flash-lite" in problems[0]      # tells you what to write


@pytest.mark.parametrize("model_id", [
    "gemini-2.5-flash-lite",
    "gemini-3.8-flash",
    "openai/gpt-oss-20b",
    "llama-3.1-8b-instant",
    "gpt-4o-mini",
])
def test_real_model_ids_pass(model_id):
    from app.config import check
    assert check(Settings(llm_api_key="k", llm_model=model_id)) == []


@pytest.mark.parametrize("bad", [
    "Gemini 2.5 Flash Lite",     # display name
    "Gemini-2.5-Flash",          # capitalised
    "gemini 2.5 flash",          # spaces
    "  ",                        # blank
])
def test_malformed_model_ids_are_caught(bad):
    from app.config import check
    assert check(Settings(llm_api_key="k", llm_model=bad))


def test_suggestion_converts_a_display_name():
    from app.config import suggest_model_id
    assert suggest_model_id("Gemini 2.5 Flash Lite") == "gemini-2.5-flash-lite"
    assert suggest_model_id("  Gemini   3.8  Flash ") == "gemini-3.8-flash"
