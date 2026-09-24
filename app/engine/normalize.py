"""RawResume -> ResumeDoc. Deterministic, no LLM.

The extraction model returns the document as printed: sections with verbatim
headings and no classification. This module does the interpreting, in code:

  1. map each heading to a kind (with `custom` as an honest fallback)
  2. assign stable ids from a monotonic counter
  3. build the skill inventory that grounds every later claim

Doing classification here rather than in the extraction prompt removes an entire
error class. A model asked to classify will occasionally file a project under
Experience, and then something downstream has to detect and undo that.
"""
from __future__ import annotations

import re
from typing import Iterable

from app.domain.ids import bullet_id, item_id, section_id
from app.domain.models import Bullet, Item, Kind, RawResume, ResumeDoc, Section

# ── heading -> kind ───────────────────────────────────────────────────

KIND_NAMES: dict[Kind, set[str]] = {
    "experience": {
        "experience", "work experience", "professional experience", "employment",
        "employment history", "work history", "career history", "relevant experience",
        "industry experience", "professional background",
    },
    "projects": {
        "projects", "personal projects", "selected projects", "side projects",
        "academic projects", "key projects", "portfolio",
    },
    "education": {
        "education", "academic background", "academic qualifications",
        "qualifications", "educational background",
    },
    "certifications": {
        "certifications", "certificates", "licenses", "licences",
        "licenses and certifications", "courses", "training",
    },
    "leadership": {
        "leadership", "volunteering", "volunteer experience", "activities",
        "extracurricular", "extracurricular activities", "community involvement",
    },
    "skills": {
        "skills", "technical skills", "core competencies", "competencies",
        "technologies", "tools", "tech stack", "areas of expertise",
        "technical proficiencies",
    },
    "summary": {
        "summary", "professional summary", "profile", "about", "objective",
        "career objective", "professional profile",
    },
}

# Substring probes, tried only after exact matching fails. Ordered — the first
# hit wins, so more specific probes must come first.
_PROBES: tuple[tuple[Kind, tuple[str, ...]], ...] = (
    ("experience", ("experience", "employment", "work history")),
    ("projects", ("project",)),
    ("education", ("education", "academic")),
    ("certifications", ("certificat", "licens", "course", "training")),
    ("leadership", ("leadership", "volunteer", "activit", "extracurricular")),
    ("skills", ("skill", "competenc", "technolog", "tech stack", "expertise")),
    ("summary", ("summary", "profile", "objective")),
)


def normalize_heading(heading: str) -> str:
    return " ".join((heading or "").lower().split()).strip(" :·-—–")


def classify_heading(heading: str) -> Kind:
    """Map a printed heading to a kind. Unknown headings become `custom`.

    `custom` is a feature, not a failure: the section survives under its own
    heading instead of being forced into a bucket it doesn't belong in.
    """
    h = normalize_heading(heading)
    if not h:
        return "custom"
    for kind, names in KIND_NAMES.items():
        if h in names:
            return kind
    for kind, probes in _PROBES:
        if any(p in h for p in probes):
            return kind
    return "custom"


# ── skill inventory ───────────────────────────────────────────────────

_SPLIT = re.compile(r"[,;|/•·]|\s{2,}|\s+[–—]\s+")
_MAX_SKILL_WORDS = 6
_LABEL_MAX_WORDS = 5


def split_skill_line(line: str) -> list[str]:
    """Split one skills line into individual terms.

    Handles the near-universal `Label: a, b, c` shape by dropping the label,
    then splits on the usual separators. Long fragments are prose, not skills.
    """
    text = " ".join((line or "").split())
    if not text:
        return []
    if ":" in text:
        head, _, tail = text.partition(":")
        if tail.strip() and len(head.split()) <= _LABEL_MAX_WORDS:
            text = tail
    out: list[str] = []
    for part in _SPLIT.split(text):
        part = part.strip().strip(".-_()[] ")
        if part and len(part.split()) <= _MAX_SKILL_WORDS:
            out.append(part)
    return out


def build_skill_inventory(sections: Iterable[Section]) -> list[str]:
    """Every skill term the document supports, in first-seen order.

    This is the grounding corpus for Class-C claims. Returns [] when the resume
    has no skills section — callers must treat empty as "cannot ground" and skip
    filtering, never as "reject everything".
    """
    inventory: list[str] = []
    seen: set[str] = set()
    for section in sections:
        if section.kind != "skills":
            continue
        for item in section.items:
            for text in (item.title, item.org, *(b.text for b in item.bullets)):
                for term in split_skill_line(text):
                    key = term.lower()
                    if key and key not in seen:
                        seen.add(key)
                        inventory.append(term)
    return inventory


# ── dates ─────────────────────────────────────────────────────────────

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_CURRENT = {"present", "current", "now", "ongoing", "to date", "till date"}
_DATE = re.compile(
    r"(?:(?P<mon>[a-z]{3,9})[a-z.]*\s+)?(?P<year>(?:19|20)\d{2})", re.IGNORECASE
)


def parse_date_range(raw: str) -> tuple[str, str]:
    """`"Jan 2022 - Present"` -> `("2022-01", "")`. Best-effort, never raises.

    An empty end means current. Unparseable input yields ("", "") — the display
    string on the item is preserved either way, so nothing is lost.
    """
    text = (raw or "").strip()
    if not text:
        return "", ""
    matches = list(_DATE.finditer(text))
    if not matches:
        return "", ""

    def iso(m: re.Match) -> str:
        year = m.group("year")
        mon = (m.group("mon") or "").lower()[:4].rstrip(".")
        num = _MONTHS.get(mon[:3]) if mon else None
        return f"{year}-{num:02d}" if num else year

    start = iso(matches[0])
    if any(c in text.lower() for c in _CURRENT):
        return start, ""
    end = iso(matches[-1]) if len(matches) > 1 else ""
    return start, end


# ── the transform ─────────────────────────────────────────────────────

def normalize(raw: RawResume) -> ResumeDoc:
    """Build the addressable document. Pure: `raw` is never mutated."""
    doc = ResumeDoc(contact=raw.contact, summary=raw.summary)
    kind_counts: dict[Kind, int] = {}

    for raw_section in raw.sections:
        kind = classify_heading(raw_section.heading)
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        # Only disambiguate when a kind genuinely repeats, so the common case
        # keeps the readable `sec.experience` form.
        suffix = kind_counts[kind] if kind_counts[kind] > 1 or kind == "custom" else None

        section = Section(
            id=section_id(kind, suffix),
            kind=kind,
            heading=raw_section.heading,
        )

        for raw_entry in raw_section.entries:
            iid = item_id(kind, doc.next_seq())
            start, end = parse_date_range(raw_entry.dates)
            item = Item(
                id=iid,
                title=raw_entry.title,
                org=raw_entry.org,
                dates=raw_entry.dates,
                date_start=start,
                date_end=end,
                bullets=[
                    Bullet(id=bullet_id(iid, n), text=text)
                    for n, text in enumerate(raw_entry.bullets, start=1)
                    if text and text.strip()
                ],
            )
            section.items.append(item)

        doc.sections.append(section)

    # A summary section carrying free text and no entries folds into the
    # document-level summary rather than becoming an empty section.
    if not doc.summary:
        for section in doc.sections:
            if section.kind == "summary" and section.items:
                doc.summary = " ".join(
                    t for i in section.items
                    for t in (i.title, *(b.text for b in i.bullets)) if t
                ).strip()
                break

    doc.skill_inventory = build_skill_inventory(doc.sections)
    return doc
