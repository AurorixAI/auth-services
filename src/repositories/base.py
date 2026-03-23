"""
ALGO V1 — Generic Repository Base

All concrete repositories inherit from BaseRepository[ModelT].

Usage:
    class TaskRepo(BaseRepository[TaskMaster]):
        model = TaskMaster

        def get_by_skill(self, skill_id: str) -> list[TaskMaster]:
            return list(self.session.scalars(
                select(self.model).where(self.model.skill_id == skill_id)
            ))
"""

from __future__ import annotations

from typing import Generic, List, Optional, Type, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

ModelT = TypeVar("ModelT")


class BaseRepository(Generic[ModelT]):
    """
    Generic session-scoped repository.

    Each subclass must declare:
        model: Type[ModelT] = MyORMModel

    All methods use self.session — passed in at construction time.
    Never calls session.commit(); the caller (service or test) is responsible
    for transaction boundaries.
    """

    model: Type[ModelT]

    def __init__(self, session: Session) -> None:
        self.session = session

    # ── Read ──────────────────────────────────────────────────────────────

    def get_by_id(self, pk) -> Optional[ModelT]:
        """Return model instance by primary key, or None."""
        return self.session.get(self.model, pk)

    def get_by_id_or_raise(self, pk, label: str = None) -> ModelT:
        """Return model instance by primary key, raise NotFoundError if missing."""
        from src.core.exceptions import NotFoundError

        obj = self.session.get(self.model, pk)
        if obj is None:
            name = label or getattr(self.model, "__tablename__", "resource")
            raise NotFoundError(name, str(pk))
        return obj

    def list_all(self, limit: int = 100, offset: int = 0) -> List[ModelT]:
        """Return a pageable slice of all rows."""
        stmt = select(self.model).limit(limit).offset(offset)
        return list(self.session.scalars(stmt))

    def exists(self, pk) -> bool:
        """Return True if a row with this primary key exists."""
        return self.session.get(self.model, pk) is not None

    # ── Write ─────────────────────────────────────────────────────────────

    def add(self, obj: ModelT) -> ModelT:
        """Persist a new model instance. Flushes (but does not commit)."""
        self.session.add(obj)
        self.session.flush()
        return obj

    def delete(self, obj: ModelT) -> None:
        """Delete a model instance. Flushes (but does not commit)."""
        self.session.delete(obj)
        self.session.flush()

    def delete_by_id(self, pk) -> bool:
        """Delete by primary key. Returns True if the row existed."""
        obj = self.get_by_id(pk)
        if obj is None:
            return False
        self.delete(obj)
        return True
