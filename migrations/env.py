"""Alembic environment.

**The URL comes from the application, not from `alembic.ini`.** A connection
string duplicated in a config file is a second source of truth, and the one that
is wrong is always the one nobody looked at. `app.db.session.database_url()`
reads `DATABASE_URL` and falls back to a local SQLite file, so a migration runs
against the same database the server just used, whichever that is.

`render_as_batch` is on for SQLite. SQLite cannot `ALTER TABLE ... DROP COLUMN`
or change a constraint; Alembic's batch mode emulates it by rebuilding the table.
Without it a migration that works on Postgres fails on a laptop, which is the
wrong way round for finding out.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.db.models import Base
from app.db.session import database_url

config = context.config
# Only when the caller has not chosen one. Setting it unconditionally silently
# ignored an explicit URL, which meant a test pointing Alembic at a temporary
# database got the real one instead — and the check that migrations match the
# models was quietly asserting against the wrong schema.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", database_url())

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _is_sqlite() -> bool:
    return config.get_main_option("sqlalchemy.url", "").startswith("sqlite")


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_is_sqlite(),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=_is_sqlite(),
            # Without this a column whose type changed is silently ignored.
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
