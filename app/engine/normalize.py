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
        # "ABOUT ME" is the Europass default and was falling through to
        # `custom`, so the document-level summary was never derived from it.
        "about me", "personal statement", "personal profile", "career summary",
        "profile summary", "summary of qualifications", "executive summary",
        "introduction", "overview",
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
    # "about" comes after the probes above so "About my projects" is still a
    # projects section — the first hit wins, and specificity comes first.
    ("summary", ("summary", "profile", "objective", "about me", "statement")),
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

_SEPARATOR_CHARS = frozenset(",;|/•·")
# Only ever a separator when spaced on both sides — see `_split_top_level`.
_DASHES = frozenset("-–—")
_MAX_SKILL_WORDS = 6
_LABEL_MAX_WORDS = 5


# Filler that is not a skill. "etc" showed up in a real grounding corpus,
# where it would have been a term the candidate was allowed to claim.
_NOT_A_SKILL = frozenset({
    "etc", "and more", "others", "other", "among others", "e.g", "i.e", "and",
})

# A word that names a CATEGORY of skills rather than a skill. On its own any of
# these is fine — "RAG systems" is a real thing to know — so the rule below
# also requires a conjunction, which is what makes a phrase a heading:
# `Python & libraries` introduces a list, `Ensemble Methods` is an item in one.
_CATEGORY_NOUNS = frozenset({
    "libraries", "library", "frameworks", "framework", "tools", "toolchain",
    "technologies", "tech", "stack", "skills", "competencies", "platforms",
})


def _is_category_label(term: str) -> bool:
    """`"Python & libraries"` is a heading for the items in its parentheses.

    It reached the grounding corpus as a claimable skill, and from there the
    protected-skill set that `set_skills` must preserve — so a planner was
    being asked to print `Python & libraries` on a resume as though it were a
    technology.
    """
    words = term.lower().replace("&", " and ").split()
    if len(words) < 3 or "and" not in words:
        return False
    return words[-1] in _CATEGORY_NOUNS


def _split_top_level(text: str) -> list[str]:
    """Split on separators that are NOT inside brackets.

    Measured on a real resume: splitting blindly turned

        "YOLO (multi-modal, 4-channel), Transfert learning (VGG16, VGG19)"

    into `YOLO (multi-modal`, `4-channel)`, `Transfert learning (VGG16`, … —
    unbalanced fragments that went straight into the grounding corpus, which
    is the single authority on what the candidate may claim. A corrupted
    corpus rejects honest claims and admits nonsense ones.
    """
    parts: list[str] = []
    buffer: list[str] = []
    depth = 0
    for index, char in enumerate(text):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)

        if depth == 0 and char in _SEPARATOR_CHARS:
            parts.append("".join(buffer))
            buffer = []
            continue

        # A SPACED dash separates two things; an unspaced one is inside a word.
        # This used to be dismissed in a comment claiming whitespace collapsing
        # had already removed such dashes — it collapses *runs* of spaces and
        # leaves single ones, so `FastAPI - Uvicorn` arrived here intact and
        # became one skill. Neither half could then match a posting asking for
        # either. `CI-friendly testing` and `multi-modal RGB-Thermal pipelines`
        # are untouched, which is what requiring the spaces buys.
        if (depth == 0 and char in _DASHES
                and buffer and buffer[-1].isspace()
                and index + 1 < len(text) and text[index + 1].isspace()):
            parts.append("".join(buffer))
            buffer = []
            continue

        buffer.append(char)
    parts.append("".join(buffer))
    return parts


def split_skill_line(line: str) -> list[str]:
    """Split one skills line into individual terms.

    Handles the near-universal `Label: a, b, c` shape by dropping the label,
    then splits on separators outside brackets. A parenthesised list is read
    as examples of the term before it, so both survive:

        "Transfer learning architectures (VGG16, ResNet50)"
        -> ["Transfer learning architectures", "VGG16", "ResNet50"]

    Long fragments are prose, not skills.
    """
    text = " ".join((line or "").split())
    if not text:
        return []
    if ":" in text:
        head, _, tail = text.partition(":")
        if tail.strip() and len(head.split()) <= _LABEL_MAX_WORDS:
            text = tail

    out: list[str] = []
    seen: set[str] = set()

    def keep(term: str) -> None:
        term = term.strip().strip(".,;-_()[]{} ")
        key = term.lower()
        if (term and key not in seen and key not in _NOT_A_SKILL
                and not _is_category_label(term)
                and len(term.split()) <= _MAX_SKILL_WORDS):
            seen.add(key)
            out.append(term)

    for part in _split_top_level(text):
        part = part.strip()
        if not part:
            continue
        # A nested label. Only the FIRST colon was stripped above, and real
        # skills lines carry more than one:
        #
        #   "Backend & Production Pipelines: Python FastAPI, API Python
        #    frameworks: Flask, FastAPI - Uvicorn"
        #
        # The outer label goes, then the comma split leaves
        # "API Python frameworks: Flask" as a single term. That exact string
        # entered the grounding corpus and was later echoed by the planner
        # into a proposed resume edit, so the fix is upstream of both.
        part = _drop_inner_label(part)
        if not part:
            continue
        # "Term (a, b, c)" is a term plus its examples. Both are claimable —
        # unless the parenthetical is describing the term rather than listing
        # instances of it. See `_is_modifier`.
        if (open_at := part.find("(")) != -1 and part.rstrip().endswith(")"):
            keep(part[:open_at])
            for inner in _split_top_level(part[open_at + 1:].rstrip().rstrip(")")):
                if not _is_modifier(inner):
                    keep(inner)
        else:
            keep(part)
    return out


