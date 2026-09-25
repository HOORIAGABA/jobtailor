"""Did the parse keep the resume? Deterministic verification of stage S0.2.

Extraction's dangerous failure is not hallucination — it is **silent loss**.
A model handed a two-page resume returns a clean, plausible, well-formed
`RawResume` containing nine of the fourteen bullets, and nothing downstream can
tell. Every later guarantee in this system is phrased against `ResumeDoc`
("no number that is not in the original", "no skill outside the grounding
corpus"), so a lossy parse does not trip any of them. It silently redefines
what "the original" means, and the candidate's best bullet is gone from a
resume they are about to send.

This module is the check that catches it, and it needs no model: compare the
words in the extracted text against the words in the parse.

* **dropped** — content words in the source that appear nowhere in the parse
* **invented** — content words in the parse that appear nowhere in the source

The comparison is per line but the lookup is document-wide, because moving a
bullet between entries is a structuring decision, not a loss. Only text that
vanished entirely counts.

This is also what makes the S0.4 confirmation screen worth a candidate's
attention. "Check your parse" over a full document invites a rubber stamp;
*"these two lines from your PDF are not in the parse"* is a question a person
can actually answer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.domain.models import RawResume
from app.engine.text import content_words

# A line survives if this much of its meaning is still present somewhere.
# Below 1.0 on purpose: a parser that drops a trailing "(remote)" has not lost
# the bullet, and flagging it would train the user to skip the screen.
MIN_RETAINED = 0.6

# Lines with fewer than this many content words are dates, page numbers and
# separators. They carry no claim, and reporting them is noise.
MIN_WORDS = 3


# ── structural collapse ───────────────────────────────────────────────
# The word comparison below measures VOCABULARY. It is blind to SHAPE, and
# shape is the other way a parse fails.
#
# Measured, on three real resumes: the model returned `sections: []` and put
# all 7,531 characters of the document into one field. Every source line's
# words were present, so the check reported "All 114 lines accounted for" —
# and the document had nothing addressable in it. Perfect retention, zero
# structure, and a verifier saying everything was fine.
#
# So the shape is checked too. A resume that becomes one blob is a failed
# parse no matter how many of its words survived.

# A single parsed field holding this much of the document is a dumping ground,
# not a field.
MAX_FIELD_SHARE = 0.4

# Below this many content lines, a document is too short to draw conclusions
# from — a one-line note legitimately has no sections.
MIN_LINES_TO_JUDGE = 8


@dataclass(frozen=True)
class ParseCoverage:
    """What survived the parse, and what did not."""
    dropped: list[str] = field(default_factory=list)
    invented: list[str] = field(default_factory=list)
    structure: list[str] = field(default_factory=list)
    source_lines: int = 0
    parsed_lines: int = 0

    @property
    def is_clean(self) -> bool:
        return not self.dropped and not self.invented and not self.structure

    def summary(self) -> str:
        if self.is_clean:
            return f"All {self.source_lines} lines accounted for."
        parts = []
        if self.structure:
            parts.append(self.structure[0])
        if self.dropped:
            parts.append(f"{len(self.dropped)} line(s) missing from the parse")
        if self.invented:
            parts.append(f"{len(self.invented)} line(s) not in the source")
        return "; ".join(parts)


def check_structure(source: list[str], raw: RawResume) -> list[str]:
    """Did the parse produce a document, or a blob? Reported in plain words."""
    problems: list[str] = []
    entries = sum(len(s.entries) for s in raw.sections)
    bullets = sum(len(e.bullets) for s in raw.sections for e in s.entries)

    if len(source) < MIN_LINES_TO_JUDGE:
        return problems

    if not raw.sections:
        problems.append(
            f"the parse has no sections at all, from {len(source)} lines of text"
        )
    elif not entries:
        problems.append(
            f"the parse has {len(raw.sections)} section(s) and no entries in any "
            f"of them"
        )
    elif not bullets:
        problems.append(
            f"the parse has {entries} entries and not one bullet between them"
        )

    total = len(set().union(*(content_words(l) for l in source))) if source else 0
    if total:
        for line in parsed_lines(raw):
            share = len(content_words(line)) / total
            if share > MAX_FIELD_SHARE:
                problems.append(
                    f"one field holds {share:.0%} of the document's text — the "
                    f"parse collapsed it instead of structuring it"
                )
                break

    return problems


# ── contact fields are checked exactly, not statistically ─────────────
# An address is two or three words, so the word-overlap rule below exempts it
# as "too short to judge" — and an address is the single worst field to get
# wrong, because it is where the application is sent. v1 mailed real
# applications to addresses a model derived from a company's domain name.
#
# So identifiers are verified by presence, the same standard `job_brief.py`
# applies to a recruiter address: it is in the document or it is invented.
# Punctuation and spacing are stripped from both sides first, so
# "+92 300 1234567" still matches "+92-300-1234567".

_NOISE = re.compile(r"[^a-z0-9]+")


def _squash(text: str) -> str:
    return _NOISE.sub("", (text or "").lower())


def identifiers(raw: RawResume) -> list[str]:
    """Contact fields that must appear literally in the source."""
    c = raw.contact
    return [
        v for v in (
            c.full_name, c.email, c.phone, c.location,
            c.linkedin, c.github, c.website,
        ) if v and v.strip()
    ]


def parsed_lines(raw: RawResume) -> list[str]:
    """Every piece of text the parse claims the document contains."""
    lines: list[str] = list(identifiers(raw))
    if raw.summary:
        lines.append(raw.summary)
    for section in raw.sections:
        lines.append(section.heading)
        for entry in section.entries:
            lines.extend(v for v in (entry.title, entry.org, entry.dates) if v)
            lines.extend(entry.bullets)
    return [l for l in (s.strip() for s in lines) if l]


def _retained(line: str, corpus: set[str]) -> tuple[bool, int]:
    """Is enough of `line` present in `corpus`? Also returns its word count."""
    words = content_words(line)
    if len(words) < MIN_WORDS:
        return True, len(words)        # too short to judge; never reported
    kept = len(words & corpus) / len(words)
    return kept >= MIN_RETAINED, len(words)


def check_coverage(source_text: str, raw: RawResume) -> ParseCoverage:
    """Compare the extracted text with the parse. No model, no network."""
    source = [l.strip() for l in (source_text or "").splitlines() if l.strip()]
    parsed = parsed_lines(raw)

    source_corpus: set[str] = set()
    for line in source:
        source_corpus |= content_words(line)

    parse_corpus: set[str] = set()
    for line in parsed:
        parse_corpus |= content_words(line)

    dropped = [l for l in source if not _retained(l, parse_corpus)[0]]

    squashed_source = _squash(source_text)
    invented = [
        l for l in parsed
        if not _retained(l, source_corpus)[0]
    ] + [
        v for v in identifiers(raw)
        if _squash(v) and _squash(v) not in squashed_source
    ]
    # An identifier can fail both rules; report it once.
    invented = list(dict.fromkeys(invented))

    return ParseCoverage(
        dropped=dropped,
        invented=invented,
        structure=check_structure(source, raw),
        source_lines=len(source),
        parsed_lines=len(parsed),
    )
