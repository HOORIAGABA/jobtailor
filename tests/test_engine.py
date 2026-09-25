"""Engine tests. Deterministic — no API key, no network, no database."""
import pytest

from app.domain.models import (
    Bullet, Contact, Item, RawEntry, RawResume, RawSection, ResumeDoc, Section,
)
from app.domain.ops import (
    DropBullet, PromoteItem, ReorderBullets, ReorderItems, RewriteBullet,
    SetSkills, SetSummary, SkillGroup, FlagGap,
)
from app.engine.normalize import (
    build_skill_inventory, classify_heading, normalize, parse_date_range,
    split_skill_line,
)
from app.engine.validator import (
    entities, escalates_seniority, grounding_corpus, is_keyword_stuffing,
    numbers, term_density_violation, validate,
)


# ══ normalize ═════════════════════════════════════════════════════════

@pytest.mark.parametrize("heading,kind", [
    ("EXPERIENCE", "experience"),
    ("Work Experience", "experience"),
    ("PROFESSIONAL EXPERIENCE", "experience"),
    ("Employment History", "experience"),
    ("Projects", "projects"),
    ("Academic Projects", "projects"),
    ("EDUCATION", "education"),
    ("Certifications", "certifications"),
    ("Volunteering", "leadership"),
    ("TECHNICAL SKILLS", "skills"),
    ("Core Competencies", "skills"),
    ("Professional Summary", "summary"),
])
def test_known_headings_classify(heading, kind):
    assert classify_heading(heading) == kind


@pytest.mark.parametrize("heading", ["Publications", "Languages", "What I've Built", "Patents"])
def test_unknown_headings_become_custom(heading):
    """`custom` keeps the section under its own heading instead of guessing."""
    assert classify_heading(heading) == "custom"


def test_heading_punctuation_and_spacing_ignored():
    assert classify_heading("  work   experience :  ") == "experience"
    assert classify_heading("") == "custom"


def test_substring_probe_catches_variants():
    assert classify_heading("Relevant Project Work") == "projects"
    assert classify_heading("My Technical Skillset") == "skills"


# ── skill inventory ───────────────────────────────────────────────────

def test_split_skill_line_drops_the_label():
    assert split_skill_line("Languages: Python, SQL, Go") == ["Python", "SQL", "Go"]


def test_split_skill_line_handles_separators():
    assert split_skill_line("Python | SQL • Airflow / Docker") == [
        "Python", "SQL", "Airflow", "Docker"]


def test_split_skill_line_rejects_prose():
    """A long sentence is not a skill, even in a skills section."""
    assert split_skill_line(
        "Strong communicator with a proven track record of delivering results"
    ) == []


def test_inventory_only_reads_skills_sections():
    doc_sections = [
        Section(id="sec.skills", kind="skills", heading="SKILLS", items=[
            Item(id="skl.1", bullets=[
                Bullet(id="skl.1.b.1", text="Languages: Python, SQL"),
                Bullet(id="skl.1.b.2", text="Tools: Airflow, Docker"),
            ]),
        ]),
        Section(id="sec.experience", kind="experience", heading="EXPERIENCE", items=[
            Item(id="exp.2", bullets=[Bullet(id="exp.2.b.1", text="Used Kubernetes daily")]),
        ]),
    ]
    inv = [s.lower() for s in build_skill_inventory(doc_sections)]
    assert inv == ["python", "sql", "airflow", "docker"]
    assert "kubernetes" not in inv        # mentioned in prose, not declared


def test_inventory_empty_when_no_skills_section():
    assert build_skill_inventory([]) == []


# ── dates ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Jan 2022 - Present", ("2022-01", "")),
    ("January 2022 – December 2024", ("2022-01", "2024-12")),
    ("2021 - 2023", ("2021", "2023")),
    ("Mar 2020 to Current", ("2020-03", "")),
    ("", ("", "")),
    ("sometime last year", ("", "")),
])
def test_parse_date_range(raw, expected):
    assert parse_date_range(raw) == expected


# ── the transform ─────────────────────────────────────────────────────

