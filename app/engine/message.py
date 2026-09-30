"""Stage S8, second half — is this message honest, and is it sendable.

The resume has a validator because an edit to a resume can lie. **An outreach
message is the same risk with the safety off.** A resume edit is constrained by
the document it edits: an op names a bullet and either changes it or does not.
A message is free prose, written last, by a model that has just been told what
the job wants and what the candidate almost has — which is exactly the position
from which overselling is the easy answer.

So the message gets the same treatment as an op, and mostly the same checks:

    Class A  numbers      must come from the resume, verbatim
    Class C  named things must be in the grounding corpus
    the gap  a requirement the resume could not demonstrate may not be
             claimed in the covering letter either

That third one is this module's real job, and it has no analogue on the resume
side. S2 already produces the list of things the candidate cannot show — S3
prints it as `flag_gap`, honestly. Nothing stopped S8 from writing "I have
extensive experience building AI voice agents" in the email attached to that
same resume, and the contradiction would ship without a word of complaint.
A system that flags a gap in one artefact and claims it in another is not
being careful; it is being careful in one place.

No model, so this runs in CI on every commit with no key. A list of problems,
never a score — a number would be a target and the thing writing the message
would be the thing optimising it.
"""
from __future__ import annotations

import re
from typing import Iterable

from app.domain.models import JobBrief, Outreach, ResumeDoc
from app.engine.evidence import TermStanding
from app.engine.validator import entities, grounding_corpus, numbers

# A recruiter reads the first three lines. Past this, a cold email is a document
# someone has to set aside time for, which is the same as not being read.
MAX_BODY_WORDS = 220
MIN_BODY_WORDS = 40
MAX_SUBJECT_CHARS = 90

# A template that reached the gate unfilled. The model is asked for a finished
# message, so a bracket is either a placeholder it failed to fill or a note to
# itself; both are worse in a recruiter's inbox than a missing sentence.
_PLACEHOLDER = re.compile(
    r"[\[\{<](?:[^\]\}>\n]{0,60})[\]\}>]|\b(?:XX+|TBD|TODO|FIXME|LOREM)\b",
    re.IGNORECASE,
)
# "Dear Hiring Manager," is fine. "Dear [Hiring Manager]," is not, and neither
# is a body that still says "your company".
_VAGUE = ("your company", "your organisation", "your organization",
          "the company", "insert ", "name of company")


# ── the frame: greeting and sign-off ─────────────────────────────────
#
# The greeting and the sign-off are the same on every message, so they are not
# the model's to write. A 3B model asked for "a professional greeting" produces
# "Hello,", "Hi there!", "Dear Sir/Madam," and "Hey team" across four runs —
# and signs with an initial, a full name, or nothing. Written in code they are
# identical every time, and the model spends its words on the part that has to
# be specific.
#
# `core_of` removes whatever greeting and sign-off the model wrote anyway, so
# the frame is applied once however obedient the model was, and `letter` is
# idempotent — re-framing a framed message changes nothing.

GREETING = "Dear Hiring Team,"
VALEDICTION = "Kind regards,"

_GREETING_LINE = re.compile(
    r"^\s*(?:dear|hello|hi|hey|greetings|good\s+(?:morning|afternoon|day)|"
    r"to\s+whom)\b[^\n]{0,60}$",
    re.IGNORECASE,
)
_VALEDICTION_LINE = re.compile(
    r"^\s*(?:(?:kind|best|warm|warmest)\s+regards|regards|best(?:\s+wishes)?|"
    r"thanks|thank\s+you|many\s+thanks|sincerely|yours\s+(?:sincerely|"
    r"faithfully|truly)|cheers|all\s+the\s+best|respectfully)[,.!]?\s*$",
    re.IGNORECASE,
)


def _is_signature(line: str, name: str) -> bool:
    """The candidate's name on its own line — `Priya Raman`, `Priya`,
    `P. Raman` — and nothing else on it."""
    words = line.strip().lower().replace(".", " ").replace(",", " ").split()
    parts = (name or "").strip().lower().replace(".", " ").split()
    if not words or not parts or len(words) > 4:
        return False
    initials = {p[0] for p in parts}
    return all(w in parts or (len(w) == 1 and w in initials) for w in words)


def core_of(body: str, name: str = "") -> str:
    """The paragraphs between the greeting and the sign-off."""
    lines = (body or "").strip().splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and _GREETING_LINE.match(lines[0]):
        lines.pop(0)
    while lines:
        last = lines[-1]
        if (not last.strip() or _VALEDICTION_LINE.match(last)
                or _is_signature(last, name)):
            lines.pop()
            continue
        break
    return "\n".join(lines).strip()


