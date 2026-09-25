"""Engine and session. SQLite on a laptop, Postgres when hosted.

**The SDD says "no SQLite in cloud" and that stands** — but the reason was
specific and is worth keeping straight, because it is not "SQLite is bad". v1
ran its migrations as `PRAGMA` statements at startup, which are SQLite-only and
silently no-op on Postgres: the schema simply never changed and nothing said so.
Alembic is the fix for that, not an argument against SQLite on a machine where
there is one process and one file.

So: one model set, two URLs, migrations that work on both.

    sqlite+pysqlite:///jobtailor.db      local
    postgresql+psycopg://...             hosted

Two SQLite-specific settings are applied, both because the defaults are wrong
for a web application rather than for a script:

`check_same_thread=False` — the pipeline runs in a worker thread via
`asyncio.to_thread`, so a connection created on the event loop is used from
another thread. SQLite's default refuses that.

`PRAGMA foreign_keys=ON` — SQLite does not enforce foreign keys unless asked,
per connection. Without it a `run.resume_id` pointing at a deleted resume is
accepted locally and rejected in production, which is the worst place to find out.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base

logger = logging.getLogger(__name__)

DEFAULT_URL = "sqlite+pysqlite:///jobtailor.db"


def database_url() -> str:
    """`DATABASE_URL`, or a SQLite file beside the project.

    Read from the environment directly rather than through `Settings`, because
    `Settings` validates the LLM configuration and the database has to be
    reachable in contexts where no model is — a migration, for instance.
    """
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        return DEFAULT_URL
    # Heroku-style URLs are still common in copy-pasted config and SQLAlchemy 2
    # rejects the bare `postgres://` scheme.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    return url


def build_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    url = url or database_url()
    is_sqlite = url.startswith("sqlite")

    engine = create_engine(
        url,
        echo=echo,
        future=True,
        # The pipeline blocks for minutes on a model call while holding nothing,
        # so a small pool is right; pre-ping because a free-tier Postgres drops
        # idle connections and a stale one surfaces as a mid-run crash.
        pool_pre_ping=not is_sqlite,
        connect_args={"check_same_thread": False} if is_sqlite else {},
    )

    if is_sqlite:
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):   # noqa: ANN001
            cursor = dbapi_connection.cursor()
            # Off by default, per connection. See the module docstring.
            cursor.execute("PRAGMA foreign_keys=ON")
            # A reader does not block on a writer, which matters because the
            # progress stream polls while the pipeline writes.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = build_engine()
        logger.info("Database: %s", _engine.url.render_as_string(hide_password=True))
    return _engine


def session_factory() -> sessionmaker[Session]:
    global _Session
    if _Session is None:
        _Session = sessionmaker(
            bind=engine(), autoflush=False, expire_on_commit=False)
    return _Session


@contextmanager
def session_scope() -> Iterator[Session]:
    """A transaction. Commits on success, rolls back on anything.

    `expire_on_commit=False` on the factory so that objects stay usable after
    the block — a handler that returns a `Run` should not have to re-fetch it to
    read a field.
    """
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency. One session per request."""
    with session_scope() as session:
        yield session


def create_all(target: Engine | None = None) -> None:
    """Create the schema directly, without Alembic.

    For tests and for a first local run. **Not for the hosted deployment** — the
    whole reason Alembic is here is that a schema which appears by itself cannot
    be changed safely later, which is the v1 failure this module's docstring
    describes. A test that calls this is asserting against the models, which is
    what it wants; a server that calls this is skipping migrations.
    """
    Base.metadata.create_all(bind=target or engine())


def reset(url: str | None = None) -> Engine:
    """Point the module at a different database. Tests only.

    **The old engine is disposed before it is dropped**, and that is not
    housekeeping. SQLAlchemy pools connections, so an engine that is merely
    replaced still holds its SQLite file open — and on Windows an open file
    cannot be deleted, so pytest's `tmp_path` cleanup fails with

        PermissionError: [WinError 32] The process cannot access the file
        because it is being used by another process

    as a teardown ERROR on a test that itself passed. POSIX lets you unlink an
    open file, so on Linux this leaks quietly and nothing ever complains: green
    in CI, red on the machine someone is working on.
    """
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = build_engine(url) if url else None
    _Session = None
    return _engine or engine()
