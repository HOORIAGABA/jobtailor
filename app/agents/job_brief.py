"""Stage S1 — understand the job.

The posting is the richest input this system gets, so it is **expanded**, not
compressed to keywords. An earlier design reduced it to a 15-row term table and
then over-interpreted the resume against it; that put the effort in exactly the
wrong place.

Everything the brief asserts as a *requirement* carries a `source_span` into
the posting, verified here in Python. Making the brief richer would otherwise
have made it unfalsifiable: a hallucinated `role_narrative` propagates into
every later stage with nothing to catch it.

Two guarantees are enforced in code, not asked for in the prompt:

* an element whose span does not support it is **dropped**
* `recruiter_email` survives only if it appears verbatim in the posting
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Protocol

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from app.agents.base import as_json, call_structured, prompt_version
from app.domain.models import Grounded, JobBrief, Problem, Term
from app.engine.contact import best_recruiter_email, emails_in, recruiter_candidates
from app.io.llm import LLMClient

logger = logging.getLogger(__name__)

EXCERPT_CHARS = 1500
MIN_SPAN_OVERLAP = 0.3


# ── what the model returns ────────────────────────────────────────────
# Narrower than JobBrief: `excerpt` and `source_hash` are owned by code, so
# they are not in the schema the model sees.

class DraftGrounded(BaseModel):
    # Models rename this field constantly — "description", "signal",
    # "requirement" — especially on endpoints that accept a schema without
    # enforcing it. Accepting the synonyms costs nothing and saves a retry,
    # which on a free tier is a real request from a per-minute allowance.
    # This latitude is only safe because the *content* is verified separately:
    # a span that does not support the statement is discarded either way.
    statement: str = Field(
        validation_alias=AliasChoices(
            "statement", "description", "signal", "requirement", "text", "summary"),
        description="One specific claim about the role, in a full sentence.",
    )
    start: int = Field(description="Character offset where the supporting text begins.")
    end: int = Field(description="Character offset where the supporting text ends.")

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def _accept_a_span_pair(cls, data):
        """`{"span": [512, 572]}` means the same as `{"start": 512, "end": 572}`.

        Measured on a real run: every one of 14 grounded items came back with
        `span` instead of the two integers, because the domain type it mirrors
        is `source_span: tuple[int, int]` and that is the obvious encoding for
        a pair of offsets. The model is not wrong; it picked the other
        unambiguous spelling of the same fact.

        This is coercion, not repair. Nothing is guessed: two numbers in, two
        numbers out, and `Grounded.verify` still checks the span against the
        posting afterwards. A malformed pair is left alone so validation
        reports it instead of a silent zero.
        """
        if not isinstance(data, dict):
            return data
        if "start" in data and "end" in data:
            return data
        pair = data.get("span") or data.get("source_span") or data.get("offsets")
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            merged = dict(data)
            merged["start"], merged["end"] = pair
            merged.pop("span", None)
            merged.pop("source_span", None)
            merged.pop("offsets", None)
            return merged
        return data


class DraftProblem(DraftGrounded):
    priority: str = Field(default="supporting",
                          description="core | supporting | peripheral")


class DraftTerm(BaseModel):
    """Every field required. See `JobBriefDraft` for why."""
    term: str
    aliases: list[str] = Field(
        description="Other SPELLINGS of the same thing: k8s for Kubernetes, "
                    "postgres for PostgreSQL. Never a BROADER word — 'API' is "
                    "not an alias for 'REST APIs', and offering one makes "
                    "every line mentioning any API count as evidence of REST "
                    "experience. Empty list when there are no other spellings."
    )
    weight: int = Field(description="1-10, how much the posting emphasises it.")
    kind: str = Field(description="skill | tool | domain | seniority | credential")
    required: bool = Field(
        description=(
            "true when the posting lists this under required/must-have, false "
            "when it is nice-to-have or merely mentioned. Read the posting's "
            "own wording — do not mark everything optional."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _kind_falls_back(cls, data):
        """A missing `kind` becomes "skill" rather than killing the brief.

        **This is not a retreat from the required-fields rule, and the
        difference is worth being precise about.** That rule exists because a
        Pydantic default keeps a field out of the schema's `required` array, so
        a constrained decoder never has to emit it and the model never looks.
        `kind` stays required *in the schema* — enforcing providers still force
        the model to choose.

        What changed is the knowledge that some endpoints accept a schema and
        do not enforce it. Against those, a hard requirement does not make the
        model look; it only turns a soft miss into a dead run. Two real runs
        against `antigravity/gemini-3.1-pro-low` died on this exact field,
        after 3 model calls apiece, with a complete and usable brief in hand.

        `kind` can absorb this and `required` cannot. `kind` appears in exactly
        one place downstream — context in the planner's payload — and gates no
        matching, no evidence, no validation. `required` is the field that once
        made a brief contradict itself (`required=False` on Python while its
        own requirement said "Strong proficiency in Python"), so it stays hard:
        a run that loses `required` should fail loudly.
        """
        if isinstance(data, dict) and not (data.get("kind") or "").strip():
            logger.warning(
                "Term %r came back without `kind` — the provider is not "
                "enforcing the schema; defaulting to \"skill\"",
                data.get("term", "?"),
            )
            return {**data, "kind": "skill"}
        return data


class JobBriefDraft(BaseModel):
    """Every field required, none defaulted.

    **A model fills what the schema forces and skips the rest.** This is the
    fourth place in this codebase the same lesson has landed, and the run that
    produced it is worth writing down: every field here carried a default, and
    a real posting came back with

        company    ""          while `recruiter_email` was admin@annovasol.com
        tone       "unclear"   the default, untouched
        required   False       on EVERY term — including Python, which the
                               brief's own req.3 called "Strong proficiency in
                               Python"

    The brief contradicted itself in one object, because `required` had a
    default and nothing forced the model to look. Requiring a key does not
    invent a value: "" and an empty list are still correct answers for a
    posting that says nothing.
    """
    company: str = Field(description="The hiring company. \"\" if not stated.")
    role: str = Field(description="The job title as written.")
    seniority: str = Field(description="Junior/mid/senior etc. \"\" if not stated.")
    role_narrative: str = Field(description="3-6 sentences on the actual job.")
    problems_to_solve: list[DraftProblem]
    success_signals: list[DraftGrounded]
    hard_requirements: list[DraftGrounded]
    tone: str = Field(description="scrappy | pragmatic | formal | academic | unclear")
    terms: list[DraftTerm]


# ── prompt ────────────────────────────────────────────────────────────

SYSTEM = """\
You read one job posting and describe the role it is really advertising.

