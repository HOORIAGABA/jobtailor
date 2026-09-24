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

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.agents.base import as_json, call_structured, prompt_version
from app.domain.models import Grounded, JobBrief, Problem, Term
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


class DraftProblem(DraftGrounded):
    priority: str = Field(default="supporting",
                          description="core | supporting | peripheral")


class DraftTerm(BaseModel):
    term: str
    aliases: list[str] = Field(default_factory=list,
                               description="Other spellings, e.g. k8s for Kubernetes.")
    weight: int = Field(default=5, description="1-10, how much the posting emphasises it.")
    kind: str = Field(default="skill",
                      description="skill | tool | domain | seniority | credential")
    required: bool = Field(
        default=False,
        # Without this the field defaults to False and stays there, so a
        # posting saying "Required: Python, PyTorch, Airflow" comes back with
        # every term marked optional.
        description=(
            "true when the posting lists this under required/must-have, false "
            "when it is nice-to-have or merely mentioned. Read the posting's "
            "own wording — do not mark everything optional."
        ),
    )


class JobBriefDraft(BaseModel):
    company: str = ""
    role: str = ""
    seniority: str = ""
    recruiter_email: str = ""
    role_narrative: str = ""
    problems_to_solve: list[DraftProblem] = Field(default_factory=list)
    success_signals: list[DraftGrounded] = Field(default_factory=list)
    hard_requirements: list[DraftGrounded] = Field(default_factory=list)
    tone: str = Field(default="unclear",
                      description="scrappy | pragmatic | formal | academic | unclear")
    terms: list[DraftTerm] = Field(default_factory=list)


# ── prompt ────────────────────────────────────────────────────────────

SYSTEM = """\
You read one job posting and describe the role it is really advertising.

You are not summarising. You are explaining, to someone who will tailor a
resume for this job, what this person will actually do and what the team cares
about.

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
- recruiter_email: only if an address appears literally in the text. Never
  construct one from a domain or a name. "" otherwise. (This is verified in
  code and discarded if invented.)
- role_narrative: 3-6 sentences. What does this person do in a normal week?
  What problem exists that made the team open this role?
- problems_to_solve: concrete problems the hire will own. priority is "core"
  for the reasons the role exists, "supporting" for real but secondary work,
  "peripheral" for occasional duties.
- success_signals: what the posting says good looks like — outcomes, not tasks.
- hard_requirements: only genuinely disqualifying requirements. A "nice to
  have" is not a hard requirement.
- tone: how the company writes. "unclear" is a valid and often correct answer.
- terms: skills, tools, domain words and seniority markers worth matching a
  resume against. weight 1-10 by how much the posting emphasises each.
  aliases matter: give the other spellings a resume might use ("k8s" for
  "Kubernetes", "JS" for "JavaScript", "postgres" for "PostgreSQL").

Return only the JSON object."""

PROMPT_VERSION = prompt_version(SYSTEM)


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

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PRIORITIES = {"core", "supporting", "peripheral"}
_TONES = {"scrappy", "pragmatic", "formal", "academic", "unclear"}
_KINDS = {"skill", "tool", "domain", "seniority", "credential"}


def emails_in(text: str) -> set[str]:
    """Every address literally present, lowercased.

    Trailing sentence punctuation is stripped: postings routinely end with
    "...send your CV to careers@acme.com." and the raw match would then carry
    the full stop, so a perfectly real address would fail verification.
    """
    return {m.group().rstrip(".,;:").lower() for m in _EMAIL.finditer(text or "")}


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
    out: list[Term] = []
    seen: set[str] = set()
    for d in drafts:
        name = (d.term or "").strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        out.append(Term(
            term=name,
            aliases=sorted({a.strip() for a in d.aliases if a.strip() and a.strip().lower() != key}),
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
        recruiter_email=verify_email(draft.recruiter_email, jd_text),
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
    max_tokens: int = 2048,
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
