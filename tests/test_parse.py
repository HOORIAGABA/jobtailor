"""Stage S0.2, and the ingest path end to end with a scripted model."""
from __future__ import annotations

import json

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
    "contact": {"full_name": "R. KHAN", "email": "r.khan@example.com", "phone": "", "location": "", "linkedin": "", "github": "", "website": ""},
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
    """A window big enough to need more than the floor asks for more.

    The bullets here have no heading between them, so they stay one section —
    which `split_windows` deliberately keeps whole rather than cutting a job
    away from its bullets.
    """
    client = ScriptedClient([PARSED] * 10)
    long_text = RESUME_TEXT + ("\n- another bullet entirely" * 400)
    parse_resume(long_text, client)
    assert max(c["max_tokens"] for c in client.calls) > MIN_OUTPUT_TOKENS


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


# ── the dumping-ground failure ────────────────────────────────────────
# Three real resumes came back with `sections: []` and the entire document in
# one field. It validated, it normalized, and it produced a document with
# nothing addressable in it.

def test_the_schema_requires_sections():
    """`default_factory=list` keeps a field OUT of `required`, so an empty
    section list satisfied the schema and the model took that path."""
    client = ScriptedClient([PARSED])
    parse_resume(RESUME_TEXT, client)
    schema = client.calls[0]["schema"]
    assert "sections" in schema.get("required", [])


def test_the_schema_requires_entries_and_bullets():
    client = ScriptedClient([PARSED])
    parse_resume(RESUME_TEXT, client)
    section = client.calls[0]["schema"]["properties"]["sections"]["items"]
    assert "entries" in section["required"]
    assert "bullets" in section["properties"]["entries"]["items"]["required"]


def test_the_model_is_given_nowhere_to_dump_text():
    """No `summary` field. A model under a token budget takes the cheapest
    path that satisfies the schema; removing the field removes the path."""
    client = ScriptedClient([PARSED])
    parse_resume(RESUME_TEXT, client)
    assert "summary" not in property_names(client.calls[0]["schema"])


def test_the_prompt_says_there_is_nowhere_else_to_put_text():
    assert "THERE IS NOWHERE ELSE TO PUT TEXT" in SYSTEM
    assert "EVERY SECTION HAS ENTRIES" in SYSTEM


def test_an_empty_section_list_no_longer_validates():
    """The schema is enforced by the client; this pins the model's shape."""
    from app.agents.parse import ResumeDraft
    with pytest.raises(Exception):
        ResumeDraft.model_validate({"contact": {"full_name": "", "email": "", "phone": "", "location": "", "linkedin": "", "github": "", "website": ""}})


def test_a_summary_section_still_becomes_the_documents_summary():
    """Removing the field loses nothing: normalize derives it from the section."""
    with_about = {
        "contact": {"full_name": "R. KHAN", "email": "", "phone": "", "location": "", "linkedin": "", "github": "", "website": ""},
        "sections": [
            {"heading": "ABOUT ME", "entries": [
                {"title": "", "org": "", "dates": "", "bullets": [
                    "AI/ML Engineer with hands-on experience deploying "
                    "production machine learning systems."]}]},
            *PARSED["sections"],
        ],
    }
    raw = parse_resume(RESUME_TEXT, ScriptedClient([with_about]))
    doc = normalize(raw)
    assert "AI/ML Engineer" in doc.summary
    assert doc.section_of_kind("summary") is not None


def test_bullets_that_are_only_whitespace_are_dropped():
    padded = json.loads(json.dumps(PARSED))
    padded["sections"][0]["entries"][0]["bullets"].extend(["", "   "])
    raw = parse_resume(RESUME_TEXT, ScriptedClient([padded]))
    assert all(b.strip() for s in raw.sections for e in s.entries for b in e.bullets)


# ── truncation is not a schema failure ────────────────────────────────
# A 7,660-char resume was cut at exactly its 4,213-token ceiling, twice, and
# both times reported "the model would not return the required shape" — which
# sends you to rewrite a prompt when the fix is a number.

def test_the_budget_covers_the_json_not_just_the_text():
    """The JSON restatement measured 3.11x the source in characters."""
    resume_chars = 7660
    assert output_budget("x" * resume_chars) >= int(resume_chars * 0.78)


def test_a_real_sized_resume_gets_more_than_the_ceiling_that_failed():
    assert output_budget("x" * 7660) > 4213


# ── windowing ─────────────────────────────────────────────────────────
# One JSON for a whole resume is a bet that the model's entire output budget
# covers it. That bet lost twice on a two-page CV — at 7,660 tokens and again
# at 15,320 — where a ~6,000-token JSON should have fit. Raising the number
# again would only move the failure.

from app.agents.parse import (            # noqa: E402
    WINDOW_CHARS, looks_like_heading, merge, split_windows,
)

TWO_PAGE = """\
R. KHAN
r.khan@example.com

ABOUT ME
Engineer with experience in production machine learning systems and pipelines.

WORK EXPERIENCE

Data Analyst
Acme Corp | 01/2022 - Present
- Worked on data pipelines for the reporting team
- Built a dashboard that reduced manual reporting by 12 hours a week

PROJECTS

Churn predictor | 2023
- Trained a model to predict customer churn using scikit-learn

TECHNICAL SKILLS
Languages: Python, SQL
"""


def test_a_short_document_is_one_window():
    """No behaviour change for a one-page resume."""
    assert split_windows("short text", window_chars=3500) == ["short text"]


def test_a_long_document_splits_at_its_headings():
    windows = split_windows(TWO_PAGE, window_chars=200)
    assert len(windows) > 1
    for window in windows[1:]:
        assert looks_like_heading(window.splitlines()[0]), window[:40]


