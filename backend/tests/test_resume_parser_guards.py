"""M5.8 — regression guards for the resume-parser deterministic helpers.

Tests the pure-Python pieces of M5.1 (contact fallback, labeled location,
group-heading detection, skill flattening). These run WITHOUT the LLM so they
are fast and deterministic. The LLM-dependent end-to-end parse is exercised via
the fixture set in fixtures/ (run manually or via smoke_test).

Run: python -m pytest tests/test_resume_parser_guards.py -q
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agents.resume_parser_agent import (
    _clean_skills,
    _fix_labeled_location,
    _flatten_skill_items,
    _looks_like_group_heading,
    _normalize_link,
    _split_skill_string,
    _validate_output,
)
from app.agents.contact_extractor import extract_contact_info

# ---------------------------------------------------------------------------
# Fix #4 — flat skills (group-heading detection)
# ---------------------------------------------------------------------------
GROUP_HEADINGS = [
    "Computer Vision & Image Processing",
    "AI/ML Frameworks & Techniques",
    "Backend & Production Pipelines",
    "Automation & Integration",
    "Deep Learning Frameworks",
    "Core Competencies",
    "Programming Languages & Tools",
]
REAL_SKILLS = [
    "YOLO",
    "PyTorch",
    "FastAPI",
    "n8n",
    "Python",
    "OpenCV",
    "LangChain",
    "REST APIs",
    "Machine Learning",
    "SQL",
]


def test_group_heading_detection():
    for h in GROUP_HEADINGS:
        assert _looks_like_group_heading(h), f"should flag heading: {h!r}"
    for s in REAL_SKILLS:
        assert not _looks_like_group_heading(s), f"should NOT flag real skill: {s!r}"


def test_skill_flattening_nested_and_heading():
    raw = [
        {"Computer Vision & Image Processing": ["YOLO", "object detection"]},
        "PyTorch",
        "AI/ML Frameworks & Techniques",
        ["FastAPI", "n8n"],
        {"Backend & Production Pipelines": "Docker"},
    ]
    out = _clean_skills(raw)
    assert out == ["YOLO", "object detection", "PyTorch", "FastAPI", "n8n", "Docker"]


def test_flatten_skill_items_string_comma():
    items = []
    _flatten_skill_items("Python, PyTorch, YOLO", items)
    assert items == ["Python", "PyTorch", "YOLO"]


def test_split_skill_string_keeps_parens_whole():
    assert _split_skill_string(
        "Transfert learning architectures (VGG16, VGG19, MobileNet V3, ResNet50, ...etc)"
    ) == ["Transfert learning architectures (VGG16, VGG19, MobileNet V3, ResNet50, ...etc)"]
    assert _split_skill_string("Python, PyTorch") == ["Python", "PyTorch"]


def test_normalize_link():
    assert _normalize_link("www.linkedin.com/in/hooria-attas") == "https://linkedin.com/in/hooria-attas"
    assert _normalize_link("github.com/saraali") == "https://github.com/saraali"
    assert _normalize_link("https://github.com/x/y") == "https://github.com/x/y"
    assert _normalize_link("") == ""


def test_validate_output_cleans_skills():
    data = {"skills": ["Python", {"Computer Vision & Image Processing": ["YOLO"]}, "Tools"]}
    out = _validate_output(data)
    assert out["skills"] == ["Python", "YOLO"]


# ---------------------------------------------------------------------------
# Fix #1 — labeled City/Country → single location
# ---------------------------------------------------------------------------
def test_labeled_location_pipe():
    r = {}
    _fix_labeled_location(r, "City: Lahore | Country: Pakistan\nEmail: x@y.com")
    assert r["location"] == "Lahore, Pakistan"


def test_labeled_location_city_only():
    r = {}
    _fix_labeled_location(r, "City: Karachi\nEmail: x@y.com")
    assert r["location"] == "Karachi"


def test_labeled_location_country_first():
    r = {}
    _fix_labeled_location(r, "Country: Pakistan\nCity: Rawalpindi | Email: a@b.c")
    assert r["location"] == "Rawalpindi, Pakistan"


def test_contact_extractor_finds_github_and_location():
    info = extract_contact_info(
        "SARA ALI\nEmail: sara@gmail.com\nCity: Lahore | Country: Pakistan\n"
        "github.com/saraali\nlinkedin.com/in/saraali\n+92 300 0000000"
    )
    assert info["github"] == "https://github.com/saraali"
    assert info["linkedin"] == "https://linkedin.com/in/saraali"
    assert info["email"] == "sara@gmail.com"
    assert "+92" in info["phone"]


def test_contact_fallback_fills_empty_fields():
    from app.agents.resume_parser_agent import _apply_contact_fallback

    r = {"location": "", "github": "", "linkedin": "", "phone": "", "email": ""}
    out = _apply_contact_fallback(
        r,
        "AHMED KHAN\nEmail: ahmed@x.com\n+1 555-1234\n"
        "linkedin.com/in/ahmed\ngithub.com/ahmed",
    )
    assert out["github"] == "https://github.com/ahmed"
    assert out["linkedin"] == "https://linkedin.com/in/ahmed"
    assert out["email"] == "ahmed@x.com"
    assert out["phone"] == "+1 555-1234"
    assert out["full_name"] == "AHMED KHAN"


# ---------------------------------------------------------------------------
# Fix #3 — experience/project binary rule (prompt-level; guard on structure)
# ---------------------------------------------------------------------------
def test_validate_output_structure_experience_projects():
    data = {
        "experience": [
            {"heading": "WORK EXPERIENCE", "title": "AI-Driven Malware Detection",
             "company": "", "dates": "Aug 2024 - Jul 2025",
             "bullets": ["Developed an ML system using CNNs"]}
        ],
        "projects": [],
    }
    out = _validate_output(data)
    # Structure must keep the entry exactly where the LLM classified it,
    # with all required keys normalized (the prompt enforces classification).
    assert len(out["experience"]) == 1
    assert out["experience"][0]["title"] == "AI-Driven Malware Detection"
    assert out["experience"][0]["company"] == ""


# ---------------------------------------------------------------------------
# Sanity: prompt contains the hard rules the fixture depends on
# ---------------------------------------------------------------------------
def test_prompt_contains_binary_classification_rule():
    from app.agents.resume_parser_agent import SYSTEM_PROMPT

    assert "experience" in SYSTEM_PROMPT
    assert "paid employment" in SYSTEM_PROMPT
    assert "When in doubt, classify as a" in SYSTEM_PROMPT


def test_prompt_contains_flat_skills_rule():
    from app.agents.resume_parser_agent import SYSTEM_PROMPT

    assert "FLAT list" in SYSTEM_PROMPT
    assert "group heading" in SYSTEM_PROMPT.lower()


def test_prompt_is_concise():
    # The prompt previously ballooned to ~250 dense lines (rules + 2 full
    # examples) which 8B local models could not follow - content drained into
    # empty skeletons (empty certs, empty titles, empty skills). Keep it lean.
    from app.agents.resume_parser_agent import SYSTEM_PROMPT

    assert len(SYSTEM_PROMPT) < 4000, f"prompt is {len(SYSTEM_PROMPT)} chars"
    assert "## Example" not in SYSTEM_PROMPT


def test_drop_empty_placeholder_entries():
    from app.agents.resume_parser_agent import _drop_empty_entries

    entries = [
        {"heading": "", "title": "", "company": "", "dates": "", "bullets": []},
        {"heading": "", "title": "", "company": "", "dates": "", "bullets": []},
        {"heading": "EDUCATION", "title": "", "company": "",
         "dates": "01/10/2021 - 17/07/2025", "bullets": []},
    ]
    out = _drop_empty_entries(entries)
    assert len(out) == 1
    assert out[0]["dates"] == "01/10/2021 - 17/07/2025"


def test_validate_output_strips_empty_certifications():
    from app.agents.resume_parser_agent import _validate_output

    data = {
        "full_name": "X",
        "certifications": [
            {"heading": "", "title": "", "company": "", "dates": "", "bullets": []},
            {"heading": "", "title": "", "company": "", "dates": "", "bullets": []},
        ],
    }
    out = _validate_output(data)
    assert out["certifications"] == []


def test_prompt_never_drops_sections():
    from app.agents.resume_parser_agent import SYSTEM_PROMPT

    assert "NEVER DROP CONTENT" in SYSTEM_PROMPT
    assert "silently" in SYSTEM_PROMPT.lower() and "discard" in SYSTEM_PROMPT.lower()


def test_email_local_part_name_fallback():
    from app.agents.resume_parser_agent import _apply_contact_fallback

    r = {"email": "sara.ali@gmail.com", "full_name": ""}
    out = _apply_contact_fallback(r, "sara.ali@gmail.com\nNo visible name line")
    assert out["full_name"] == "Sara Ali"


def test_empty_name_untouched_when_no_email():
    from app.agents.resume_parser_agent import _apply_contact_fallback

    r = {"email": "", "full_name": ""}
    out = _apply_contact_fallback(r, "no contact at all")
    assert out["full_name"] == ""


def test_location_guard_rejects_tool_stack():
    from app.agents.resume_parser_agent import _guard_location

    r = {"location": "Numpy, Matplotlib"}
    out = _guard_location(r)
    assert out["location"] == ""


def test_location_guard_keeps_real_place():
    from app.agents.resume_parser_agent import _guard_location

    r = {"location": "Lahore, Pakistan"}
    out = _guard_location(r)
    assert out["location"] == "Lahore, Pakistan"


def test_non_location_words_blocks_libraries():
    from app.agents.contact_extractor import _looks_like_location

    # "Numpy, Matplotlib" is a skills line, not a place - must not pass.
    assert _looks_like_location("Numpy, Matplotlib") is False


def test_validate_output_folds_work_experience_alias():
    from app.agents.resume_parser_agent import _validate_output

    data = {
        "full_name": "Hooria Attas",
        "work_experience": [
            {"position": "ML Engineer", "company": "Acme", "city": "Lahore",
             "country": "Pakistan", "dates": "01/2022 - Present"},
        ],
        "experience": [],
    }
    out = _validate_output(data)
    assert "work_experience" not in out
    exp = out["experience"]
    assert len(exp) == 1
    assert exp[0]["title"] == "ML Engineer"          # position -> title
    assert exp[0]["location"] == "Lahore, Pakistan"  # city/country -> location
    assert exp[0]["date_start"] == "2022-01"         # M5.3 normalization applied
    assert exp[0]["date_ongoing"] is True


def test_validate_output_merges_pre_existing_experience():
    from app.agents.resume_parser_agent import _validate_output

    data = {
        "full_name": "X",
        "experience": [{"title": "Old Role", "company": "B", "dates": "2020"}],
        "work_experience": [{"position": "New Role", "company": "C", "dates": "2023"}],
    }
    out = _validate_output(data)
    titles = [e["title"] for e in out["experience"]]
    assert titles == ["Old Role", "New Role"]


# ---------------------------------------------------------------------------
# Second-resume failures (53bf2eb5): invented key, platform-as-location,
# drained titles, duplicate entries
# ---------------------------------------------------------------------------
def test_validate_output_folds_management_leadership_alias():
    from app.agents.resume_parser_agent import _validate_output

    data = {
        "full_name": "Hooria Attas",
        "management_and_leadership_skills": [
            {"title": "Co-Ambassador, Google Developer Groups on Campus",
             "dates": "", "bullets": ["Organized technical workshops"]},
        ],
        "leadership": [],
    }
    out = _validate_output(data)
    assert "management_and_leadership_skills" not in out
    assert len(out["leadership"]) == 1
    assert out["leadership"][0]["title"] == "Co-Ambassador, Google Developer Groups on Campus"


def test_validate_output_folds_invented_entry_keys_to_extra():
    from app.agents.resume_parser_agent import _validate_output

    data = {
        "full_name": "X",
        "publications": [
            {"title": "A Paper", "dates": "2023", "bullets": ["some content"]},
        ],
    }
    out = _validate_output(data)
    assert "publications" not in out
    assert len(out["extra_sections"]) == 1
    assert out["extra_sections"][0]["heading"] == "PUBLICATIONS"
    assert out["extra_sections"][0]["entries"][0]["title"] == "A Paper"


def test_location_guard_rejects_platform_list():
    from app.agents.resume_parser_agent import _guard_location

    r = {"location": "Google,\n     Slack"}
    out = _guard_location(r)
    assert out["location"] == ""


def test_validate_output_dedupes_identical_entries():
    from app.agents.resume_parser_agent import _validate_output

    data = {
        "projects": [
            {"title": "Automated Timetable Alarm System",
             "company": "", "dates": "10/06/2025 - 19/12/2025", "bullets": ["a"]},
            {"title": "Automated Timetable Alarm System",
             "company": "", "dates": "10/06/2025 - 19/12/2025", "bullets": ["b"]},
        ],
    }
    out = _validate_output(data)
    assert len(out["projects"]) == 1


def test_restore_missing_titles_patches_verbatim_only():
    from app.agents import resume_parser_agent
    from app.agents.resume_parser_agent import _restore_missing_titles

    raw = ("HOORIA ATTAS\nPROJECTS\nRGB-Thermal Aerial Object Detection Pipeline\n"
           "Feb 2026 - May 2026\n- Built a 4-channel detector\n"
           "- deployed via TensorRT\n")
    result = {
        "projects": [
            {"title": "", "company": "", "dates": "Feb 2026 - May 2026",
             "bullets": ["- Built a 4-channel detector", "- deployed via TensorRT"]},
            {"title": "", "company": "", "dates": "Feb 2025 - Mar 2025",
             "bullets": ["- something else"]},
        ],
    }

    def fake_llm(system, user, json_mode=False):
        return ('{"titles": [{"id": 0, "title": "RGB-Thermal Aerial Object '
                'Detection Pipeline"}, {"id": 1, "title": "Drone Surveillance System"}]}')

    resume_parser_agent.call_resume_parser_llm = fake_llm
    out = _restore_missing_titles(result, raw)
    # Verbatim title is patched; hallucinated one is discarded.
    assert out["projects"][0]["title"] == "RGB-Thermal Aerial Object Detection Pipeline"
    assert out["projects"][1]["title"] == ""


def test_restore_missing_titles_noop_when_none_missing():
    from app.agents import resume_parser_agent
    from app.agents.resume_parser_agent import _restore_missing_titles

    result = {
        "projects": [{"title": "FBR Tax Assistant", "company": "", "dates": "2026",
                      "bullets": ["- built"], "heading": "PROJECTS"}],
        "experience": [], "certifications": [], "leadership": [],
    }

    def bomb(*a, **k):
        raise AssertionError("repair LLM must not be called when no titles are missing")

    resume_parser_agent.call_resume_parser_llm = bomb
    out = _restore_missing_titles(result, "FBR Tax Assistant 2026 - built")
    assert out["projects"][0]["title"] == "FBR Tax Assistant"


# ---------------------------------------------------------------------------
# Stdlib runner (no pytest needed)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys, traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)