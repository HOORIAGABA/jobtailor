"""Canonical domain types. No I/O, no LLM, no database.

Two resume shapes exist on purpose:

* `RawResume` mirrors the SOURCE DOCUMENT. Sections carry their heading exactly
  as printed and are not classified. This is what the extraction model returns.
* `ResumeDoc` is the DOMAIN MODEL, produced from `RawResume` by deterministic
  code: headings mapped to kinds, ids assigned, skill inventory built.

Keeping them separate means the model is never asked to classify. Classification
is judgment, and judgment inside an extraction stage is what forces a repair
layer downstream to move misfiled entries back.
"""
from __future__ import annotations

from typing import Iterator, Literal, Optional

from pydantic import BaseModel, Field

from app.domain.ids import item_id_of_bullet

Kind = Literal[
    "summary", "skills", "experience", "projects",
    "education", "certifications", "leadership", "custom",
]


# ── Contact ───────────────────────────────────────────────────────────

class Contact(BaseModel):
    full_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    website: str = ""


# ── Raw (document-mirroring) ──────────────────────────────────────────

class RawEntry(BaseModel):
    """One entry exactly as it appears: a job, a project, a degree."""
    title: str = ""
    org: str = ""
    dates: str = ""
    bullets: list[str] = Field(default_factory=list)


class RawSection(BaseModel):
    """A section of the source document. `heading` is verbatim, never normalized."""
    heading: str
    entries: list[RawEntry] = Field(default_factory=list)


class RawResume(BaseModel):
    """What the extraction stage returns. No ids, no classification."""
    contact: Contact = Field(default_factory=Contact)
    summary: str = ""
    sections: list[RawSection] = Field(default_factory=list)


# ── Domain model ──────────────────────────────────────────────────────

class Bullet(BaseModel):
    id: str
    text: str


class Item(BaseModel):
    id: str
    title: str = ""
    org: str = ""
    dates: str = ""
    date_start: str = ""          # ISO "YYYY-MM" when parseable
    date_end: str = ""            # "" means current
    bullets: list[Bullet] = Field(default_factory=list)

    def bullet_ids(self) -> list[str]:
        return [b.id for b in self.bullets]


class Section(BaseModel):
    id: str
    kind: Kind
    heading: str                  # still verbatim — rendered as the user wrote it
    items: list[Item] = Field(default_factory=list)

    def item_ids(self) -> list[str]:
        return [i.id for i in self.items]


class ResumeDoc(BaseModel):
    """The addressable resume. Immutable once confirmed (SDD S0.4)."""
    contact: Contact = Field(default_factory=Contact)
    summary: str = ""
    sections: list[Section] = Field(default_factory=list)

    skill_inventory: list[str] = Field(default_factory=list)
    seq: int = 0                  # monotonic id counter; never reset

    # ── lookup ────────────────────────────────────────────────────────

    def section(self, sid: str) -> Optional[Section]:
        return next((s for s in self.sections if s.id == sid), None)

    def section_of_kind(self, kind: Kind) -> Optional[Section]:
        return next((s for s in self.sections if s.kind == kind), None)

    def item(self, iid: str) -> Optional[Item]:
        for s in self.sections:
            for i in s.items:
                if i.id == iid:
                    return i
        return None

    def bullet(self, bid: str) -> Optional[Bullet]:
        """Resolve a bullet by id.

        Uses the item id embedded in the bullet id rather than scanning every
        bullet, so this stays O(items) on a malformed id instead of O(bullets).
        """
        try:
            iid = item_id_of_bullet(bid)
        except ValueError:
            return None
        item = self.item(iid)
        if item is None:
            return None
        return next((b for b in item.bullets if b.id == bid), None)

    def section_of_item(self, iid: str) -> Optional[Section]:
        return next((s for s in self.sections if any(i.id == iid for i in s.items)), None)

    # ── iteration ─────────────────────────────────────────────────────

    def all_items(self) -> Iterator[Item]:
        for s in self.sections:
            yield from s.items

    def all_bullets(self) -> Iterator[Bullet]:
        for i in self.all_items():
            yield from i.bullets

    def text_of(self, ref_id: str) -> str:
        """Resolve any addressable id to its text. '' when unknown."""
        b = self.bullet(ref_id)
        if b is not None:
            return b.text
        i = self.item(ref_id)
        if i is not None:
            return " ".join(x for x in (i.title, i.org) if x)
        s = self.section(ref_id)
        if s is not None:
            return s.heading
        return ""

    def has(self, ref_id: str) -> bool:
        return bool(self.text_of(ref_id)) or self.section(ref_id) is not None

    def next_seq(self) -> int:
        """Allocate the next id number. Mutates `seq` — callers own the copy."""
        self.seq += 1
        return self.seq