def _raw() -> RawResume:
    return RawResume(
        contact=Contact(full_name="R. Khan", email="r@example.com"),
        sections=[
            RawSection(heading="WORK EXPERIENCE", entries=[
                RawEntry(title="Data Analyst", org="Acme", dates="Jan 2022 - Present",
                         bullets=["Worked on data pipelines", "Built a dashboard"]),
                RawEntry(title="Intern", org="Beta", dates="2021", bullets=["Wrote SQL"]),
            ]),
            RawSection(heading="TECHNICAL SKILLS", entries=[
                RawEntry(bullets=["Languages: Python, SQL", "Tools: Airflow"]),
            ]),
            RawSection(heading="PUBLICATIONS", entries=[RawEntry(title="A paper")]),
        ],
    )


def test_normalize_assigns_unique_stable_ids():
    doc = normalize(_raw())
    ids = [i.id for i in doc.all_items()]
    assert len(ids) == len(set(ids))
    bids = [b.id for b in doc.all_bullets()]
    assert len(bids) == len(set(bids))


def test_normalize_is_deterministic():
    a, b = normalize(_raw()), normalize(_raw())
    assert a.model_dump() == b.model_dump()


def test_normalize_preserves_every_section():
    doc = normalize(_raw())
    assert [s.heading for s in doc.sections] == [
        "WORK EXPERIENCE", "TECHNICAL SKILLS", "PUBLICATIONS"]
    assert doc.section_of_kind("custom").heading == "PUBLICATIONS"


def test_normalize_builds_inventory_and_dates():
    doc = normalize(_raw())
    assert [s.lower() for s in doc.skill_inventory] == ["python", "sql", "airflow"]
    first = doc.section_of_kind("experience").items[0]
    assert (first.date_start, first.date_end) == ("2022-01", "")
    assert first.dates == "Jan 2022 - Present"       # display string kept


def test_normalize_does_not_mutate_input():
    raw = _raw()
    before = raw.model_dump()
    normalize(raw)
    assert raw.model_dump() == before


def test_bullets_are_addressable_from_their_item():
    doc = normalize(_raw())
    item = doc.section_of_kind("experience").items[0]
    assert doc.bullet(item.bullets[0].id).text == "Worked on data pipelines"


# ══ validator ═════════════════════════════════════════════════════════

def _doc() -> ResumeDoc:
    return ResumeDoc(
        sections=[
            Section(id="sec.experience", kind="experience", heading="EXPERIENCE", items=[
                Item(id="exp.1", title="Data Analyst", org="Acme", bullets=[
                    Bullet(id="exp.1.b.1", text="Worked on data pipelines for the reporting team"),
                    Bullet(id="exp.1.b.2", text="Built a dashboard that saved 12 hours a week"),
                ]),
                Item(id="exp.2", title="Intern", org="Beta", bullets=[
                    Bullet(id="exp.2.b.1", text="Helped with the data migration"),
                ]),
            ]),
        ],
        skill_inventory=["Python", "SQL", "Airflow"],
    )


def _accept(op, doc=None, confirmed=()):
    return validate([op], doc or _doc(), confirmed)


# ── extraction helpers ────────────────────────────────────────────────

def test_numbers_normalizes_thousands_separators():
    assert numbers("served 1,200 users in 3 regions") == {"1200", "3"}


def test_entities_skips_the_first_word_and_common_verbs():
    found = entities("Built Airflow pipelines with Python")
    assert "airflow" in found and "python" in found
    assert "built" not in found


def test_entities_catches_acronyms_and_tool_shaped_tokens():
    found = entities("wrote an ETL job in C++ and .NET")
    assert {"etl", "c++", ".net"} <= found


# ── Class A: fabricated numbers ───────────────────────────────────────

def test_rejects_an_invented_metric():
    r = _accept(RewriteBullet(op_id="1", bullet_id="exp.1.b.1",
                              text="Built pipelines, cutting latency by 40%"))
    assert r.accepted == [] and r.rejected[0].code == "fabricated_number"
    assert r.rejected[0].ask_user           # asks instead of silently dropping


def test_rejects_a_derived_number_even_though_it_is_arithmetically_true():
    """12 hours/week is ~624/year, but 624 appears nowhere. Still a fabrication."""
    r = _accept(RewriteBullet(op_id="1", bullet_id="exp.1.b.2",
                              text="Built a dashboard that saved 624 hours annually"))
    assert r.rejected[0].code == "fabricated_number"


