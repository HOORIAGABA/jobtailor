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
    assert "was rejected" in client.calls[1]["user"]


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


def term(name: str, *, aliases=None, weight: int = 5, kind: str = "skill",
         required: bool = False) -> DraftTerm:
    """Every DraftTerm field is required now, so tests build them through here
    and still name only what they are actually testing."""
    return DraftTerm(term=name, aliases=list(aliases or []), weight=weight,
                     kind=kind, required=required)


def _draft(**over) -> JobBriefDraft:
    s, e = _span("own model retraining, which is currently manual and slow")
    base = dict(
        company="Nimbus", role="Machine Learning Engineer", seniority="",
        role_narrative="Owns the retraining pipeline end to end.",
        problems_to_solve=[DraftProblem(
            statement="own model retraining which is manual and slow",
            start=s, end=e, priority="core")],
        success_signals=[], hard_requirements=[],
        tone="pragmatic",
        terms=[
            term("PyTorch", aliases=["torch"], weight=10, required=True),
            term("Python", weight=9, required=True),
            term("pytorch", weight=3),                    # duplicate, different case
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
    brief = to_brief(_draft(terms=[term("Go", weight=99)]), JD)
    assert brief.terms[0].weight == 10


def test_unknown_enum_values_fall_back_safely():
    brief = to_brief(_draft(tone="jazzy", terms=[term("Go", kind="vibes")]), JD)
    assert brief.tone == "unclear"            # the escape hatch, not a crash
    assert brief.terms[0].kind == "skill"


def test_aliases_exclude_the_term_itself():
    brief = to_brief(_draft(terms=[
        term("Kubernetes", aliases=["k8s", "kubernetes", "  "])]), JD)
    assert brief.terms[0].aliases == ["k8s"]


def test_code_owns_excerpt_and_hash():
    """Fields the code sets are not in the schema the model sees."""
    assert "excerpt" not in JobBriefDraft.model_fields
    assert "source_hash" not in JobBriefDraft.model_fields
    brief = to_brief(_draft(), JD)
    assert brief.source_hash == hash_jd(JD) and brief.excerpt


def test_empty_lists_are_accepted_not_padded():
    """A vague posting should yield a thin brief, not invented requirements."""
    brief = to_brief(_draft(role="Engineer", company="", terms=[],
                            problems_to_solve=[]), JD)
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


# ══ field-name tolerance ══════════════════════════════════════════════

@pytest.mark.parametrize("alias", ["statement", "description", "signal", "requirement", "text"])
def test_the_brief_accepts_the_names_models_actually_use(alias):
    """Observed live: the model returned "description" and "signal" instead of
    "statement", costing a retry — a real request from a per-minute allowance.

    Being liberal here is safe because the CONTENT is verified separately: a
    span that does not support the statement is discarded whatever it is called.
    """
    draft = DraftGrounded.model_validate({alias: "own model retraining", "start": 0, "end": 10})
    assert draft.statement == "own model retraining"


def test_required_has_guidance_so_terms_are_not_all_optional():
    """Observed live: a posting saying "Required: Python, PyTorch, Airflow"
    came back with every term marked nice-to-have."""
    field = DraftTerm.model_fields["required"]
    assert field.description and "required" in field.description.lower()


# ── truncation detection ──────────────────────────────────────────────

def test_a_truncated_response_is_recognised():
    from app.io.llm import LLMResponse
    assert LLMResponse(text="{", finish_reason="length").was_truncated
    assert LLMResponse(text="{", finish_reason="MAX_TOKENS").was_truncated
    assert not LLMResponse(text="{}", finish_reason="stop").was_truncated
    assert not LLMResponse(text="{}").was_truncated


def test_finish_reason_survives_the_openai_response_parser():
    from app.io.llm import _parse_openai_response
    response = _parse_openai_response({
        "choices": [{"message": {"content": '{"a":1'}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4213},
    })
    assert response.was_truncated
    assert response.completion_tokens == 4213


class TruncatingClient:
    """Cut off every time, like a model that has run out of room."""

    def __init__(self) -> None:
        self.ceilings: list[int] = []

    def complete(self, *, system, user, schema=None, max_tokens=2048,
                 temperature=0.2, stage=""):
        from app.io.llm import LLMResponse
        self.ceilings.append(max_tokens)
        return LLMResponse(text='{"sections": [', completion_tokens=max_tokens,
                           finish_reason="length")


def test_truncation_raises_a_truncation_error_not_a_schema_error():
    from app.agents.base import call_structured
    from app.domain.errors import ResponseTruncated, SchemaValidationFailed

    with pytest.raises(ResponseTruncated) as excinfo:
        call_structured(TruncatingClient(), system="s", user="u",
                        schema_model=_Tiny, max_tokens=1000, stage="parse")
    assert "out of output room" in str(excinfo.value)
    assert not isinstance(excinfo.value, SchemaValidationFailed)


def test_a_truncated_call_is_retried_with_a_bigger_ceiling():
    """Retrying with the SAME ceiling is guaranteed to fail identically."""
    from app.agents.base import call_structured
    from app.domain.errors import ResponseTruncated

    client = TruncatingClient()
    with pytest.raises(ResponseTruncated):
        call_structured(client, system="s", user="u", schema_model=_Tiny,
                        max_tokens=1000, stage="parse")
    assert client.ceilings == [1000, 2000, 4000]


def test_the_retry_ceiling_is_capped():
    """At the hard ceiling there is nothing left to escalate to.

    The old loop still spent a second call at the identical ceiling, which
    could only fail the identical way — a wasted request from a per-minute
    free-tier allowance.
    """
    from app.agents.base import HARD_TOKEN_CEILING, call_structured
    from app.domain.errors import ResponseTruncated

    client = TruncatingClient()
    with pytest.raises(ResponseTruncated):
        call_structured(client, system="s", user="u", schema_model=_Tiny,
                        max_tokens=HARD_TOKEN_CEILING, stage="parse")
    assert client.ceilings == [HARD_TOKEN_CEILING]


class SchemaThenTruncateClient:
    """Wrong shape first, then cut off — the sequence that killed a real run.

    A provider that accepts a schema without enforcing it returns a plausible
    object missing a required key; the correction is restated; the model then
    deliberates over the correction and runs out of room. Under one shared
    retry budget the second failure was terminal and the ceiling never moved.
    """

    def __init__(self) -> None:
        self.ceilings: list[int] = []

    def complete(self, *, system, user, schema=None, max_tokens=2048,
                 temperature=0.2, stage=""):
        from app.io.llm import LLMResponse
        self.ceilings.append(max_tokens)
        if len(self.ceilings) == 1:
            return LLMResponse(text='{"wrong": 1}', finish_reason="stop")
        if len(self.ceilings) == 2:
            return LLMResponse(text='{"na', finish_reason="length")
        return LLMResponse(text='{"value": "ok"}', finish_reason="stop")


def test_a_schema_retry_does_not_spend_the_truncation_allowance():
    """The bug this encodes cost a real run its last stage.

    attempt 1  2048  schema mismatch   -> retry note, ceiling unchanged
    attempt 2  2048  cut off at 2048   -> raised, ceiling NEVER raised

    The one fix guaranteed to work — a bigger number — was the one thing the
    loop never tried, because a shape failure had already spent the budget.
    """
    from app.agents.base import call_structured

    client = SchemaThenTruncateClient()
    result = call_structured(client, system="s", user="u", schema_model=_Tiny,
                             max_tokens=1000, stage="job_brief")

    assert result.value == "ok"
    # The schema retry raises the ceiling too: a restated error makes the
    # reply longer, not shorter.
    assert client.ceilings == [1000, 2000, 4000]


def test_the_retry_note_forbids_explaining():
    """A reasoning model spends deliberation from the answer's budget."""
    from app.agents.base import _RETRY_NOTE

    note = _RETRY_NOTE.format(error="terms.0.kind: Field required")
    assert "Do not explain" in note
    assert "terms.0.kind" in note


# ── the fourth time the same lesson landed ────────────────────────────
# A real posting produced a brief that contradicted itself in one object:
#   company    ""        while recruiter_email was admin@annovasol.com
#   tone       "unclear" the default, untouched
#   required   False     on EVERY term — including Python, which the brief's
#                        own req.3 called "Strong proficiency in Python"
# Every field carried a default, so nothing forced the model to look.

def test_every_brief_field_is_required():
    from app.agents.job_brief import JobBriefDraft
    from app.io.schema import to_provider_schema

    required = set(to_provider_schema(JobBriefDraft)["required"])
    assert {"company", "role", "tone", "terms", "hard_requirements"} <= required


def test_every_term_field_is_required():
    from app.agents.job_brief import JobBriefDraft
    from app.io.schema import to_provider_schema

    schema = to_provider_schema(JobBriefDraft)
    required = set(schema["properties"]["terms"]["items"]["required"])
    assert required == {"term", "aliases", "weight", "kind", "required"}


def test_a_required_brief_field_may_still_be_empty():
    """Requiring the key does not invent a value — a posting that names no
    company still yields ""."""
    brief = to_brief(_draft(company="", terms=[], problems_to_solve=[]), JD)
    assert brief.company == ""


def test_the_prompt_forbids_a_broader_alias():
    from app.agents.job_brief import SYSTEM
    """`REST APIs` was given the alias `API`, so five bullets mentioning any
    API became STRONG evidence of REST experience."""
    assert "not an alias for" in SYSTEM
    assert "broader" in SYSTEM.lower()


def test_the_prompt_excludes_conditions_of_employment():
    from app.agents.job_brief import SYSTEM
    """The brief listed "Onsite, G-13, Islamabad" and "9:00 PM - 6:00 AM" as
    hard requirements, producing gaps no bullet can ever close."""
    assert "condition of employment" in SYSTEM
    assert "working hours" in SYSTEM


# ── a casual post is a normal input ───────────────────────────────────
# Span verification divided only by the STATEMENT's words, which punishes the
# model for the one thing a LinkedIn post forces: normalising loose phrasing.
# Measured: "Experience building APIs with FastAPI" pointing at "comfortable
# with FastAPI" scored 1/4 = 0.25 and was deleted. Four of those in a row
# leaves an empty brief, thin evidence, and an empty plan.

POST = (
    "We are hiring an AI Engineer!\n\n"
    "Looking for someone to build AI calling and voice agents with us. You "
    "should be strong in Python, comfortable with FastAPI, and have played "
    "with n8n.\n\n"
    "Onsite in Islamabad, night shift. DM me or mail admin@annovasol.com"
)


def _at(phrase: str) -> tuple[int, int]:
    start = POST.index(phrase)
    return start, start + len(phrase)


def _grounded(statement: str, phrase: str) -> bool:
    from app.domain.models import Grounded
    return Grounded(statement=statement, source_span=_at(phrase)).verify(POST, 0.3)


def test_a_normalised_reading_of_casual_wording_survives():
    assert _grounded("Experience building APIs with FastAPI",
                     "comfortable with FastAPI")
    assert _grounded("Hands-on experience with n8n and workflow automation",
                     "have played with n8n")


def test_a_near_verbatim_statement_still_survives():
    assert _grounded("Strong proficiency in Python", "strong in Python")
    assert _grounded("Develop and integrate AI calling and voice agents",
                     "build AI calling and voice agents")


def test_a_statement_the_span_does_not_support_is_still_refused():
    """The guarantee the check exists for. Loosening the direction must not
    loosen this."""
    assert not _grounded("Must have 5 years of Kubernetes in production",
                         "DM me or mail admin@annovasol.com")
    assert not _grounded("Requires a PhD in machine learning",
                         "Onsite in Islamabad")
    assert not _grounded("Experience with distributed training on GPU clusters",
                         "night shift")


def test_a_one_word_span_vouches_for_nothing():
    """Without a floor on the span, "experience" would support any statement
    containing that word — the loophole the whole check closes."""
    assert not _grounded("10 years of senior Python leadership", "Python")


def test_an_out_of_range_span_is_still_refused():
    from app.domain.models import Grounded
    assert not Grounded(statement="Python", source_span=(0, 99999)).verify(POST)
    assert not Grounded(statement="Python", source_span=(50, 10)).verify(POST)


def test_the_prompt_says_a_post_is_a_normal_input():
    from app.agents.job_brief import SYSTEM
    assert "may not be a formal posting" in SYSTEM.lower()
    assert "do not manufacture them" in SYSTEM


def test_the_prompt_asks_for_the_postings_own_words():
    from app.agents.job_brief import SYSTEM
    assert "KEEP THE POSTING'S OWN WORDS" in SYSTEM
    assert "the less the span supports you" in SYSTEM


def test_an_alias_that_is_another_term_is_refused():
    """`FastAPI: aliases=["Python"]` would make every Python bullet STRONG
    evidence of FastAPI experience. A strong link is supposed to be proof, so
    this is enforced in code, not asked for in the prompt."""
    brief = to_brief(_draft(terms=[
        term("Python", weight=10, required=True),
        term("FastAPI", aliases=["Python", "fast-api"], weight=9, required=True),
    ]), JD)
    fastapi = next(t for t in brief.terms if t.term == "FastAPI")
    assert "Python" not in fastapi.aliases
    assert "fast-api" in fastapi.aliases          # a real spelling survives


def test_a_self_alias_is_still_refused():
    brief = to_brief(_draft(terms=[term("Kubernetes", aliases=["kubernetes", "k8s"])]), JD)
    assert brief.terms[0].aliases == ["k8s"]


# ── the address is a regex, not judgment (D-20) ───────────────────────
# The model used to return `recruiter_email` and code checked it appeared
# literally. That catches a constructed careers@<domain>, but catching an
# invented value every time is worse than never asking for one.

POSTING_WITH_EMAILS = (
    "We are hiring an AI Engineer!\n"
    "Send your CV to careers@annovasol.com to apply.\n"
    "Questions? admin@annovasol.com\n"
    "Unsubscribe: noreply@linkedin.com\n"
    "Privacy: privacy@linkedin.com\n"
)


def test_the_model_is_not_asked_for_an_address():
    from app.agents.job_brief import JobBriefDraft
    from app.io.schema import to_provider_schema
    assert "recruiter_email" not in to_provider_schema(JobBriefDraft)["properties"]


def test_the_hiring_address_ranks_first():
    from app.engine.contact import best_recruiter_email, recruiter_candidates
    assert best_recruiter_email(POSTING_WITH_EMAILS) == "careers@annovasol.com"
    assert recruiter_candidates(POSTING_WITH_EMAILS) == [
        "careers@annovasol.com", "admin@annovasol.com",
        "noreply@linkedin.com", "privacy@linkedin.com",
    ]


def test_an_automated_address_is_ranked_last_not_dropped():
    """A posting whose only address is noreply@ should still show it, clearly
    last, rather than claiming there is no way to apply."""
    from app.engine.contact import recruiter_candidates
    assert recruiter_candidates("Apply via noreply@acme.com") == ["noreply@acme.com"]


def test_no_address_is_an_empty_list_not_a_guess():
    from app.engine.contact import best_recruiter_email, recruiter_candidates
    assert recruiter_candidates("Apply on our website") == []
    assert best_recruiter_email("Apply on our website") == ""


def test_an_address_cannot_be_constructed_any_more():
    """The failure this removes: careers@<domain> invented from a company name
    that never appeared as an address. There is no field to invent it in."""
    from app.engine.contact import recruiter_candidates
    assert recruiter_candidates("Annovasol is hiring. Visit annovasol.com") == []


def test_trailing_punctuation_is_stripped():
    from app.engine.contact import recruiter_candidates
    assert recruiter_candidates("Mail careers@acme.com.") == ["careers@acme.com"]


def test_the_alternatives_travel_with_the_brief():
    """So the gate can offer them instead of the system pretending to be sure."""
    brief = to_brief(_draft(terms=[]), POSTING_WITH_EMAILS)
    assert brief.recruiter_email == "careers@annovasol.com"
    assert len(brief.email_candidates) == 4


# ══ providers that accept a schema without enforcing it ═══════════════
# Two runs against antigravity/gemini-3.1-pro-low died after 3 model calls
# each, holding a complete brief, on two encoding differences. Neither is a
# malformed response: both are the other unambiguous spelling of the fact.

def _draft_payload(**over):
    base = {
        "company": "Annova Solutions", "role": "AI Engineer",
        "seniority": "Junior", "role_narrative": "Build ML-backed services.",
        "problems_to_solve": [], "success_signals": [],
        "hard_requirements": [], "tone": "pragmatic", "terms": [],
    }
    base.update(over)
    return base


def test_a_span_pair_is_accepted_as_start_and_end():
    """`{"span": [512, 572]}` == `{"start": 512, "end": 572}`."""
    from app.agents.job_brief import JobBriefDraft

    draft = JobBriefDraft.model_validate(_draft_payload(
        problems_to_solve=[{"statement": "deploy models", "span": [512, 572]}],
        hard_requirements=[{"statement": "Python", "source_span": [200, 230]}],
        success_signals=[{"statement": "ships weekly", "start": 1, "end": 9}],
    ))
    assert (draft.problems_to_solve[0].start, draft.problems_to_solve[0].end) == (512, 572)
    assert (draft.hard_requirements[0].start, draft.hard_requirements[0].end) == (200, 230)
    assert (draft.success_signals[0].start, draft.success_signals[0].end) == (1, 9)


def test_an_explicit_start_and_end_wins_over_a_span():
    from app.agents.job_brief import JobBriefDraft

    draft = JobBriefDraft.model_validate(_draft_payload(
        hard_requirements=[
            {"statement": "Python", "start": 1, "end": 9, "span": [99, 100]}
        ],
    ))
    assert (draft.hard_requirements[0].start, draft.hard_requirements[0].end) == (1, 9)


def test_a_malformed_span_is_not_guessed_at():
    """Coercion re-encodes two numbers. It does not invent them."""
    import pytest
    from pydantic import ValidationError
    from app.agents.job_brief import JobBriefDraft

    for bad in ([512], [512, 572, 600], "512-572", None):
        with pytest.raises(ValidationError):
            JobBriefDraft.model_validate(_draft_payload(
                hard_requirements=[{"statement": "Python", "span": bad}],
            ))


def test_a_missing_term_kind_falls_back_instead_of_killing_the_run():
    from app.agents.job_brief import JobBriefDraft

    draft = JobBriefDraft.model_validate(_draft_payload(terms=[
        {"term": "Python", "weight": 9, "aliases": [], "required": True},
    ]))
    assert draft.terms[0].kind == "skill"
    assert draft.terms[0].required is True


def test_kind_is_still_required_in_the_schema_the_model_sees():
    """The fallback is for providers that ignore `required`, not a retreat.

    An enforcing decoder must still be forced to choose a kind — that is the
    whole required-fields lesson. The fallback only decides what happens when
    the provider declined to enforce it.
    """
    from app.agents.job_brief import JobBriefDraft
    from app.io.schema import to_provider_schema

    schema = to_provider_schema(JobBriefDraft)
    assert "kind" in schema["properties"]["terms"]["items"]["required"]
    assert set(schema["properties"]["hard_requirements"]["items"]["required"]) == {
        "statement", "start", "end"
    }


def test_a_missing_required_flag_is_still_fatal():
    """`kind` can absorb a miss; `required` cannot.

    `kind` appears once downstream, as context in the planner payload. A wrong
    `required` is what once let a brief say `required=False` for Python while
    its own requirement read "Strong proficiency in Python".
    """
    import pytest
    from pydantic import ValidationError
    from app.agents.job_brief import JobBriefDraft

    with pytest.raises(ValidationError):
        JobBriefDraft.model_validate(_draft_payload(terms=[
            {"term": "Python", "weight": 9, "aliases": [], "kind": "skill"},
        ]))


def test_a_failed_stage_names_itself_on_the_error():
    """`05_model_response.txt` answers the wrong question."""
    from app.agents.base import call_structured
    from app.domain.errors import ResponseTruncated, SchemaValidationFailed

    with pytest.raises(ResponseTruncated) as truncated:
        call_structured(TruncatingClient(), system="s", user="u",
                        schema_model=_Tiny, max_tokens=1000, stage="job_brief")
    assert truncated.value.stage == "job_brief"
    assert truncated.value.raw

    with pytest.raises(SchemaValidationFailed) as bad_shape:
        call_structured(ScriptedClient(["nope", "still nope"]), system="s",
                        user="u", schema_model=_Tiny, stage="planner")
    assert bad_shape.value.stage == "planner"