def _is_modifier(term: str) -> bool:
    """A parenthetical that describes the term instead of naming an instance.

    Two readings of the same bracket, both real, from one resume:

        "Python & libraries (Numpy, Matplotlib, Pytorch)"   -> instances
        "YOLO (multi-modal / 4-channel)"                    -> description

    `Numpy` is a thing to know; `multi-modal` is an adjective about how YOLO was
    used, and it entered the grounding corpus as a skill the candidate could
    claim and the planner had to preserve.

    The discriminator is deliberately narrow: lowercase-initial AND hyphenated.
    A library is a proper noun and keeps its capital, so nothing real is caught;
    the hyphen requirement then protects the genuinely lowercase tool names —
    `n8n`, `numpy`, `zapier` — that would otherwise be lost to the first half
    of the rule alone.
    """
    body = term.strip()
    if not body or "-" not in body:
        return False
    return not body[0].isupper()


def _drop_inner_label(part: str) -> str:
    """`"API Python frameworks: Flask"` -> `"Flask"`.

    The head is a category, not a skill — keeping it produces a corpus entry
    no resume ever prints. Only applied when the head looks like a label
    (short, and outside brackets), so `"C++: the language"` style oddities and
    ratios are left alone.
    """
    if ":" not in part:
        return part
    head, _, tail = part.partition(":")
    if tail.strip() and len(head.split()) <= _LABEL_MAX_WORDS:
        return tail.strip()
    return part


def existing_skills(doc) -> dict[str, str]:
    """Every skill currently printed in the skills section: `{lower: as written}`.

    Read from the section itself rather than from `skill_inventory`, because
    the inventory is a derived, split-and-cleaned view of the whole document.
    The question this answers is "what would the candidate notice missing from
    their resume", and that is answered by what the page prints.

    Two callers need it and they need it for opposite reasons: the validator,
    to refuse an edit that drops a skill, and the applier, to restore the
    resume's own capitalisation over the planner's lowercased echo.
    """
    found: dict[str, str] = {}
    for section in doc.sections:
        if section.kind != "skills":
            continue
        for item in section.items:
            for bullet in item.bullets:
                for term in split_skill_line(bullet.text):
                    found.setdefault(skill_key(term), term)
    return found


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

# Numeric dates: `13/08/2026`, `08-2026`, `2026/08`. Europass — which a lot of
# resumes are built with — writes every date this way, and the month-name
# pattern above finds only the year in them. A role running March to August
# then reads as 2026 to 2026, so two jobs at the same company in one year
# cannot be ordered.
_NUMERIC_DATE = re.compile(
    r"\b(?:"
    r"(?P<d>\d{1,2})[/.\-](?P<m>\d{1,2})[/.\-](?P<y>(?:19|20)\d{2})"   # d/m/y
    r"|(?P<m2>\d{1,2})[/.\-](?P<y2>(?:19|20)\d{2})"                    # m/y
    r"|(?P<y3>(?:19|20)\d{2})[/.\-](?P<m3>\d{1,2})"                    # y/m
    r")\b"
)


def _numeric_iso(m: re.Match) -> str:
    """One numeric date to `YYYY-MM`, or `YYYY` when the month is unusable.

    `01/03/2026` is ambiguous: 1 March in most of the world, 3 January in the
    US. The rule is the only one available without knowing the author —
    whichever component cannot be a month decides, and a genuinely ambiguous
    pair is read day-first, which is the convention of the formats that write
    dates this way at all.
    """
    if m.group("y2"):
        year, month = m.group("y2"), int(m.group("m2"))
    elif m.group("y3"):
        year, month = m.group("y3"), int(m.group("m3"))
    else:
        year = m.group("y")
        first, second = int(m.group("d")), int(m.group("m"))
        month = first if second > 12 else second
    return f"{year}-{month:02d}" if 1 <= month <= 12 else year


