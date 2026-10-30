"""The database session of the work that runs AFTER the response (letters, outcomes of letters). Tests replace ``background_session``."""
from __future__ import annotations

from sqlalchemy.orm import Session

from src.core.database import SessionLocal


def background_session() -> Session:
    return SessionLocal()
