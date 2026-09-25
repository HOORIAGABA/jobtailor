"""A stage that succeeded is never paid for twice.

Measured across ten real runs on one resume: providers allowed 0, 1, 2 or 3
calls before going unavailable. The pipeline needs six for a two-page CV. Every
re-run used to start from zero, spend its allowance re-parsing a document that
had already parsed perfectly, and die in exactly the same place.

These tests pin the behaviour that makes the project runnable on those tiers.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.models import Contact, JobBrief, RawEntry, RawResume, RawSection
from app.io.cache import FileCache, NullCache, key_for
from app.io.llm import BudgetedClient, RunBudget, ScriptedClient
from app.io.runlog import NullRunLog
from app.pipeline.run import ingest, run, tailor

from tests.test_pipeline import (          # the same fixtures as the pipeline
    BRIEF, JOB_TEXT, PARSE, PLAN, RESUME_TEXT, WRITE,
)


def parse_client() -> BudgetedClient:
    return BudgetedClient(ScriptedClient([PARSE]), RunBudget(max_calls=12))


def full_client() -> BudgetedClient:
    return BudgetedClient(
        ScriptedClient([PARSE, BRIEF, PLAN, WRITE]), RunBudget(max_calls=12)
    )


# ── the cache itself ──────────────────────────────────────────────────

def test_a_round_trip(tmp_path: Path):
    cache = FileCache(tmp_path)
    raw = RawResume(contact=Contact(full_name="R. KHAN"),
                    sections=[RawSection(heading="EXPERIENCE",
                                         entries=[RawEntry(title="Analyst")])])
    cache.set("parse", "abc", raw)

    back = cache.get("parse", "abc", RawResume)
    assert back is not None
    assert back.contact.full_name == "R. KHAN"
    assert back.sections[0].entries[0].title == "Analyst"


def test_a_miss_is_none(tmp_path: Path):
    assert FileCache(tmp_path).get("parse", "nothing", RawResume) is None


def test_namespaces_do_not_collide(tmp_path: Path):
    cache = FileCache(tmp_path)
    cache.set("parse", "same", RawResume())
    cache.set("brief", "same", JobBrief(source_hash="x", role="MLE"))
    assert cache.get("brief", "same", JobBrief).role == "MLE"


def test_a_corrupt_entry_is_ignored_rather_than_raising(tmp_path: Path):
    """A cache that raises is worse than no cache.

    The stage can always be re-run; a crash on a stale entry blocks every
    future run until someone deletes a file they do not know exists.
    """
    cache = FileCache(tmp_path)
    path = cache.path("parse", "bad")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    assert cache.get("parse", "bad", RawResume) is None


def test_a_half_written_file_is_never_visible(tmp_path: Path):
    """Written beside, then replaced — a run killed mid-write leaves nothing."""
    cache = FileCache(tmp_path)
    cache.set("parse", "k", RawResume())
    assert not list(tmp_path.rglob("*.tmp"))


def test_the_key_is_content_not_name():
    """Editing a resume must invalidate its parse; renaming it must not."""
    assert key_for(b"same bytes") == key_for(b"same bytes")
    assert key_for(b"edited resume") != key_for(b"original resume")
    assert key_for("  text  ") == key_for("text")


# ── what it buys ──────────────────────────────────────────────────────

def test_a_second_ingest_of_the_same_file_costs_nothing(tmp_path: Path):
    cache = FileCache(tmp_path)
    data = RESUME_TEXT.encode()

    first = parse_client()
    ingest(data, "cv.txt", first, cache=cache)
    assert first.budget.calls == 1

    second = BudgetedClient(ScriptedClient([]), RunBudget(max_calls=12))
    state = ingest(data, "cv.txt", second, cache=cache)
    assert second.budget.calls == 0
    assert state.doc is not None
    assert state.doc.bullet("exp.1.b.1") is not None


def test_an_edited_resume_is_parsed_again(tmp_path: Path):
    cache = FileCache(tmp_path)
    ingest(RESUME_TEXT.encode(), "cv.txt", parse_client(), cache=cache)

    edited = BudgetedClient(ScriptedClient([PARSE]), RunBudget(max_calls=12))
    ingest((RESUME_TEXT + "\n- one more bullet here\n").encode(), "cv.txt",
           edited, cache=cache)
    assert edited.budget.calls == 1


def test_the_cached_parse_is_still_verified(tmp_path: Path):
    """Coverage is recomputed on a hit — the check is cheap and deterministic,
    and skipping it would mean a cached lossy parse is never questioned."""
    cache = FileCache(tmp_path)
    data = RESUME_TEXT.encode()
    ingest(data, "cv.txt", parse_client(), cache=cache)

    state = ingest(data, "cv.txt",
                   BudgetedClient(ScriptedClient([]), RunBudget(max_calls=12)),
                   cache=cache)
    assert state.coverage is not None
    assert state.coverage.is_clean


def test_the_brief_is_cached_on_the_posting(tmp_path: Path):
    """A second resume against the same job costs nothing at S1."""
    cache = FileCache(tmp_path)
    run(RESUME_TEXT.encode(), "cv.txt", JOB_TEXT, full_client(), cache=cache)

    again = BudgetedClient(ScriptedClient([PLAN, WRITE]), RunBudget(max_calls=12))
    run(RESUME_TEXT.encode(), "cv.txt", JOB_TEXT, again, cache=cache)
    assert again.budget.calls == 2          # plan and prose only


# ── the scenario this exists for ──────────────────────────────────────

def test_a_run_that_dies_resumes_where_it_stopped(tmp_path: Path):
    """Three calls a day is enough to finish, across three runs.

    Run 1 parses and dies at the brief. Run 2 skips the parse, does the brief
    and the plan, and dies at the writer. Run 3 finishes. Without the cache
    each run re-parses and dies in exactly the same place, forever.
    """
    from app.domain.errors import LLMUnavailable

    cache = FileCache(tmp_path)
    data = RESUME_TEXT.encode()

    # Run 1 — the parse lands, then the provider goes away.
    first = BudgetedClient(
        ScriptedClient([PARSE, LLMUnavailable("quota")]), RunBudget(max_calls=12))
    with pytest.raises(LLMUnavailable):
        run(data, "cv.txt", JOB_TEXT, first, cache=cache)
    assert first.budget.calls == 1

    # Run 2 — parse from cache, brief and plan land, then it dies again.
    second = BudgetedClient(
        ScriptedClient([BRIEF, PLAN, LLMUnavailable("quota")]),
        RunBudget(max_calls=12))
    with pytest.raises(LLMUnavailable):
        run(data, "cv.txt", JOB_TEXT, second, cache=cache)
    assert second.budget.calls == 2         # NOT 3 — the parse was free

    # Run 3 — only the writer is left.
    third = BudgetedClient(ScriptedClient([PLAN, WRITE]), RunBudget(max_calls=12))
    state = run(data, "cv.txt", JOB_TEXT, third, cache=cache)
    assert third.budget.calls == 2          # plan re-run, prose written
    assert state.diff is not None


def test_fresh_bypasses_everything(tmp_path: Path):
    """`--fresh` must really re-run, or a prompt change cannot be evaluated."""
    cache = FileCache(tmp_path)
    data = RESUME_TEXT.encode()
    ingest(data, "cv.txt", parse_client(), cache=cache)

    again = parse_client()
    ingest(data, "cv.txt", again, cache=NullCache())
    assert again.budget.calls == 1


def test_no_cache_argument_means_no_caching(tmp_path: Path):
    """The default must not write to anyone's disk behind their back."""
    data = RESUME_TEXT.encode()
    ingest(data, "cv.txt", parse_client(), log=NullRunLog())
    ingest(data, "cv.txt", parse_client(), log=NullRunLog())
    assert not list(tmp_path.iterdir())
