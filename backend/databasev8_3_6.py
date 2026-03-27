"""
Meridian — database.py v8.3.6
SQLAlchemy engine + session factory + lightweight column migrations.

v8.3.6 adds: import_status, import_error, validated_at,
             transactions_imported, available_models to the providers table.
_run_migrations() adds them safely to existing deployed databases.
"""
import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./meridian.db",
)

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

_is_sqlite     = DATABASE_URL.startswith("sqlite")
_connect_args  = {"check_same_thread": False} if _is_sqlite else {}

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
    """Create all tables. Safe to call every startup."""
    from modelsv8_3_6 import Base  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _run_migrations()


def _run_migrations():
    """
    Add new columns to existing tables without a full migration framework.
    Each ALTER TABLE is wrapped in try/except so re-runs are harmless.
    PostgreSQL supports ADD COLUMN IF NOT EXISTS; SQLite does not — both
    code paths are handled.
    """
    new_provider_cols = [
        ("import_status",         "VARCHAR DEFAULT 'pending'"),
        ("import_error",          "TEXT"),
        ("validated_at",          "TIMESTAMP"),
        ("transactions_imported", "INTEGER DEFAULT 0"),
        ("available_models",      "JSON"),
    ]

    with engine.begin() as conn:
        for col, col_type in new_provider_cols:
            if _is_sqlite:
                try:
                    conn.execute(text(f"ALTER TABLE providers ADD COLUMN {col} {col_type}"))
                except Exception:
                    pass  # column already exists in SQLite
            else:
                # PostgreSQL supports IF NOT EXISTS
                try:
                    conn.execute(text(
                        f"ALTER TABLE providers ADD COLUMN IF NOT EXISTS {col} {col_type}"
                    ))
                except Exception:
                    pass