# A résumé line id the model pasted into the prose — `[exp.3.b.1]`,
# `(exp.1.b.2, prj.3.b.1)`. Ids belong in `cites`, where the gate resolves them;
# in the email they are noise a recruiter cannot read. A 3B model does this on
# almost every run however the prompt is worded, so it is removed in code.
_LINE_ID = r"(?:[a-z]{3}\.\d+(?:\.b\.\d+)?)"
_INLINE_CITE = re.compile(
    rf"\s*[\[\(]\s*(?:ids?:?\s*)?{_LINE_ID}(?:\s*[,;/]\s*{_LINE_ID})*\s*[\]\)]"
)
# "Dear Hiring Team, My work building…" — a greeting run into the first
# sentence on one line, which `_GREETING_LINE` (a whole line) cannot see.
_GREETING_PREFIX = re.compile(
    r"^\s*(?:dear|hello|hi|hey|greetings)\b[^,\n.!]{0,40}[,!:]\s+",
    re.IGNORECASE,
)
_TRAILING_VALEDICTION = re.compile(
    r"\s+(?:thanks|many thanks|(?:kind|best|warm) regards|regards|best|sincerely)"
    r"[,.!]?\s*$",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'])")
_CLOSING = re.compile(
    r"^(?:thank you|thanks|i would (?:appreciate|welcome|be glad|love)|"
    r"i look forward|please (?:find|see)|my (?:résumé|resume|cv) is attached)",
    re.IGNORECASE,
)


def tidy(core: str) -> str:
    """The paragraphs, cleaned of what a small model reliably gets wrong.

    Three repairs, all mechanical and none of them about content:
    line ids pasted into the prose are removed; a greeting run into the first
    sentence is removed; and a message returned as one unbroken block is split
    into an opening, the body and the close — the shape the prompt asks for,
    which a 3B model ignores about half the time.
    """
    text = _INLINE_CITE.sub("", core or "")
    text = _GREETING_PREFIX.sub("", text, count=1)
    text = _TRAILING_VALEDICTION.sub("", text)
    text = re.sub(r"[ \t]+([.,;:])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    if "\n\n" in text or not text:
        return text
    sentences = [s.strip() for s in _SENTENCE.split(text.replace("\n", " ")) if s.strip()]
    if len(sentences) < 3:
        return text
    close_at = next((i for i, s in enumerate(sentences) if i > 0 and _CLOSING.match(s)),
                    len(sentences))
    opening, middle, closing = sentences[:1], sentences[1:close_at], sentences[close_at:]
    return "\n\n".join(" ".join(part) for part in (opening, middle, closing) if part)


def letter(body: str, name: str = "") -> str:
    """`Dear Hiring Team,` / the paragraphs / `Kind regards,` and the name."""
    core = tidy(core_of(body, name))
    signature = f"{VALEDICTION}\n{name.strip()}" if (name or "").strip() \
        else VALEDICTION
    return f"{GREETING}\n\n{core}\n\n{signature}" if core else ""


def _resume_words(doc: ResumeDoc) -> set[str]:
    parts = [doc.summary or "", doc.contact.full_name or ""]
    for item in doc.all_items():
        parts += [item.title or "", item.org or "", item.dates or ""]
    parts += [bullet.text for bullet in doc.all_bullets()]
    return {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9+#.]*", " ".join(parts))}


_NOT_CLAIMS = frozenset({
    "pdf", "cv", "resume", "résumé", "linkedin", "github",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
})


def _as_written(text: str, terms: Iterable[str]) -> list[str]:
    """Each term in the casing the message actually used."""
    wanted = {t.lower() for t in terms}
    found: dict[str, str] = {}
    for token in re.findall(r"[A-Za-z][A-Za-z0-9+#.\-]*", text or ""):
        key = token.lower()
        if key in wanted:
            found.setdefault(key, token)
    return [found.get(t, t) for t in sorted(wanted)]


def claim_check(body: str, not_found: Iterable[str]) -> list[str]:
    """Requirements the resume could not demonstrate, asserted in the message.

    Matched on a whole-word alias basis rather than substring, for the reason
    `Term.alias_forms` exists: substring matching is how `ml` comes to match
    `html`, and a false positive here blocks an honest message.

    A term may be *mentioned* — "I have not yet worked with voice agents, but"
    is an honest sentence and the system should not forbid it. What is checked
    is a first-person claim of experience near the term, which is the shape
    overselling actually takes.
    """
    text = " ".join((body or "").lower().split())
    found: list[str] = []
    for term in not_found:
        needle = (term or "").lower().strip()
        if not needle or not re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", text):
            continue
        for match in re.finditer(rf"(?<!\w){re.escape(needle)}(?!\w)", text):
            window = text[max(0, match.start() - 140): match.end() + 60]
            if _CLAIMS_EXPERIENCE.search(window) and not _DISCLAIMS.search(window):
                found.append(term)
                break
    return found


# First person, plus a word that asserts having done the thing.
_CLAIMS_EXPERIENCE = re.compile(
    r"\b(?:i|my|i've|i have)\b[^.!?]{0,80}?"
    r"\b(?:experience|experienced|expertise|built|build|building|developed|"
    r"develop|developing|delivered|shipped|worked|work|working|used|using|"
    r"implemented|implementing|led|proficient|skilled|strong|deep|extensive|"
    r"hands-on|specialis|specializ)",
    re.IGNORECASE,
)
# An honest acknowledgement of the gap. Explicitly allowed.
_DISCLAIMS = re.compile(
    r"\b(?:not yet|no direct|haven't|have not|never|without|lack|new to|"
    r"learning|eager to learn|keen to learn|would like to|looking to)\b",
    re.IGNORECASE,
)


def check(
    message: Outreach,
    doc: ResumeDoc,
    brief: JobBrief,
    standing: dict[TermStanding, list[str]] | None = None,
    confirmed: Iterable[str] = (),
) -> list[str]:
    """Everything wrong with this draft, in the order a reviewer would care.

    `standing` is S2's three-grade output. When it is absent the gap check is
    skipped rather than guessed at — the alternative is inferring a gap list
    here, which would be a second producer of a fact S2 already owns.
    """
    problems: list[str] = []
    # Checked without the frame. The greeting and sign-off are written by
    # `letter`, not by the model, so there is nothing in them to verify — and
    # "Dear Hiring Team" would otherwise count toward the length and be read
    # as two named things the resume does not support.
    body = core_of(message.body or "", doc.contact.full_name)
    subject = message.subject or ""

    if not subject.strip():
        problems.append("no subject line")
    elif len(subject) > MAX_SUBJECT_CHARS:
        problems.append(
            f"the subject is {len(subject)} characters; it will be truncated "
            f"in most inboxes past about {MAX_SUBJECT_CHARS}"
        )

    words = len(body.split())
    if not body.strip():
        problems.append("the message is empty")
        return problems
    if words > MAX_BODY_WORDS:
        problems.append(
            f"{words} words — past about {MAX_BODY_WORDS} a cold email becomes "
            f"something a recruiter sets aside rather than reads"
        )
    elif words < MIN_BODY_WORDS:
        problems.append(f"only {words} words; too thin to say anything specific")

    # Class A — a number in the message must be a number on the resume. The
    # resume's own numbers were already checked against their source bullet, so
    # this is the second link in the same chain rather than a new rule.
    on_resume = numbers(doc.summary)
    for bullet in doc.all_bullets():
        on_resume |= numbers(bullet.text)
    for item in doc.all_items():
        on_resume |= numbers(f"{item.title} {item.org} {item.dates}")
    if invented := numbers(body) - on_resume - numbers(brief.excerpt):
        problems.append(
            f"number(s) not on the resume or in the posting: {sorted(invented)}"
        )

    # Class C — a named thing must be groundable. The posting's own words count:
    # naming the company and the role is the point of a covering letter.
    corpus = grounding_corpus(doc, confirmed)
    allowed = set(corpus)
    allowed |= {t.lower() for t in entities(brief.excerpt)}
    allowed |= {w.lower() for w in (brief.company or "").split()}
    allowed |= {w.lower() for w in (brief.role or "").split()}
    allowed |= {w.lower() for w in (doc.contact.full_name or "").split()}
    # Words the system itself puts in play, and calendar words, are not
    # capabilities. "My résumé is attached as a PDF" is a fact about the
    # envelope — the send path attaches exactly one PDF — and "every Monday"
    # names a day, not a skill. Both were reported as unsupported named things.
    allowed |= _NOT_CLAIMS
    # Anything the résumé itself says is grounded, whatever its case: the
    # employer ("Datum Analytics"), a word from a bullet ("PDFs", "Llama"), a
    # course. The corpus above is the skill inventory, which is the right
    # authority for *capabilities* but not for every proper noun a résumé
    # contains — and a covering letter that names the candidate's own employer
    # was being reported as claiming something unsupported.
    allowed |= _resume_words(doc)
    for term in brief.terms:
        allowed |= set(term.alias_forms())
    # `entities` lowercases, and a problem that reports `['kubernetes']` when
    # the message says `Kubernetes` sends the reader hunting. Reported as
    # written.
    if corpus and (ungrounded := {
        e for e in entities(body) if e.lower() not in allowed
    }):
        problems.append(
            "named thing(s) the resume does not support: "
            f"{sorted(_as_written(body, ungrounded))}"
        )

    if standing:
        if claimed := claim_check(body, standing.get("not_found", ())):
            problems.append(
                f"claims experience with {claimed}, which S2 could not find in "
                f"the resume — the same run flags it as a gap"
            )

    if stray := _PLACEHOLDER.findall(body) or _PLACEHOLDER.findall(subject):
        problems.append(f"unfilled placeholder(s): {stray[:4]}")
    lowered = body.lower()
    if vague := [phrase for phrase in _VAGUE if phrase in lowered]:
        problems.append(f"generic filler instead of the real name: {vague}")

    if missing := [c for c in message.cites if not doc.has(c)]:
        problems.append(f"cites ids that are not in the resume: {missing}")

    if not message.recipient:
        problems.append(
            "no recipient found in the posting — a person must supply one "
            "before this can be sent"
        )
    return problems
