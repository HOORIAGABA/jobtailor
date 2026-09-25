"""The whole of S0 against a real file, with a scripted model and no network.

This is the test that would have caught a broken debugging script — a tool you
only verify by running it is not much of a tool.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app.io.llm import BudgetedClient, RunBudget, ScriptedClient
from app.io.runlog import NullRunLog, RunLog

ROOT = Path(__file__).resolve().parent.parent


def load_script():
    """Import `scripts/parse_resume.py`, which is not on the package path."""
    spec = importlib.util.spec_from_file_location(
        "parse_resume_script", ROOT / "scripts" / "parse_resume.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = load_script()


PARSED = {
    "contact": {"full_name": "A. MORGAN", "email": "a.morgan@example.com", "phone": "", "location": "", "linkedin": "", "github": "", "website": ""},
    "summary": "",
    "sections": [
        {
            "heading": "EXPERIENCE",
            "entries": [{
                "title": "Data Engineer",
                "org": "Northwind Analytics",
                "dates": "03/2022 - Present",
                "bullets": [
                    "Rebuilt the nightly ingestion pipeline so a failed source "
                    "retries independently, cutting manual reruns from nine a "
                    "week to none",
                    "Migrated twelve scheduled jobs onto Airflow with explicit "
                    "dependencies",
                ],
            }],
        },
        {
            "heading": "SKILLS",
            "entries": [{"title": "", "org": "", "dates": "",
                         "bullets": ["Languages: Python, SQL, Go"]}],
        },
    ],
}

RESUME = """\
A. MORGAN
a.morgan@example.com

EXPERIENCE

Data Engineer
Northwind Analytics | 03/2022 - Present
- Rebuilt the nightly ingestion pipeline so a failed source retries independently, cutting manual reruns from nine a week to none
- Migrated twelve scheduled jobs onto Airflow with explicit dependencies

