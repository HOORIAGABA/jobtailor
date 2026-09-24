"""The honesty guarantee. Deterministic, no LLM.

Every operation is checked here before anything is applied. The checks are
rule-based rather than model-judged for three reasons: these properties are
decidable, rules cost nothing, and an LLM judge is gameable by the same model it
judges.

Claims are not all the same, so they are not all checked the same way:

  Class A  asserted fact   numbers, employers, titles, dates
                           -> must appear verbatim in the origin. No inference.
  Class B  framing         describing real work in the role's language
                           -> must be entailed by cited evidence, and must not
                              escalate seniority or merely inject keywords.
  Class C  named capability  "Airflow", "PyTorch"
                           -> must be in the grounding corpus.

Treating all three alike is what made the earlier design block legitimate
rewording while still leaving an escalation loophole open.

A rejection is never a run failure. The original is kept, a question may be
raised with the candidate, and the run continues.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from app.domain.models import ResumeDoc
from app.domain.ops import (
    ADVISORY_OPS, DropBullet, Op, PromoteItem, Reject, ReorderBullets,
    ReorderItems, RewriteBullet, SetSkills, SetSummary,
)

# ── grounding corpus ──────────────────────────────────────────────────

def grounding_corpus(doc: ResumeDoc, confirmed: Iterable[str] = ()) -> set[str]:
    """THE single authority on what may be claimed.

    Two sources: what the document says, and what the candidate has explicitly
    confirmed. They were previously two parallel authorities, which is the same
    shape of bug as having three keyword tables — one function, one answer.

    `confirmed` comes from the capability ledger and is human-supplied. Inference
    may propose an entry; only a person may add one.
    """
    corpus = {s.lower().strip() for s in doc.skill_inventory if s.strip()}
    corpus |= {c.lower().strip() for c in confirmed if c and c.strip()}
    return corpus


# ── extraction helpers ────────────────────────────────────────────────

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")

# A leading dot is allowed so `.NET` survives as one token. Without it the
# token becomes `net`, which no longer matches a `.net` entry in the grounding
# corpus — and the rewrite gets rejected for naming a tool the resume declares.
_WORD = re.compile(r"(?:\.[A-Za-z]|[A-Za-z0-9])[A-Za-z0-9+#.\-]*")


def _tokens(text: str) -> list[str]:
    """Words with trailing sentence punctuation stripped, leading dot kept."""
    return [t.rstrip(".-") or t for t in _WORD.findall(text or "")]

# Words that are capitalized for grammatical reasons, not because they name
# something. Without this every sentence-initial word looks like an entity.
_NOT_ENTITIES = frozenset({
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "for", "with",
    "at", "by", "from", "as", "is", "are", "was", "were", "be", "been", "this",
    "that", "these", "those", "it", "we", "i", "my", "our", "their", "his",
    "her", "led", "built", "created", "designed", "developed", "implemented",
    "managed", "improved", "reduced", "increased", "delivered", "launched",
    "owned", "drove", "worked", "helped", "supported", "maintained", "wrote",
    "analyzed", "automated", "migrated", "scaled", "shipped", "used",
})


def numbers(text: str) -> set[str]:
    """Numeric literals, comma-normalized so `1,200` and `1200` compare equal."""
    return {m.group().replace(",", "") for m in _NUMBER.finditer(text or "")}


def entities(text: str) -> set[str]:
    """Candidate named things: proper nouns, acronyms, tool-shaped tokens.

    Deliberately over-inclusive on tokens that *look* like names and
    conservative about position: the first word of the text is skipped, since
    it is capitalized by convention.
    """
    tokens = _tokens(text)
    found: set[str] = set()
    for i, tok in enumerate(tokens):
        low = tok.lower()
        if low in _NOT_ENTITIES or len(tok) < 2:
            continue
        is_acronym = tok.isupper() and len(tok) >= 2
        is_titlecase = tok[0].isupper() and i > 0
        is_toolish = any(c in tok for c in "+#.") and any(c.isalpha() for c in tok)
        if is_acronym or is_titlecase or is_toolish:
            found.add(low)
    return found


def content_tokens(text: str) -> list[str]:
    return [t.lower() for t in _tokens(text)]


# ── Class B: seniority escalation ─────────────────────────────────────

WEAK_VERBS = frozenset({
    "assisted", "assist", "helped", "help", "supported", "support",
    "contributed", "contribute", "participated", "participate", "involved",
    "shadowed", "observed",
})

OWNERSHIP_VERBS = frozenset({
    "led", "lead", "drove", "drive", "owned", "own", "spearheaded", "spearhead",
    "headed", "head", "directed", "direct", "architected", "architect",
    "founded", "established", "orchestrated",
})

# Scope claims that quietly inflate impact when they weren't in the original.
SCOPE_MARKERS = (
    "team of", "company-wide", "companywide", "org-wide", "end-to-end",
    "end to end", "from scratch", "cross-functional", "enterprise-wide",
    "across the company", "at scale",
)


def escalates_seniority(origin: str, rewritten: str) -> str | None:
    """Return a reason when a rewrite inflates the candidate's role.

    "helped with the migration" -> "drove the migration" describes the same work
    and asserts no new number, so the Class-A check passes it. It is still a
    promotion the resume does not support.
    """
    o, r = set(content_tokens(origin)), set(content_tokens(rewritten))
    if (o & WEAK_VERBS) and (r & OWNERSHIP_VERBS) and not (o & OWNERSHIP_VERBS):
        return (
            f"origin uses {sorted(o & WEAK_VERBS)}; rewrite claims "
            f"{sorted(r & OWNERSHIP_VERBS)}"
        )
    ol, rl = (origin or "").lower(), (rewritten or "").lower()
    for marker in SCOPE_MARKERS:
        if marker in rl and marker not in ol:
            return f"rewrite adds unsupported scope claim {marker!r}"
    return None


# ── Class B: keyword stuffing ─────────────────────────────────────────

_STUFF_SIMILARITY = 0.85
_MAX_TERMS_PER_BULLET = 3
_MAX_TERM_DENSITY = 0.25


def _strip_terms(text: str, terms: Sequence[str]) -> list[str]:
    toks = content_tokens(text)
    drop = {t.lower() for term in terms for t in _tokens(term)}
    return [t for t in toks if t not in drop]


def _jaccard(a: Sequence[str], b: Sequence[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def is_keyword_stuffing(origin: str, rewritten: str, terms: Sequence[str]) -> bool:
    """True when the rewrite added keywords and changed nothing else.

    Strip the job terms from both sides. If what remains is essentially the same
    sentence, the rewrite did not change what the bullet *says* — it decorated
    it. That is the failure mode a coverage metric would have rewarded.
    """
    present_before = {t.lower() for t in terms if t.lower() in (origin or "").lower()}
    present_after = {t.lower() for t in terms if t.lower() in (rewritten or "").lower()}
    if not (present_after - present_before):
        return False
    return _jaccard(_strip_terms(origin, terms), _strip_terms(rewritten, terms)) > _STUFF_SIMILARITY


def term_density_violation(text: str, terms: Sequence[str]) -> str | None:
    low = (text or "").lower()
    hits = [t for t in terms if t and t.lower() in low]
    if len(hits) > _MAX_TERMS_PER_BULLET:
        return f"{len(hits)} job terms in one bullet (max {_MAX_TERMS_PER_BULLET})"
    words = len(content_tokens(text))
    if words and len(hits) / words > _MAX_TERM_DENSITY:
        return f"term density {len(hits)}/{words} exceeds {_MAX_TERM_DENSITY}"
    return None


# ── result ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ValidationResult:
    accepted: list[Op]
    rejected: list[Reject]

    @property
    def fabrication_count(self) -> int:
        return sum(
            1 for r in self.rejected
            if r.code in ("fabricated_number", "unsupported_entity")
        )


# ── per-op checks ─────────────────────────────────────────────────────

def _reject(op: Op, code, detail: str, ask: bool = False) -> Reject:
    return Reject(op_id=op.op_id, op_kind=op.op, code=code, detail=detail, ask_user=ask)


def _check_rewrite(op: RewriteBullet, doc: ResumeDoc, corpus: set[str]) -> Reject | None:
    origin = doc.bullet(op.bullet_id)
    if origin is None:
        return _reject(op, "unknown_id", f"no bullet {op.bullet_id!r}")
    if not (op.text or "").strip():
        return _reject(op, "unknown_id", "rewrite has no text")

    # Class A — numbers must come from the origin. This is why the writer is
    # not asked to declare which numbers it used: a self-report can lie.
    invented = numbers(op.text) - numbers(origin.text)
    if invented:
        return _reject(
            op, "fabricated_number",
            f"introduces {sorted(invented)} not present in the original bullet",
            ask=True,
        )

    # Class C — named things must be in the origin or the grounding corpus.
    unsupported = entities(op.text) - entities(origin.text) - corpus
    if unsupported:
        return _reject(
            op, "unsupported_entity",
            f"names {sorted(unsupported)} which the resume does not support",
            ask=True,
        )

    # Class B — framing may not inflate, and may not be decoration.
    if (reason := escalates_seniority(origin.text, op.text)):
        return _reject(op, "seniority_escalation", reason)
    if op.target_terms:
        if is_keyword_stuffing(origin.text, op.text, op.target_terms):
            return _reject(op, "keyword_stuffing",
                           "only keywords changed; the bullet says the same thing")
        if (reason := term_density_violation(op.text, op.target_terms)):
            return _reject(op, "term_density", reason)
    return None


def _check_reorder(op, doc: ResumeDoc) -> Reject | None:
    if isinstance(op, ReorderItems):
        section = doc.section(op.section_id)
        if section is None:
            return _reject(op, "unknown_id", f"no section {op.section_id!r}")
        existing = section.item_ids()
    else:
        item = doc.item(op.item_id)
        if item is None:
            return _reject(op, "unknown_id", f"no item {op.item_id!r}")
        existing = item.bullet_ids()
    # Multiset equality: a reorder can neither drop nor duplicate.
    if sorted(op.order) != sorted(existing):
        return _reject(op, "not_a_permutation",
                       f"expected a permutation of {existing}, got {op.order}")
    return None


def validate(
    ops: Sequence[Op],
    doc: ResumeDoc,
    confirmed_capabilities: Iterable[str] = (),
) -> ValidationResult:
    """Check every op against the base document. Never mutates anything."""
    corpus = grounding_corpus(doc, confirmed_capabilities)
    accepted: list[Op] = []
    rejected: list[Reject] = []
    promotions: dict[str, int] = {}

    for op in ops:
        if op.op in ADVISORY_OPS:
            accepted.append(op)          # never mutates; routes to the UI
            continue

        problem: Reject | None = None

        if isinstance(op, RewriteBullet):
            problem = _check_rewrite(op, doc, corpus)

        elif isinstance(op, (ReorderItems, ReorderBullets)):
            problem = _check_reorder(op, doc)

        elif isinstance(op, PromoteItem):
            if doc.item(op.item_id) is None:
                problem = _reject(op, "unknown_id", f"no item {op.item_id!r}")
            elif not op.rationale.strip():
                problem = _reject(op, "missing_citation",
                                  "promotion out of chronological order needs a rationale")
            else:
                section = doc.section_of_item(op.item_id)
                key = section.id if section else "?"
                promotions[key] = promotions.get(key, 0) + 1
                if promotions[key] > 1:
                    problem = _reject(op, "too_many_promotions",
                                      f"more than one promotion in {key}")

        elif isinstance(op, SetSkills):
            unsupported = {s for s in op.all_skills() if s.lower().strip() not in corpus}
            # An empty corpus means we cannot ground anything, so we do not
            # filter — rejecting every skill would be worse than allowing them.
            if corpus and unsupported:
                problem = _reject(op, "unsupported_entity",
                                  f"skills not evidenced by the resume: {sorted(unsupported)}",
                                  ask=True)

        elif isinstance(op, SetSummary):
            if not op.cites:
                problem = _reject(op, "missing_citation",
                                  "a summary must cite the bullets backing its claims")
            elif (missing := [c for c in op.cites if not doc.has(c)]):
                problem = _reject(op, "unknown_id", f"cites unknown ids {missing}")

        elif isinstance(op, DropBullet):
            item = doc.item(_safe_item_of(op.bullet_id))
            if item is None or doc.bullet(op.bullet_id) is None:
                problem = _reject(op, "unknown_id", f"no bullet {op.bullet_id!r}")
            elif len(item.bullets) <= 1:
                problem = _reject(op, "would_empty_item",
                                  "dropping this would leave the entry with no bullets")

        (rejected if problem else accepted).append(problem or op)

    return ValidationResult(accepted=accepted, rejected=rejected)


def _safe_item_of(bid: str) -> str:
    from app.domain.ids import item_id_of_bullet
    try:
        return item_id_of_bullet(bid)
    except ValueError:
        return ""