# ── Job side ──────────────────────────────────────────────────────────

class Term(BaseModel):
    """A term the job cares about.

    `aliases` is load-bearing: matching is exact against an alias form, never a
    substring. Substring matching is how `ml` comes to match `html`.
    """
    term: str
    aliases: list[str] = Field(default_factory=list)
    weight: int = 5               # 1..10
    kind: Literal["skill", "tool", "domain", "seniority", "credential"] = "skill"
    required: bool = False

    def alias_forms(self) -> list[str]:
        """Every surface form to match on, lowercased and deduped."""
        forms = {self.term.lower().strip()}
        forms.update(a.lower().strip() for a in self.aliases if a.strip())
        return sorted(f for f in forms if f)


class Grounded(BaseModel):
    """A claim about the job that must be traceable to the posting's own text."""
    statement: str
    source_span: tuple[int, int]

    def verify(self, jd_text: str, min_overlap: float = 0.3) -> bool:
        """True when the span is valid and actually supports the statement.

        Cheap, deterministic grounding check: the span must be in range and the
        statement's content words must overlap the quoted text. A model can
        still paraphrase, but it cannot cite a span that says something else.
        """
        start, end = self.source_span
        if not (0 <= start < end <= len(jd_text)):
            return False
        quoted = set(_content_words(jd_text[start:end]))
        claimed = set(_content_words(self.statement))
        if not claimed:
            return False
        return len(claimed & quoted) / len(claimed) >= min_overlap


class Problem(Grounded):
    priority: Literal["core", "supporting", "peripheral"] = "supporting"


class JobBrief(BaseModel):
    company: str = ""
    role: str = ""
    seniority: str = ""
    recruiter_email: str = ""     # verbatim only; verified in code, never trusted

    role_narrative: str = ""      # synthesis — may never be cited as a requirement
    problems_to_solve: list[Problem] = Field(default_factory=list)
    success_signals: list[Grounded] = Field(default_factory=list)
    hard_requirements: list[Grounded] = Field(default_factory=list)

    # Linguistic register — how the company talks. Named `tone` because
    # `register` shadows a BaseModel attribute in pydantic v2.
    # "unclear" is the escape hatch: without it a closed enum forces a
    # confident wrong answer on an ambiguous posting.
    tone: Literal["scrappy", "pragmatic", "formal", "academic", "unclear"] = "unclear"
    terms: list[Term] = Field(default_factory=list)

    excerpt: str = ""             # passed to the planner instead of the full posting
    source_hash: str = ""


# ── Evidence ──────────────────────────────────────────────────────────

class EvidenceLink(BaseModel):
    job_element_id: str
    bullet_id: str
    similarity: float = 0.0
    lexical_hit: bool = False
    strength: Literal["strong", "hint"] = "hint"


class EvidenceIndex(BaseModel):
    """Retrieval output. Deliberately carries NO scalar score.

    A coverage number rises when terms are inserted into bullets, so optimizing
    it is keyword stuffing — and the model being measured is the one doing the
    inserting. The field is absent from the type so no code can optimize it and
    no CI check can gate on it.
    """
    links: list[EvidenceLink] = Field(default_factory=list)
    unmatched_elements: list[str] = Field(default_factory=list)

    def for_bullet(self, bid: str) -> list[EvidenceLink]:
        return [l for l in self.links if l.bullet_id == bid]

    def for_element(self, eid: str) -> list[EvidenceLink]:
        return [l for l in self.links if l.job_element_id == eid]


# ── helpers ───────────────────────────────────────────────────────────

_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "for", "with",
    "at", "by", "from", "as", "is", "are", "was", "were", "be", "been", "being",
    "will", "would", "can", "could", "should", "may", "might", "must", "this",
    "that", "these", "those", "you", "your", "our", "we", "they", "their", "it",
})


def _content_words(text: str) -> list[str]:
    import re
    tokens = re.findall(r"[a-z0-9][a-z0-9+#.\-]*", (text or "").lower())
    return [t for t in tokens if t not in _STOPWORDS and len(t) > 1]
