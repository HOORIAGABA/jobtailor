"""Never pay twice for the same model call.

A free tier is not a budget, it is a **cliff**. Measured across ten real runs
on one resume:

    gemini-3.7-flash-medium   1 call   then unavailable
    gpt-oss-120b-medium       2 calls  then unavailable
    gemini-3.7-flash-high     3 calls  then unavailable
    three other providers     0 calls  unavailable immediately

The pipeline needs six calls for a two-page resume — three parse windows, then
the brief, the plan and the prose. On a provider that allows three, that never
completes, and re-running starts from zero: the three parse calls are spent
again on a document that already parsed perfectly, and the run dies in the same
place forever.

So a stage that succeeded is never re-run. The parse is keyed on the **resume
file's hash**, the brief on the **posting's hash**, and both survive the
process. What used to be an impossible six-call run becomes three short runs:

    run 1   parse x3                    -> dies at the brief
    run 2   parse from cache (0 calls), brief, plan   -> dies at the writer
    run 3   everything from cache, writer             -> complete

This is not a performance optimisation. It is what makes the project runnable
at all on the tiers available.

**Why a file and not a database.** The cache has to work before `app/db`
exists, survive a crash, and be inspectable with `cat`. A JSON file per key
does all three. Postgres replaces it later without any caller changing, because
callers only ever see the `Cache` protocol.

**Why hashing the input rather than the filename.** Editing a resume must
invalidate its parse. A name cannot express that; a content hash does, and it
is the same identity the run manifest already records.

**These files hold personal data** — a cached parse is the full text of a
resume. `.cache/` is gitignored for the same reason `runs/` is.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

DEFAULT_DIR = Path(".cache")


def key_for(data: bytes | str) -> str:
    """Content identity. The same document always has the same key."""
    if isinstance(data, str):
        data = data.strip().encode()
    return hashlib.sha256(data).hexdigest()


class Cache(Protocol):
    def get(self, namespace: str, key: str, model: type[T]) -> T | None: ...
    def set(self, namespace: str, key: str, value: BaseModel) -> None: ...


class FileCache:
    """One JSON file per entry, under `<root>/<namespace>/<key>.json`."""

    def __init__(self, root: Path | str = DEFAULT_DIR) -> None:
        self.root = Path(root)

    def path(self, namespace: str, key: str) -> Path:
        return self.root / namespace / f"{key}.json"

    def get(self, namespace: str, key: str, model: type[T]) -> T | None:
        path = self.path(namespace, key)
        if not path.exists():
            return None
        try:
            value = model.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, ValueError, OSError) as exc:
            # A cache that raises is worse than no cache: the stage can always
            # be re-run, but a crash on a stale entry blocks every future run
            # until someone deletes a file they do not know exists.
            logger.warning("Ignoring unreadable cache entry %s: %s", path, exc)
            return None
        logger.info("Cache hit: %s/%s", namespace, key[:8])
        return value

    def set(self, namespace: str, key: str, value: BaseModel) -> None:
        path = self.path(namespace, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write beside, then replace: a run killed mid-write must not leave a
        # half-file that every later run then fails to parse.
        temporary = path.with_suffix(".tmp")
        temporary.write_text(value.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(path)
        logger.info("Cached %s/%s", namespace, key[:8])


class NullCache:
    """Caches nothing. For `--fresh`, and for tests that assert call counts."""

    def get(self, namespace: str, key: str, model: type[T]) -> T | None:
        return None

    def set(self, namespace: str, key: str, value: BaseModel) -> None:
        return None
