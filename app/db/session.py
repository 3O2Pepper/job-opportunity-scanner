"""Database engine and session helpers."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import PROJECT_ROOT, settings
from app.db.models import Base


def _normalize_sqlite_url(url: str) -> str:
    """Ensure SQLite file parent directories exist for default paths."""
    if not url.startswith("sqlite:///"):
        return url
    path_part = url.replace("sqlite:///", "", 1)
    p = Path(path_part)
    if not p.is_absolute():
        p = (PROJECT_ROOT / p).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{p.as_posix()}"


def get_engine():
    url = settings.database_url
    if url.startswith("sqlite"):
        url = _normalize_sqlite_url(url)
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url)


_engine = None
_SessionLocal = None


def get_engine_instance():
    global _engine
    if _engine is None:
        _engine = get_engine()
    return _engine


def get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine_instance(),
            autoflush=False,
            autocommit=False,
        )
    return _SessionLocal


def init_db_tables() -> None:
    engine = get_engine_instance()
    Base.metadata.create_all(bind=engine)
    if settings.database_url.startswith("sqlite"):
        _migrate_sqlite_jobs_debug_column(engine)


def _migrate_sqlite_jobs_debug_column(engine) -> None:
    """Add extraction_debug_json if missing (SQLite has no ALTER IF NOT EXISTS)."""
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(jobs)"))
        col_names = [r[1] for r in rows.fetchall()]
        if "extraction_debug_json" not in col_names:
            conn.execute(text("ALTER TABLE jobs ADD COLUMN extraction_debug_json TEXT"))
            conn.commit()


def session_scope() -> Session:
    SessionLocal = get_session_factory()
    return SessionLocal()
