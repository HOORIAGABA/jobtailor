"""S8 — the message, and the checks that keep it honest.

The central one: a run that flags a requirement as a gap must not claim that
same requirement in the email attached to the same resume.
"""
from __future__ import annotations

import pytest

from app.domain.models import (
    Bullet, Contact, EvidenceIndex, EvidenceLink, Grounded, Item, JobBrief,
    Outreach, ResumeDoc, Section, Term,
)
from app.engine import message as msg
from app.io.llm import ScriptedClient


def _doc() -> ResumeDoc:
    doc = ResumeDoc(
        contact=Contact(full_name="Hooria Attas", email="h@example.com"),
        summary="AI engineer building automations and backend services.",
        sections=[
            Section(id="sec.experience", kind="experience", heading="EXPERIENCE",
                    items=[Item(id="exp.1", title="AI Automation Engineer",
                                org="Funsol", date_start="2025-12", bullets=[
                        Bullet(id="exp.1.b.1",
                               text="Built 20 production n8n workflows integrating "
                                    "OpenAI with Slack and Google APIs"),
                        Bullet(id="exp.1.b.2",
                               text="Deployed FastAPI services with request validation"),
                    ])]),
            Section(id="sec.skills", kind="skills", heading="SKILLS",
                    items=[Item(id="skl.1", title="Backend", bullets=[
                        Bullet(id="skl.1.b.1", text="Python, FastAPI, n8n, OpenAI")])]),
        ],
    )
    doc.skill_inventory = ["Python", "FastAPI", "n8n", "OpenAI", "Slack"]
    return doc


def _brief() -> JobBrief:
    return JobBrief(
        company="Annova", role="AI Engineer", tone="pragmatic",
        recruiter_email="careers@annova.com",
        email_candidates=["careers@annova.com", "noreply@annova.com"],
        hard_requirements=[Grounded(statement="Strong Python and FastAPI",
                                    source_span=(0, 24))],
        terms=[Term(term="Python"), Term(term="FastAPI"), Term(term="n8n")],
        excerpt="AI Engineer at Annova. Strong Python and FastAPI. Voice agents a plus.",
    )


def _index() -> EvidenceIndex:
    return EvidenceIndex(links=[
        EvidenceLink(job_element_id="req.0", bullet_id="exp.1.b.2",
                     lexical_hit=True, strength="strong"),
    ])


STANDING = {
    "demonstrated": ["FastAPI", "n8n"],
    "declared_only": ["Python"],
    "not_found": ["AI voice agents"],
}


def _message(body: str, subject: str = "AI Engineer - Hooria Attas",
             **over) -> Outreach:
    base = dict(recipient="careers@annova.com", subject=subject, body=body,
                cites=["exp.1.b.2"])
    base.update(over)
    return Outreach(**base)


GOOD_BODY = (
    "I am applying for the AI Engineer role at Annova. I build backend "
    "services in Python and FastAPI, most recently deploying FastAPI "
    "services with request validation. I have also built 20 production n8n "
    "workflows integrating OpenAI with Slack, which is the kind of "
    "integration work the posting describes. I would be glad to walk through "
    "either of these on a call this week."
)


# ══ the gap check ═════════════════════════════════════════════════════
# S2 already produces the list of things the candidate cannot show, and S3
# prints it honestly as flag_gap. Nothing stopped S8 from claiming the same
# requirement in the covering letter.

def test_a_message_may_not_claim_a_requirement_the_resume_cannot_show():
    body = GOOD_BODY + " I have extensive experience building AI voice agents."
    problems = msg.check(_message(body), _doc(), _brief(), STANDING)
    assert any("AI voice agents" in p and "gap" in p for p in problems)


def test_acknowledging_the_gap_honestly_is_allowed():
    """"No direct experience with X yet" is a true sentence, not a claim."""
    body = GOOD_BODY + " I have no direct experience with AI voice agents yet."
    problems = msg.check(_message(body), _doc(), _brief(), STANDING)
    assert not [p for p in problems if "voice agents" in p]


def test_mentioning_the_gap_without_claiming_it_is_allowed():
    body = GOOD_BODY + " I saw the posting mentions AI voice agents."
    assert not [p for p in msg.check(_message(body), _doc(), _brief(), STANDING)
                if "voice agents" in p]


def test_the_gap_check_matches_whole_terms_not_substrings():
    """Substring matching is how `ml` comes to match `html`.

    A false positive here blocks an honest message, which is worse than the
    check not firing: the person stops trusting it.
    """
    assert msg.claim_check("I have experience with HTML and CSS", ["ML"]) == []
    assert msg.claim_check("I have built ML pipelines", ["ML"]) == ["ML"]


def test_without_a_standing_list_the_gap_check_is_skipped_not_guessed():
    """S2 owns the gap list. Inferring one here would be a second producer."""
    body = GOOD_BODY + " I have extensive experience building AI voice agents."
    problems = msg.check(_message(body), _doc(), _brief(), standing=None)
    assert not [p for p in problems if "gap" in p]


# ══ Class A — numbers ═════════════════════════════════════════════════

def test_a_number_not_on_the_resume_is_caught():
    body = GOOD_BODY.replace("20 production", "450 production")
    problems = msg.check(_message(body), _doc(), _brief(), STANDING)
    assert any("450" in p for p in problems)


