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

# ★ `.env`, read here as a fallback — and the reason is a bug, not convenience.
#
# This module deliberately reads the process environment rather than `Settings`,
# and for a while that was the whole story. But `Settings` has a `database_url`
# field and loads `.env`, and pydantic-settings parses that file into the model
# **without exporting anything to `os.environ`**. So putting `DATABASE_URL` in
# `.env` — the obvious place, the place the file exists for — produced two
# different answers to one question:
#
#     Settings().database_url   → postgresql+psycopg://…neon…    (read .env)
#     session.database_url()    → sqlite+pysqlite:///jobtailor.db (read os.environ)
#
# Nothing failed. `alembic upgrade head` migrated the local SQLite file and
# printed success, the production database stayed empty, and the first symptom
# was a deployed `GET /` naming missing tables with no hint as to why. That is
# I3 — one producer per fact class — violated for the single most consequential
# fact in the system: which database is this.
#
# So the fallback exists to make the two agree. It is parsed by hand rather than
# by importing `Settings`, which keeps the property the docstring below is about:
# a migration must work in a checkout with no model configured.
#
# `ENV_FILE = None` detaches it, and `tests/conftest.py` does exactly that, for
# the same reason it detaches `Settings`: a test must never read the developer's
# `.env`. Without that line, a developer whose `.env` points at production would
# have the suite point there too.
ENV_FILE: Path | None = Path(__file__).resolve().parents[2] / ".env"


def _dotenv_database_url() -> str:
    """`DATABASE_URL` out of `.env`, or `""`.

    A deliberately small parser: `KEY=value`, `#` comments, optional matching
    quotes. It is not a dotenv implementation and does not want to be — the one
    key it reads is the one whose absence is silent.
    """
    path = ENV_FILE
    if path is None or not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:                                        # unreadable is absent
        return ""

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip().upper() != "DATABASE_URL":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value.strip()
    return ""


# The schemes that mean "Postgres, driver unspecified". Both get the driver
# named, because SQLAlchemy's default for an unqualified `postgresql://` is
# **psycopg2** — the 2 — and this project depends on psycopg 3.
#
# ★ `postgresql://` was missing from this list, and the cost was a traceback that
# points at the wrong thing entirely:
#
#     File ".../sqlalchemy/dialects/postgresql/psycopg2.py", line 690
#     ModuleNotFoundError: No module named 'psycopg2'
#
# Nothing in that says "your URL does not name a driver". It reads as a missing
# dependency, so the obvious response is `pip install psycopg2` — which installs
# a second, unwanted Postgres driver and makes the symptom disappear for the
# wrong reason.
#
# `postgres://` was handled because it is the Heroku form and the one every blog
# post mentions. `postgresql://` is what Neon, Supabase and Render actually hand
# you, which made the common case the broken one. DEPLOY.md papered over it with
# an instruction to edit the scheme by hand; an instruction that exists because
# the code is wrong is a bug with documentation on top.
_BARE_POSTGRES_SCHEMES = ("postgres", "postgresql")
_PREFERRED_POSTGRES_URL_SCHEME = "postgresql+psycopg"


def normalise_url(url: str) -> str:
    """Name the Postgres driver if the URL does not, and change nothing else.

    An explicit driver is left alone — `postgresql+asyncpg://` or even
    `postgresql+psycopg2://` is a deliberate choice and not ours to override.
    Only the unqualified forms are rewritten.
    """
    scheme, separator, rest = url.partition("://")
    if separator and scheme.lower() in _BARE_POSTGRES_SCHEMES:
        return f"{_PREFERRED_POSTGRES_URL_SCHEME}://{rest}"
    return url


def database_url() -> str:
    """`DATABASE_URL`, or a SQLite file beside the project.

    Read from the environment directly rather than through `Settings`, because
    `Settings` validates the LLM configuration and the database has to be
    reachable in contexts where no model is — a migration, for instance. The
    process environment wins; `.env` is the fallback, for the reason above.

    **This is the one producer of the answer.** `Settings.database_url` holds the
    raw string and is only ever checked for presence; the value used to connect
    comes from here, normalised, so there is nowhere for the two to disagree.
    """
    url = (os.environ.get("DATABASE_URL") or "").strip() or _dotenv_database_url()
    return normalise_url(url) if url else DEFAULT_URL


def build_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    url = url or database_url()
    is_sqlite = url.startswith("sqlite")

    # Postgres-only pool settings. SQLite gets none of them: `create_engine`
    # gives a file-backed SQLite a SingletonThreadPool/NullPool where `pool_size`
    # is either meaningless or a TypeError, so passing them unconditionally
    # breaks every local run and every test.
    pooled: dict[str, object] = {}
    if not is_sqlite:
        # Pre-ping because a free-tier Postgres drops idle connections and a
        # stale one surfaces as a mid-run crash rather than as a reconnect.
        pooled["pool_pre_ping"] = True

        # ── Why these numbers, on a serverless host ──────────────────────────
        #
        # The hosted API is a Vercel Function. Under Fluid compute one instance
        # is a real ASGI process serving many concurrent requests, so a pool is
        # right — NullPool would open and close a Postgres connection per
        # request, which on a database that scales to zero is the slowest thing
        # in the response. But instances multiply under load, and the pool is
        # per instance: the connection count at the database is
        # (pool_size + max_overflow) × instances, and it is the database that
        # runs out first. 5 + 5 keeps ten instances inside Neon's pooled
        # endpoint comfortably.
        #
        # `pool_recycle` is the one that bites. Neon's pooled endpoint is
        # PgBouncer and it closes an idle client connection after a few minutes;
        # a function instance that has been quiet holds a handle to a socket
        # that is already gone. `pool_pre_ping` catches that on the next
        # checkout, but it pays a round trip to find out, and it only helps if
        # the server actually sent a FIN we noticed. Recycling at 300s means we
        # drop the connection before the proxy does.
        pooled["pool_size"] = 5
        pooled["max_overflow"] = 5
        pooled["pool_recycle"] = 300

        # Not set, and worth writing down because the internet will tell you to:
        # psycopg 3 prepares a statement automatically after it has seen it five
        # times, and under PgBouncer's transaction pooling that used to fail with
        # `prepared statement "_pg3_0" already exists` — hence the folklore fix
        # `prepare_threshold=None`. PgBouncer has supported protocol-level
        # prepared statements since 1.21 and Neon's pooler since 1.22 (Feb 2024),
        # so on Neon the folklore fix now only costs performance. If a *different*
        # pooler ever sits in front of this, that is the knob.

    engine = create_engine(
        url,
        echo=echo,
        future=True,
        connect_args={"check_same_thread": False} if is_sqlite else {},
        **pooled,                                          # type: ignore[arg-type]
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