You are not summarising. You are explaining, to someone who will tailor a
resume for this job, what this person will actually do and what the team cares
about.

WHAT YOU ARE READING MAY NOT BE A FORMAL POSTING
Half of these arrive as a LinkedIn post: a few sentences, no headings, casual
wording, requirements and responsibilities in the same breath. That is a normal
input, not a broken one.

When there are no separable sections, do not manufacture them. One sentence may
be the only problem, the only requirement and the only success signal in the
text — put it where it fits best and leave the other lists empty. Empty lists
are correct and expected here. Padding them with plausible-sounding items is
the single worst thing you can do, because everything downstream treats these
as real.

KEEP THE POSTING'S OWN WORDS
Write each statement close to how the posting says it. "comfortable with
FastAPI" can become "Experience with FastAPI"; it should not become "Proven
track record of designing production-grade REST services". The further you
drift, the less the span supports you, and an element whose span does not
support it is discarded.

GROUNDING
Every element of problems_to_solve, success_signals and hard_requirements must
carry character offsets (start, end) into the posting text that support it.
The quoted range must genuinely say what your statement claims. Offsets are
checked in code; anything that does not verify is discarded, so a wrong span is
worse than a missing element.

role_narrative is the one exception: it is your synthesis, and it is never
treated as a requirement.

