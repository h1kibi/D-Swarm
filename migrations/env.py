"""Alembic environment — wired to apps.web.db models.

URL resolution order:
1. ``DATABASE_URL`` in the process env (deployments, tests),
2. the URL on the Alembic config (``init_db``/tests set it explicitly),
3. the app default from ``apps.web.db.session``.

SQLite gets ``render_as_batch`` so future migrations can ALTER tables (SQLite
has no native ALTER COLUMN).
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.web.db.models import Base  # noqa: E402
from apps.web.db.session import DATABASE_URL as APP_DATABASE_URL  # noqa: E402

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False: ensure_schema runs inside the app process
    # on every boot; the default (True) would silence every already-created
    # application logger as a side effect of running migrations.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

_url = os.getenv("DATABASE_URL") or config.get_main_option("sqlalchemy.url") or ""
if not _url or _url.startswith("driver://"):
    _url = APP_DATABASE_URL
config.set_main_option("sqlalchemy.url", _url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live connection (``alembic upgrade --sql``)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
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
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
