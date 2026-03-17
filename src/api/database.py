"""
ALGO V1 — Centralised database configuration.

Single source of truth for engine, SessionLocal, and the ``get_db``
FastAPI dependency.  Every router imports ``get_db`` from here instead
of maintaining its own ``set_db_dependency`` boilerplate.
"""

from __future__ import annotations

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:password@localhost:5432/algo_v1",
)

DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"

engine = create_engine(DATABASE_URL, echo=DEBUG)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """FastAPI dependency — yields a DB session and ensures cleanup."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
