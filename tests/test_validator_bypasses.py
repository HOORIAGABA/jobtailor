"""Regression tests for four ways the honesty guard could be walked around.

Every case here was **accepted** by the validator before the fix, and each one
falsifies the product's central claim — that nothing reaches the document which
cannot be traced to a line the candidate wrote.

They are in their own file rather than folded into `test_engine.py` for one
reason: `test_engine.py` contains a test that *asserted* one of these bypasses
was correct behaviour (`test_summary_must_cite_and_cites_must_resolve` checked
that a cited summary is accepted, which was true and was the whole hole). A test
suite can lock a bug in. Keeping the adversarial cases together, named for what
they attack, makes that harder to do twice.

Each test states the failing input in the form it was reported.
"""
from __future__ import annotations

import pytest

from app.domain.models import Bullet, Item, ResumeDoc, Section
from app.domain.ops import RewriteBullet, SetSummary
from app.engine.validator import escalates_seniority, numbers, validate


def _doc() -> ResumeDoc:
    """A deliberately small resume: one role, one bullet, two skills.

    Small because every claim in these tests has to be obviously absent from it.
    """
    return ResumeDoc(
        summary="Data engineer.",
        sections=[
            Section(id="exp", kind="experience", heading="Experience", items=[
                Item(id="exp.1", title="Data Engineer", org="Acme",
                     dates="2022-2024", bullets=[
                         Bullet(id="exp.1.b.1",
                                text="Worked on data pipelines for the "
                                     "reporting team"),
                         Bullet(id="exp.1.b.2",
                                text="Reduced latency by 40 ms on the search "
                                     "path"),
                     ]),
            ]),
        ],
        skill_inventory=["Python", "SQL"],
    )


def _codes(ops) -> list[str]:
    return [r.code for r in validate(ops, _doc()).rejected]


# ── the summary was not checked at all ────────────────────────────────

def test_a_fabricated_summary_is_refused():
    """★ `set_summary` ran no Class A, B or C check — only "do the cites exist".

    It is the one operation where the model writes free prose, and it writes it
    into the block at the top of the page. This exact string was accepted, with
    `fabrication_count == 0`.
    """
    result = validate([SetSummary(
        op_id="1",
        text="Senior ML lead who grew revenue 400% at Google, managing a team "
             "of 30 engineers using Kubernetes.",
        cites=["exp.1.b.1"],
    )], _doc())

    assert result.accepted == []
    assert result.rejected[0].code == "fabricated_number"
    assert result.fabrication_count == 1


def test_a_summary_may_not_name_an_unevidenced_capability():
    assert _codes([SetSummary(op_id="1", cites=["exp.1.b.1"],
                              text="Data engineer specialising in Kubernetes "
                                   "and Terraform.")]) == ["unsupported_entity"]


def test_a_summary_may_not_promote():
    assert _codes([SetSummary(op_id="1", cites=["exp.1.b.1"],
                              text="Engineer who directed the reporting "
                                   "platform.")]) == ["seniority_escalation"]


def test_an_honest_summary_still_passes():
    """The guard has to let the legitimate case through, or it is useless.

    Everything here is on the resume: the role, the work, both skills, and a
    number that appears in a bullet.
    """
    result = validate([SetSummary(
        op_id="1",
        text="Data engineer at Acme who worked on reporting data pipelines in "
             "Python and SQL, cutting search latency by 40 ms.",
        cites=["exp.1.b.1", "exp.1.b.2"],
    )], _doc())
    assert result.rejected == [], result.rejected


# ── a number is its unit, not just its digits ─────────────────────────

def test_changing_the_unit_is_fabrication():
    """`40 ms` → `40%` was accepted: the unit was discarded before comparing."""
    assert _codes([RewriteBullet(
        op_id="1", bullet_id="exp.1.b.2",
        text="Reduced latency by 40% on the search path",
    )]) == ["fabricated_number"]


def test_a_grouped_number_is_one_literal():
    """`12,000` tokenised to `{"12", "000"}`, so a 1000x inflation could be
    assembled from digits "present in the original"."""
    assert numbers("handled 12,000 sessions") == {"12000"}
    assert "12000900" not in numbers("handled 900 tickets across 12,000 users")


@pytest.mark.parametrize("original,rewritten", [
    ("served 1,200 users in 3 regions", "Served 1200 users across 3 regions"),
    ("Reduced latency by 40 ms on the search path", "Cut latency 40ms"),
    ("Shipped 5 features over 2 years", "Delivered 5 features in 2 years"),
])
def test_honest_rewordings_of_a_number_are_not_fabrication(original, rewritten):
    """The unit rule must not make ordinary rephrasing a rejection.

    A unit is attached only when it is four letters or fewer, so `900 tickets`
    stays `900` and the words around a number stay free to change.
    """
    assert numbers(rewritten) - numbers(original) == set()


# ── promotion from a neutral origin ───────────────────────────────────

def test_a_neutral_origin_can_still_be_promoted():
    """`escalates_seniority` required the ORIGIN to contain a weak verb, so a
    neutral bullet was a free promotion. Neither verb is an entity and no
    number changes, so Class A and Class C saw nothing either."""
    assert escalates_seniority(
        "Fixed bugs in the payments service",
        "Directed the payments service rebuild",
    ) is not None


def test_keeping_an_ownership_verb_the_origin_already_had_is_fine():
    assert escalates_seniority(
        "Led the migration to Postgres",
        "Led the database migration to Postgres",
    ) is None


# ── the model decided whether it got checked ──────────────────────────

def test_stuffing_is_caught_when_the_model_declares_no_terms():
    """★ `if op.target_terms:` let the planner switch off its own check.

    Identical text; the only difference is whether the model declared what it
    was inserting. Declaring nothing was permitted *and* incentivised, because
    the planner prompt says listing terms is what triggers rejection.
    """
    stuffed = ("Worked on data pipelines for the reporting team using Python, "
               "SQL, PostgreSQL and Docker")

    declared = _codes([RewriteBullet(
        op_id="1", bullet_id="exp.1.b.1", text=stuffed,
        target_terms=["Python", "SQL", "PostgreSQL", "Docker"])])
    silent = _codes([RewriteBullet(
        op_id="1", bullet_id="exp.1.b.1", text=stuffed, target_terms=[])])

    assert declared == silent, (
        "declaring target terms changed the verdict, so the model still "
        f"controls its own checking: declared={declared} silent={silent}")
    assert declared and declared[0] in ("keyword_stuffing", "term_density",
                                        "unsupported_entity")


def test_naming_two_real_tools_is_not_stuffing():
    """The count rule replaces the ratio rule for derived terms precisely so
    this stays legal — two names in a short bullet is how people write."""
    doc = _doc()
    doc.skill_inventory = ["Python", ".NET", "C++"]
    result = validate([RewriteBullet(
        op_id="1", bullet_id="exp.1.b.1",
        text="Built reporting services in .NET and C++")], doc)
    assert result.rejected == [], result.rejected
