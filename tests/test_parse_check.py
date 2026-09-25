"""Verification of the parse — the check that catches silent loss."""
from __future__ import annotations

from app.domain.models import Contact, RawEntry, RawResume, RawSection
from app.engine.parse_check import check_coverage, parsed_lines

SOURCE = """\
R. KHAN
r.khan@example.com

WORK EXPERIENCE

Data Analyst
Acme Corp | Jan 2022 - Present
Worked on data pipelines for the reporting team
Built a dashboard that reduced manual reporting by 12 hours a week
Responsible for weekly stakeholder reports

SKILLS
Languages: Python, SQL, JavaScript
"""


def faithful() -> RawResume:
    return RawResume(
        contact=Contact(full_name="R. KHAN", email="r.khan@example.com"),
        sections=[
            RawSection(heading="WORK EXPERIENCE", entries=[
                RawEntry(
                    title="Data Analyst", org="Acme Corp",
                    dates="Jan 2022 - Present",
                    bullets=[
                        "Worked on data pipelines for the reporting team",
                        "Built a dashboard that reduced manual reporting by 12 hours a week",
                        "Responsible for weekly stakeholder reports",
                    ],
                ),
            ]),
            RawSection(heading="SKILLS", entries=[
                RawEntry(bullets=["Languages: Python, SQL, JavaScript"]),
            ]),
        ],
    )


# ── the clean case ────────────────────────────────────────────────────

def test_a_faithful_parse_is_clean():
    coverage = check_coverage(SOURCE, faithful())
    assert coverage.is_clean, coverage.summary()


def test_the_summary_says_so():
    assert "accounted for" in check_coverage(SOURCE, faithful()).summary()


# ── loss: the failure this module exists for ──────────────────────────

def test_a_dropped_bullet_is_reported():
    """The failure nothing else in the system can see.

    A parse missing one bullet is well-formed, plausible, and silently
    redefines the document every later guarantee is checked against.
    """
    parse = faithful()
    parse.sections[0].entries[0].bullets.pop(1)     # the dashboard bullet

    coverage = check_coverage(SOURCE, parse)
    assert not coverage.is_clean
    assert any("dashboard" in line for line in coverage.dropped)


def test_a_dropped_entry_is_reported():
    parse = faithful()
    parse.sections[0].entries = []

    dropped = " ".join(check_coverage(SOURCE, parse).dropped)
    assert "Worked on data pipelines" in dropped
    assert "stakeholder" in dropped


def test_a_dropped_section_is_reported():
    parse = faithful()
    parse.sections.pop()                            # the whole SKILLS section

    dropped = check_coverage(SOURCE, parse).dropped
    assert any("Python" in line for line in dropped)


def test_the_summary_counts_the_damage():
    parse = faithful()
    parse.sections[0].entries[0].bullets.pop(1)
    assert "missing from the parse" in check_coverage(SOURCE, parse).summary()


# ── fabrication ───────────────────────────────────────────────────────

def test_an_invented_bullet_is_reported():
    parse = faithful()
    parse.sections[0].entries[0].bullets.append(
        "Led the migration of forty microservices to Kubernetes"
    )

    coverage = check_coverage(SOURCE, parse)
    assert any("Kubernetes" in line for line in coverage.invented)
    assert not coverage.dropped


def test_an_invented_email_is_reported():
    """Two words, so the overlap rule exempts it. Identifiers bypass that rule.

    This is the field v1 got wrong: it mailed applications to addresses the
    model built out of a company's domain name.
    """
    parse = faithful()
    parse.contact.email = "recruiting@some-other-company.example"
    assert "recruiting@some-other-company.example" in check_coverage(SOURCE, parse).invented


def test_an_invented_phone_number_is_reported():
    parse = faithful()
    parse.contact.phone = "+1 555 0100"
    assert check_coverage(SOURCE, parse).invented


def test_a_real_identifier_written_with_different_punctuation_is_accepted():
    """"+92-300-1234567" and "+92 300 1234567" are the same number."""
    source = SOURCE + "\n+92 300 1234567\n"
    parse = faithful()
    parse.contact.phone = "+92-300-1234567"
    assert check_coverage(source, parse).is_clean