SKILLS
Languages: Python, SQL, Go
"""


@pytest.fixture
def resume(tmp_path: Path) -> Path:
    path = tmp_path / "a_morgan.txt"
    path.write_text(RESUME, encoding="utf-8")
    return path


def scripted() -> BudgetedClient:
    return BudgetedClient(ScriptedClient([PARSED]), RunBudget(max_calls=3))


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ── every stage lands on disk ─────────────────────────────────────────

def test_a_full_run_writes_every_stage(resume: Path, tmp_path: Path):
    log = RunLog.create(resume.stem, root=tmp_path / "runs")
    script.process(resume, text_only=False, log=log, settings=None,
                   quiet=True, client=scripted())
    log.finish()

    written = sorted(p.name for p in log.directory.iterdir())
    assert written == [
        "00_extract.txt",
        "01_parse.json",
        "02_coverage.json",
        "03_normalize.json",
        "manifest.json",
    ]


def test_the_saved_artifacts_are_the_real_stage_outputs(resume: Path, tmp_path: Path):
    log = RunLog.create("x", root=tmp_path / "runs")
    script.process(resume, text_only=False, log=log, settings=None,
                   quiet=True, client=scripted())

    assert "Northwind Analytics" in (log.directory / "00_extract.txt").read_text(encoding="utf-8")

    parse = read(log.directory / "01_parse.json")
    assert parse["sections"][0]["heading"] == "EXPERIENCE"

    normalize = read(log.directory / "03_normalize.json")
    assert normalize["sections"][0]["kind"] == "experience"
    assert normalize["sections"][0]["items"][0]["id"] == "exp.1"
    assert normalize["sections"][0]["items"][0]["date_start"] == "2022-03"
    assert "Python" in normalize["skill_inventory"]


def test_a_clean_parse_is_recorded_as_clean(resume: Path, tmp_path: Path):
    log = RunLog.create("x", root=tmp_path / "runs")
    script.process(resume, text_only=False, log=log, settings=None,
                   quiet=True, client=scripted())

    coverage = read(log.directory / "02_coverage.json")
    assert coverage["dropped"] == []
    assert coverage["invented"] == []


def test_a_lossy_parse_is_recorded_with_the_missing_line(resume: Path, tmp_path: Path):
    """The artifact that makes "my bullet went missing" actionable."""
    lossy = json.loads(json.dumps(PARSED))
    lossy["sections"][0]["entries"][0]["bullets"].pop()      # the Airflow bullet

    log = RunLog.create("x", root=tmp_path / "runs")
    client = BudgetedClient(ScriptedClient([lossy]), RunBudget(max_calls=3))
    row = script.process(resume, text_only=False, log=log, settings=None,
                         quiet=True, client=client)

    coverage = read(log.directory / "02_coverage.json")
    assert any("Airflow" in line for line in coverage["dropped"])
    assert row["dropped"] == 1


def test_the_summary_row_carries_what_the_table_prints(resume: Path, tmp_path: Path):
    row = script.process(resume, text_only=False, log=NullRunLog(),
                         settings=None, quiet=True, client=scripted())
    assert row["sections"] == 2
    assert row["bullets"] == 3
    assert row["calls"] == 1
    assert row["dropped"] == 0


# ── text-only ─────────────────────────────────────────────────────────

def test_text_only_makes_no_model_call(resume: Path, tmp_path: Path):
    """The zero-cost first check on a new resume."""
    client = ScriptedClient([])              # any call raises
    log = RunLog.create("x", root=tmp_path / "runs")
    row = script.process(resume, text_only=True, log=log, settings=None,
                         quiet=True, client=client)

    assert client.calls == []
    assert "calls" not in row
    assert sorted(p.name for p in log.directory.iterdir()) == ["00_extract.txt"]


# ── a folder of resumes ───────────────────────────────────────────────

def test_a_folder_finds_only_readable_resumes(tmp_path: Path):
    for name in ("a.pdf", "b.docx", "c.txt", "d.md", "notes.xlsx", "photo.png"):
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "subfolder").mkdir()

    assert [p.name for p in script.resumes_in(tmp_path)] == [
        "a.pdf", "b.docx", "c.txt", "d.md",
    ]


def test_the_committed_samples_all_extract():
    """A fresh clone must be able to run the pipeline with no real CV."""
    samples = ROOT / "samples"
    files = script.resumes_in(samples)
    assert {p.name for p in files} == {
        "two_column.pdf", "single_column.pdf", "table_layout.docx",
    }

    for sample in files:
        row = script.process(sample, text_only=True, log=NullRunLog(),
                             settings=None, quiet=True)
        assert row["chars"] > 300, sample.name


def test_the_two_column_sample_is_still_not_interleaved():
    """The sample exists to exercise engine.layout on a real PDF."""
    from app.io.extract import extract_text

    sample = ROOT / "samples" / "two_column.pdf"
    text = extract_text(sample.read_bytes(), sample.name)

    sidebar = max(text.find("Python, SQL, Go"), text.find("Example University"))
    assert sidebar >= 0
    assert sidebar < text.find("EXPERIENCE")


def test_a_readme_is_not_treated_as_a_resume(tmp_path: Path):
    """`.md` is a supported format, so a folder's own README would be parsed
    as a CV — and then reported as one that lost every line."""
    for name in ("README.md", "LICENSE.md", ".hidden.txt", "real_cv.pdf"):
        (tmp_path / name).write_bytes(b"x")
    assert [p.name for p in script.resumes_in(tmp_path)] == ["real_cv.pdf"]


# ── the model preflight ───────────────────────────────────────────────

def load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_model_script", ROOT / "scripts" / "check_model.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = load_checker()


def test_the_schema_probe_accepts_a_correct_answer():
    from app.io.llm import ScriptedClient
    assert checker.probe_schema(
        ScriptedClient([{"city": "Paris", "year": 1889}])) is True


def test_the_schema_probe_rejects_a_wrong_answer():
    """Shape held, content did not — a model echoing the schema back."""
    from app.io.llm import ScriptedClient
    assert checker.probe_schema(
        ScriptedClient([{"city": "Berlin", "year": 1889}])) is False


def test_the_context_probe_passes_when_the_marker_survives():
    from app.io.llm import ScriptedClient
    assert checker.probe_context(
        ScriptedClient([{"marker": checker.MARKER}]), target_tokens=200) is True


def test_the_context_probe_catches_a_truncated_prompt():
    """Ollama cuts anything past its context with no error.

    The only symptom is a parse that lost things, three stages later.
    """
    from app.io.llm import ScriptedClient
    assert checker.probe_context(
        ScriptedClient([{"marker": "I was not given a code"}]),
        target_tokens=200) is False


def test_the_context_probe_sends_a_prompt_of_the_requested_size():
    from app.io.llm import ScriptedClient
    client = ScriptedClient([{"marker": checker.MARKER}])
    checker.probe_context(client, target_tokens=3000)
    assert len(client.calls[0]["user"]) > 3000 * 3


def test_the_marker_is_at_the_very_start():
    """It has to be first, or a truncation from the front would not show."""
    from app.io.llm import ScriptedClient
    client = ScriptedClient([{"marker": checker.MARKER}])
    checker.probe_context(client, target_tokens=400)
    user = client.calls[0]["user"]
    assert user.index(checker.MARKER) < user.index("notes")
