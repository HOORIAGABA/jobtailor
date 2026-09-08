"""Regression tests for M7.2 deterministic post-processing (tailoring_agent).

Locks the safety nets that protect against the jumbling symptom when the
parser (M5) still misses occasionally:
  - `_reclassify_entries` moves entries back between experience/projects,
  - `_dedupe_entries` never collapses two entries that share a title but have
    no company (cross-section contamination guard),
  - `_merge_missing_sections` runs reclassification on BOTH the original and
    the tailored resume before merging.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.tailoring_agent import _reclassify_entries, _dedupe_entries, _merge_missing_sections


def test_reclassify_project_back_to_projects():
    tailored = {
        "experience": [
            {"title": "AI-Driven Malware Detection", "company": "",
             "bullets": ["Built a CNN classifier for PE files"]},
            {"title": "Data Engineer", "company": "Acme", "bullets": ["etl"]},
        ],
        "projects": [],
    }
    original = {
        "experience": [{"title": "Data Engineer", "company": "Acme"}],
        "projects": [{"title": "AI-Driven Malware Detection"}],
    }
    _reclassify_entries(tailored, original)
    assert [e["title"] for e in tailored["experience"]] == ["Data Engineer"]
    assert [p["title"] for p in tailored["projects"]] == ["AI-Driven Malware Detection"]


def test_reclassify_job_back_to_experience():
    tailored = {
        "experience": [],
        "projects": [
            {"title": "Junior Data Analyst", "company": "FinCorp", "bullets": ["built dashboards"]},
            {"title": "Passion Project", "company": "", "bullets": ["fun"]},
        ],
    }
    original = {
        "experience": [{"title": "Junior Data Analyst", "company": "FinCorp"}],
        "projects": [{"title": "Passion Project"}],
    }
    _reclassify_entries(tailored, original)
    assert [p["title"] for p in tailored["projects"]] == ["Passion Project"]
    assert [e["title"] for e in tailored["experience"]] == ["Junior Data Analyst"]


def test_dedup_keeps_companyless_entries_apart():
    entries = [
        {"title": "Sub System", "company": "", "bullets": ["a"]},
        {"title": "Sub System", "company": "", "bullets": ["b"]},
    ]
    out = _dedupe_entries(entries)
    assert len(out) == 2  # same key but no company on either -> both kept


def test_dedup_collapses_when_both_have_company():
    entries = [
        {"title": "Software Engineer", "company": "Acme", "bullets": ["old"]},
        {"title": "Software Engineer", "company": "Acme", "bullets": ["old", "new"]},
    ]
    out = _dedupe_entries(entries)
    assert len(out) == 1
    assert out[0]["bullets"] == ["old", "new"]


def test_merge_missing_sections_reclassifies_original_before_match():
    # The original itself has the project misclassified in experience; the
    # merge step must not pull it back into a wrong section just because the
    # tailored output dropped it.
    tailored = {"skills": ["python"], "experience": [{"title": "Data Engineer", "company": "Acme"}]}
    original = {
        "skills": ["python"],
        "experience": [
            {"title": "Data Engineer", "company": "Acme"},
            {"title": "Secure Chat App", "company": "", "bullets": ["end-to-end encryption"]},
        ],
        "projects": [],
    }
    merged = _merge_missing_sections(tailored, original)
    exp = [e["title"] for e in merged.get("experience", [])]
    proj = [p["title"] for p in merged.get("projects", [])]
    assert "Data Engineer" in exp
    assert "Secure Chat App" in proj
    assert "Secure Chat App" not in exp


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception:
            fails += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"{len(tests) - fails}/{len(tests)} passed")
    raise SystemExit(1 if fails else 0)