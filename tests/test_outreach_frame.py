"""The greeting and sign-off are written in code, not by the model."""
from __future__ import annotations

import pytest

from app.engine.message import GREETING, VALEDICTION, core_of, letter

NAME = "Priya Raman"
CORE = ("I am applying for the Backend Engineer role at Halverson Labs.\n\n"
        "I wrote the nightly Python jobs that load sales data into PostgreSQL.")


def test_every_message_opens_dear_hiring_team_and_is_signed():
    out = letter(CORE, NAME)
    assert out.startswith(f"{GREETING}\n\n")
    assert out.endswith(f"\n\n{VALEDICTION}\n{NAME}")
    assert CORE in out


@pytest.mark.parametrize("greeting", [
    "Hello,", "Hi there!", "Hey team", "Dear Sir/Madam,", "Dear Hiring Manager,",
    "Good morning,", "To whom it may concern,", "Greetings,",
])
@pytest.mark.parametrize("signoff", [
    "Thanks,\nPriya Raman", "Best regards,\nPriya", "Kind regards,\nP. Raman",
    "Sincerely,", "Priya Raman", "Cheers!\nPriya Raman", "",
])
def test_whatever_frame_the_model_wrote_is_replaced(greeting, signoff):
    """A small model asked for a professional greeting writes a different one
    every run. The frame is replaced, not trusted."""
    body = f"{greeting}\n\n{CORE}\n\n{signoff}".strip()
    assert letter(body, NAME) == letter(CORE, NAME)


def test_framing_twice_changes_nothing():
    once = letter(CORE, NAME)
    assert letter(once, NAME) == once


def test_a_closing_sentence_is_not_mistaken_for_a_sign_off():
    body = CORE + "\n\nThank you for your time; I would welcome a short call."
    assert core_of(body, NAME).endswith("I would welcome a short call.")


def test_an_empty_body_stays_empty():
    """So the gate still reports `the message is empty` instead of sending a
    greeting and a signature with nothing between them."""
    assert letter("Hello,\n\nThanks,\nPriya Raman", NAME) == ""


def test_without_a_name_the_valediction_stands_alone():
    assert letter(CORE, "").endswith(f"\n\n{VALEDICTION}")


def test_the_demo_email_is_framed_and_passes_every_message_rule():
    """The seeded demo is what a visitor to the public instance reads first,
    so it is held to the same rules as a live draft — and it goes through the
    same frame."""
    from evals.harness import ScriptedClient
    from app.engine.normalize import normalize
    from app.pipeline.run import tailor
    from scripts.demo_seed import ANSWERS, POSTING, RESUME

    state = tailor(normalize(RESUME), POSTING, ScriptedClient(ANSWERS),
                   write_outreach=True, render_output=False)
    assert state.outreach.body.startswith("Dear Hiring Team,\n\n")
    assert state.outreach.body.endswith("Kind regards,\nPriya Raman")
    assert state.outreach.problems == []


def test_the_attachment_and_a_weekday_are_not_unsupported_claims():
    """"attached as a PDF" and "every Monday" were both reported as named
    things the résumé does not support."""
    from app.engine.message import _NOT_CLAIMS
    assert {"pdf", "monday"} <= _NOT_CLAIMS
