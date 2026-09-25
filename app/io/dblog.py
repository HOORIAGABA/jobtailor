"""A `RunLog` that writes to the database instead of a folder.

**No stage changes to make this work.** `RunLog` was already the seam where
"what did this stage produce" leaves the pipeline, and `NullRunLog` already
proved the interface is substitutable — the pipeline calls `log.json("brief",
...)` and has no opinion about where that goes. So persistence slots in as a
third implementation rather than as an edit to fifteen stages.

What changes is where the four kinds of output land:

    log.json(stage, value)   -> a column on the run, and a checkpoint row
    log.bytes(stage, data)   -> an `artifacts` row, bytes in the row
    log.text(stage, content) -> an `artifacts` row (the ATS view, a raw reply)
    log.note(**fields)       -> the run's own columns

**Every write commits.** A run takes minutes and the point of checkpointing is
that a process which dies halfway leaves the stages that finished — holding one
transaction open for the whole run would discard exactly the thing being
recorded. The cost is that a failed run leaves partial state, which is the
intended outcome: the folder-based log has the same property and it is what
makes a failure explicable.

**Artifacts are bytes in a row, not paths.** v1 stored absolute paths; the host
restarted, the filesystem was ephemeral, and the rows pointed at files that had
stopped existing. `io.render` returns bytes for this reason.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Artifact, Run, RunCheckpoint
from app.io.runlog import RunLog

logger = logging.getLogger(__name__)

# Stage name -> the column on `runs` it belongs in. A stage not listed here
# still becomes a checkpoint; the columns exist for the handful the API serves
# individually, so a UI showing the gap list does not have to download the run.
STAGE_COLUMNS: dict[str, str] = {
    "brief": "brief_json",
    "evidence": "index_json",
    "plan": "ops_json",
    "tailored": "tailored_json",
    "diff": "diff_json",
    "outreach": "outreach_json",
}

MEDIA = {
    ".docx": "application/vnd.openxmlformats-officedocument."
             "wordprocessingml.document",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
}

# A stage whose payload is only useful as a file. The rest are inlined as JSON
# on the run or kept as checkpoint state.
FILE_STAGES = frozenset({"resume_docx", "resume_pdf", "resume_ats"})


class DbRunLog(RunLog):
    """Writes a run's stages to the database as they complete.

    Takes a session *factory* rather than a session: the pipeline runs in a
    worker thread and a SQLAlchemy `Session` is not safe to share across
    threads. Each write opens its own short transaction, which is also what
    makes each one durable the moment the stage finishes.
    """

    def __init__(self, run_id: str, factory: sessionmaker[Session]) -> None:
        # Deliberately not calling super().__init__: there is no directory.
        self.run_id = run_id
        self._factory = factory
        self._seq = 0

    # ── the four kinds of output ──────────────────────────────────────

    def json(self, stage: str, value: Any) -> Path:
        payload = _jsonable(value)
        with self._factory() as session:
            run = session.get(Run, self.run_id)
            if run is not None:
                if column := STAGE_COLUMNS.get(stage):
                    setattr(run, column, _unwrap(stage, payload))
                # The three-grade term standing is the most user-facing thing
                # a run produces and it travels inside the evidence payload.
                # Lifting it to its own column here keeps one producer — S2 —
                # while letting the review screen read it without unpacking a
                # blob it otherwise has no reason to fetch.
                if (standing := standing_of(stage, payload)) is not None:
                    run.standing_json = standing
                run.stage = stage
            self._checkpoint(session, stage, {"kind": "json"})
            session.commit()
        return Path(stage)

    def bytes(self, stage: str, data: bytes, suffix: str) -> Path:
        filename = f"{stage}{suffix}"
        with self._factory() as session:
            self._artifact(session, stage, filename, data)
            self._checkpoint(session, stage, {"bytes": len(data)})
            session.commit()
        return Path(filename)

    def text(self, stage: str, content: str, suffix: str = ".txt") -> Path:
        return self.bytes(stage, content.encode("utf-8"), suffix)

    def note(self, **fields: Any) -> None:
        """Scalars onto the run's own columns.

        Unknown keys are dropped rather than stored in a bag. A note that lands
        nowhere is a note nobody reads, and a schemaless side-table of
        everything a stage felt like recording is how a manifest becomes
        untrustworthy.
        """
        with self._factory() as session:
            run = session.get(Run, self.run_id)
            if run is None:
                return
            for key, value in fields.items():
                if key in _NOTE_COLUMNS:
                    setattr(run, _NOTE_COLUMNS[key], value)
                else:
                    logger.debug("Dropping note %r (no column)", key)
            session.commit()

    def input_file(self, path: str | Path, data: bytes) -> None:
        """Recorded on the resume, not the run — that is where it belongs, and
        by this point the resume row already has it."""
        return None

    def finish(self, error: str = "") -> Path:
        """No manifest to write. The run row IS the manifest.

        Status is not set here: `pipeline.runs` owns the transitions, because
        the legal ones depend on what the run was doing and a log has no way to
        know that.
        """
        if error:
            with self._factory() as session:
                run = session.get(Run, self.run_id)
                if run is not None:
                    run.error = error
                session.commit()
        return Path(self.run_id)

    # ── internals ─────────────────────────────────────────────────────

    def _checkpoint(self, session: Session, stage: str,
                    state: dict[str, Any]) -> None:
        """One row per stage, and only one — a stage that writes twice is the
        same stage, and the unique constraint says so."""
        existing = (session.query(RunCheckpoint)
                    .filter(RunCheckpoint.run_id == self.run_id,
                            RunCheckpoint.stage == stage)
                    .one_or_none())
        self._seq += 1
        if existing is not None:
            existing.state_json = {**state, "seq": self._seq}
            return
        session.add(RunCheckpoint(
            run_id=self.run_id, stage=stage,
            state_json={**state, "seq": self._seq}))

    def _artifact(self, session: Session, stage: str, filename: str,
                  data: bytes) -> None:
        existing = (session.query(Artifact)
                    .filter(Artifact.run_id == self.run_id,
                            Artifact.stage == stage)
                    .one_or_none())
        media = MEDIA.get(Path(filename).suffix.lower(),
                          "application/octet-stream")
        if existing is not None:
            existing.filename, existing.content_type = filename, media
            existing.bytes, existing.blob = len(data), data
            return
        session.add(Artifact(
            run_id=self.run_id, stage=stage, filename=filename,
            content_type=media, bytes=len(data), blob=data))


_NOTE_COLUMNS = {
    "calls": "llm_calls",
    "tokens": "tokens",
    "call_log": "call_log_json",
    "proof": "proof_json",
    "role": "role",
    "company": "company",
}


def standing_of(stage: str, payload: Any) -> Any:
    """S2's three grades, which travel inside the evidence payload.

    Public and shared because there are two writers — a live run through
    `DbRunLog`, and `scripts/seed.py` importing a folder — and the rule about
    where standing lives must not be written twice. It was, briefly, and the
    seeded runs came back with no gap list at all.
    """
    if stage != "evidence" or not isinstance(payload, dict):
        return None
    return payload.get("term_standing")


def _jsonable(value: Any) -> Any:
    """Pydantic models, dataclasses and plain values, all to JSON types.

    Goes through `json.dumps` with the same default the folder log uses, so a
    value that lands in a run folder and the same value in a row cannot differ
    in shape — which would otherwise show up as a UI that renders one and not
    the other.
    """
    from app.io.runlog import _default
    return json.loads(json.dumps(value, default=_default))


def _unwrap(stage: str, payload: Any) -> Any:
    """`plan` is logged as `{"operations": [...], ...}` and stored as the list.

    The column is `ops_json` and what the API serves is a list of operations,
    so the unwrapping happens once here rather than in every reader.
    """
    if stage == "plan" and isinstance(payload, dict):
        return payload.get("operations", payload)
    return payload
