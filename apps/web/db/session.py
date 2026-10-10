"""Database connection and session management."""

import os
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


def init_db():
    """Initialize database with all tables."""
    Base.metadata.create_all(bind=engine)


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