def parse_date_range(raw: str) -> tuple[str, str]:
    """`"Jan 2022 - Present"` -> `("2022-01", "")`. Best-effort, never raises.

    An empty end means current. Unparseable input yields ("", "") — the display
    string on the item is preserved either way, so nothing is lost.
    """
    text = (raw or "").strip()
    if not text:
        return "", ""

    ongoing = any(c in text.lower() for c in _CURRENT)

    # Numeric first: its matches contain years that the month-name pattern
    # would also match, in which case only the year survives.
    if numeric := list(_NUMERIC_DATE.finditer(text)):
        start = _numeric_iso(numeric[0])
        if ongoing:
            return start, ""
        return start, (_numeric_iso(numeric[-1]) if len(numeric) > 1 else "")

    matches = list(_DATE.finditer(text))
    if not matches:
        return "", ""

    def iso(m: re.Match) -> str:
        year = m.group("year")
        mon = (m.group("mon") or "").lower()[:4].rstrip(".")
        num = _MONTHS.get(mon[:3]) if mon else None
        return f"{year}-{num:02d}" if num else year

    start = iso(matches[0])
    if ongoing:
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

    for section in doc.sections:
        if section.kind == "skills":
            section.items = regroup_skills(section.items, doc)

    doc.skill_inventory = build_skill_inventory(doc.sections)
    return doc


# A slash between two letters is part of a word — `AI/ML`, `RGB/Thermal`,
# `CI/CD` — not a list separator. Measured: without this, the heading
# `AI/ML Frameworks & Techniques` read as a two-item list, stayed a bullet,
# and entered the grounding corpus as the two skills `AI` and
# `ML Frameworks & Techniques`.
_LIST_SEPARATOR = re.compile(r"[,;|•·]|(?<=\s)/|/(?=\s)")


def _looks_like_a_group_heading(text: str) -> bool:
    """A skills line with no list in it is a heading for the lines below."""
    line = " ".join((text or "").split())
    if not line or line.endswith("."):
        return False
    if _LIST_SEPARATOR.search(line):
        return False
    return len(line.split()) <= _LABEL_MAX_WORDS


def skill_key(term: str) -> str:
    """The form two spellings of one skill must share to count as the same.

    Case and surrounding punctuation are noise here; `Pytorch` and `PyTorch`
    are not two skills. Kept deliberately shallow — no stemming, no synonyms —
    because this key decides whether an edit is refused for dropping a skill,
    and a clever key that quietly equates two different tools would let a real
    deletion through.
    """
    return " ".join((term or "").lower().split()).strip(".,;:|-_()[]{} ")


def regroup_skills(items: list[Item], doc: ResumeDoc) -> list[Item]:
    """Turn heading-shaped bullets back into the groups they were printed as.

    **A real resume, parsed:** the whole skills section arrived as one entry
    whose bullets interleaved headings with their contents —

        'Computer Vision & Image Processing'   <- title
        'YOLO (multi-modal / 4-channel) | computer vision | ...'
        'AI/ML Frameworks & Techniques'        <- a heading, as a bullet
        'Python & libraries (Numpy, ...) | Ensemble Methods | ...'
        'Backend & Production Pipelines'       <- and another
        'Python FastAPI | Flask | CI-friendly testing | ...'

    The page shows four labelled groups. The document had one entry and eight
    bullets, three of which were headings pretending to be skills.

    That mattered the moment anything tried to reason about skills. The
    grounding corpus gained `Backend & Production Pipelines` as a claimable
    term, and the rule that refuses an edit for dropping a skill would have
    demanded a planner keep *that* — a requirement no correct plan can meet.
    A guard built on a wrong reading of the document is worse than no guard,
    because it fails on honest work.

    A bullet is a heading when it carries no list separator, is short, and is
    followed by a bullet that does carry one. The last condition is what keeps
    a lone real skill (`Python`, on its own line, at the end) from being
    promoted into an empty group.
    """
    if not items:
        return items

    lines: list[tuple[str, str]] = []          # (heading-or-"", text)
    for item in items:
        pending = item.title
        for bullet in item.bullets:
            lines.append((pending, bullet.text))
            pending = ""
        if not item.bullets and item.title:
            lines.append(("", item.title))

    groups: list[tuple[str, list[str]]] = []
    index = 0
    while index < len(lines):
        label, text = lines[index]
        nxt = lines[index + 1][1] if index + 1 < len(lines) else ""
        if (not label and _looks_like_a_group_heading(text)
                and nxt and not _looks_like_a_group_heading(nxt)):
            label, text = text, nxt
            index += 1
        if groups and not label:
            groups[-1][1].append(text)
        else:
            groups.append((label, [text]))
        index += 1

    rebuilt: list[Item] = []
    for label, texts in groups:
        iid = item_id("skills", doc.next_seq())
        rebuilt.append(Item(
            id=iid,
            title=label,
            bullets=[Bullet(id=bullet_id(iid, n), text=t)
                     for n, t in enumerate(texts, start=1)],
        ))
    return rebuilt
