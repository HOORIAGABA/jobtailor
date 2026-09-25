"""Stage S2 — which of the candidate's bullets evidence which parts of the job.

Deterministic. No model, no network, no scalar score.

**Terms are the bridge.** Each job element (a problem, a success signal, a hard
requirement) mentions some terms; each bullet mentions some terms; a shared
term is evidence. Terms carry aliases, so "k8s" in a bullet matches a
requirement that says "Kubernetes".

Matching is **exact on an alias form, at word boundaries**. Never substring.
The previous version scored with `term in token or token in term`, so `ml`
matched `html` and `r` matched every word containing an r. Measured
consequence on a Machine Learning Engineer posting: a Barista entry scored 60
against an ML Engineer's 47.

There is deliberately **no coverage number** here. A score that rises when
terms are inserted into bullets makes keyword stuffing the optimal strategy,
and the model being measured is the one doing the inserting. The output is a
mapping — evidence for a human and context for the planner, not a target.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.domain.models import EvidenceIndex, EvidenceLink, JobBrief, ResumeDoc, Term
from app.engine.text import content_words, lower_tokens, ngrams

# A "hint" link needs at least this many shared meaningful words. Two is low
# enough to catch a genuine paraphrase and high enough that one incidental
# word ("data", "team") is not treated as evidence.
MIN_SHARED_WORDS = 2


# A skills section LISTS abilities; experience and projects DEMONSTRATE them.
# Only the second kind can be evidence — otherwise "Languages: Python, SQL"
# becomes strong proof that the candidate has done Python work, when all it
# proves is that they wrote the word down.
NON_EVIDENCE_KINDS = frozenset({"skills", "summary"})


def evidence_bullets(doc: ResumeDoc):
    """Bullets that can serve as evidence — everything except declarations."""
    for section in doc.sections:
        if section.kind in NON_EVIDENCE_KINDS:
            continue
        for item in section.items:
            yield from item.bullets


@dataclass(frozen=True)
class JobElement:
    """One addressable thing the job asks for."""
    id: str
    kind: Literal["problem", "signal", "requirement", "term"]
    text: str
    priority: str = ""


def job_elements(brief: JobBrief) -> list[JobElement]:
    """Give every part of the brief a stable id so links can point at it.

    `JobBrief` holds plain lists, so ids are positional within a brief — which
    is safe because a brief is immutable once built and cached by the posting's
    hash.
    """
    elements: list[JobElement] = []
    for i, p in enumerate(brief.problems_to_solve):
        elements.append(JobElement(f"problem.{i}", "problem", p.statement, p.priority))
    for i, s in enumerate(brief.success_signals):
        elements.append(JobElement(f"signal.{i}", "signal", s.statement))
    for i, r in enumerate(brief.hard_requirements):
        elements.append(JobElement(f"req.{i}", "requirement", r.statement))
    for t in brief.terms:
        elements.append(JobElement(f"term.{t.term.lower()}", "term", t.term))
    return elements


# ── alias matching ────────────────────────────────────────────────────

@dataclass
class AliasIndex:
    """Maps every surface form of every term to the term itself.

    Multi-word aliases ("apache airflow", "machine learning") become n-grams,
    so they match as phrases rather than as loose words.
    """
    by_form: dict[tuple[str, ...], str] = field(default_factory=dict)
    max_n: int = 1

    @classmethod
    def build(cls, terms: list[Term]) -> "AliasIndex":
        index = cls()
        for term in terms:
            for form in term.alias_forms():
                key = tuple(lower_tokens(form))
                if not key:
                    continue
                index.by_form[key] = term.term
                index.max_n = max(index.max_n, len(key))
        return index

    def find(self, text: str) -> set[str]:
        """Canonical terms present in `text`, matched exactly at word boundaries."""
        toks = lower_tokens(text)
        found: set[str] = set()
        for n in range(1, self.max_n + 1):
            for gram in ngrams(toks, n):
                if (term := self.by_form.get(gram)) is not None:
                    found.add(term)
        return found


# ── the stage ─────────────────────────────────────────────────────────

def compute_evidence(brief: JobBrief, doc: ResumeDoc) -> EvidenceIndex:
    """Link job elements to the bullets that evidence them.

    A link is `strong` when the two share a job term, and a `hint` when they
    merely share wording. A hint may inform the planner but must never be the
    sole justification for an edit — that distinction is what stops loose
    overlap being treated as proof.
    """
    index = AliasIndex.build(brief.terms)
    elements = job_elements(brief)

    bullets = [(b.id, b.text) for b in evidence_bullets(doc)]
    bullet_terms = {bid: index.find(text) for bid, text in bullets}
    bullet_words = {bid: content_words(text) for bid, text in bullets}

    # The skill inventory is a declaration, not a demonstration: it makes a
    # term `present` but never supplies an evidence bullet.
    declared = index.find(" , ".join(doc.skill_inventory))

    links: list[EvidenceLink] = []
    unmatched: list[str] = []

    for element in elements:
        element_terms = index.find(element.text)
        element_words = content_words(element.text)
        found_for_element: list[EvidenceLink] = []

        for bid, _text in bullets:
            shared_terms = element_terms & bullet_terms[bid]
            if shared_terms:
                found_for_element.append(EvidenceLink(
                    job_element_id=element.id, bullet_id=bid,
                    similarity=_overlap(element_words, bullet_words[bid]),
                    lexical_hit=True, strength="strong",
                ))
                continue

            shared_words = element_words & bullet_words[bid]
            if len(shared_words) >= MIN_SHARED_WORDS:
                found_for_element.append(EvidenceLink(
                    job_element_id=element.id, bullet_id=bid,
                    similarity=_overlap(element_words, bullet_words[bid]),
                    lexical_hit=False, strength="hint",
                ))

        if found_for_element:
            # Strongest first, so a consumer reading only the top link gets the
            # best evidence rather than whichever bullet happened to be first.
            found_for_element.sort(
                key=lambda l: (l.strength == "strong", l.similarity), reverse=True
            )
            links.extend(found_for_element)
        elif not (element.kind == "term" and element.text in declared):
            # A term that is declared in the skills section is not a gap, even
            # with no bullet demonstrating it.
            unmatched.append(element.id)

    return EvidenceIndex(links=links, unmatched_elements=unmatched)


def _overlap(a: set[str], b: set[str]) -> float:
    """Jaccard, for ordering links only — never aggregated into a score."""
    if not a or not b:
        return 0.0
    return round(len(a & b) / len(a | b), 4)


# ── consumer helpers ──────────────────────────────────────────────────
# Functions, not stored fields: nothing persists these, so nothing can
# optimise against them.

def strong_bullets(index: EvidenceIndex) -> set[str]:
    return {l.bullet_id for l in index.links if l.strength == "strong"}


def undemonstrated_terms(brief: JobBrief, index: EvidenceIndex) -> list[str]:
    """Terms with no bullet behind them — the honest gap list for the UI."""
    unmatched = set(index.unmatched_elements)
    return [t.term for t in brief.terms if f"term.{t.term.lower()}" in unmatched]


# ── how confident the gap list is allowed to sound ────────────────────

TermStanding = Literal["demonstrated", "declared_only", "not_found"]


def term_standing(
    brief: JobBrief, doc: ResumeDoc, index: EvidenceIndex,
) -> dict[str, TermStanding]:
    """Where each job term stands in this resume, in three honest grades.

    Matching here is lexical: a term is found when a bullet *names* it. So a
    line that demonstrates a capability without naming it —

        "Built nightly jobs with dependency handling and automatic retries"

    — produces nothing for `Airflow`, and the old two-way split then reported
    that as a flat gap. The candidate is told to go add something they already
    have, and the planner is handed the same false claim as context.

    Three grades instead of two, because the system's confidence genuinely
    differs between them:

    * `demonstrated`  — a bullet names it and shows the work. Certain.
    * `declared_only` — it is in the skills section, nothing demonstrates it.
      Also certain, and a different problem: a declaration is not evidence.
    * `not_found`     — this matcher did not find it. **Not the same as
      absent.** Until retrieval can recognise an unnamed capability, this
      grade is a prompt to look, not a verdict.

    The wording matters as much as the grouping. `not_found` is the only
    honest name for what lexical matching can conclude.
    """
    aliases = AliasIndex.build(brief.terms)
    declared = aliases.find(" , ".join(doc.skill_inventory))

    # Demonstrated means a BULLET LINKS to it — not merely "absent from the
    # unmatched list". Those differ precisely where it matters: a declared
    # skill is deliberately kept out of `unmatched_elements` so it is not
    # reported as a gap, and reading absence-from-unmatched as evidence would
    # grade every declared skill as demonstrated. Which is the exact claim
    # this module exists to refuse.
    linked = {link.job_element_id for link in index.links}

    standing: dict[str, TermStanding] = {}
    for term in brief.terms:
        if f"term.{term.term.lower()}" in linked:
            standing[term.term] = "demonstrated"
        elif term.term in declared:
            standing[term.term] = "declared_only"
        else:
            standing[term.term] = "not_found"
    return standing


def terms_by_standing(
    brief: JobBrief, doc: ResumeDoc, index: EvidenceIndex,
) -> dict[TermStanding, list[str]]:
    """The same answer grouped, for a UI that shows three lists."""
    out: dict[TermStanding, list[str]] = {
        "demonstrated": [], "declared_only": [], "not_found": [],
    }
    for term, grade in term_standing(brief, doc, index).items():
        out[grade].append(term)
    return out


def elements_by_bullet(index: EvidenceIndex) -> dict[str, list[str]]:
    """bullet id -> the job elements it evidences. The planner's view."""
    out: dict[str, list[str]] = {}
    for link in index.links:
        out.setdefault(link.bullet_id, []).append(link.job_element_id)
    return out