def test_allows_reusing_a_number_already_in_the_origin():
    r = _accept(RewriteBullet(op_id="1", bullet_id="exp.1.b.2",
                              text="Shipped a reporting dashboard, saving 12 hours weekly"))
    assert r.rejected == []


# ── Class C: unsupported entities ─────────────────────────────────────

def test_rejects_a_tool_the_resume_never_mentions():
    r = _accept(RewriteBullet(op_id="1", bullet_id="exp.1.b.1",
                              text="Ran pipelines on Kubernetes for the reporting team"))
    assert r.rejected[0].code == "unsupported_entity"


def test_allows_a_tool_from_the_skill_inventory():
    r = _accept(RewriteBullet(op_id="1", bullet_id="exp.1.b.1",
                              text="Ran Airflow pipelines in Python for the reporting team"))
    assert r.rejected == []


def test_a_confirmed_capability_unlocks_a_term():
    """The ledger is the only route from 'true but unstated' to 'sayable'."""
    op = RewriteBullet(op_id="1", bullet_id="exp.1.b.1",
                       text="Ran scheduled pipelines on Kubernetes for the reporting team")
    assert _accept(op).rejected[0].code == "unsupported_entity"
    assert _accept(op, confirmed=["Kubernetes"]).rejected == []


# ── Class B: escalation ───────────────────────────────────────────────

def test_rejects_helped_becoming_drove():
    r = _accept(RewriteBullet(op_id="1", bullet_id="exp.2.b.1",
                              text="Drove the data migration"))
    assert r.rejected[0].code == "seniority_escalation"


def test_allows_rewording_that_does_not_promote():
    r = _accept(RewriteBullet(op_id="1", bullet_id="exp.2.b.1",
                              text="Supported the data migration across systems"))
    assert r.rejected == []


def test_rejects_added_scope_claims():
    r = _accept(RewriteBullet(op_id="1", bullet_id="exp.1.b.1",
                              text="Owned data pipelines end-to-end for the reporting team"))
    assert r.rejected[0].code == "seniority_escalation"


def test_ownership_verb_already_present_is_not_escalation():
    assert escalates_seniority("Led and helped the team", "Led the team") is None


# ── Class B: keyword stuffing ─────────────────────────────────────────

def test_detects_pure_keyword_injection():
    assert is_keyword_stuffing(
        "Built data pipelines for the reporting team",
        "Built Python data pipelines for the reporting team",
        ["Python"],
    )


def test_a_real_rewrite_is_not_stuffing():
    assert not is_keyword_stuffing(
        "Worked on data pipelines for the reporting team",
        "Built and maintained nightly ingestion jobs feeding the reporting warehouse",
        ["Python"],
    )


def test_no_new_terms_means_no_stuffing():
    assert not is_keyword_stuffing("Built Python tools", "Shipped Python tooling", ["Python"])


def test_term_density_is_capped():
    assert term_density_violation(
        "Python SQL Airflow Docker work", ["Python", "SQL", "Airflow", "Docker"])
    assert term_density_violation(
        "Built and maintained nightly ingestion jobs in Python for the team", ["Python"]) is None


# ── structural checks ─────────────────────────────────────────────────

def test_reorder_must_be_a_permutation():
    bad = _accept(ReorderBullets(op_id="1", item_id="exp.1", order=["exp.1.b.1"]))
    assert bad.rejected[0].code == "not_a_permutation"      # dropped one
    dup = _accept(ReorderBullets(op_id="1", item_id="exp.1",
                                 order=["exp.1.b.1", "exp.1.b.1"]))
    assert dup.rejected[0].code == "not_a_permutation"      # duplicated one
    ok = _accept(ReorderBullets(op_id="1", item_id="exp.1",
                                order=["exp.1.b.2", "exp.1.b.1"]))
    assert ok.rejected == []


def test_reorder_items_checks_the_section():
    ok = _accept(ReorderItems(op_id="1", section_id="sec.experience",
                              order=["exp.2", "exp.1"]))
    assert ok.rejected == []


