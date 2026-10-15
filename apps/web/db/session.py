"""Database connection and session management."""

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool
from typing import Generator

from apps.web.db.models import Base


# Database URL configuration
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./dswarm.db"  # Default SQLite for development
)

# Create engine with connection pool
if DATABASE_URL.startswith("sqlite"):
    # SQLite: use StaticPool for thread-safety in dev
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=os.getenv("SQL_ECHO", "false").lower() == "true"
    )
else:
    # PostgreSQL or other databases: use default pool
    engine = create_engine(
        DATABASE_URL,
        echo=os.getenv("SQL_ECHO", "false").lower() == "true",
        pool_pre_ping=True,  # Verify connections before use
        pool_recycle=3600  # Recycle connections after 1 hour
    )

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def ensure_schema(bind_engine, url: str, *, alembic_ini: Path, migrations_dir: Path) -> None:
    """Bring one database to the current schema.

    Alembic owns the schema: an empty database is upgraded from the baseline; a
    pre-Alembic database that already has the app tables is adopted (stamped)
    first. ``create_all`` remains only as the fallback when the migrations
    directory is not shipped with this checkout.
    """
    if not (alembic_ini.is_file() and migrations_dir.is_dir()):
        Base.metadata.create_all(bind=bind_engine)
        return

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect as sa_inspect

    cfg = Config(str(alembic_ini))
    cfg.set_main_option("script_location", str(migrations_dir))
    cfg.set_main_option("sqlalchemy.url", url)

    inspector = sa_inspect(bind_engine)
    has_version = inspector.has_table("alembic_version")
    existing_app_tables = [
        name for name in inspector.get_table_names() if name in Base.metadata.tables
    ]
    if not has_version and existing_app_tables:
        # A database created before Alembic landed: it matches the baseline.
        command.stamp(cfg, "head")
    command.upgrade(cfg, "head")


def init_db() -> None:
    """Initialize/upgrade the database schema (see ensure_schema)."""
    repo_root = Path(__file__).resolve().parents[3]
    ensure_schema(
        engine,
        DATABASE_URL,
        alembic_ini=repo_root / "alembic.ini",
        migrations_dir=repo_root / "migrations",
    )


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency for database session.
    
    Usage:
        @app.get("/users")
        def get_users(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_db_session() -> Session:
    """Get a standalone database session (not for FastAPI)."""
    return SessionLocal()
