"""S2b — the semantic pass, and the rails that keep it from undoing S2.

Measured on a real run: 16 links, all strong, zero hints across 8 requirements
and ~50 bullets. Everything that matched, matched by name.
"""
from __future__ import annotations

import pytest

from app.agents.evidence_hints import (
    MAX_ELEMENTS,
    SYSTEM,
    build_payload,
    find_hints,
    merge,
    unmatched_elements,
)
from app.domain.models import (
    JobBrief, RawEntry, RawResume, RawSection, Term,
)
from app.engine.evidence import compute_evidence
from app.engine.normalize import normalize
from app.io.llm import ScriptedClient


def fixture():
    brief = JobBrief(
        role="MLE", company="N", excerpt="x", source_hash="x",
        terms=[
            Term(term="Airflow", weight=9, kind="tool", required=True),
            Term(term="Kubernetes", weight=6, kind="tool"),
            Term(term="Python", weight=9, kind="skill", required=True),
        ],
    )
    doc = normalize(RawResume(sections=[
        RawSection(heading="EXPERIENCE", entries=[RawEntry(
            title="Data Engineer", bullets=[
                "Built nightly jobs with dependency handling and automatic retries",
                "Wrote the ingestion service in Python",
                "Ran the weekly stakeholder reporting meeting",
            ])]),
        RawSection(heading="SKILLS", entries=[RawEntry(bullets=["Tools: Docker"])]),
    ]))
    return brief, doc, compute_evidence(brief, doc)


def hint(element: str, bullet: str, why: str = "because") -> dict:
    return {"element_id": element, "bullet_id": bullet, "why": why}


# ── the hole it exists for ────────────────────────────────────────────

def test_a_bullet_that_proves_without_naming_is_found():
    """The measured failure: orchestration work that never says "Airflow"."""
    brief, doc, index = fixture()
    assert "term.airflow" in index.unmatched_elements      # lexical missed it

    links = find_hints(brief, doc, index, ScriptedClient(
        [{"hints": [hint("term.airflow", "exp.1.b.1")]}]))
    assert [(l.job_element_id, l.bullet_id) for l in links] == \
        [("term.airflow", "exp.1.b.1")]


def test_merging_clears_the_gap_it_covers():
    brief, doc, index = fixture()
    links = find_hints(brief, doc, index, ScriptedClient(
        [{"hints": [hint("term.airflow", "exp.1.b.1")]}]))
    merged = merge(index, links)

    assert "term.airflow" not in merged.unmatched_elements
    assert "term.kubernetes" in merged.unmatched_elements   # still honest
    assert len(merged.links) == len(index.links) + 1


def test_the_lexical_index_is_not_mutated():
    """The deterministic result stays intact beside the augmented one."""
    brief, doc, index = fixture()
    before = list(index.unmatched_elements)
    merge(index, find_hints(brief, doc, index, ScriptedClient(
        [{"hints": [hint("term.airflow", "exp.1.b.1")]}])))
    assert index.unmatched_elements == before


# ── the three rails ───────────────────────────────────────────────────

def test_a_hint_is_never_strong():
    """A strong link is mechanically checkable. This one is an assertion, so
    it can draw attention to a bullet but cannot make a claim legal."""
    brief, doc, index = fixture()
    links = find_hints(brief, doc, index, ScriptedClient(
        [{"hints": [hint("term.airflow", "exp.1.b.1")]}]))
    assert all(l.strength == "hint" for l in links)
    assert all(l.lexical_hit is False for l in links)


def test_an_invented_bullet_id_is_dropped():
    brief, doc, index = fixture()
    dropped: list[str] = []
    links = find_hints(brief, doc, index, ScriptedClient(
        [{"hints": [hint("term.airflow", "exp.9.b.9")]}]), dropped=dropped)
    assert links == []
    assert any("no such bullet" in d for d in dropped)


def test_an_element_that_was_not_unmatched_is_dropped():
    """It cannot re-decide something exact matching already settled."""
    brief, doc, index = fixture()
    dropped: list[str] = []
    links = find_hints(brief, doc, index, ScriptedClient(
        [{"hints": [hint("term.python", "exp.1.b.3")]}]), dropped=dropped)
    assert links == []
    assert any("not unmatched" in d for d in dropped)


def test_duplicates_are_dropped():
    brief, doc, index = fixture()
    dropped: list[str] = []
    links = find_hints(brief, doc, index, ScriptedClient(
        [{"hints": [hint("term.airflow", "exp.1.b.1"),
                    hint("term.airflow", "exp.1.b.1")]}]), dropped=dropped)
    assert len(links) == 1
    assert any("duplicate" in d for d in dropped)


# ── it costs nothing when there is nothing to do ──────────────────────

def test_nothing_unmatched_means_no_call():
    brief, doc, index = fixture()
    covered = merge(index, find_hints(brief, doc, index, ScriptedClient(
        [{"hints": [hint("term.airflow", "exp.1.b.1"),
                    hint("term.kubernetes", "exp.1.b.1")]}])))

    spent = ScriptedClient([])                    # any call raises
    assert find_hints(brief, doc, covered, spent) == []
    assert spent.calls == []


def test_an_empty_answer_is_accepted():
    """"Nothing in this resume touches these" is a correct answer."""
    brief, doc, index = fixture()
    assert find_hints(brief, doc, index, ScriptedClient([{"hints": []}])) == []


def test_merge_with_no_hints_returns_the_same_index():
    _, _, index = fixture()
    assert merge(index, []) is index


# ── what the model is shown ───────────────────────────────────────────

def test_only_unmatched_elements_are_sent():
    brief, doc, index = fixture()
    payload = build_payload(unmatched_elements(brief, index), doc)
    ids = {e["id"] for e in payload["unmatched"]}
    assert "term.python" not in ids            # already matched by name
    assert "term.airflow" in ids


def test_skills_lines_are_not_offered_as_evidence():
    """A declaration is not a demonstration — the same rule as S2."""
    brief, doc, index = fixture()
    payload = build_payload(unmatched_elements(brief, index), doc)
    assert not any("Docker" in text for text in payload["bullets"].values())


def test_the_payload_is_capped():
    brief, doc, index = fixture()
    payload = build_payload(unmatched_elements(brief, index), doc)
    assert len(payload["unmatched"]) <= MAX_ELEMENTS


def test_the_prompt_refuses_plausibility():
    assert "Adjacency" in SYSTEM
    assert "probably used" in SYSTEM
    assert "Prefer being right" in SYSTEM and "over being helpful" in SYSTEM
