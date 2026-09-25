"""Starting a run without holding the request open.

A run takes minutes, so `POST /api/runs` cannot wait for it. It hands the work
to a background task and answers with an id.

**The recovery story is the run folder, not the task.** v1 put this work on a
daemon thread and lost a run when a free-tier host spun down after 15 minutes
idle — the thread died with the instance and there was nothing to go back to.
The fix is not a sturdier thread; it is that every stage writes itself to disk
as it completes, so a run killed halfway leaves the stages that finished. A
client reconnecting to the event stream reads the folder and sees all of them,
not just the ones that happened while it was connected.

This is the single-process version, which is the right amount of machinery for
one person running it on their own laptop. The SDD's `SELECT … FOR UPDATE SKIP
LOCKED` queue is what replaces it when there is a second machine to run a worker
on, and the stage functions will not change when that happens — they are already
pure functions of their inputs.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from app.config import Settings, check
from app.io.cache import FileCache
from app.io.jobsource import load_job
from app.io.llm import BudgetedClient, RunBudget, build_client
from app.io.runlog import RunLog
from app.pipeline.run import run as run_pipeline

logger = logging.getLogger(__name__)

MAX_RESUME_BYTES = 10 * 1024 * 1024
SUPPORTED = (".pdf", ".docx", ".txt", ".md")


class Runner:
    """Owns the background tasks. One in flight per instance is plenty here."""

    def __init__(self, runs_dir: Path, cache_dir: Path | None = None) -> None:
        self.runs_dir = Path(runs_dir)
        self.cache_dir = Path(cache_dir or ".cache")
        self._tasks: dict[str, asyncio.Task] = {}

    # ── what this instance can do ─────────────────────────────────────

    @property
    def can_start(self) -> bool:
        """True when a model is actually reachable from here.

        Checked rather than assumed: the public deployment has no model, and a
        UI that offers a button which always fails is worse than one that does
        not offer it.
        """
        try:
            return not check(Settings())
        except Exception as exc:                          # noqa: BLE001
            logger.info("Cannot start runs here: %s", exc)
            return False

    @property
    def model(self) -> str:
        try:
            return Settings().llm_model
        except Exception:                                 # noqa: BLE001
            return ""

    def is_running(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    # ── starting ──────────────────────────────────────────────────────

    async def start(self, payload: dict[str, Any]) -> str:
        """Validate, create the run folder, hand off. Returns the run id."""
        resume_path = Path(str(payload.get("resume") or "")).expanduser()
        job = str(payload.get("job") or "")

        if not resume_path.is_file():
            raise ValueError(f"no resume file at {resume_path}")
        if resume_path.suffix.lower() not in SUPPORTED:
            raise ValueError(
                f"cannot read {resume_path.suffix!r}; supported: "
                f"{', '.join(SUPPORTED)}"
            )
        data = resume_path.read_bytes()
        if len(data) > MAX_RESUME_BYTES:
            raise ValueError(f"resume is {len(data)} bytes; limit is "
                             f"{MAX_RESUME_BYTES}")
        if not job.strip():
            raise ValueError("no job posting given")

        # No `allow_paths`: this runs behind an HTTP handler, and reading a
        # server-side file named by a request is arbitrary file disclosure.
        jd_text = load_job(job)
        log = RunLog.create(resume_path.stem, root=self.runs_dir)
        log.input_file(resume_path, data)

        task = asyncio.create_task(
            self._execute(log, data, resume_path.name, jd_text, payload)
        )
        self._tasks[log.run_id] = task
        logger.info("Run %s started in the background", log.run_id)
        return log.run_id

    async def _execute(
        self, log: RunLog, data: bytes, filename: str,
        jd_text: str, payload: dict[str, Any],
    ) -> None:
        """Run the pipeline off the event loop.

        `to_thread` because the pipeline is synchronous and blocking — mostly on
        a model that takes two minutes to answer. Running it inline would stop
        the server answering the very requests the client uses to watch it.
        """
        try:
            await asyncio.to_thread(
                self._pipeline, log, data, filename, jd_text, payload)
            log.finish()
        except Exception as exc:                          # noqa: BLE001
            # The folder is the record, so the failure belongs in it. Raising
            # here would lose it: nobody is awaiting this task.
            logger.exception("Run %s failed", log.run_id)
            raw = getattr(exc, "raw", "")
            if raw:
                log.text(f"{getattr(exc, 'stage', 'model')}_response", raw)
            log.finish(f"{type(exc).__name__}: {exc}")

    def _pipeline(
        self, log: RunLog, data: bytes, filename: str,
        jd_text: str, payload: dict[str, Any],
    ) -> None:
        settings = Settings()
        budget = RunBudget(max_calls=settings.max_llm_calls_per_run,
                           max_tokens=settings.max_tokens_per_run)
        client = BudgetedClient(build_client(settings), budget)
        smart = client
        if settings.has_smart_model:
            smart = BudgetedClient(build_client(settings, role="smart"), budget)

        # `run` records the call log, the budget and the proof checks itself,
        # so there is nothing to account for here.
        return run_pipeline(
            data, filename, jd_text, client,
            log=log, cache=FileCache(self.cache_dir), smart_client=smart,
            semantic_hints=bool(payload.get("hints")),
            write_outreach=bool(payload.get("outreach", True)),
        )