def test_a_job_never_gets_separated_from_its_bullets():
    """The one thing a split must not do."""
    windows = split_windows(TWO_PAGE, window_chars=200)
    holder = next(w for w in windows if "Data Analyst" in w)
    assert "Worked on data pipelines" in holder
    assert "reduced manual reporting" in holder


def test_a_section_bigger_than_a_window_is_left_whole():
    """Cutting a section is worse than one large call."""
    big = "EXPERIENCE\n" + "\n".join(f"- bullet number {n} here" for n in range(80))
    assert len(split_windows(big, window_chars=200)) == 1


def test_nothing_is_lost_in_the_split():
    rejoined = " ".join(split_windows(TWO_PAGE, window_chars=200)).split()
    assert rejoined == TWO_PAGE.split()


def test_empty_text_makes_no_windows():
    assert split_windows("") == []
    assert split_windows("   \n  ") == []


def test_headings_are_recognised_conservatively():
    assert looks_like_heading("WORK EXPERIENCE")
    assert looks_like_heading("PROJECTS")
    assert looks_like_heading("Technical Skills")
    # Not headings — a false split cuts a job from its bullets.
    assert not looks_like_heading("- Built a dashboard that saved 12 hours")
    assert not looks_like_heading("Worked on data pipelines for the team.")
    assert not looks_like_heading("")
    assert not looks_like_heading("2018 - 2022")


# ── merging ───────────────────────────────────────────────────────────

def test_a_section_split_across_windows_is_merged():
    """Two "WORK EXPERIENCE" sections would normalize into sec.experience and
    sec.experience.2, splitting one job history into two."""
    from app.domain.models import RawEntry, RawResume, RawSection

    merged = merge([
        RawResume(sections=[RawSection(heading="WORK EXPERIENCE",
                                       entries=[RawEntry(title="Data Analyst")])]),
        RawResume(sections=[RawSection(heading="Work Experience",
                                       entries=[RawEntry(title="Intern")])]),
    ])
    assert len(merged.sections) == 1
    assert [e.title for e in merged.sections[0].entries] == ["Data Analyst", "Intern"]


def test_merge_keeps_document_order():
    from app.domain.models import RawResume, RawSection

    merged = merge([
        RawResume(sections=[RawSection(heading="ABOUT ME")]),
        RawResume(sections=[RawSection(heading="EXPERIENCE")]),
        RawResume(sections=[RawSection(heading="SKILLS")]),
    ])
    assert [s.heading for s in merged.sections] == ["ABOUT ME", "EXPERIENCE", "SKILLS"]


def test_contact_is_taken_from_the_window_that_has_it():
    from app.domain.models import Contact, RawResume

    merged = merge([
        RawResume(),
        RawResume(contact=Contact(full_name="R. KHAN", email="r@example.com")),
    ])
    assert merged.contact.full_name == "R. KHAN"


def test_one_call_per_window():
    client = ScriptedClient([PARSED, PARSED, PARSED])
    parse_resume(TWO_PAGE, client, window_chars=200)
    assert len(client.calls) == len(split_windows(TWO_PAGE, window_chars=200))


def test_each_window_gets_its_own_ceiling():
    """The ceiling stops depending on the length of the whole document."""
    client = ScriptedClient([PARSED, PARSED, PARSED])
    parse_resume(TWO_PAGE, client, window_chars=200)
    assert all(c["max_tokens"] <= MAX_OUTPUT_TOKENS for c in client.calls)


# ── what the grammar does not force, a model does not fill ────────────
# Measured on llama3.1:8b against a real resume: THREE work-experience entries
# came back with empty title, org and dates while `bullets` was filled
# perfectly — and `contact` was `{}` with the email plainly on screen. Only
# `bullets` was required. This is the third time the same lesson has landed.

def test_every_entry_field_is_required():
    client = ScriptedClient([PARSED])
    parse_resume(RESUME_TEXT, client)
    entry = (client.calls[0]["schema"]["properties"]["sections"]["items"]
             ["properties"]["entries"]["items"])
    assert set(entry["required"]) == {"title", "org", "dates", "bullets"}


def test_every_contact_field_is_required():
    client = ScriptedClient([PARSED])
    parse_resume(RESUME_TEXT, client)
    contact = client.calls[0]["schema"]["properties"]["contact"]
    assert "full_name" in contact["required"]
    assert "email" in contact["required"]
    assert "contact" in client.calls[0]["schema"]["required"]


def test_a_required_field_may_still_be_empty():
    """Requiring the key does not invent a value: "" stays legal for something
    the document does not say. It forces the model to LOOK."""
    from app.agents.parse import ResumeDraft
    draft = ResumeDraft.model_validate({
        "contact": {k: "" for k in ("full_name", "email", "phone", "location",
                                    "linkedin", "github", "website")},
        "sections": [{"heading": "SKILLS", "entries": [
            {"title": "", "org": "", "dates": "", "bullets": ["Python, SQL"]}]}],
    })
    assert draft.sections[0].entries[0].title == ""


def test_an_entry_missing_a_field_no_longer_validates():
    from app.agents.parse import ResumeDraft
    with pytest.raises(Exception):
        ResumeDraft.model_validate({
            "contact": {k: "" for k in ("full_name", "email", "phone",
                                        "location", "linkedin", "github",
                                        "website")},
            "sections": [{"heading": "X", "entries": [{"bullets": ["a"]}]}],
        })


def test_the_prompt_says_every_field_appears():
    assert "EVERY FIELD APPEARS, ALWAYS" in SYSTEM
    assert "broken entry" in SYSTEM


def test_the_contact_survives_the_widening():
    raw = parse_resume(RESUME_TEXT, ScriptedClient([PARSED]))
    assert raw.contact.full_name == "R. KHAN"
    assert raw.contact.email == "r.khan@example.com"