def test_a_number_that_is_on_the_resume_passes():
    assert not [p for p in msg.check(_message(GOOD_BODY), _doc(), _brief(), STANDING)
                if "number" in p]


# ══ Class C — named things ════════════════════════════════════════════

def test_a_tool_the_resume_never_mentions_is_caught():
    body = GOOD_BODY + " I also run our Kubernetes clusters."
    problems = msg.check(_message(body), _doc(), _brief(), STANDING)
    assert any("Kubernetes" in p for p in problems)


def test_the_company_and_role_from_the_posting_are_allowed():
    """Naming the company is the point of a covering letter."""
    assert not [p for p in msg.check(_message(GOOD_BODY), _doc(), _brief(), STANDING)
                if "named thing" in p]


# ══ sendability ═══════════════════════════════════════════════════════

@pytest.mark.parametrize("body", [
    GOOD_BODY + " Looking forward to hearing from [Hiring Manager].",
    GOOD_BODY + " I admire the work your company does.",
    GOOD_BODY + " TODO: add closing",
])
def test_an_unfinished_message_is_caught(body):
    """A bracket in a recruiter's inbox is worse than a missing sentence."""
    assert msg.check(_message(body), _doc(), _brief(), STANDING)


def test_an_overlong_message_is_caught():
    body = GOOD_BODY + " Additionally I would note that " * 40
    problems = msg.check(_message(body), _doc(), _brief(), STANDING)
    assert any("words" in p for p in problems)


def test_a_missing_subject_is_caught():
    assert any("subject" in p for p in
               msg.check(_message(GOOD_BODY, subject=""), _doc(), _brief(), STANDING))


def test_a_missing_recipient_is_caught():
    problems = msg.check(_message(GOOD_BODY, recipient=""), _doc(), _brief(), STANDING)
    assert any("recipient" in p for p in problems)


def test_a_citation_to_an_unknown_id_is_caught():
    problems = msg.check(_message(GOOD_BODY, cites=["exp.9.b.9"]),
                         _doc(), _brief(), STANDING)
    assert any("exp.9.b.9" in p for p in problems)


def test_a_good_message_has_no_problems():
    assert msg.check(_message(GOOD_BODY), _doc(), _brief(), STANDING) == []


# ══ the stage ═════════════════════════════════════════════════════════

def test_the_stage_runs_with_no_api_key():
    from app.agents.outreach import draft_outreach
    client = ScriptedClient([{
        "subject": "AI Engineer - Hooria Attas",
        "body": GOOD_BODY,
        "cites": ["exp.1.b.2"],
    }])
    out = draft_outreach(_brief(), _doc(), _index(), client, standing=STANDING)
    assert out.subject.startswith("AI Engineer")
    assert out.cites == ["exp.1.b.2"]
    assert out.is_clean


def test_the_recipient_is_never_asked_of_the_model():
    """An address is a regex, not judgment — the same call as `recruiter_email`."""
    from app.agents.outreach import DraftOutreach
    from app.io.schema import to_provider_schema

    schema = to_provider_schema(DraftOutreach)
    assert set(schema["properties"]) == {"subject", "body", "cites"}
    assert "recipient" not in schema["properties"]


def test_the_recipient_comes_from_the_posting_text():
    from app.agents.outreach import draft_outreach
    client = ScriptedClient([{"subject": "s", "body": GOOD_BODY, "cites": []}])
    out = draft_outreach(
        _brief(), _doc(), _index(), client, standing=STANDING,
        jd_text="Send your CV to hiring@annova.com. Do not reply to noreply@annova.com.",
    )
    assert out.recipient == "hiring@annova.com"
    assert "noreply@annova.com" in out.recipient_candidates      # offered, ranked last
    assert out.recipient_candidates[-1] == "noreply@annova.com"


def test_an_invented_citation_is_dropped_not_fatal():
    """A bad citation is a weaker message, not an unsendable one."""
    from app.agents.outreach import draft_outreach
    client = ScriptedClient([{
        "subject": "AI Engineer - Hooria Attas", "body": GOOD_BODY,
        "cites": ["exp.1.b.2", "exp.99.b.1"],
    }])
    out = draft_outreach(_brief(), _doc(), _index(), client, standing=STANDING)
    assert out.cites == ["exp.1.b.2"]


def test_a_flawed_draft_is_returned_with_its_problems_not_raised():
    """A message a person can fix in five seconds beats no message."""
    from app.agents.outreach import draft_outreach
    client = ScriptedClient([{
        "subject": "AI Engineer - Hooria Attas",
        "body": GOOD_BODY + " I have deep expertise in AI voice agents.",
        "cites": [],
    }])
    out = draft_outreach(_brief(), _doc(), _index(), client, standing=STANDING)
    assert not out.is_clean
    assert any("voice agents" in p for p in out.problems)


def test_the_gaps_are_sent_to_the_model_as_things_it_may_not_claim():
    from app.agents.outreach import build_payload
    payload = build_payload(_brief(), _doc(), _index(), STANDING)
    assert payload["must_not_claim"] == ["AI voice agents"]
    assert payload["role"] == "AI Engineer"
    # Not the whole resume: a message written from everything reads like a
    # summary of the resume the recruiter already has attached.
    assert len(payload["evidence"]) <= 8
