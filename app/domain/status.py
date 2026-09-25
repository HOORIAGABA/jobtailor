"""The two state machines, and the one access rule that matters.

A resume and a run are the only things in this system with a lifecycle, and both
acquired one for the same reason: a human gate means the work stops existing as
a call stack and starts existing as a row. A row needs to say what may happen to
it next.

**The transitions are data, not conditionals.** A dozen `if status == ...`
checks spread across the API is how a status field becomes decorative — every
site enforces a slightly different rule and the union of them is not a machine.
One table, one function, one error.

This lives in `domain` on purpose: it has no imports beyond the error type, so
the database layer, the API layer and the pipeline all reach the same verdict.
The alternative — the enum in the ORM and the rules in the route handlers — puts
the authority in whichever file was edited last.
"""
from __future__ import annotations

from typing import Literal

from app.domain.errors import IllegalTransition

# ── resume ────────────────────────────────────────────────────────────

ResumeStatus = Literal[
    "uploaded",        # bytes stored, nothing read yet
    "parsing",         # S0.1–S0.3 running
    "needs_confirm",   # ★ S0.4 — the parse is a hypothesis awaiting the user
    "confirmed",       # doc_json frozen; ids are permanent from here
    "superseded",      # a newer version of the same resume exists
    "failed",
]

# `needs_confirm -> needs_confirm` is the edit loop and is the only legal
# self-transition in either machine: the user corrects a field, `normalize` runs
# again, and the parse still needs confirming. Every other self-transition is a
# bug — re-confirming a confirmed resume, or re-sending a sent message.
_RESUME: dict[str, frozenset[str]] = {
    "uploaded": frozenset({"parsing", "failed"}),
    "parsing": frozenset({"needs_confirm", "failed"}),
    "needs_confirm": frozenset({"needs_confirm", "parsing", "confirmed", "failed"}),
    "confirmed": frozenset({"superseded"}),
    "superseded": frozenset(),
    "failed": frozenset({"parsing"}),
}

TERMINAL_RESUME = frozenset({"superseded"})


# ── run ───────────────────────────────────────────────────────────────

RunStatus = Literal[
    "created",       # a posting has arrived, nothing run yet
    "tailoring",     # S1–S8 and S10
    "needs_review",  # ★ S9 — the diff, the gaps and the draft await a decision
    "approved",      # the human said send
    "rejected",      # the human said do not send. Terminal, and NOT a failure.
    "sending",
    "sent",
    "failed",
]

_RUN: dict[str, frozenset[str]] = {
    "created": frozenset({"tailoring", "failed"}),
    "tailoring": frozenset({"needs_review", "failed"}),
    # Exactly two ways out, which is what "approve or reject, nothing in
    # between" means when it is written down as a machine.
    "needs_review": frozenset({"approved", "rejected", "failed"}),
    "approved": frozenset({"sending", "failed"}),
    # `sending -> approved` is the retry, and it is here because the alternative
    # is cruel: `failed` leads only back to `tailoring`, so a provider that
    # answered "rate limited" would cost a full re-run — seven model calls on a
    # free tier — to send a message that was already approved and never left the
    # machine. A provider that REFUSES has delivered nothing, so the run goes
    # back to exactly where it was and the approval still stands.
    #
    # `failed` is kept for the other kind: the send whose outcome is unknown,
    # where the recruiter may have the email and this database may not know it.
    # That one must not be retried by a machine. See `pipeline.send`.
    "sending": frozenset({"sent", "approved", "failed"}),
    "sent": frozenset(),
    "rejected": frozenset(),
    "failed": frozenset({"tailoring"}),
}

TERMINAL_RUN = frozenset({"sent", "rejected"})


# ── the access rule ───────────────────────────────────────────────────

# Once tailoring has finished, the rendered resume exists and belongs to the
# candidate. These are the states in which they may download it.
#
# **`rejected` is in this set, and that is the point.** Rejecting means "do not
# send this on my behalf" — not "destroy the work". The likeliest real rejection
# is a good resume with a clumsy covering letter, and a gate that throws away
# the resume to punish the email would make the honest answer the expensive one.
# A user who rejects keeps the .docx and sends their own email.
#
# `sending` is included because the files were approved before dispatch began.
_ARTIFACTS_AVAILABLE = frozenset({
    "needs_review", "approved", "rejected", "sending", "sent",
})

# Sending, by contrast, requires the one state that means a person said yes.
_MAY_SEND = frozenset({"approved"})


def artifacts_available(status: str) -> bool:
    """May the candidate download the tailored resume in this state?"""
    return status in _ARTIFACTS_AVAILABLE


def may_send(status: str) -> bool:
    """Only from `approved`. Not from `needs_review`, not from `sent`."""
    return status in _MAY_SEND


def is_terminal_run(status: str) -> bool:
    return status in TERMINAL_RUN


# ── enforcement ───────────────────────────────────────────────────────

def _check(table: dict[str, frozenset[str]], kind: str,
           current: str, target: str) -> None:
    if current not in table:
        raise IllegalTransition(f"{kind}: {current!r} is not a status")
    if target not in table:
        raise IllegalTransition(f"{kind}: {target!r} is not a status")
    allowed = table[current]
    if target not in allowed:
        nowhere = "it is terminal" if not allowed else \
            f"only {sorted(allowed)} are reachable"
        raise IllegalTransition(
            f"{kind}: cannot go from {current!r} to {target!r} — {nowhere}"
        )


def check_resume_transition(current: str, target: str) -> None:
    _check(_RESUME, "resume", current, target)


def check_run_transition(current: str, target: str) -> None:
    _check(_RUN, "run", current, target)


def next_resume_states(current: str) -> list[str]:
    """For a UI that needs to know which buttons to show."""
    return sorted(_RESUME.get(current, frozenset()))


def next_run_states(current: str) -> list[str]:
    return sorted(_RUN.get(current, frozenset()))