NEVER PAD
There are no minimum counts. If the posting names two requirements, return two.
An empty list is a correct answer for a vague posting. Padding a list with
plausible-sounding items is the single worst thing you can do here, because
everything downstream treats these as real.

FIELDS
- company, role, seniority: as stated. "" if the posting does not say.
- role_narrative: 3-6 sentences. What does this person do in a normal week?
  What problem exists that made the team open this role?
- problems_to_solve: concrete problems the hire will own. priority is "core"
  for the reasons the role exists, "supporting" for real but secondary work,
  "peripheral" for occasional duties.
- success_signals: what the posting says good looks like — outcomes, not tasks.
- hard_requirements: only genuinely disqualifying requirements THE RESUME CAN
  ANSWER — capabilities and experience. A "nice to have" is not a hard
  requirement, and neither is a condition of employment: office location,
  working hours, shift times, visa status and salary are facts about the job,
  not things a candidate demonstrates in a bullet. Listing them produces gaps
  nobody can ever close.
- tone: how the company writes. "unclear" is a valid and often correct answer.
- terms: skills, tools, domain words and seniority markers worth matching a
  resume against. weight 1-10 by how much the posting emphasises each.
  aliases are other SPELLINGS of the same thing ("k8s" for "Kubernetes",
  "JS" for "JavaScript", "postgres" for "PostgreSQL"). Never a broader word:
  "API" is not an alias for "REST APIs". A broader alias makes every line
  mentioning anything in that family count as proof of the specific thing.
  Mark required true for anything the posting lists as required or must-have.

