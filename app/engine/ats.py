"""Stage S10, first half — the ATS-safe *shape* of a resume, as data.

Rendering is two problems and they belong apart. This module answers "what
should the page say, in what order, spelled how" and is pure: it produces a
list of lines and knows nothing about .docx or .pdf. `io.render` answers "put
those lines in a file" and does nothing else. Keeping the decisions here means
they are unit-testable without writing a byte to disk, and that the .docx and
the .pdf cannot disagree about content — they are two renderings of one list.

**Every rule below is a measured ATS parsing failure, not a style preference.**
The sources are in `SDD-AMENDMENT-04`; the failures they describe are:

    two columns          parser reads top-to-bottom, left-to-right, and
                         interleaves the columns into word salad
    tables               structure is stripped, so a date loses its job title
    headers / footers    most engines ignore those regions entirely
    custom headings      recognition is keyword matching on the heading, so
                         "ABOUT ME" is not seen as a summary
    decorative fonts     not installed on the parser's host; ligatures fail
    text in images       no OCR is run, so the text simply is not there
    mixed date formats   "Jan 2021 - Present" beside "01/2021 -" extracts wrong
    special characters   custom bullets, em dashes and smart quotes are
                         stripped mid-extraction, sometimes taking a word with
                         them

This project has unusual standing to take the column rule seriously: its own
`engine.layout` exists *because* two-column resumes defeat naive extraction.
The renderer is on the other side of the same problem, so it declines to create
the thing the reader had to be built to survive.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from app.domain.models import Item, Kind, ResumeDoc, Section

# ── canonical headings ────────────────────────────────────────────────

# Recognition is keyword matching against a known set, so a heading the
# candidate invented is a section the parser does not classify. `RawSection`
# keeps the printed heading verbatim and `Section.heading` still carries it —
# that is right for provenance and wrong for output, so the substitution
# happens here, at the last possible moment, and only for kinds the system
# actually recognised.
#
# `custom` keeps its own heading: a section this system could not classify is
# one the substitution has no opinion about, and inventing a standard name for
# it would be a guess printed on someone's resume.
ATS_HEADINGS: dict[Kind, str] = {
    "summary": "PROFESSIONAL SUMMARY",
    "skills": "SKILLS",
    "experience": "WORK EXPERIENCE",
    "projects": "PROJECTS",
    "education": "EDUCATION",
    "certifications": "CERTIFICATIONS",
    "leadership": "LEADERSHIP",
}


def ats_heading(section: Section) -> str:
    """The heading to print. Standard when the kind is known, else verbatim."""
    canonical = ATS_HEADINGS.get(section.kind)
    if canonical:
        return canonical
    return (section.heading or "").strip().upper() or "ADDITIONAL INFORMATION"


# ── text safety ───────────────────────────────────────────────────────

# Characters that are stripped during extraction, sometimes taking an adjacent
# word with them. Replaced rather than deleted: a candidate who wrote an em
# dash meant a dash, and losing it silently is its own corruption.
_SUBSTITUTIONS = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    # U+2010/U+2011 matter more than they look: a non-breaking hyphen that is
    # dropped rather than replaced turns "re-architected" into
    # "rearchitected", which no keyword match will ever find.
    "‐": "-", "‑": "-",
    "–": "-", "—": "-", "―": "-", "−": "-",
    "…": "...",
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl",
    "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
    "→": "->", "←": "<-", "⇒": "=>",
    "•": "-", "●": "-", "▪": "-", "‣": "-",
    "⁃": "-", "·": "-", "∙": "-",
    "✓": "", "✔": "", "★": "", "☆": "", "■": "",
    "­": "",          # soft hyphen — invisible, splits a word for a parser
    "​": "", "‌": "", "‍": "", "﻿": "",
}

_TRANSLATION = str.maketrans(_SUBSTITUTIONS)


def sanitize(text: str) -> str:
    """ASCII-safe text with the meaning kept.

    Smart quotes, em dashes, ligatures and decorative bullets are the
    characters that vanish during extraction. A resume that reads perfectly in
    Word can lose a word to a soft hyphen, so this runs on everything printed.

    Anything still non-ASCII after substitution is decomposed and stripped of
    combining marks, so `café` becomes `cafe` rather than `caf`. A character
    with no ASCII form at all is dropped — the alternative is a PDF that cannot
    be encoded at all.
    """
    if not text:
        return ""
    body = text.translate(_TRANSLATION)
    if body.isascii():
        return " ".join(body.split())
    folded = unicodedata.normalize("NFKD", body)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = folded.encode("ascii", "ignore").decode("ascii")
    return " ".join(folded.split())


# ── dates ─────────────────────────────────────────────────────────────

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
DATE_SEPARATOR = " - "
PRESENT = "Present"


def _month_year(iso: str) -> str:
    """`"2026-03"` -> `"Mar 2026"`. A bare year stays a bare year."""
    parts = (iso or "").split("-")
    if not parts or not parts[0].isdigit():
        return ""
    year = parts[0]
    if len(parts) < 2 or not parts[1].isdigit():
        return year
    month = int(parts[1])
    if not 1 <= month <= 12:
        return year
    return f"{_MONTHS[month - 1]} {year}"


# "Present" asserts something ongoing, which is true of a job and false of a
# certificate. Rendering a credential issued in Feb 2025 as `Feb 2025 - Present`
# reads as a claim that it is still being earned, and a date range where the
# reader expects one date is also a parsing risk. Only these kinds may be open
# ended; everywhere else a missing end date means there is only one date.
OPEN_ENDED_KINDS: frozenset[Kind] = frozenset({"experience", "leadership", "education"})


def date_line(item: Item, kind: Kind = "experience") -> str:
    """One format for every date on the page, derived from the ISO fields.

    Mixing `Jan 2021 - Present` with `01/2021 -` is a documented extraction
    failure, and this resume set is full of the second kind: a Europass CV
    prints `[ 01/03/2026 - 13/08/2026 ]`, brackets and day numbers included.

    `date_start` / `date_end` are already normalised to `YYYY-MM` by
    `engine.normalize`, so they, not the verbatim `dates` string, are the
    source here. The verbatim string is the fallback for a date that could not
    be parsed at all — printing something the candidate wrote beats printing
    nothing, even when its shape is unhelpful.

    An empty `date_end` beside a real `date_start` means current — but only for
    a kind that can be ongoing. See `OPEN_ENDED_KINDS`.
    """
    start = _month_year(item.date_start)
    if not start:
        return sanitize(item.dates)
    end = _month_year(item.date_end)
    if not end:
        return f"{start}{DATE_SEPARATOR}{PRESENT}" if kind in OPEN_ENDED_KINDS else start
    if end == start:
        return start
    return f"{start}{DATE_SEPARATOR}{end}"


# ── the document ──────────────────────────────────────────────────────

@dataclass
class Line:
    """One printed line and what it is, so a renderer can style it.

    `role` is presentational intent, never content: a renderer may make a
    heading bold or larger, but nothing downstream may depend on the styling
    to understand the line. That is the whole point — the text alone has to
    carry the meaning, because for an ATS the text alone is all there is.
    """
    role: str                      # name | title | contact | heading | entry
                                   # | meta | bullet | text
    text: str


@dataclass
class AtsResume:
    lines: list[Line] = field(default_factory=list)

    def plain_text(self) -> str:
        """The resume as an ATS sees it: text, top to bottom, nothing else.

        This is also what the round-trip check compares against, which is why
        it is the same function the renderers build from rather than a separate
        reconstruction that could drift.
        """
        out: list[str] = []
        for line in self.lines:
            if line.role == "heading" and out:
                out.append("")
            out.append(line.text)
        return "\n".join(out).strip()


def contact_lines(doc: ResumeDoc, role: str = "") -> list[Line]:
    """Name, the role applied for, then contact details — in the body.

    Most ATS engines ignore the header and footer regions entirely, so a resume
    whose name lives in a Word header arrives anonymous. The name is simply the
    first line of the body.

    Details go on one line joined by a pipe — one of the separators that
    survives extraction — rather than in a table or a row of icons.
    """
    contact = doc.contact
    lines: list[Line] = []
    if name := sanitize(contact.full_name):
        lines.append(Line("name", name))

    # The title of the job being applied for, directly under the name.
    #
    # Two reasons, and they pull the same way. A recruiter scanning a stack
    # wants to know in one line which opening this is for. An ATS keyword-
    # matches the job title with high weight, and the exact phrasing from the
    # posting is what it is matching against — `brief.role` is that phrasing
    # verbatim, because S1 is required to take the title as written.
    #
    # It is a target, not a claim of tenure: `AI Engineer` under a name says
    # which role the resume is for, the way a subject line does. Nothing here
    # asserts the candidate has held it, and the experience section — which
    # does make tenure claims — is untouched and still validated.
    if headline := sanitize(role):
        lines.append(Line("title", headline))

    details = [
        sanitize(value) for value in (
            contact.email, contact.phone, contact.location,
            contact.linkedin, contact.github, contact.website,
        ) if value and value.strip()
    ]
    if details:
        lines.append(Line("contact", " | ".join(details)))
    return lines


# A URL never contains a space. When one does, extraction put it there: a link
# printed across two lines in the source PDF is rejoined with a space, so the
# resume ships an unclickable link *and* feeds the parser a broken token.
# Measured on a real certification entry:
#
#   "https://www.datacamp.com/completed/.../track/ d507ff33...? utm_medium=..."
#
# Closing the gaps is not content editing — there is exactly one correct
# reading of a space inside a URL, and it is that the space is not there.
_URL_RUN = re.compile(r"(https?://\S+(?:\s+\S+)*?)(?=\s{2,}|\s*$|\s+[A-Z][a-z])")
_URL_SPACES = re.compile(r"(?<=\S)\s+(?=\S)")


def close_url_gaps(text: str) -> str:
    """Remove whitespace that extraction inserted inside a URL."""
    if "http" not in text:
        return text

    def repair(match: re.Match) -> str:
        return _URL_SPACES.sub("", match.group(0))

    return _URL_RUN.sub(repair, text)


# Query parameters that exist to tell the issuer where the click came from.
# None of them are needed to open the page, and they roughly double the length
# of a certificate URL — on a real entry, 180 characters of which 85 were
# `utm_medium=organic_social&utm_campaign=sharewidget&utm_content=soa`.
# A recruiter reads the line; an ATS indexes every one of those tokens.
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "utm_id", "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid",
    "ref", "referrer", "share", "shared", "source", "trk", "trkinfo",
    "originalsubdomain", "si", "feature",
})
_URL_IN_TEXT = re.compile(r"https?://\S+")
# "Link: https://..." — the word adds nothing the URL does not say.
_LINK_PREFIX = re.compile(r"^\s*(?:link|url|certificate|credential\s+url)\s*:\s*",
                          re.IGNORECASE)
# "BCG X Credential ID: 9ungT9MMB8YDwizqx" — the parse glued two facts into the
# organisation field, so the org printed as a sentence and the id was buried.
_CREDENTIAL = re.compile(
    r"\s*(?:credential|certificate|license|licence)\s*(?:id|no|number|#)?\s*[:#]\s*(\S+)",
    re.IGNORECASE,
)


def clean_url(url: str) -> str:
    """Drop tracking parameters, keep the link working.

    Only named tracking keys are removed. A query string that carries the
    identity of the thing being linked — a token, a share id the page needs —
    survives, because a shorter URL that 404s is worse than a long one.
    """
    head, sep, query = url.partition("?")
    if not sep or not query:
        return head
    kept = [
        pair for pair in query.split("&")
        if pair and pair.split("=", 1)[0].lower() not in _TRACKING_PARAMS
    ]
    return f"{head}?{'&'.join(kept)}" if kept else head


def tidy_link_text(text: str) -> str:
    """A certification line: no `Link:` label, no tracking on the URL."""
    body = _LINK_PREFIX.sub("", close_url_gaps(text))
    return _URL_IN_TEXT.sub(lambda m: clean_url(m.group()), body)


def split_credential(org: str) -> tuple[str, str]:
    """`"BCG X Credential ID: 9ung..."` -> `("BCG X", "Credential ID: 9ung...")`.

    Two facts in one field is a parse artefact, and printing it whole makes the
    issuer unreadable — which matters because the issuer is what a recruiter
    scans for and what an ATS maps to a known certification authority.
    """
    match = _CREDENTIAL.search(org or "")
    if not match:
        return org, ""
    issuer = (org[:match.start()] + org[match.end():]).strip(" ,;|-")
    return issuer, f"Credential ID: {match.group(1)}"


def entry_lines(item: Item, kind: Kind = "experience") -> list[Line]:
    """Title, organisation and dates — on one line, never in a table.

    A table would separate the date from the title in the parser's view, which
    is failure #2 in the source list. Joining them with a pipe keeps them in
    one text run, so whatever the parser makes of the line, it has all three
    facts together.
    """
    lines: list[Line] = []
    org, credential = split_credential(item.org)
    headline = " | ".join(
        part for part in (sanitize(item.title), sanitize(org)) if part)
    dates = date_line(item, kind)
    if headline and dates:
        lines.append(Line("entry", f"{headline} | {dates}"))
    elif headline or dates:
        lines.append(Line("entry", headline or dates))

    # A credential id belongs under its certificate, not inside the issuer's
    # name. `meta` rather than `bullet`: it is a reference, not an achievement,
    # so a renderer need not give it a bullet marker.
    if credential:
        lines.append(Line("meta", sanitize(credential)))

    for bullet in item.bullets:
        body = tidy_link_text(sanitize(bullet.text))
        if not body:
            continue
        # A bare link is a reference too. Bulleting it reads as if the
        # candidate is claiming a URL as an accomplishment.
        role = "meta" if body.startswith(("http://", "https://")) else "bullet"
        lines.append(Line(role, body))
    return lines


def skill_lines(item: Item) -> list[Line]:
    """One group, printed as `Label: a, b, c`. Plain text, one line.

    Not a table, not a rating bar, not an image — no OCR is ever run, so a
    skill drawn as a progress bar is a skill the parser cannot see.

    **The terms are the split ones, not the raw source line.** Printing the
    line verbatim carried every artefact of the original document into the
    deliverable. Measured on a real resume:

        Computer Vision & Image Processing: YOLO (multi-modal / 4-channel) |
        computer vision | Transfert learning architectures (VGG16, VGG19,
        MobileNet V3, ResNet50, ...etc) | Object detection. | Feature
        Extraction, | multi-modal RGB-Thermal pipelines | Onnx

    — a stray `...etc`, a full stop and a comma mid-list, a nested
    `API Python frameworks: Flask` label, and pipes and commas mixed as
    separators.

    Using `split_skill_line` has a second, better reason than tidiness: it is
    the same function that builds the grounding corpus and the protected-skill
    set that `set_skills` may not shrink. **What the resume prints and what the
    validator defends are now one list.** They were two, and two lists of the
    same fact is the shape of bug this project has an invariant against.
    """
    from app.engine.normalize import split_skill_line

    label = sanitize(item.title)
    terms: list[str] = []
    seen: set[str] = set()
    for bullet in item.bullets:
        for term in split_skill_line(bullet.text):
            clean = sanitize(term)
            key = clean.lower()
            if clean and key not in seen:
                seen.add(key)
                terms.append(clean)

    if not terms:
        return []
    body = ", ".join(terms)
    return [Line("text", f"{label}: {body}" if label else body)]


def build(doc: ResumeDoc, role: str = "") -> AtsResume:
    """A `ResumeDoc` as an ordered list of ATS-safe lines. Pure.

    `role` is the job title from the brief, printed under the name. Empty is
    valid and simply omits the line — rendering must not depend on a brief,
    so a resume can be produced from a document alone.
    """
    lines: list[Line] = list(contact_lines(doc, role))

    if summary := sanitize(doc.summary):
        lines.append(Line("heading", ATS_HEADINGS["summary"]))
        lines.append(Line("text", summary))

    for section in doc.sections:
        # The document-level summary was already printed; a summary section
        # whose text folded into it would otherwise appear twice.
        if section.kind == "summary" and summary:
            continue
        if not section.items:
            continue
        lines.append(Line("heading", ats_heading(section)))

        for item in section.items:
            if section.kind == "skills":
                lines.extend(skill_lines(item))
            else:
                lines.extend(entry_lines(item, section.kind))

    return AtsResume(lines=lines)


# ── the checklist ─────────────────────────────────────────────────────

_NON_ASCII = re.compile(r"[^\x00-\x7f]")
# Long enough that a recruiter skims past it and a parser may truncate.
MAX_BULLET_CHARS = 400


def problems(resume: AtsResume) -> list[str]:
    """What is still wrong with this document, as a list, never a score.

    A number here would be a target, and the thing producing the document is
    the thing that would be optimising the number — the same reason
    `EvidenceIndex` carries no coverage figure. A list of named problems can be
    fixed or accepted; a score can only be gamed.
    """
    found: list[str] = []
    if not resume.lines:
        found.append("the document is empty")
        return found

    if resume.lines[0].role != "name":
        found.append("no name on the first line — an ATS reads the body, not a header")
    if not any(line.role == "contact" for line in resume.lines):
        found.append("no contact line: nothing to reach the candidate on")
    if not any(line.role == "heading" for line in resume.lines):
        found.append("no section headings — the parser has nothing to classify")

    for line in resume.lines:
        if stray := set(_NON_ASCII.findall(line.text)):
            found.append(
                f"non-ASCII character(s) {sorted(stray)!r} survived sanitising "
                f"in: {line.text[:60]!r}"
            )
        if line.role == "bullet" and len(line.text) > MAX_BULLET_CHARS:
            found.append(f"a bullet runs to {len(line.text)} characters")

    headings = [l.text for l in resume.lines if l.role == "heading"]
    if len(headings) != len(set(headings)):
        found.append(f"a heading appears twice: {headings}")
    return found