def test_unknown_ids_are_rejected_not_crashed():
    for op in (
        RewriteBullet(op_id="1", bullet_id="nope.1.b.1", text="x"),
        ReorderBullets(op_id="2", item_id="nope.1", order=[]),
        ReorderItems(op_id="3", section_id="sec.nope", order=[]),
        PromoteItem(op_id="4", item_id="nope.1", rationale="because"),
    ):
        assert validate([op], _doc()).rejected[0].code == "unknown_id"


def test_summary_must_cite_and_cites_must_resolve():
    assert _accept(SetSummary(op_id="1", text="Engineer.")).rejected[0].code == "missing_citation"
    assert _accept(SetSummary(op_id="1", text="Engineer.",
                              cites=["exp.9.b.9"])).rejected[0].code == "unknown_id"
    assert _accept(SetSummary(op_id="1", text="Engineer.",
                              cites=["exp.1.b.1"])).rejected == []


def test_skills_must_be_evidenced():
    bad = _accept(SetSkills(op_id="1", groups=[
        SkillGroup(label="Core", skills=["Python", "Kubernetes"])]))
    assert bad.rejected[0].code == "unsupported_entity"
    ok = _accept(SetSkills(op_id="1", groups=[
        SkillGroup(label="Core", skills=["Python", "SQL"])]))
    assert ok.rejected == []


def test_empty_corpus_disables_skill_filtering():
    """Cannot ground ⇒ do not filter. Rejecting everything would be worse."""
    doc = _doc()
    doc.skill_inventory = []
    r = validate([SetSkills(op_id="1", groups=[
        SkillGroup(label="Core", skills=["Python"])])], doc)
    assert r.rejected == []


def test_only_one_promotion_per_section():
    r = validate([
        PromoteItem(op_id="1", item_id="exp.2", rationale="most relevant"),
        PromoteItem(op_id="2", item_id="exp.1", rationale="also relevant"),
    ], _doc())
    assert len(r.accepted) == 1
    assert r.rejected[0].code == "too_many_promotions"


def test_promotion_requires_a_rationale():
    r = _accept(PromoteItem(op_id="1", item_id="exp.2", rationale="  "))
    assert r.rejected[0].code == "missing_citation"


def test_cannot_empty_an_item():
    r = _accept(DropBullet(op_id="1", bullet_id="exp.2.b.1", reason="irrelevant"))
    assert r.rejected[0].code == "would_empty_item"


def test_advisory_ops_pass_through_untouched():
    r = _accept(FlagGap(op_id="1", requirement="PyTorch", severity="blocking"))
    assert len(r.accepted) == 1 and r.rejected == []


# ── corpus ────────────────────────────────────────────────────────────

def test_grounding_corpus_is_one_authority():
    corpus = grounding_corpus(_doc(), confirmed=["Kubernetes"])
    assert {"python", "sql", "airflow", "kubernetes"} <= corpus


def test_validate_never_mutates_the_document():
    doc = _doc()
    before = doc.model_dump()
    validate([RewriteBullet(op_id="1", bullet_id="exp.1.b.1", text="Anything at all")], doc)
    assert doc.model_dump() == before


def test_fabrication_count_is_reported_for_evals():
    r = validate([
        RewriteBullet(op_id="1", bullet_id="exp.1.b.1", text="Cut costs by 90%"),
        RewriteBullet(op_id="2", bullet_id="exp.1.b.1", text="Ran it on Kubernetes"),
        ReorderBullets(op_id="3", item_id="exp.1", order=["exp.1.b.2", "exp.1.b.1"]),
    ], _doc())
    assert r.fabrication_count == 2 and len(r.accepted) == 1


def test_dotted_tool_names_match_the_corpus():
    """Regression: `.NET` tokenized to `net` and no longer matched a `.net`
    entry in the grounding corpus, so a legitimate rewrite was rejected."""
    doc = _doc()
    doc.skill_inventory = ["Python", ".NET", "C++"]
    r = validate([RewriteBullet(op_id="1", bullet_id="exp.1.b.1",
                                text="Built reporting services in .NET and C++")], doc)
    assert r.rejected == []


