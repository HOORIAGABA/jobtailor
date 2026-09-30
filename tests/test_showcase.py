"""The five public demo applications, checked on every commit.

`scripts/showcase.py` refuses to seed a case that fails `verify`. Running the
same checks here means a change to the validator, the email rules or the
renderer that would break a demo screen breaks the build first — the public
link is not where that should be discovered.
"""
from __future__ import annotations

import pytest

from scripts.showcase import CASES, RESUME, run_case, verify

EXPECTED = {
    "meridian": "fabricated_number",
    "sadaf": "seniority_escalation",
    "qalam": "unsupported_entity",
    "indus": "keyword_stuffing",
    "rahbar": None,
}


@pytest.fixture(scope="module")
def runs():
    return {case.key: (case, run_case(case)) for case in CASES}


def test_there_are_five_cases_each_showing_a_different_behaviour():
    assert [c.key for c in CASES] == list(EXPECTED)
    shown = [c.refused for c in CASES]
    assert len({tuple(r) for r in shown}) == 5


@pytest.mark.parametrize("key", list(EXPECTED))
def test_every_case_passes_its_own_checks(runs, key):
    case, state = runs[key]
    assert verify(case, state) == []


@pytest.mark.parametrize("key,code", list(EXPECTED.items()))
def test_each_case_refuses_exactly_what_it_is_there_to_show(runs, key, code):
    _, state = runs[key]
    assert [r.code for r in state.rejected] == ([code] if code else [])


@pytest.mark.parametrize("key", list(EXPECTED))
def test_every_case_changes_the_resume_visibly(runs, key):
    """An accepted op that changes nothing — a promote of an item already
    first — is a demo of nothing. Two were caught this way while writing
    the cases."""
    _, state = runs[key]
    assert len(state.diff.changes) >= 2


@pytest.mark.parametrize("key", list(EXPECTED))
def test_the_refused_text_never_reaches_the_tailored_resume(runs, key):
    _, state = runs[key]
    tailored = " ".join(b.text for b in state.tailored.all_bullets())
    tailored += " " + state.tailored.summary
    for rejected in state.rejected:
        text = getattr(rejected.op, "text", "") if hasattr(rejected, "op") else ""
        if text:
            assert text not in tailored


def test_the_candidate_matches_the_demo_pdf():
    """The showcase résumé is the text of demo/Zara_Ahmed_CV.pdf, so the
    downloadable original and the tailored PDF compare like with like."""
    assert RESUME.contact.full_name == "Zara Ahmed"
    assert RESUME.contact.email.endswith("@example.com")


def test_seeding_twice_replaces_rather_than_fails(tmp_path, monkeypatch):
    """★ The second seed used to die on a foreign key: runs were marked for
    deletion but not flushed before their résumés were bulk-deleted."""
    from app.db import models
    from app.db.session import create_all, engine, reset, session_scope
    from scripts import showcase

    url = f"sqlite+pysqlite:///{tmp_path / 'seed.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    reset(url)
    create_all(engine())
    try:
        assert showcase.main([]) == 0
        assert showcase.main([]) == 0
        with session_scope() as s:
            assert s.query(models.Run).count() == 5
            assert s.query(models.Resume).count() == 1
            assert all(r.proof_json.get("seeded") for r in s.query(models.Run))
    finally:
        reset()


def test_a_read_only_instance_shows_the_gate_without_a_signing_secret(
        tmp_path, monkeypatch):
    """★ The public demo's run pages rendered an error where the diff, the
    refusals and the email belong: preview demanded CONFIRM_TOKEN_SECRET to
    sign a decision the instance can never accept."""
    from fastapi.testclient import TestClient
    from app.api.main import create_app
    from app.db.session import create_all, engine, reset
    from scripts import showcase

    url = f"sqlite+pysqlite:///{tmp_path / 'ro.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("CONFIRM_TOKEN_SECRET", raising=False)
    monkeypatch.delenv("DEV_USER_EMAIL", raising=False)
    monkeypatch.setenv("DEMO_USER_EMAIL", "demo@jobtailor.example")
    reset(url)
    create_all(engine())
    try:
        assert showcase.main([]) == 0
        api = TestClient(create_app(tmp_path / "runs", read_only=True))
        runs = api.get("/api/runs").json()
        assert len(runs) == 5
        for row in runs:
            page = api.get(f"/api/runs/{row['id']}/preview")
            assert page.status_code == 200, page.text
            body = page.json()
            assert body["changes"] and body["body"].startswith("Dear Hiring Team")
            assert body["confirm_token"] == ""
        assert api.post(f"/api/runs/{runs[0]['id']}/decision",
                        json={"decision": "approve"}).status_code == 403
    finally:
        reset()
