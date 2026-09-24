"""Stage S0.2, and the ingest path end to end with a scripted model."""
from __future__ import annotations

import pytest

from app.agents.parse import (
    MAX_OUTPUT_TOKENS,
    MIN_OUTPUT_TOKENS,
    SYSTEM,
    output_budget,
    parse_resume,
)
from app.domain.errors import SchemaValidationFailed
from app.engine.normalize import normalize
from app.engine.parse_check import check_coverage
from app.io.llm import ScriptedClient

RESUME_TEXT = """\
R. KHAN
r.khan@example.com

WORK EXPERIENCE

Data Analyst
Acme Corp | Jan 2022 - Present
- Worked on data pipelines for the reporting team
- Built a dashboard that reduced manual reporting by 12 hours a week

TECHNICAL SKILLS
Languages: Python, SQL
"""

PARSED = {
    "contact": {"full_name": "R. KHAN", "email": "r.khan@example.com"},
    "summary": "",
    "sections": [
        {
            "heading": "WORK EXPERIENCE",
            "entries": [{
                "title": "Data Analyst",
                "org": "Acme Corp",
                "dates": "Jan 2022 - Present",
                "bullets": [
                    "Worked on data pipelines for the reporting team",
                    "Built a dashboard that reduced manual reporting by 12 hours a week",
                ],
            }],
        },
        {
            "heading": "TECHNICAL SKILLS",
            "entries": [{"title": "", "org": "", "dates": "",
                         "bullets": ["Languages: Python, SQL"]}],
        },
    ],
}


# ── the stage ─────────────────────────────────────────────────────────

def test_the_text_reaches_the_model():
    client = ScriptedClient([PARSED])
    parse_resume(RESUME_TEXT, client)
    assert "Worked on data pipelines" in client.calls[0]["user"]


def test_extraction_is_deterministic():
    """One correct answer, so re-uploading a file must re-parse identically."""
    client = ScriptedClient([PARSED])
    parse_resume(RESUME_TEXT, client)
    assert client.calls[0]["temperature"] == 0.0


def test_headings_are_kept_verbatim():
    raw = parse_resume(RESUME_TEXT, ScriptedClient([PARSED]))
    assert [s.heading for s in raw.sections] == ["WORK EXPERIENCE", "TECHNICAL SKILLS"]


def property_names(schema) -> set[str]:
    """Every key the model is asked to produce, at any depth."""
    found: set[str] = set()
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                found |= set(value)
            found |= property_names(value)
    elif isinstance(schema, list):
        for item in schema:
            found |= property_names(item)
    return found


def test_the_schema_asks_for_no_ids():
    """Models duplicate and invent identifiers; code assigns them in S0.3."""
    client = ScriptedClient([PARSED])
    parse_resume(RESUME_TEXT, client)
    names = property_names(client.calls[0]["schema"])
    assert not {n for n in names if n == "id" or n.endswith("_id")}


def test_the_schema_does_not_ask_the_model_to_classify():
    """No `experience` or `projects` keys — that is what forced v1's repair layer."""
    client = ScriptedClient([PARSED])
    parse_resume(RESUME_TEXT, client)
    names = property_names(client.calls[0]["schema"])
    assert "experience" not in names
    assert "projects" not in names
    assert "kind" not in names
    assert "sections" in names          # the document-mirroring shape


def test_empty_input_does_not_call_the_model():
    client = ScriptedClient([])
    raw = parse_resume("   \n  ", client)
    assert raw.sections == []
    assert client.calls == []


def test_a_second_schema_failure_raises_rather_than_half_parsing():
    with pytest.raises(SchemaValidationFailed):
        parse_resume(RESUME_TEXT, ScriptedClient(["nope", "still nope"]))


# ── the truncation trap ───────────────────────────────────────────────

def test_a_long_resume_gets_a_bigger_ceiling():
    """A fixed ceiling truncates the JSON mid-string on a three-page resume.

    That surfaces as "the model would not follow the schema", which sends you
    looking in entirely the wrong place.
    """
    short = output_budget("x" * 500)
    long = output_budget("x" * 12_000)
    assert long > short


def test_the_ceiling_has_a_floor_and_a_cap():
    assert output_budget("hi") == MIN_OUTPUT_TOKENS
    assert output_budget("x" * 500_000) == MAX_OUTPUT_TOKENS


def test_the_budget_reaches_the_client():
    client = ScriptedClient([PARSED])
    long_text = RESUME_TEXT + ("\n- another bullet entirely" * 400)
    parse_resume(long_text, client)
    assert client.calls[0]["max_tokens"] > MIN_OUTPUT_TOKENS


def test_an_explicit_budget_wins():
    client = ScriptedClient([PARSED])
    parse_resume(RESUME_TEXT, client, max_tokens=999)
    assert client.calls[0]["max_tokens"] == 999


# ── the prompt carries the rules that are not enforceable in code ─────

def test_the_prompt_forbids_dropping_and_inventing():
    assert "LOSING TEXT" in SYSTEM
    assert "INVENTING TEXT" in SYSTEM


def test_the_prompt_forbids_splitting_skills():
    """Splitting is `engine.normalize.split_skill_line`, and it is tested."""
    assert "Do not split it into individual skills" in SYSTEM


# ── S0.2 -> S0.3 -> verification ──────────────────────────────────────

def test_ingest_produces_an_addressable_document():
    raw = parse_resume(RESUME_TEXT, ScriptedClient([PARSED]))
    doc = normalize(raw)

    assert doc.section_of_kind("experience") is not None
    assert doc.section_of_kind("skills") is not None
    assert doc.bullet("exp.1.b.1") is not None
    assert "Python" in doc.skill_inventory
    assert "SQL" in doc.skill_inventory


def test_a_faithful_parse_passes_verification():
    raw = parse_resume(RESUME_TEXT, ScriptedClient([PARSED]))
    coverage = check_coverage(RESUME_TEXT, raw)
    assert coverage.is_clean, coverage.summary()


def test_a_lossy_parse_is_caught_before_anything_downstream_runs():
    """The whole point of S0.2 + parse_check together.

    The lossy parse is perfectly valid JSON and normalizes into a perfectly
    valid document. Only the word-level comparison against the source notices.
    """
    lossy = {**PARSED, "sections": [dict(PARSED["sections"][0])]}
    lossy["sections"][0] = {
        **PARSED["sections"][0],
        "entries": [{
            **PARSED["sections"][0]["entries"][0],
            "bullets": ["Worked on data pipelines for the reporting team"],
        }],
    }

    raw = parse_resume(RESUME_TEXT, ScriptedClient([lossy]))
    assert normalize(raw).sections            # valid document, nothing complains

    coverage = check_coverage(RESUME_TEXT, raw)
    assert not coverage.is_clean
    assert any("dashboard" in line for line in coverage.dropped)
