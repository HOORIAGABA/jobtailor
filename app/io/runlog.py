"""Every stage's output, written to a folder, so a bug can be looked at.

A pipeline that only prints to a terminal is one you debug by re-running it.
That is expensive here in a way it usually is not: each run costs real calls
from a free tier's per-minute allowance, the model is not deterministic above
`temperature=0`, and the interesting failures show up four stages after the one
that caused them. By the time S5 rejects something strange, the S0 text that
produced it has scrolled away.

So each run writes a folder:

    runs/2026-09-24T14-32-10_HooriaAttas/
      00_input.txt          what the file actually said
      01_extract.txt        S0.1 — reading order
      02_parse.json         S0.2 — the model's structure
      03_coverage.json      S0.2v — what it dropped or invented
      04_normalize.json     S0.3 — ids and classification
      manifest.json         inputs, config, token cost, timings

Three things this buys beyond convenience:

**Diffable regressions.** Change a prompt, re-run, `diff` the two folders. That
turns "it feels worse" into a specific line that changed.

**Bug reports that contain the evidence.** `03_coverage.json` names the lines
the parse lost. Without it, "my bullet went missing" is unactionable.

**The eval corpus, for free.** `evals/cases/` is empty. A folder of resumes and
their saved stage outputs *is* the regression corpus — the only extra work is
deciding which runs are correct.

**Write-only, deliberately.** Nothing in the pipeline ever reads these files
back. The moment a run folder can be read, it is a cache, and a cache is a
second source of truth that drifts from the first. This module has no `load`.

**These files hold personal data.** A resume is a name, an address, a phone
number and an employment history. `runs/` is gitignored, and that is not
incidental: v1 shipped a `output/<uuid>/Hooria__Attas/` folder at the repo root
that `.gitignore` did not cover, so real resume PDFs were one `git add -A` away
from being public. Writing anywhere else is the caller's decision to make
knowingly.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_ROOT = Path("runs")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slug(text: str, limit: int = 40) -> str:
    """A filesystem-safe fragment of `text`, for naming a run folder."""
    cleaned = _UNSAFE.sub("-", (text or "").strip()).strip("-.")
    return (cleaned[:limit].rstrip("-.") or "run")


def _default(value: Any) -> Any:
    """Serialise the things stage outputs actually contain."""
    if hasattr(value, "model_dump"):          # pydantic
        return value.model_dump()
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict
        return asdict(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, Path):
        return str(value)
    return str(value)


class RunLog:
    """One folder for one run. Artifacts are numbered in the order written."""

    def __init__(self, directory: Path, run_id: str) -> None:
        self.directory = directory
        self.run_id = run_id
        self.started = datetime.now(timezone.utc)
        self._step = 0
        self._artifacts: list[dict[str, Any]] = []
        self._meta: dict[str, Any] = {}
        directory.mkdir(parents=True, exist_ok=True)

    # ── construction ──────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        label: str = "",
        *,
        root: Path | str = DEFAULT_ROOT,
        stamp: datetime | None = None,
    ) -> "RunLog":
        """A new folder under `root`, named so runs sort chronologically."""
        when = stamp or datetime.now(timezone.utc)
        run_id = when.strftime("%Y-%m-%dT%H-%M-%S")
        if label:
            run_id = f"{run_id}_{slug(label)}"
        return cls(Path(root) / run_id, run_id)

    # ── writing ───────────────────────────────────────────────────────

    def text(self, stage: str, content: str, suffix: str = ".txt") -> Path:
        path = self._path(stage, suffix)
        path.write_text(content or "", encoding="utf-8")
        self._record(stage, path)
        return path

    def json(self, stage: str, value: Any) -> Path:
        path = self._path(stage, ".json")
        path.write_text(
            json.dumps(value, indent=2, ensure_ascii=False, default=_default),
            encoding="utf-8",
        )
        self._record(stage, path)
        return path

    def bytes(self, stage: str, data: bytes, suffix: str) -> Path:
        path = self._path(stage, suffix)
        path.write_bytes(data)
        self._record(stage, path)
        return path

    def note(self, **fields: Any) -> None:
        """Add to the manifest. Use for config, costs, and the input's hash."""
        self._meta.update(fields)

    def input_file(self, path: str | Path, data: bytes) -> None:
        """Record which file this run read, and prove which version of it.

        The bytes are hashed, not copied: a resume is personal data, and one
        copy of it is enough. Two runs over the same document are identifiable
        by the hash even after the original has been edited.

        Takes a name or a path, because the caller that has both is the CLI and
        the caller that matters is `pipeline.ingest`, which only ever has the
        filename. Recording it there is what stopped every non-CLI run from
        producing a manifest that could not say which resume it read.
        """
        self.note(
            input_name=Path(path).name,
            input_bytes=len(data),
            input_sha256=hashlib.sha256(data).hexdigest(),
        )

    # ── finishing ─────────────────────────────────────────────────────

    def finish(self, error: str = "") -> Path:
        """Write `manifest.json`. Safe to call after a failure."""
        finished = datetime.now(timezone.utc)
        manifest = {
            "run_id": self.run_id,
            "started": self.started.isoformat(),
            "finished": finished.isoformat(),
            "seconds": round((finished - self.started).total_seconds(), 2),
            "ok": not error,
            **({"error": error} if error else {}),
            **self._meta,
            "artifacts": self._artifacts,
        }
        path = self.directory / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False,
                                   default=_default), encoding="utf-8")
        logger.info("Run artifacts in %s", self.directory)
        return path

    # ── internals ─────────────────────────────────────────────────────

    def _path(self, stage: str, suffix: str) -> Path:
        name = f"{self._step:02d}_{slug(stage)}{suffix}"
        self._step += 1
        return self.directory / name

    def _record(self, stage: str, path: Path) -> None:
        self._artifacts.append({
            "stage": stage,
            "file": path.name,
            "bytes": path.stat().st_size,
        })


class NullRunLog(RunLog):
    """Writes nothing. The default, so production does not litter a disk.

    A null object rather than an `Optional[RunLog]`, because the alternative is
    `if log is not None:` around every write — and the one place that guard
    gets forgotten is the stage you most needed to see.
    """

    def __init__(self) -> None:                      # noqa: D107 — no folder
        self.directory = Path()
        self.run_id = ""
        self.started = datetime.now(timezone.utc)
        self._step = 0
        self._artifacts = []
        self._meta = {}

    def text(self, stage: str, content: str, suffix: str = ".txt") -> Path:
        return Path()

    def json(self, stage: str, value: Any) -> Path:
        return Path()

    def bytes(self, stage: str, data: bytes, suffix: str) -> Path:
        return Path()

    def note(self, **fields: Any) -> None:
        return None

    def input_file(self, path: Path, data: bytes) -> None:
        return None

    def finish(self, error: str = "") -> Path:
        return Path()