def test_a_real_email_is_not_reported_as_invented():
    assert "r.khan@example.com" not in check_coverage(SOURCE, faithful()).invented


# ── what must NOT be reported ─────────────────────────────────────────

def test_moving_a_bullet_between_entries_is_not_a_loss():
    """Structuring is this stage's job. Only vanished text counts."""
    parse = faithful()
    moved = parse.sections[0].entries[0].bullets.pop(2)
    parse.sections.append(
        RawSection(heading="OTHER", entries=[RawEntry(bullets=[moved])])
    )
    coverage = check_coverage(SOURCE, parse)
    assert not coverage.dropped


def test_a_heading_the_parser_did_not_repeat_is_not_a_loss():
    """Short lines carry no claim; flagging them trains the user to skip."""
    parse = faithful()
    parse.sections[1].heading = ""
    assert check_coverage(SOURCE, parse).is_clean


def test_dropping_a_trailing_qualifier_is_not_a_loss():
    """Below MIN_RETAINED is the bar, not perfect equality."""
    parse = faithful()
    bullets = parse.sections[0].entries[0].bullets
    bullets[0] = "Worked on data pipelines for the reporting"
    assert check_coverage(SOURCE, parse).is_clean


def test_dates_and_page_numbers_are_not_reported():
    coverage = check_coverage(SOURCE + "\n2\nPage 1 of 2\n", faithful())
    assert coverage.is_clean


# ── edges ─────────────────────────────────────────────────────────────

def test_an_empty_parse_of_a_real_document_reports_everything():
    coverage = check_coverage(SOURCE, RawResume())
    assert len(coverage.dropped) >= 4
    assert coverage.parsed_lines == 0


def test_two_empties_are_clean():
    assert check_coverage("", RawResume()).is_clean


def test_parsed_lines_reaches_every_field():
    lines = parsed_lines(faithful())
    assert "R. KHAN" in lines
    assert "Jan 2022 - Present" in lines
    assert "Languages: Python, SQL, JavaScript" in lines


# ── structural collapse ───────────────────────────────────────────────
# Every test below is built from a real failure: three resumes parsed with
# `sections: []` and the whole document in one field. The word comparison
# above reported "All 114 lines accounted for", because it measures vocabulary
# and the failure was shape.

BLOB = RawResume(
    contact=Contact(full_name="R. KHAN", email="r.khan@example.com"),
    summary=SOURCE,                       # the entire document, in one field
    sections=[],
)


def test_a_parse_with_no_sections_is_not_clean():
    coverage = check_coverage(SOURCE, BLOB)
    assert not coverage.is_clean
    assert not coverage.dropped           # every word IS present — that is the trap
    assert any("no sections" in p for p in coverage.structure)


def test_a_field_holding_the_whole_document_is_reported():
    assert any("collapsed it" in p for p in check_coverage(SOURCE, BLOB).structure)


def test_the_summary_leads_with_the_structural_failure():
    """"All 114 lines accounted for" was the old answer to this input."""
    text = check_coverage(SOURCE, BLOB).summary()
    assert "accounted for" not in text
    assert "no sections" in text


def test_sections_with_no_entries_are_reported():
    parse = faithful()
    for section in parse.sections:
        section.entries = []
    assert any("no entries" in p for p in check_coverage(SOURCE, parse).structure)


def test_entries_with_no_bullets_at_all_are_reported():
    parse = faithful()
    for section in parse.sections:
        for entry in section.entries:
            entry.bullets = []
    assert any("not one bullet" in p for p in check_coverage(SOURCE, parse).structure)


def test_a_faithful_parse_has_no_structural_complaint():
    assert check_coverage(SOURCE, faithful()).structure == []


def test_a_short_document_is_not_judged_on_structure():
    """A three-line note legitimately has no sections."""
    short = "R. KHAN\nr.khan@example.com\nAvailable from March.\n"
    assert check_coverage(short, RawResume()).structure == []


def test_one_long_bullet_in_a_real_document_is_not_a_collapse():
    """The share test must not fire on a resume that happens to have a long
    bullet — only on a field carrying most of the document."""
    parse = faithful()
    assert check_coverage(SOURCE, parse).structure == []
