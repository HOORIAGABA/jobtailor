"""The eval suite, run as a test.

An eval nobody runs is a folder of JSON. Running it here means a regression in
the honesty guarantee fails the build the same way a broken function does —
which is the only way the guarantee stays a guarantee rather than a claim that
used to be true.

Offline, so it costs nothing and needs no key.
"""
from __future__ import annotations

import pytest

from evals.harness import load_cases, run_case


def _ids(cases):
    return [c["name"] for c in cases]


CASES = load_cases()


def test_there_are_cases_at_all():
    """The folder was empty for most of this project's life, and 'the evals
    pass' was technically true the whole time."""
    assert len(CASES) >= 4


@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_case(case):
    result = run_case(case)
    assert not result.error, result.error
    assert result.passed, "\n".join(result.failures)


def test_every_case_says_why_it_exists():
    """A case without a reason is a case nobody can judge the value of when it
    starts failing two years from now."""
    for case in CASES:
        assert len(case.get("why", "")) > 80, f"{case['name']} has no rationale"


def test_the_suite_contains_a_case_that_must_be_accepted():
    """Otherwise 'the validator refuses dishonest edits' is satisfied by a
    validator that refuses everything, and the product quietly stops working
    while every test stays green."""
    accepting = [c for c in CASES
                 if "rewrite_bullet" in c.get("expect", {}).get("accepts", [])]
    assert accepting, "no case asserts that an honest rewrite is applied"