# ── numeric dates (Europass and every dd/mm/yyyy resume) ──────────────

def test_a_europass_date_range_keeps_its_months():
    """`01/03/2026 - 13/08/2026` read as ("2026", "2026") loses the ordering.

    Two roles at the same company in one year then cannot be told apart.
    """
    from app.engine.normalize import parse_date_range
    assert parse_date_range("[ 01/03/2026 - 13/08/2026 ]") == ("2026-03", "2026-08")
    assert parse_date_range("24/06/2024 - 24/09/2024") == ("2024-06", "2024-09")
    assert parse_date_range("01/10/2021 - 17/07/2025") == ("2021-10", "2025-07")


def test_a_day_first_date_is_disambiguated_by_the_impossible_month():
    from app.engine.normalize import parse_date_range
    assert parse_date_range("13/08/2026") == ("2026-08", "")     # 13 is not a month


def test_a_month_first_date_is_disambiguated_too():
    from app.engine.normalize import parse_date_range
    assert parse_date_range("06/24/2024") == ("2024-06", "")     # 24 is not a month


def test_an_ambiguous_numeric_date_is_read_day_first():
    """01/03 could be either. Day-first is the convention of formats that
    write dates numerically at all, so that is the documented choice."""
    from app.engine.normalize import parse_date_range
    assert parse_date_range("01/03/2026") == ("2026-03", "")


def test_a_month_year_date_works():
    from app.engine.normalize import parse_date_range
    assert parse_date_range("03/2026 - 08/2026") == ("2026-03", "2026-08")
    assert parse_date_range("2024-06 - 2024-09") == ("2024-06", "2024-09")


def test_numeric_dates_still_honour_present():
    from app.engine.normalize import parse_date_range
    assert parse_date_range("01/2020 - Present") == ("2020-01", "")


def test_month_names_are_unaffected():
    from app.engine.normalize import parse_date_range
    assert parse_date_range("Jan 2022 - Present") == ("2022-01", "")
    assert parse_date_range("2021") == ("2021", "")


# ── headings from real resumes ────────────────────────────────────────

def test_about_me_is_a_summary_not_a_custom_section():
    """Europass's default heading. It fell to `custom`, so the document-level
    summary was never derived from it."""
    from app.engine.normalize import classify_heading
    assert classify_heading("ABOUT ME") == "summary"
    assert classify_heading("Personal Statement") == "summary"
    assert classify_heading("Profile Summary") == "summary"


def test_a_specific_probe_still_wins_over_about():
    """First hit wins, so "About my projects" must not become a summary."""
    from app.engine.normalize import classify_heading
    assert classify_heading("About my projects") == "projects"


def test_europass_headings_classify():
    from app.engine.normalize import classify_heading
    assert classify_heading("EDUCATION AND TRAINING") == "education"
    assert classify_heading("DIGITAL SKILLS") == "skills"
    assert classify_heading("LANGUAGE SKILLS") == "skills"


def test_an_unknown_heading_still_survives_as_custom():
    """`custom` is the honest answer, not a failure — the content is kept."""
    from app.engine.normalize import classify_heading
    assert classify_heading("HONOURS AND AWARDS") == "custom"


# ══ set_skills may rearrange, never shrink ═══════════════════════════
# From runs/2026-09-25T07-51-32: `set_skills` took a real skills section of 8
# grouped lines down to 3 unlabelled ones and deleted the candidate's entire
# computer-vision group. The validator accepted it — correctly by its own
# rules, since nothing was invented. Deleting true content was not a thing
# this stage checked.

def _skilled_doc() -> ResumeDoc:
    """A resume whose skills section is grouped, the way real ones are."""
    from app.domain.models import Bullet, Item, Section
    doc = _doc()
    section = Section(id="sec.skills", kind="skills", heading="SKILLS", items=[
        Item(id="skl.1", title="Computer Vision", bullets=[
            Bullet(id="skl.1.b.1", text="YOLO, VGG16, ResNet50, object detection")]),
        Item(id="skl.2", title="Backend", bullets=[
            Bullet(id="skl.2.b.1", text="Python FastAPI, Flask, n8n")]),
    ])
    doc.sections.append(section)
    doc.skill_inventory = [
        "YOLO", "VGG16", "ResNet50", "object detection",
        "Python FastAPI", "Flask", "n8n",
    ]
    return doc


