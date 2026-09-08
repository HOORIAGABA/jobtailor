"""Tests for the bounded review loop in orchestrator.py (M8.1 / M10.1).

The pipeline agent calls are monkeypatched so no LLM is invoked; this locks
the *control flow* of the bounded review loop:
  - tailors at most 1 + MAX_REVIEW_REVISIONS times, never unbounded,
  - keeps the revision with the highest reviewer fit score,
  - skips the loop cleanly when the reviewer itself fails,
  - propagates step failures into state["error"].
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.agents.orchestrator as orch
from app.agents.orchestrator import run_pipeline, MAX_REVIEW_REVISIONS


def _job_requirements():
    return {"job_title": "Data Engineer", "skills": ["python", "sql"], "responsibilities": []}


def _resume_json():
    return {"full_name": "Hooria Attas", "skills": ["python", "sql"], "experience": []}


def _tailored_a():
    return {"skills": ["python", "sql"], "experience": [{"title": "DE", "bullet_points": ["etl"]}]}


def _tailored_b():
    return {"skills": ["python", "sql", "spark", "airflow"], "experience": [{"title": "DE", "bullet_points": ["etl", "spark"]}]}


def test_bounded_loop_stops_after_max_revisions(monkeypatch):
    """Reviewer keeps saying needs_revision -> we must NOT run away."""
    calls = {"tailors": 0, "reviews": 0}

    def fake_tailor(resume_id, resume_json, job_requirements, feedback=None):
        calls["tailors"] += 1
        return _tailored_b() if calls["tailors"] > 1 else _tailored_a()

    def fake_parse(job_raw_text):
        return _job_requirements()

    def fake_review(job_requirements, tailored_resume, original_resume=None):
        calls["reviews"] += 1
        return {
            "needs_revision": True,
            "feedback": "add more keywords",
            "fit_comparison": {"tailored_fit_score": calls["reviews"]},
        }

    def fake_gaps(job_requirements, tailored_resume):
        return []

    def fake_draft(job_requirements, tailored_resume, feedback=None):
        return "Hello, I am keen to apply."

    monkeypatch.setattr(orch, "parse_job_post", fake_parse)
    monkeypatch.setattr(orch, "tailor_resume", fake_tailor)
    monkeypatch.setattr(orch, "review_resume", fake_review)
    monkeypatch.setattr(orch, "analyze_missing_requirements", fake_gaps)
    monkeypatch.setattr(orch, "draft_outreach_message", fake_draft)
    monkeypatch.setattr(orch, "review_message", lambda *a, **k: {"needs_revision": False})

    state = run_pipeline("some job text", "resume-1", _resume_json())

    assert calls["tailors"] == 1 + MAX_REVIEW_REVISIONS
    assert state["revisions"] == MAX_REVIEW_REVISIONS
    assert state["error"] is None


def test_keeps_best_fit_revision(monkeypatch):
    """A revision with a *lower* reviewer score must not replace the current best."""
    reviews = [{"needs_revision": True, "score": 2.0},
               {"needs_revision": False, "score": 1.0}]
    order = []

    def fake_tailor(resume_id, resume_json, job_requirements, feedback=None):
        order.append("tailor")
        return len(order)  # unique marker per call

    def fake_parse(job_raw_text):
        return _job_requirements()

    def fake_review(job_requirements, tailored_resume, original_resume=None):
        order.append("review")
        r = reviews.pop(0)
        return {"needs_revision": r["needs_revision"],
                "feedback": "nudge",
                "fit_comparison": {"tailored_fit_score": r["score"]}}

    monkeypatch.setattr(orch, "parse_job_post", fake_parse)
    monkeypatch.setattr(orch, "tailor_resume", fake_tailor)
    monkeypatch.setattr(orch, "review_resume", fake_review)
    monkeypatch.setattr(orch, "analyze_missing_requirements", lambda **_: [])
    monkeypatch.setattr(orch, "draft_outreach_message", lambda **_: "msg")
    monkeypatch.setattr(orch, "review_message", lambda *a, **k: {"needs_revision": False})

    state = run_pipeline("job", "r", _resume_json())
    # First tailor returned 1 (score 2.0) -> kept. Second returned 2 (score 1.0) -> rejected.
    assert state["tailored_resume"] == 1


def test_review_failure_skips_loop_gracefully(monkeypatch):
    """Reviewer raising must NOT kill the pipeline; keep the first tailor."""

    def fake_tailor(resume_id, resume_json, job_requirements, feedback=None):
        return _tailored_a()

    def fake_parse(job_raw_text):
        return _job_requirements()

    def fake_review(job_requirements, tailored_resume, original_resume=None):
        raise RuntimeError("reviewer down")

    monkeypatch.setattr(orch, "parse_job_post", fake_parse)
    monkeypatch.setattr(orch, "tailor_resume", fake_tailor)
    monkeypatch.setattr(orch, "review_resume", fake_review)
    monkeypatch.setattr(orch, "analyze_missing_requirements", lambda **_: [])
    monkeypatch.setattr(orch, "draft_outreach_message", lambda **_: "msg")
    monkeypatch.setattr(orch, "review_message", lambda *a, **k: {"needs_revision": False})

    state = run_pipeline("job", "r", _resume_json())
    assert state["revisions"] == 0
    assert state["tailored_resume"] == _tailored_a()
    assert state["error"] is None


def test_step_failure_sets_error(monkeypatch):
    """Job parsing failure short-circuits into state["error"]."""

    def fake_parse(job_raw_text):
        raise RuntimeError("llm unavailable")

    monkeypatch.setattr(orch, "parse_job_post", fake_parse)
    state = run_pipeline("job", "r", _resume_json())
    assert "job parsing failed" in state["error"]


class _MonkeyPatch:
    """Minimal monkeypatch shim so the module runs without pytest installed."""

    def setattr(self, target, name, value):
        setattr(target, name, value)


if __name__ == "__main__":
    import traceback

    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            fn(_MonkeyPatch())
            print(f"PASS  {name}")
        except Exception:
            fails += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    print(f"{len([n for n in globals() if n.startswith('test_')]) - fails}/{len([n for n in globals() if n.startswith('test_')])} passed")
    raise SystemExit(1 if fails else 0)