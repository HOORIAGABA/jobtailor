"""Evidence matching — deterministic, no model, no network."""
import pytest

from app.domain.models import (
    Bullet, Grounded, Item, JobBrief, Problem, ResumeDoc, Section, Term,
)
from app.engine.evidence import (
    AliasIndex, MIN_SHARED_WORDS, compute_evidence, elements_by_bullet,
    job_elements, strong_bullets, undemonstrated_terms,
)
from app.engine.text import content_words, lower_tokens, tokens


# ══ tokenizer ═════════════════════════════════════════════════════════

def test_dotted_and_plussed_tokens_survive():
    assert tokens("built in .NET and C++") == ["built", "in", ".NET", "and", "C++"]


def test_trailing_punctuation_is_stripped():
    assert lower_tokens("shipped it.") == ["shipped", "it"]


def test_content_words_drop_stopwords_and_short_tokens():
    assert content_words("Worked on the data pipelines for a team") == {
        "worked", "data", "pipelines", "team"}


# ══ alias matching ════════════════════════════════════════════════════

def _terms() -> list[Term]:
    return [
        Term(term="Kubernetes", aliases=["k8s"], weight=4),
        Term(term="Python", weight=9, required=True),
        Term(term="Airflow", aliases=["Apache Airflow"], weight=8, required=True),
        Term(term="ML", aliases=["machine learning"], weight=7),
    ]


def test_alias_matches_the_canonical_term():
    index = AliasIndex.build(_terms())
    assert index.find("ran it on k8s") == {"Kubernetes"}
    assert index.find("deployed to Kubernetes") == {"Kubernetes"}


def test_multi_word_alias_matches_as_a_phrase():
    index = AliasIndex.build(_terms())
    assert "Airflow" in index.find("scheduled with Apache Airflow")
    assert "ML" in index.find("a machine learning pipeline")


def test_no_substring_matching():
    """The v1 bug: `ml` matched `html`, `r` matched every word with an r."""
    index = AliasIndex.build(_terms())
    assert index.find("wrote some html and css") == set()
    assert index.find("pythonic style") == set()      # not "Python"


def test_matching_is_case_insensitive_but_boundary_aware():
    index = AliasIndex.build(_terms())
    assert index.find("PYTHON scripts") == {"Python"}
    assert index.find("monty-pythonesque") == set()


def test_empty_terms_match_nothing():
    assert AliasIndex.build([]).find("Python Airflow") == set()


# ══ the Barista regression ════════════════════════════════════════════

def _ml_brief() -> JobBrief:
    return JobBrief(
        role="Machine Learning Engineer", company="Nimbus",
        problems_to_solve=[Problem(
            statement="own model retraining pipelines in Python",
            source_span=(0, 1), priority="core")],
        terms=_terms(),
    )


def _mixed_resume() -> ResumeDoc:
    """The exact shape that broke the old scorer: a short, genuinely relevant
    entry against a long, wordy, irrelevant one."""
    return ResumeDoc(
        sections=[Section(id="sec.experience", kind="experience", heading="EXPERIENCE", items=[
            Item(id="exp.1", title="Machine Learning Engineer", org="Acme", bullets=[
                Bullet(id="exp.1.b.1", text="Trained models and shipped Airflow pipelines in Python"),
            ]),
            Item(id="exp.2", title="Barista", org="Cafe", bullets=[
                Bullet(id="exp.2.b.1", text=(
                    "Work with the team to build and maintain the daily service and "
                    "drive the end to end product of large scale coffee for models "
                    "of customers that serve design and data")),
            ]),
        ])],
    )


def test_the_relevant_entry_wins_not_the_wordy_one():
    """v1 scored Barista 60 vs ML Engineer 47 on an ML role, because longer
    text collected more accidental substring hits."""
    index = compute_evidence(_ml_brief(), _mixed_resume())
    strong = strong_bullets(index)
    assert "exp.1.b.1" in strong
    assert "exp.2.b.1" not in strong


