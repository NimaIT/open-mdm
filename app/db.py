"""Database engines and session management.

Two engines by design (least privilege):
  * ``engine``      — runtime DML connection used by all request handling.
  * ``ddl_engine``  — elevated connection used *only* when publishing models.
"""
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.config import settings

Base = declarative_base()

_engine: Optional[Engine] = None
_ddl_engine: Optional[Engine] = None
_SessionLocal: Optional[sessionmaker] = None


def _make_engine(dsn: str, *, pooled: bool = True) -> Engine:
    kwargs = {"echo": settings.DB_ECHO, "future": True, "pool_pre_ping": True}
    if pooled:
        kwargs.update(
            pool_size=settings.DB_POOL_SIZE, max_overflow=settings.DB_MAX_OVERFLOW
        )
    return create_engine(dsn, **kwargs)


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _make_engine(settings.runtime_dsn)
    return _engine


def get_ddl_engine() -> Engine:
    """Elevated engine for CREATE SCHEMA / CREATE TABLE / ALTER TABLE."""
    global _ddl_engine
    if _ddl_engine is None:
        _ddl_engine = _make_engine(settings.ddl_dsn, pooled=False)
    return _ddl_engine


def get_maintenance_engine() -> Engine:
    """Connects to the maintenance database — needed for CREATE DATABASE,
    which cannot execute inside a transaction block."""
    return create_engine(
        settings.maintenance_dsn, isolation_level="AUTOCOMMIT", future=True
    )


def get_sessionmaker() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(), autoflush=False, autocommit=False, expire_on_commit=False
        )
    return _SessionLocal


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a transactional session."""
    db = get_sessionmaker()()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context-managed session for background / CLI use."""
    db = get_sessionmaker()()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def reset_engines() -> None:
    """Drop cached engines — used by tests and after connection changes."""
    global _engine, _ddl_engine, _SessionLocal
    for eng in (_engine, _ddl_engine):
        if eng is not None:
            eng.dispose()
    _engine = _ddl_engine = _SessionLocal = None


def check_connection() -> dict:
    """Health probe against the target cluster."""
    try:
        with get_engine().connect() as conn:
            row = conn.execute(
                text(
                    "select current_database(), current_user, "
                    "current_setting('server_version')"
                )
            ).one()
        return {
            "connected": True,
            "database": row[0],
            "user": row[1],
            "server_version": row[2],
        }
    except Exception as exc:  # pragma: no cover - environment dependent
        return {"connected": False, "error": str(exc)}
