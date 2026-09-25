"""Who to write to, found in the text rather than asked for.

The model used to return `recruiter_email` and code checked it appeared
literally in the posting. That works — a constructed `careers@annovasol.com`
is caught — but it is the wrong shape. Catching an invented value every time
is worse than never asking for one, because the failure still has to be
handled, the field still occupies schema surface the model can misuse, and the
check only holds as long as someone remembers to run it.

An address is not judgment. It is a regex. So it is extracted here, and the
model is not asked.

**Which address, though, is a little bit of judgment**, and that part is
handled honestly: every address is extracted, ranked by rules, and the whole
ranked list travels with the brief. The gate (S9) shows the top one as the
default and the rest as alternatives, because the product's core promise is
that a human approves the recipient before anything is sent. Being *right* is
not required; being *honest about the alternatives* is.
"""
from __future__ import annotations

import re

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Never a person to apply to. A posting's footer is full of these.
_NEVER = (
    "noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon",
    "postmaster", "webmaster", "abuse", "privacy", "legal", "unsubscribe",
    "notifications", "security",
)

# The local part of an address someone applies through.
_HIRING = (
    "career", "job", "hr", "recruit", "hiring", "talent", "apply",
    "resume", "cv", "people", "join",
)

# Words near an address that mean "send your application here". The distance
# matters more than the word: "email your CV to x@y.com" and "x@y.com to
# apply" both count, and a signature block at the end of a page does not.
_APPLICATION_CUES = (
    "apply", "send", "email", "mail", "write", "reach", "contact", "submit",
    "cv", "resume", "application", "dm",
)
_CUE_WINDOW = 120


def emails_in(text: str) -> set[str]:
    """Every address literally present, lowercased.

    Trailing sentence punctuation is stripped: postings routinely end with
    "...send your CV to careers@acme.com." and the raw match would otherwise
    carry the full stop, so a real address fails a later comparison.
    """
    return {m.group().rstrip(".,;:").lower() for m in _EMAIL.finditer(text or "")}


def _score(address: str, text: str, at: int) -> tuple[int, int]:
    """Higher is better. Returns (score, position) so ties keep document order."""
    local = address.split("@", 1)[0]
    score = 0

    if any(bad in local for bad in _NEVER):
        score -= 100
    if any(good in local for good in _HIRING):
        score += 10

    window = text[max(0, at - _CUE_WINDOW): at + _CUE_WINDOW].lower()
    if any(cue in window for cue in _APPLICATION_CUES):
        score += 5

    return score, at


def recruiter_candidates(text: str) -> list[str]:
    """Every address in the posting, best first.

    Rules, in order of weight: an address that is plainly automated is pushed
    to the bottom rather than dropped — a posting whose only address is
    `noreply@` should still show it, clearly ranked last, instead of claiming
    there is no way to apply.
    """
    body = text or ""
    seen: dict[str, tuple[int, int]] = {}

    for match in _EMAIL.finditer(body):
        address = match.group().rstrip(".,;:").lower()
        if address not in seen:
            seen[address] = _score(address, body, match.start())

    return [
        address for address, _ in
        sorted(seen.items(), key=lambda kv: (-kv[1][0], kv[1][1]))
    ]


def best_recruiter_email(text: str) -> str:
    """The most likely address to apply through, or "" when there is none."""
    candidates = recruiter_candidates(text)
    return candidates[0] if candidates else ""