def test_length_alone_does_not_create_strong_evidence():
    index = compute_evidence(_ml_brief(), _mixed_resume())
    barista = [l for l in index.links if l.bullet_id == "exp.2.b.1"]
    assert all(l.strength == "hint" for l in barista)


# ══ links ═════════════════════════════════════════════════════════════

def _doc() -> ResumeDoc:
    return ResumeDoc(
        sections=[Section(id="sec.experience", kind="experience", heading="EXPERIENCE", items=[
            Item(id="exp.1", title="Data Analyst", org="Acme", bullets=[
                Bullet(id="exp.1.b.1", text="Built Airflow pipelines in Python"),
                Bullet(id="exp.1.b.2", text="Made a dashboard for the reporting team"),
            ]),
        ])],
        skill_inventory=["Python", "Airflow", "Kubernetes"],
    )


def test_a_shared_term_is_strong_evidence():
    index = compute_evidence(_ml_brief(), _doc())
    link = next(l for l in index.links
                if l.job_element_id == "problem.0" and l.bullet_id == "exp.1.b.1")
    assert link.strength == "strong" and link.lexical_hit


def test_shared_wording_without_a_term_is_only_a_hint():
    brief = JobBrief(problems_to_solve=[Problem(
        statement="improve reporting dashboard quality for the team",
        source_span=(0, 1))])
    index = compute_evidence(brief, _doc())
    links = index.for_bullet("exp.1.b.2")
    assert links and all(l.strength == "hint" and not l.lexical_hit for l in links)


def test_one_shared_word_is_not_enough():
    brief = JobBrief(problems_to_solve=[Problem(
        statement="manage vendor procurement contracts", source_span=(0, 1))])
    index = compute_evidence(brief, _doc())
    assert index.links == []
    assert "problem.0" in index.unmatched_elements


def test_links_are_ordered_strongest_first():
    index = compute_evidence(_ml_brief(), _doc())
    for element in {l.job_element_id for l in index.links}:
        group = index.for_element(element)
        strengths = [l.strength for l in group]
        assert strengths == sorted(strengths, key=lambda s: s != "strong")


# ══ gaps ══════════════════════════════════════════════════════════════

def test_a_term_with_no_evidence_is_a_gap():
    brief = JobBrief(terms=[Term(term="PyTorch", weight=10, required=True)])
    index = compute_evidence(brief, _doc())
    assert undemonstrated_terms(brief, index) == ["PyTorch"]


def test_a_declared_but_undemonstrated_skill_is_not_a_gap():
    """Kubernetes is in the skills section but no bullet shows it. That is a
    weak claim, not a missing one — the candidate did say they have it."""
    brief = JobBrief(terms=[Term(term="Kubernetes", aliases=["k8s"], weight=4)])
    index = compute_evidence(brief, _doc())
    assert undemonstrated_terms(brief, index) == []
    assert strong_bullets(index) == set()      # still no bullet evidences it


def test_a_demonstrated_term_is_never_a_gap():
    brief = JobBrief(terms=[Term(term="Airflow", weight=8)])
    index = compute_evidence(brief, _doc())
    assert undemonstrated_terms(brief, index) == []
    assert "exp.1.b.1" in strong_bullets(index)


# ══ shape and invariants ══════════════════════════════════════════════

def test_no_scalar_score_is_produced():
    """D-11: a maximizable number must not exist for anything to optimise."""
    index = compute_evidence(_ml_brief(), _doc())
    assert set(index.model_dump()) == {"links", "unmatched_elements"}


def test_similarity_is_per_link_and_never_aggregated():
    index = compute_evidence(_ml_brief(), _doc())
    assert all(0.0 <= l.similarity <= 1.0 for l in index.links)
    assert not hasattr(index, "score")


def test_job_elements_get_stable_ids():
    brief = JobBrief(
        problems_to_solve=[Problem(statement="a", source_span=(0, 1))],
        success_signals=[Grounded(statement="b", source_span=(0, 1))],
        hard_requirements=[Grounded(statement="c", source_span=(0, 1))],
        terms=[Term(term="Python")],
    )
    assert [e.id for e in job_elements(brief)] == [
        "problem.0", "signal.0", "req.0", "term.python"]


