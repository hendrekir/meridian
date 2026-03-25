"""
Meridian — database.py v8.3
SQLAlchemy engine + session factory.
Reads DATABASE_URL from env; falls back to SQLite for local dev.
PostgreSQL is required on Railway (set DATABASE_URL in Railway variables).
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./meridian.db",
)

# Railway injects postgres:// — SQLAlchemy 2.x needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
    pool_recycle=300,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """FastAPI dependency — yields a DB session, always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables():
    """Create all tables defined in models.py. Safe to call on every startup."""
    from modelsv8_3_2 import Base  # noqa: F401  — side-effect import registers all mappers
    Base.metadata.create_all(bind=engine)