Return only the JSON object."""

PROMPT_VERSION = prompt_version(SYSTEM)

# Measured, not guessed: a complete brief for a 1081-character posting —
# 8 requirements, 6 terms, 1 problem — serialised to 1848 characters, about
# 530 completion tokens. The old 2048 ceiling was roughly 4x that and still
# produced a truncation, because the failure was never about brief size.
#
# The headroom here is for two things the measurement does not cover: a long
# posting (this one was short), and a reasoning model spending part of the
# budget before it writes anything. It is not a fix for truncation — that is
# `call_structured`'s escalating ceiling. It is a first attempt that does not
# need to escalate on an ordinary posting.
BRIEF_MAX_TOKENS = 4096


# ── cache ─────────────────────────────────────────────────────────────

class BriefCache(Protocol):
    def get(self, key: str) -> JobBrief | None: ...
    def set(self, key: str, brief: JobBrief) -> None: ...


class MemoryBriefCache:
    """Default in-process cache. The DB-backed one replaces it later."""

    def __init__(self) -> None:
        self._store: dict[str, JobBrief] = {}

    def get(self, key: str) -> JobBrief | None:
        return self._store.get(key)

    def set(self, key: str, brief: JobBrief) -> None:
        self._store[key] = brief


# ── verification (code, not prompt) ───────────────────────────────────

_PRIORITIES = {"core", "supporting", "peripheral"}
_TONES = {"scrappy", "pragmatic", "formal", "academic", "unclear"}
_KINDS = {"skill", "tool", "domain", "seniority", "credential"}


def verify_email(claimed: str, jd_text: str) -> str:
    """Keep the address only if it appears literally in the posting.

    The earlier version instructed the model never to derive an email from a
    domain name. It was prompt text with nothing enforcing it, and the system
    mailed real applications to addresses the model produced.
    """
    claimed = (claimed or "").strip().rstrip(".,;:").lower()
    if not claimed:
        return ""
    return claimed if claimed in emails_in(jd_text) else ""


def _grounded(items, jd_text: str, factory) -> list:
    """Convert draft elements to verified ones, dropping what does not check out."""
    kept = []
    for raw in items:
        candidate = factory(raw)
        if candidate.verify(jd_text, MIN_SPAN_OVERLAP):
            kept.append(candidate)
        else:
            logger.info("Dropped ungrounded element: %s", raw.statement[:80])
    return kept


def _terms(drafts: list[DraftTerm]) -> list[Term]:
    """Dedupe, clamp, and refuse an alias that belongs to another term.

    The prompt asks for aliases to be other *spellings*. A model that offers
    `FastAPI: aliases=["Python"]` is not misspelling anything — it is naming a
    second, broader term, and `engine.evidence` would then treat every bullet
    mentioning Python as STRONG evidence of FastAPI experience. A strong link
    is supposed to be proof, so this one is enforced in code rather than asked
    for: a rule that only lives in a prompt is a rule nothing keeps.
    """
    names = {(d.term or "").strip().lower() for d in drafts if (d.term or "").strip()}

    out: list[Term] = []
    seen: set[str] = set()
    for d in drafts:
        name = (d.term or "").strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)

        aliases: set[str] = set()
        for raw in d.aliases:
            alias = raw.strip()
            low = alias.lower()
            if not alias or low == key:
                continue
            if low in names:
                logger.info("Dropped alias %r on %r: it is another term", alias, name)
                continue
            aliases.add(alias)

        out.append(Term(
            term=name,
            aliases=sorted(aliases),
            weight=max(1, min(10, d.weight)),
            kind=d.kind if d.kind in _KINDS else "skill",
            required=bool(d.required),
        ))
    return out


def to_brief(draft: JobBriefDraft, jd_text: str) -> JobBrief:
    """Apply every code-side check. Pure — easy to test without a model."""
    return JobBrief(
        company=draft.company.strip(),
        role=draft.role.strip(),
        seniority=draft.seniority.strip(),
        # Not from the draft: an address is a regex, not judgment (D-20).
        recruiter_email=best_recruiter_email(jd_text),
        email_candidates=recruiter_candidates(jd_text),
        role_narrative=draft.role_narrative.strip(),
        problems_to_solve=_grounded(
            draft.problems_to_solve, jd_text,
            lambda r: Problem(
                statement=r.statement,
                source_span=(r.start, r.end),
                priority=r.priority if r.priority in _PRIORITIES else "supporting",
            ),
        ),
        success_signals=_grounded(
            draft.success_signals, jd_text,
            lambda r: Grounded(statement=r.statement, source_span=(r.start, r.end)),
        ),
        hard_requirements=_grounded(
            draft.hard_requirements, jd_text,
            lambda r: Grounded(statement=r.statement, source_span=(r.start, r.end)),
        ),
        tone=draft.tone if draft.tone in _TONES else "unclear",
        terms=_terms(draft.terms),
        excerpt=jd_text[:EXCERPT_CHARS],
        source_hash=hash_jd(jd_text),
    )


def hash_jd(jd_text: str) -> str:
    return hashlib.sha256(jd_text.strip().encode()).hexdigest()


# ── the stage ─────────────────────────────────────────────────────────

def build_job_brief(
    jd_text: str,
    client: LLMClient,
    cache: BriefCache | None = None,
    *,
    max_tokens: int = BRIEF_MAX_TOKENS,
) -> JobBrief:
    """One model call, cached on the posting's hash.

    Re-running the same posting costs nothing, which makes iterating on later
    prompts cheap — the expensive stage is skipped.
    """
    text = (jd_text or "").strip()
    if not text:
        return JobBrief(source_hash=hash_jd(""))

    key = hash_jd(text)
    if cache is not None and (hit := cache.get(key)) is not None:
        logger.info("Job brief cache hit for %s", key[:8])
        return hit

    draft = call_structured(
        client,
        system=SYSTEM,
        user=as_json({"posting": text}),
        schema_model=JobBriefDraft,
        max_tokens=max_tokens,
        temperature=0.3,
        stage="job_brief",
    )
    brief = to_brief(draft, text)

    logger.info(
        "Job brief: %s at %s — %d problems, %d signals, %d requirements, %d terms",
        brief.role or "?", brief.company or "?",
        len(brief.problems_to_solve), len(brief.success_signals),
        len(brief.hard_requirements), len(brief.terms),
    )

    if cache is not None:
        cache.set(key, brief)
    return brief
