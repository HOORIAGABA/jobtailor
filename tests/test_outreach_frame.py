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


# ── what a 3B model does to the body, repaired in code ────────────────

REAL_DRAFT = (
    "My work building document question-answering prototypes and evaluating "
    "prompt templates aligns directly with the Junior AI Engineer role at "
    "Meridian Labs. I developed a prototype over 1,200 internal PDFs using "
    "sentence-transformers embeddings and FAISS retrieval [exp.3.b.1]. To "
    "ensure accuracy, I wrote an evaluation script that compared three prompt "
    "templates on 150 labelled questions [exp.3.b.2]. Thank you for your "
    "time. I would appreciate a short conversation about the role."
)


def test_line_ids_pasted_into_the_prose_are_removed():
    """★ The draft a real llama3.2:3b run produced. The ids belong in
    `cites`; in the email they are noise a recruiter cannot read."""
    out = letter(REAL_DRAFT, "Zara Ahmed")
    assert "exp.3" not in out and "[" not in out
    assert "FAISS retrieval." in out          # no " ." left behind


@pytest.mark.parametrize("cite", [
    "[exp.1.b.2]", "(exp.1.b.2)", "[exp.1.b.2, prj.4.b.1]", "(ids: exp.2.b.1; sum.1)",
])
def test_every_citation_shape_is_removed(cite):
    assert letter(f"I built the loader {cite}. It runs nightly. Thank you.",
                  NAME).count(".b.") == 0


def test_a_single_block_is_split_into_opening_body_and_close():
    core = core_of(letter(REAL_DRAFT, "Zara Ahmed"), "Zara Ahmed")
    paragraphs = core.split("\n\n")
    assert len(paragraphs) == 3
    assert paragraphs[0].startswith("My work building")
    assert paragraphs[-1].startswith("Thank you for your time")


def test_a_greeting_run_into_the_first_sentence_is_removed():
    out = letter("Dear Hiring Team, I build retrieval pipelines in Python. "
                 "They are evaluated weekly. Thank you.", NAME)
    assert out.count("Dear Hiring Team") == 1
    assert "\n\nI build retrieval pipelines" in out


def test_paragraphs_the_model_did_write_are_kept():
    assert core_of(letter(CORE, NAME), NAME) == CORE


def test_a_number_with_a_decimal_point_is_not_a_sentence_break():
    body = "I reached 87.5 percent accuracy on the test set. It shipped. Thanks."
    assert "87.5 percent" in letter(body, NAME)


def test_the_candidates_own_employer_is_not_an_unsupported_name():
    """★ "At Datum Analytics I built…" was reported as naming things the
    résumé does not support — the résumé's own employer. Found by the
    showcase checks, which refuse to seed an email with any problem."""
    from scripts.showcase import CASE_RAHBAR, run_case
    state = run_case(CASE_RAHBAR)
    assert "Datum Analytics" in state.outreach.body
    assert state.outreach.problems == []


@pytest.mark.parametrize("opener", ["Separately", "Serving", "Recently"])
def test_an_adverb_or_gerund_opening_a_sentence_is_not_a_name(opener):
    from app.engine.validator import entities
    assert opener.lower() not in entities(
        f"I built a pipeline. {opener} it took a week.")


def test_a_tool_name_opening_a_sentence_is_still_a_name():
    """The -ly/-ing rule must not swallow real names."""
    from app.engine.validator import entities
    assert "terraform" in entities("I built a pipeline. Terraform ran it.")
