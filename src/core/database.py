"""
ALGO V1 — Database Session Factory  (moved from src/api/database.py)

Usage:
    # In FastAPI routes — use the Depends injection:
    from src.core.database import get_db
    def my_route(db: Session = Depends(get_db)): ...

    # In scripts / CLI / domain modules — use the context manager:
    from src.core.database import db_session
    with db_session() as db:
        result = db.query(...)
"""

from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.core.config import get_settings
from src.core.db_timezone import utc_connect_args


def _make_engine():
    s = get_settings()
    return create_engine(
        s.database_url,
        echo=s.debug,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        connect_args=utc_connect_args(s.database_url),  # session TimeZone=UTC (ROADMAP #49)
    )


engine = _make_engine()

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)


def get_db():
    """
    FastAPI dependency — yields a scoped SQLAlchemy Session.

    Rolls back automatically on any exception, closes session in all cases.
    """
    db: Session = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def db_session():
    """
    Synchronous context manager for use outside FastAPI.

    Example:
        with db_session() as db:
            repo = KnowledgeRepository(db)
            items = repo.get_all_skills()
    """
    db: Session = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