def test_set_skills_may_not_drop_a_skill_already_on_the_resume():
    """The exact op that shipped a resume without its own specialisation."""
    doc = _skilled_doc()
    r = validate([SetSkills(op_id="1", groups=[
        SkillGroup(label="Automation", skills=["n8n", "Python FastAPI", "Flask"]),
    ])], doc)

    assert len(r.rejected) == 1
    reject = r.rejected[0]
    assert reject.code == "skills_dropped"
    # The message names what would have gone, so the failure is readable.
    for gone in ("YOLO", "VGG16", "ResNet50", "object detection"):
        assert gone in reject.detail


def test_set_skills_may_regroup_rename_and_reorder_freely():
    """Rearranging is the whole point of the op — only loss is refused.

    The job's skills lead, the candidate's own specialism follows under a
    heading the model invented. Nothing is lost, so nothing is rejected.
    """
    doc = _skilled_doc()
    r = validate([SetSkills(op_id="1", groups=[
        SkillGroup(label="Automation & Backend",
                   skills=["n8n", "Python FastAPI", "Flask"]),
        SkillGroup(label="Machine Learning & Vision",
                   skills=["YOLO", "VGG16", "ResNet50", "object detection"]),
    ])], doc)
    assert r.rejected == []
    assert len(r.accepted) == 1


def test_set_skills_rejects_an_unlabelled_group():
    """The failing op emitted three groups with `label: ""`."""
    doc = _skilled_doc()
    r = validate([SetSkills(op_id="1", groups=[
        SkillGroup(label="", skills=["n8n", "Python FastAPI", "Flask"]),
        SkillGroup(label="Vision",
                   skills=["YOLO", "VGG16", "ResNet50", "object detection"]),
    ])], doc)
    assert r.rejected[0].code == "unlabelled_group"


def test_set_skills_rejects_the_same_skill_in_two_groups():
    doc = _skilled_doc()
    r = validate([SetSkills(op_id="1", groups=[
        SkillGroup(label="Automation", skills=["n8n", "Python FastAPI", "Flask"]),
        SkillGroup(label="Vision", skills=["YOLO", "VGG16", "ResNet50",
                                           "object detection", "n8n"]),
    ])], doc)
    assert r.rejected[0].code == "duplicate_skill"
    assert "n8n" in r.rejected[0].detail


def test_a_resume_with_no_skills_section_can_have_one_composed():
    """Nothing on the page means nothing to lose — the model proposes freely.

    This is a deliberate choice, not an oversight: a resume without a skills
    section still deserves one, and there is no prior list to preserve. The
    corpus is empty in this case too, so the ungrounded-skill check also stands
    down rather than rejecting every entry.
    """
    doc = _doc()
    doc.skill_inventory = []
    assert doc.section_of_kind("skills") is None

    r = validate([SetSkills(op_id="1", groups=[
        SkillGroup(label="Core", skills=["Python", "FastAPI"])])], doc)
    assert r.rejected == []


def test_a_nested_label_does_not_become_a_skill():
    """`"API Python frameworks: Flask"` reached a proposed resume edit.

    Only the first colon was stripped, so a line like

        "Backend & Production Pipelines: Python FastAPI,
         API Python frameworks: Flask, FastAPI - Uvicorn"

    left the second label glued to its value. That string entered the
    grounding corpus and the planner later echoed it back as a skill.
    """
    from app.engine.normalize import split_skill_line

    terms = split_skill_line(
        "Backend & Production Pipelines: Python FastAPI, "
        "API Python frameworks: Flask, Github actions")
    assert "Flask" in terms
    assert not [t for t in terms if ":" in t]