def test_is_deterministic():
    brief, doc = _ml_brief(), _doc()
    assert compute_evidence(brief, doc).model_dump() == compute_evidence(brief, doc).model_dump()


def test_empty_inputs_are_safe():
    assert compute_evidence(JobBrief(), ResumeDoc()).links == []
    assert compute_evidence(_ml_brief(), ResumeDoc()).unmatched_elements


def test_planner_view_groups_by_bullet():
    index = compute_evidence(_ml_brief(), _doc())
    by_bullet = elements_by_bullet(index)
    assert "problem.0" in by_bullet["exp.1.b.1"]


# ══ the old algorithm, pinned ═════════════════════════════════════════

def _v1_score(text: str, terms: set[str], title: str = "", company: str = "") -> int:
    """The previous version's `_relevance_score`, reproduced verbatim.

    Kept in the test suite because the README makes a claim about it, and a
    claim an interviewer can ask you to reproduce should be reproducible.
    """
    import re
    tok = lambda t: set(re.findall(r"[a-z0-9][a-z0-9+\-.]*", (t or "").lower()))
    tk, score = tok(text), 0
    for term in terms:
        t = term.lower().strip()
        if not t:
            continue
        if t in tk:
            score += 3
        elif any(t in x or x in t for x in tk):     # ← substring, both directions
            score += 1
    tt, ct = tok(title), tok(company)
    for term in terms:
        t = term.lower().strip()
        if not t:
            continue
        if t in tt:
            score += 10
        elif any(t in x or x in t for x in tt):
            score += 4
        if t in ct:
            score += 2
    return score


_V1_TERMS = {  # what _job_terms() produced: sentence fields tokenized wholesale
    "machine", "learning", "engineer", "python", "pytorch", "own", "the", "end",
    "to", "and", "training", "serving", "of", "models", "that", "drive",
    "product", "recommendations", "work", "with", "data", "team", "build",
    "maintain", "feature", "pipelines", "system", "design", "for", "large",
    "scale", "model",
}

_ML_BULLET = "Trained PyTorch models"
_BARISTA_BULLET = (
    "Work with the team to build and maintain the daily service and drive the end "
    "to end product of large scale coffee for models of customers that serve "
    "design and data"
)


def test_the_old_scorer_really_did_rank_barista_above_ml_engineer():
    """Pins the number quoted in the README: 60 vs 38 on an ML role.

    The cause is visible in `_V1_TERMS`: tokenizing whole sentences put "and",
    "the", "with", "of", "for", "end" into the term set, so a long bullet full
    of ordinary English outscored a short, genuinely relevant one.
    """
    barista = _v1_score(_BARISTA_BULLET, _V1_TERMS, "Barista", "Cafe")
    ml = _v1_score(_ML_BULLET, _V1_TERMS, "Machine Learning Engineer", "Acme")
    assert (barista, ml) == (60, 38)
    assert barista > ml          # the bug, in one line


def test_the_new_matcher_inverts_that_result():
    brief = JobBrief(
        role="Machine Learning Engineer",
        problems_to_solve=[Problem(
            statement="Own the end to end training and serving of models that drive "
                      "product recommendations", source_span=(0, 1))],
        terms=[Term(term="PyTorch", aliases=["torch"], weight=10, required=True),
               Term(term="Python", weight=9, required=True)],
    )
    doc = ResumeDoc(sections=[Section(id="s", kind="experience", heading="EXPERIENCE", items=[
        Item(id="exp.1", title="Machine Learning Engineer", org="Acme",
             bullets=[Bullet(id="exp.1.b.1", text=_ML_BULLET)]),
        Item(id="exp.2", title="Barista", org="Cafe",
             bullets=[Bullet(id="exp.2.b.1", text=_BARISTA_BULLET)]),
    ])])
    index = compute_evidence(brief, doc)

    assert strong_bullets(index) == {"exp.1.b.1"}
    assert all(l.strength == "hint" for l in index.for_bullet("exp.2.b.1"))
