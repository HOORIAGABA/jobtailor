"""Run artifacts: written, ordered, and never read back."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.domain.models import Contact, RawEntry, RawResume, RawSection
from app.engine.parse_check import check_coverage
from app.io.runlog import NullRunLog, RunLog, slug


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ── naming ────────────────────────────────────────────────────────────

def test_a_run_folder_sorts_chronologically(tmp_path: Path):
    stamp = datetime(2026, 9, 24, 14, 32, 10, tzinfo=timezone.utc)
    log = RunLog.create("HooriaAttas", root=tmp_path, stamp=stamp)
    assert log.directory.name == "2026-09-24T14-32-10_HooriaAttas"


def test_a_hostile_filename_cannot_escape_the_runs_folder(tmp_path: Path):
    log = RunLog.create("../../etc/passwd", root=tmp_path)
    assert log.directory.parent == tmp_path
    assert ".." not in log.directory.name


def test_slug_keeps_something_readable():
    assert slug("Hooria_Attas (1).pdf").startswith("Hooria_Attas")
    assert slug("") == "run"
    assert slug("///") == "run"


def test_artifacts_are_numbered_in_the_order_written(tmp_path: Path):
    """So the folder reads in pipeline order rather than alphabetically."""
    log = RunLog.create("x", root=tmp_path)
    log.text("extract", "some text")
    log.json("parse", {"a": 1})
    log.json("coverage", {"b": 2})

    names = sorted(p.name for p in log.directory.iterdir())
    assert names == ["00_extract.txt", "01_parse.json", "02_coverage.json"]


# ── writing ───────────────────────────────────────────────────────────

def test_a_pydantic_model_serialises(tmp_path: Path):
    log = RunLog.create("x", root=tmp_path)
    raw = RawResume(
        contact=Contact(full_name="A. Morgan"),
        sections=[RawSection(heading="EXPERIENCE",
                             entries=[RawEntry(title="Data Engineer")])],
    )
    written = read(log.json("parse", raw))
    assert written["contact"]["full_name"] == "A. Morgan"
    assert written["sections"][0]["heading"] == "EXPERIENCE"


def test_a_dataclass_serialises(tmp_path: Path):
    """`ParseCoverage` is a frozen dataclass, not a pydantic model."""
    log = RunLog.create("x", root=tmp_path)
    coverage = check_coverage("Wrote SQL queries for the marketing team\n",
                              RawResume())
    written = read(log.json("coverage", coverage))
    assert written["dropped"]


def test_a_set_serialises(tmp_path: Path):
    """The grounding corpus is a set, and json refuses those by default."""
    log = RunLog.create("x", root=tmp_path)
    assert read(log.json("corpus", {"terms": {"Python", "SQL"}}))["terms"] \
        == ["Python", "SQL"]


# ── the manifest ──────────────────────────────────────────────────────

def test_the_manifest_lists_every_artifact(tmp_path: Path):
    log = RunLog.create("x", root=tmp_path)
    log.text("extract", "hello")
    log.json("parse", {"a": 1})
    manifest = read(log.finish())

    assert [a["stage"] for a in manifest["artifacts"]] == ["extract", "parse"]
    assert manifest["ok"] is True


def test_the_input_is_hashed_not_copied(tmp_path: Path):
    """A resume is personal data. One copy of it is enough."""
    log = RunLog.create("x", root=tmp_path)
    log.input_file(Path("resume.pdf"), b"%PDF-1.7 pretend")
    manifest = read(log.finish())

    assert manifest["input_sha256"]
    assert manifest["input_name"] == "resume.pdf"
    assert not (log.directory / "resume.pdf").exists()


def test_two_runs_over_the_same_file_are_identifiable(tmp_path: Path):
    data = b"%PDF-1.7 pretend"
    hashes = []
    for _ in range(2):
        log = RunLog.create("x", root=tmp_path)
        log.input_file(Path("resume.pdf"), data)
        hashes.append(read(log.finish())["input_sha256"])
    assert hashes[0] == hashes[1]


def test_notes_reach_the_manifest(tmp_path: Path):
    log = RunLog.create("x", root=tmp_path)
    log.note(provider="google", model="gemini-x", tokens=5711)
    manifest = read(log.finish())
    assert manifest["model"] == "gemini-x"
    assert manifest["tokens"] == 5711


def test_a_failed_run_still_writes_its_manifest(tmp_path: Path):
    """The artifacts of a run that crashed are the ones worth reading."""
    log = RunLog.create("x", root=tmp_path)
    log.text("extract", "the text that broke the next stage")
    manifest = read(log.finish("SchemaValidationFailed: ..."))

    assert manifest["ok"] is False
    assert "SchemaValidationFailed" in manifest["error"]
    assert (log.directory / "00_extract.txt").exists()


# ── the null log ──────────────────────────────────────────────────────

def test_the_null_log_writes_nothing(tmp_path: Path):
    """A null object, so no stage needs an `if log is not None` around it.

    The one place that guard gets forgotten is the stage you needed to see.
    """
    log = NullRunLog()
    log.input_file(Path("resume.pdf"), b"data")
    log.text("extract", "text")
    log.json("parse", {"a": 1})
    log.note(model="x")
    log.finish()
    assert list(tmp_path.iterdir()) == []


def test_the_null_log_satisfies_the_same_interface():
    """Anything `RunLog` offers, `NullRunLog` must accept."""
    public = {n for n in dir(RunLog) if not n.startswith("_")}
    assert public <= {n for n in dir(NullRunLog) if not n.startswith("_")}


# ── the rule ──────────────────────────────────────────────────────────

def test_there_is_no_way_to_read_a_run_back():
    """Write-only on purpose.

    A run folder that can be read is a cache, and a cache is a second source of
    truth that drifts from the first. No `load`, no `read`, no `open`.
    """
    assert not [n for n in dir(RunLog)
                if n.startswith(("load", "read", "open", "restore"))]


# ── the manifest must account for every call ──────────────────────────

def test_the_merged_call_log_accounts_for_every_call():
    """A manifest said `calls: 5` beside three log entries.

    Two `BudgetedClient`s share one `RunBudget`, so the counter saw all five
    calls while `client.log` held only the fast model's three. The two that
    were missing were the two that failed — the exact calls a run log exists
    to explain. The invariant is simple enough to assert: the merged log has
    one entry per call the budget counted.
    """
    from app.io.llm import (
        BudgetedClient, RunBudget, ScriptedClient, merged_call_log,
    )

    budget = RunBudget(max_calls=10)
    fast = BudgetedClient(ScriptedClient([{"value": "a"}] * 3), budget)
    smart = BudgetedClient(ScriptedClient([{"value": "b"}] * 2), budget)

    fast.complete(system="s", user="u", stage="parse")
    fast.complete(system="s", user="u", stage="parse")
    smart.complete(system="s", user="u", stage="job_brief")
    fast.complete(system="s", user="u", stage="parse")
    smart.complete(system="s", user="u", stage="planner")

    entries = merged_call_log(fast, smart)
    assert len(entries) == budget.calls
    # Interleaved by when they actually happened, not grouped by client.
    assert [e["stage"] for e in entries] == [
        "parse", "parse", "job_brief", "parse", "planner"
    ]


def test_merging_a_client_with_itself_does_not_double_count():
    """`smart = client` when no separate smart model is configured."""
    from app.io.llm import (
        BudgetedClient, RunBudget, ScriptedClient, merged_call_log,
    )

    budget = RunBudget(max_calls=10)
    client = BudgetedClient(ScriptedClient([{"value": "a"}] * 2), budget)
    client.complete(system="s", user="u", stage="parse")
    client.complete(system="s", user="u", stage="job_brief")

    assert len(merged_call_log(client, client)) == budget.calls == 2


def test_the_merged_order_does_not_depend_on_the_clock(monkeypatch):
    """★ The failure this guards against only appeared on Windows.

    `time.monotonic()` advances in ~15.6 ms steps there, so five calls that
    finish in microseconds all recorded the same `started`, every sort key was
    equal, and a stable sort left the entries grouped by client — the run log
    claimed an order the calls did not happen in. That is worse than claiming
    none: someone reading it to find which stage failed first is reading
    fiction.

    Freezing the clock reproduces a coarse timer exactly, on any platform.
    """
    from app.io import llm as llm_module
    from app.io.llm import (
        BudgetedClient, RunBudget, ScriptedClient, merged_call_log,
    )

    monkeypatch.setattr(llm_module.time, "monotonic", lambda: 1234.5)

    budget = RunBudget(max_calls=10)
    fast = BudgetedClient(ScriptedClient([{"value": "a"}] * 3), budget)
    smart = BudgetedClient(ScriptedClient([{"value": "b"}] * 2), budget)

    fast.complete(system="s", user="u", stage="parse")
    smart.complete(system="s", user="u", stage="job_brief")
    fast.complete(system="s", user="u", stage="parse")
    smart.complete(system="s", user="u", stage="planner")
    fast.complete(system="s", user="u", stage="parse")

    assert [e["stage"] for e in merged_call_log(fast, smart)] == [
        "parse", "job_brief", "parse", "planner", "parse"
    ]


def test_a_log_from_an_older_run_still_merges():
    """Entries read back from a seeded folder have no sequence number and never
    will, so the fallback has to keep working."""
    from app.io.llm import merged_call_log

    class Stored:
        def __init__(self, log):
            self.log = log

    old = Stored([{"stage": "parse", "started": 1.0},
                  {"stage": "planner", "started": 3.0}])
    other = Stored([{"stage": "job_brief", "started": 2.0}])
    assert [e["stage"] for e in merged_call_log(old, other)] == [
        "parse", "job_brief", "planner"
    ]
