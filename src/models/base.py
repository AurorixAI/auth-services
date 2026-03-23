"""
ALGO V1 — SQLAlchemy declarative base.

Single source of truth for Base.  All ORM model files import from here:
    from src.models.base import Base
"""

from sqlalchemy.orm import declarative_base

Base = declarative_base()
