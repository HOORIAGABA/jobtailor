"""Stage S1.0 — is there a job posting in this text, and where does it start?

The resume side of this pipeline has four stages before a model is asked to
understand anything: extract with layout awareness, parse, verify the parse,
normalize. The job side had one line — read a file — and then went straight to
asking a model to understand it.

That asymmetry is not justified by the inputs. A posting arrives as a LinkedIn
post between a feed and a footer, a saved web page wrapped in navigation, a
recruiter's message, a PDF, or a paste that starts mid-sentence. "Any format"
is the normal case, not the awkward one.

Two jobs here, both deterministic:

**Strip what is plainly not the posting.** Feed chrome, cookie notices, "N
connections work here", share buttons. These are recognisable by their exact
wording, so they are removed by rule rather than by asking a model to ignore
them — the same reason heading classification lives in `engine.normalize`
rather than in the parse prompt.

**Say what the text actually contains.** Not a score — a checklist. A posting
with responsibilities and requirements is something to tailor against; a
"we're hiring, DM me" post is not, and the difference has to be visible
*before* four model calls are spent discovering it. This is the same idea as
`engine.parse_check`: report what is there, let the caller decide.

There is deliberately no "quality score". A number would invite tuning the
threshold until things pass, and what matters is *which* parts are missing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Lines that are never part of a posting. Matched on the whole line, after
# whitespace collapsing, so a requirement that happens to contain the word
# "apply" survives.
_CHROME = (
    r"like\s*[·|]?\s*comment\s*[·|]?\s*share",
    r"^\d+\s*(connections?|followers?|applicants?)\b.*",
    r"^\d+\s*(people|others)\s+(work|viewed|clicked)\b.*",
    r"^(promoted|sponsored|suggested)$",
    r"^(easy apply|apply now|apply on company website|save|share|report this job)$",
    r"^(see more|show more|see less|read more)$",
    r"^(sign in|join now|create account|log in)$",
    r"^(accept (all )?cookies?|we use cookies.*|cookie (policy|preferences).*)$",
    r"^(skip to (main )?content|back to (search|results))$",
    r"^\d+\s*(minutes?|hours?|days?|weeks?|months?)\s+ago$",
    r"^(posted|reposted)\s+\d+.*ago$",
    r"^(your profile|job activity)$",
    r"^(©|copyright)\s*\d{4}.*",
    r"^(privacy policy|terms of (use|service)|accessibility)$",
    r"^\W{0,3}$",                       # a line of punctuation or an emoji alone
)
_CHROME_RE = tuple(re.compile(p, re.IGNORECASE) for p in _CHROME)

# Cues that a section is present. Substring matches on the lowercased text —
# loose on purpose, because a posting writes these a hundred ways and a missed
# cue only makes the report more cautious than it needs to be.
_RESPONSIBILITY_CUES = (
    "responsibilities", "what you'll do", "what you will do", "the role",
    "day to day", "day-to-day", "you will", "your role", "about the role",
    "key duties", "in this role",
)
_REQUIREMENT_CUES = (
    "requirements", "qualifications", "what we're looking for",
    "what we are looking for", "must have", "must-have", "you have",
    "skills", "experience with", "proficiency", "required", "we require",
    "minimum", "you should",
)
# A posting names a role. A post that only says "we're hiring!" does not.
_ROLE_CUES = (
    "engineer", "developer", "scientist", "analyst", "manager", "designer",
    "architect", "consultant", "specialist", "lead", "director", "intern",
    "researcher", "administrator", "technician", "coordinator", "associate",
)

# A floor, not a target. Below this there is no posting, only a teaser — but
# length alone never decides: a compact posting that names a role and states
# its requirements is more useful than three pages of company boilerplate.
MIN_USABLE_CHARS = 250


@dataclass(frozen=True)
class PostingAssessment:
    """What this text contains. A checklist, never a score."""
    chars: int = 0
    lines: int = 0
    has_responsibilities: bool = False
    has_requirements: bool = False
    names_a_role: bool = False
    removed: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        """Enough to tailor against.

        Requirements or responsibilities, plus a named role. A posting missing
        both sections has nothing for the planner to aim at, whatever its
        length.
        """
        return (
            self.chars >= MIN_USABLE_CHARS
            and self.names_a_role
            and (self.has_requirements or self.has_responsibilities)
        )

    def summary(self) -> str:
        if self.is_usable:
            return f"{self.chars} characters, with a role and what it asks for."
        return "; ".join(self.concerns) or "not enough to tailor against"


def strip_boilerplate(text: str) -> tuple[str, list[str]]:
    """Remove lines that are page furniture. Returns the text and what went.

    What was removed is returned rather than discarded so a run folder records
    it: a rule that eats half a posting should be visible, not silent.
    """
    kept: list[str] = []
    removed: list[str] = []
    for raw in (text or "").splitlines():
        line = " ".join(raw.split())
        if not line:
            kept.append("")
            continue
        if any(pattern.match(line) or pattern.search(line) for pattern in _CHROME_RE):
            removed.append(line)
        else:
            kept.append(line)

    # Collapse the blank runs the removals leave behind.
    out: list[str] = []
    for line in kept:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip(), removed


def assess(text: str, removed: list[str] | None = None) -> PostingAssessment:
    """Report what the text contains, and what is missing. No model."""
    body = (text or "").strip()
    lowered = body.lower()
    lines = [l for l in body.splitlines() if l.strip()]

    has_resp = any(cue in lowered for cue in _RESPONSIBILITY_CUES)
    has_req = any(cue in lowered for cue in _REQUIREMENT_CUES)
    names_role = any(cue in lowered for cue in _ROLE_CUES)

    concerns: list[str] = []
    if len(body) < MIN_USABLE_CHARS:
        concerns.append(
            f"only {len(body)} characters — that is a teaser, not a posting"
        )
    if not names_role:
        concerns.append("no job title anywhere in the text")
    if not (has_req or has_resp):
        concerns.append(
            "nothing that reads like requirements or responsibilities — this "
            "looks like a 'we're hiring, DM me' post rather than a posting"
        )
    elif not has_req:
        concerns.append("responsibilities but no stated requirements")
    elif not has_resp:
        concerns.append("requirements but no description of the work")

    return PostingAssessment(
        chars=len(body), lines=len(lines),
        has_responsibilities=has_resp, has_requirements=has_req,
        names_a_role=names_role,
        removed=list(removed or []), concerns=concerns,
    )


def prepare(text: str) -> tuple[str, PostingAssessment]:
    """Strip the furniture, then report what is left. The whole stage."""
    cleaned, removed = strip_boilerplate(text)
    return cleaned, assess(cleaned, removed)