def test_skill_group_headings_are_recovered_from_bullets():
    """A real parse put the group headings in with the skills.

    The page printed four labelled groups; the parse produced one entry whose
    bullets interleaved headings and contents. Left alone, the grounding
    corpus gained `Backend & Production Pipelines` as a claimable skill, and
    the no-loss rule would have demanded a planner keep it — something no
    correct plan can do.
    """
    from app.domain.models import RawEntry, RawResume, RawSection
    from app.engine.normalize import existing_skills, normalize

    doc = normalize(RawResume(sections=[RawSection(
        heading="SKILLS", entries=[RawEntry(
            title="Computer Vision & Image Processing",
            bullets=[
                "YOLO | computer vision | Object detection",
                "AI/ML Frameworks & Techniques",
                "Python & libraries (Numpy, Pytorch) | Ensemble Methods",
                "Backend & Production Pipelines",
                "Python FastAPI | API Python frameworks: Flask | Github actions",
            ])])]))

    section = doc.section_of_kind("skills")
    assert [i.title for i in section.items] == [
        "Computer Vision & Image Processing",
        "AI/ML Frameworks & Techniques",
        "Backend & Production Pipelines",
    ]
    skills = existing_skills(doc)
    assert "backend & production pipelines" not in skills
    assert "ai/ml frameworks & techniques" not in skills
    assert {"yolo", "flask", "pytorch", "n8n"} & set(skills) == {"yolo", "flask", "pytorch"}


def test_a_lone_skill_is_not_promoted_into_an_empty_group():
    """`Python` on its own last line is a skill, not a heading."""
    from app.domain.models import RawEntry, RawResume, RawSection
    from app.engine.normalize import existing_skills, normalize

    doc = normalize(RawResume(sections=[RawSection(
        heading="SKILLS", entries=[RawEntry(
            title="Tools", bullets=["Docker | Git", "Python"])])]))
    assert "python" in existing_skills(doc)


def test_a_slash_inside_a_word_is_not_a_list_separator():
    from app.engine.normalize import _looks_like_a_group_heading

    assert _looks_like_a_group_heading("AI/ML Frameworks & Techniques")
    assert _looks_like_a_group_heading("CI/CD Tooling")
    assert not _looks_like_a_group_heading("Python / Java / Go")
    assert not _looks_like_a_group_heading("Docker, Git, Make")


def test_skill_key_ignores_case_and_edge_punctuation_only():
    """Shallow on purpose — a clever key would hide a real deletion."""
    from app.engine.normalize import skill_key

    assert skill_key("PyTorch") == skill_key("pytorch") == "pytorch"
    assert skill_key("  Object detection.  ") == "object detection"
    assert skill_key("FastAPI") != skill_key("Flask")
    # No stemming, no synonyms: these stay distinct.
    assert skill_key("API integrations") != skill_key("API integration")


# ══ entity extraction is position-aware ═══════════════════════════════

def test_a_sentence_opener_is_grammar_not_a_named_capability():
    """★ Measured false positive.

    An ordinary covering letter — "Happy to talk this week", "If that is a hard
    requirement" — had `Happy` and `If` reported as "named thing(s) the resume
    does not support". The old rule skipped only the FIRST word of the whole
    text, so in anything longer than one sentence every sentence opener read as
    a capability claim. A warning on every letter is how a person learns to
    ignore the warnings, and the warnings are the product.
    """
    from app.engine.validator import entities

    body = ("I write Python day to day. If that is a hard requirement, I would "
            "rather know now. Happy to talk this week.")
    found = entities(body)
    assert "python" in found
    assert "happy" not in found
    assert "if" not in found
    assert "would" not in found


def test_a_real_name_mid_sentence_is_still_caught():
    """The fix must not turn the check off."""
    from app.engine.validator import entities

    found = entities("I have not worked with Kubernetes, so I have not "
                     "claimed it.")
    assert "kubernetes" in found


def test_an_acronym_or_tool_shaped_token_counts_anywhere():
    """Position-awareness applies to ordinary capitals only. `SQL` opening a
    sentence is still `SQL`."""
    from app.engine.validator import entities

    assert "sql" in entities("Built reports. SQL was the main tool.")
    assert "node.js" in entities("Services run on Node.js in production.")


def test_every_line_of_a_bullet_list_is_a_sentence_start():
    """A bullet list has no full stops and every line begins with a capital."""
    from app.engine.validator import entities

    found = entities("Skills\n- Python and SQL\n- Please see the appendix")
    assert "python" in found
    assert "please" not in found
