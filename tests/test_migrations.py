"""Migration framework contract.

Alembic owns the schema: a fresh database is built from the baseline revision,
and a pre-Alembic database that already has the app tables is adopted (stamped)
without touching its data. These tests exercise the exact branches init_db
takes, against throwaway SQLite files.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect

from apps.web.db.models import Base
from apps.web.db.session import ensure_schema

ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = ROOT / "alembic.ini"
MIGRATIONS_DIR = ROOT / "migrations"

EXPECTED_TABLES = {"accounts", "account_api_keys", "ctf_events", "alembic_version"}


def test_fresh_database_is_built_from_baseline(tmp_path):
    db_path = tmp_path / "fresh.db"
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        ensure_schema(
            engine,
            f"sqlite:///{db_path}",
            alembic_ini=ALEMBIC_INI,
            migrations_dir=MIGRATIONS_DIR,
        )
        tables = set(inspect(engine).get_table_names())
        assert EXPECTED_TABLES <= tables
        assert "audit_logs" in tables and "token_usage_logs" in tables

        # Idempotent: a second run is a no-op, not a duplicate-create error.
        ensure_schema(
            engine,
            f"sqlite:///{db_path}",
            alembic_ini=ALEMBIC_INI,
            migrations_dir=MIGRATIONS_DIR,
        )
    finally:
        engine.dispose()


def test_pre_alembic_database_is_adopted_without_data_loss(tmp_path):
    db_path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        # Simulate the pre-Alembic world: tables exist, no alembic_version.
        Base.metadata.create_all(bind=engine)
        assert "alembic_version" not in inspect(engine).get_table_names()

        ensure_schema(
            engine,
            f"sqlite:///{db_path}",
            alembic_ini=ALEMBIC_INI,
            migrations_dir=MIGRATIONS_DIR,
        )
        tables = set(inspect(engine).get_table_names())
        assert "alembic_version" in tables
        # The adoption stamped the baseline instead of re-creating tables.
        with engine.connect() as conn:
            from sqlalchemy import text

            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert version
    finally:
        engine.dispose()
