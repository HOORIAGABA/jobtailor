"""Stage S8 — the message to the recruiter. One model call.

What the model is given is deliberately narrow: the role, the company, what the
posting asks for, and the handful of resume lines that actually evidence it. It
does not get the full resume, and it does not get the gap list as prose it might
paraphrase — it gets the gaps as a list of things it may not claim, which is a
different instruction.

**The recipient is not in the schema.** `engine.contact` extracts addresses from
the posting with a regex and ranks them. That decision was made once already for
`JobBrief.recruiter_email` and the reasoning holds harder here: this is the
field that decides who a message goes to, and a field the model can fill is a
field it can invent. Catching an invented address every time is worse than
never asking for one.

**The model writes subject and body only, and cites its claims.** Every specific
statement names the bullet behind it, which is what lets `engine.message` refuse
a draft whose claims are not on the resume — and what lets a reviewer follow a
sentence back to the line that supports it rather than taking it on trust.

The draft is never sent. It goes to S9 with its problem list attached, editable,
and S11 will not dispatch without a human decision.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.agents.base import as_json, call_structured, prompt_version
from app.domain.models import (
    EvidenceIndex, JobBrief, Outreach, ResumeDoc,
)
from app.engine.contact import recruiter_candidates
from app.engine.evidence import TermStanding, evidence_bullets
from app.engine.message import MAX_BODY_WORDS, check
from app.io.llm import LLMClient

logger = logging.getLogger(__name__)

# Enough to make a specific case, few enough that the model has to choose which
# evidence is strongest rather than listing everything.
MAX_EVIDENCE_LINES = 8
OUTREACH_MAX_TOKENS = 1536


class DraftOutreach(BaseModel):
    """Every field required. The fifth time this lesson landed is documented in
    `agents.job_brief`; the schema here starts where that one ended up."""
    subject: str = Field(
        description="One line, under 90 characters. Name the role. No 'Re:', "
                    "no 'Application' alone — say which role and who you are."
    )
    body: str = Field(
        description=f"The message. Under {MAX_BODY_WORDS} words, plain text, "
                    f"no markdown. Open with why this role, give two specific "
                    f"pieces of evidence, close with a clear next step."
    )
    cites: list[str] = Field(
        description="The bullet ids behind the specific claims in the body, "
                    "exactly as given in `evidence`. Empty only if the body "
                    "makes no specific claim, which would be a weak message."
    )


SYSTEM = """\
You write one short email from a candidate to the person hiring for a role.

WHAT YOU HAVE
The role and company, what the posting asks for, and the resume lines that
actually demonstrate those things. The lines are the only evidence you have and
the only evidence you may use.

WHAT MAKES THIS MESSAGE WORK
Specificity. A recruiter has read fifty messages saying "I am passionate about
AI and excited by your mission". None of them said "I built the 4-channel
detection pipeline you would want for this". The second one is a reason to
reply.

So: two concrete pieces of evidence, drawn from the lines you were given, each
one connected to something the posting asked for. Name the technology. Name what
was built. Cite the line id.

WHAT YOU MAY NOT DO
- Do not claim anything in `must_not_claim`. Those are requirements this
  candidate's resume cannot demonstrate. The same system is telling them,
  honestly, that these are gaps. You may acknowledge one plainly ("no direct
  experience with X yet") or say nothing about it. You may not assert it.
- Do not invent a number. Every figure must appear in the lines you were given.
- Do not invent a name — no tool, framework, product or team that is not in the
  lines or the posting.
- Do not write a placeholder. No [brackets], no "your company", no TBD. This is
  a finished message.
- Do not flatter the company about things you were not told. You do not know
  what they are "doing in the space".

TONE
Match `register`. Write like a competent person who read the posting, not like
an application form. Short sentences. No "I am writing to express my interest".
No "I would be a great fit" without the reason attached.

Close with one concrete next step, and nothing about salary or start dates.

Return only the JSON object."""

PROMPT_VERSION = prompt_version(SYSTEM)


def build_payload(
    brief: JobBrief,
    doc: ResumeDoc,
    index: EvidenceIndex,
    standing: dict[TermStanding, list[str]] | None,
) -> dict:
    """What the model sees. Narrow on purpose.

    Only the bullets that carry evidence, and only the ones linked to something
    the posting asked for — a message written from the whole resume reads like a
    summary of the resume, which is the thing the recruiter already has
    attached.
    """
    linked = {link.bullet_id for link in index.links}
    ranked = [b for b in evidence_bullets(doc) if b.id in linked]
    if len(ranked) < MAX_EVIDENCE_LINES:
        seen = {b.id for b in ranked}
        ranked += [b for b in evidence_bullets(doc) if b.id not in seen]

    return {
        "role": brief.role,
        "company": brief.company,
        "candidate": doc.contact.full_name,
        "register": brief.tone,
        "what_the_posting_asks_for": [
            r.statement for r in brief.hard_requirements
        ][:8],
        "evidence": [
            {"id": b.id, "text": b.text} for b in ranked[:MAX_EVIDENCE_LINES]
        ],
        "must_not_claim": (standing or {}).get("not_found", []),
    }


def draft_outreach(
    brief: JobBrief,
    doc: ResumeDoc,
    index: EvidenceIndex,
    client: LLMClient,
    *,
    jd_text: str = "",
    standing: dict[TermStanding, list[str]] | None = None,
    confirmed: tuple[str, ...] = (),
    max_tokens: int = OUTREACH_MAX_TOKENS,
) -> Outreach:
    """One call. Returns the draft with its problems already attached.

    The problems are not raised. A message with a flaw is still the most useful
    thing to show a person — they can fix a sentence in five seconds, and a
    refusal would leave them with nothing to fix. S9 decides; this stage
    reports.
    """
    draft = call_structured(
        client,
        system=SYSTEM,
        user=as_json(build_payload(brief, doc, index, standing)),
        schema_model=DraftOutreach,
        max_tokens=max_tokens,
        temperature=0.4,          # prose, not extraction
        stage="outreach",
    )

    # Ids the model made up are dropped rather than failing the draft: a bad
    # citation is a weaker message, not an unsendable one, and `engine.message`
    # reports the loss either way.
    cites = [c for c in draft.cites if doc.has(c)]
    if dropped := [c for c in draft.cites if not doc.has(c)]:
        logger.info("Dropped %d citation(s) to unknown ids: %s", len(dropped), dropped)

    # The posting is the single source for an address. When `jd_text` is here,
    # it IS the text the brief's own `recruiter_email` was extracted from, so
    # re-deriving cannot disagree with anything a human has decided — S9 has not
    # run yet — and it keeps one producer for the fact rather than preferring a
    # cached copy that may predate a prompt change. Without the posting text the
    # brief's list is the only copy available, and it is used as-is.
    candidates = recruiter_candidates(jd_text) if jd_text else list(
        brief.email_candidates)
    recipient = candidates[0] if candidates else brief.recruiter_email

    message = Outreach(
        recipient=recipient,
        recipient_candidates=candidates or (
            [brief.recruiter_email] if brief.recruiter_email else []),
        subject=draft.subject.strip(),
        body=draft.body.strip(),
        cites=cites,
    )
    message.problems = check(message, doc, brief, standing, confirmed)

    logger.info(
        "Outreach: %d words to %s, %d citation(s), %d problem(s)",
        len(message.body.split()), message.recipient or "(no address)",
        len(message.cites), len(message.problems),
    )
    for problem in message.problems:
        logger.warning("Outreach: %s", problem)
    return message
