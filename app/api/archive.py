"""Reading finished runs back off disk, for the API to serve.

**Why this is not a method on `RunLog`.** That class is write-only, with a test
asserting it has no `load`, `read` or `open`, because a run folder that can be
read *during* a run is a second source of truth that drifts from the first: a
stage would be able to take its input from the folder instead of from the run
state, and the two would disagree the moment one changed.

None of that applies afterwards. A finished run folder is an archive — nothing
is going to write to it again, and the only thing reading it is a UI showing
what happened. So the reader is a separate type, in a separate layer, and it
refuses a folder with no `manifest.json`, which is precisely the marker that a
run is over.

The archive is the demo. A public deployment of this product cannot execute a
run — the model runs on a laptop, an eight-minute request survives no HTTP
timeout, and every visitor would spend a free-tier quota. What it *can* do is
serve runs that really happened, with every stage inspectable: the brief, the
evidence, the plan, what the validator refused, the diff, the outreach draft,
and the rendered resume. That is the product, minus the waiting.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MANIFEST = "manifest.json"

# Artifact stems the UI knows how to present specially. Anything else is still
# listed and downloadable as raw JSON — a new stage should appear in the
# inspector without this module being edited.
RESUME_DOCX = "resume_docx"
RESUME_PDF = "resume_pdf"
RESUME_TEXT = "resume_ats"


class RunNotFound(KeyError):
    """No such run id in this archive."""


@dataclass(frozen=True)
class Artifact:
    stage: str
    filename: str
    bytes: int

    @property
    def is_json(self) -> bool:
        return self.filename.endswith(".json")


@dataclass
class RunSummary:
    """Enough for a list row. Read from the manifest alone — one small file."""
    run_id: str
    ok: bool
    started: str
    seconds: float
    resume_name: str
    role: str = ""
    company: str = ""
    model: str = ""
    smart_model: str = ""
    calls: int = 0
    tokens: int = 0
    error: str = ""
    proof: dict[str, bool] = field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        """Reached the end. A failed run is still worth showing — the run
        folder exists *because* a failure leaves the stages that explain it."""
        return self.ok


@dataclass
class RunDetail:
    summary: RunSummary
    artifacts: list[Artifact]
    manifest: dict[str, Any]

    def artifact(self, stage: str) -> Artifact | None:
        return next((a for a in self.artifacts if a.stage == stage), None)


class Archive:
    """A directory of run folders, read-only.

    Nothing here caches: a run folder is immutable once finished, so re-reading
    a manifest is cheap and always current. A cache would be the one way to
    make this wrong.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ── listing ───────────────────────────────────────────────────────

    def run_ids(self) -> list[str]:
        """Finished runs, newest first.

        A folder with no manifest is a run that is still going or was killed;
        either way it has no consistent story to tell yet, so it is not listed.
        """
        if not self.root.is_dir():
            return []
        found = [
            path.name for path in self.root.iterdir()
            if path.is_dir() and (path / MANIFEST).is_file()
        ]
        return sorted(found, reverse=True)

    def summaries(self, limit: int = 50) -> list[RunSummary]:
        out: list[RunSummary] = []
        for run_id in self.run_ids()[:limit]:
            try:
                out.append(self.summary(run_id))
            except (RunNotFound, ValueError) as exc:
                # One unreadable folder must not take down the list.
                logger.warning("Skipping run %s: %s", run_id, exc)
        return out

    # ── one run ───────────────────────────────────────────────────────

    def summary(self, run_id: str) -> RunSummary:
        manifest = self._manifest(run_id)
        return RunSummary(
            run_id=manifest.get("run_id", run_id),
            ok=bool(manifest.get("ok")),
            started=str(manifest.get("started", "")),
            seconds=float(manifest.get("seconds") or 0.0),
            resume_name=str(manifest.get("input_name", "")),
            role=str(manifest.get("role", "")),
            company=str(manifest.get("company", "")),
            model=str(manifest.get("model", "")),
            smart_model=str(manifest.get("smart_model", "")),
            calls=int(manifest.get("calls") or 0),
            tokens=int(manifest.get("tokens") or 0),
            error=str(manifest.get("error", "")),
            proof={k: bool(v) for k, v in (manifest.get("proof") or {}).items()},
        )

    def detail(self, run_id: str) -> RunDetail:
        manifest = self._manifest(run_id)
        artifacts = [
            Artifact(
                stage=str(entry.get("stage", "")),
                filename=str(entry.get("file", "")),
                bytes=int(entry.get("bytes") or 0),
            )
            for entry in manifest.get("artifacts", [])
            if entry.get("file")
        ]
        return RunDetail(
            summary=self.summary(run_id), artifacts=artifacts, manifest=manifest,
        )

    def stage_json(self, run_id: str, stage: str) -> Any:
        """One stage's artifact, parsed. `RunNotFound` when there is no such stage."""
        artifact = self.detail(run_id).artifact(stage)
        if artifact is None or not artifact.is_json:
            raise RunNotFound(f"{run_id} has no JSON artifact for stage {stage!r}")
        return json.loads(self._file(run_id, artifact.filename).read_text("utf-8"))

    def stage_bytes(self, run_id: str, stage: str) -> tuple[bytes, str]:
        """An artifact's raw bytes and filename — for the .docx and .pdf."""
        artifact = self.detail(run_id).artifact(stage)
        if artifact is None:
            raise RunNotFound(f"{run_id} has no artifact for stage {stage!r}")
        return self._file(run_id, artifact.filename).read_bytes(), artifact.filename

    def stage_text(self, run_id: str, stage: str) -> str:
        data, _ = self.stage_bytes(run_id, stage)
        return data.decode("utf-8", errors="replace")

    # ── internals ─────────────────────────────────────────────────────

    def _dir(self, run_id: str) -> Path:
        """Resolve a run id to its folder, refusing anything that escapes.

        `run_id` arrives from a URL. `../` in a path segment is the oldest
        file-serving bug there is, and this project already declined to fetch a
        user-supplied URL for the same class of reason — a value from outside
        does not get to choose which file is read.
        """
        if not run_id or "/" in run_id or "\\" in run_id or run_id.startswith("."):
            raise RunNotFound(f"not a run id: {run_id!r}")
        path = (self.root / run_id).resolve()
        if path.parent != self.root.resolve() or not path.is_dir():
            raise RunNotFound(run_id)
        return path

    def _file(self, run_id: str, filename: str) -> Path:
        if "/" in filename or "\\" in filename or filename.startswith("."):
            raise RunNotFound(f"not an artifact name: {filename!r}")
        path = self._dir(run_id) / filename
        if not path.is_file():
            raise RunNotFound(f"{run_id}/{filename}")
        return path

    def _manifest(self, run_id: str) -> dict[str, Any]:
        path = self._dir(run_id) / MANIFEST
        if not path.is_file():
            raise RunNotFound(f"{run_id} has no {MANIFEST} — still running?")
        return json.loads(path.read_text("utf-8"))
