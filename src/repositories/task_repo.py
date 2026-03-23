"""
ALGO V1 — Task Repository  (Step 10)
"""

from __future__ import annotations

from typing import List, Optional, Set

from sqlalchemy import select

from src.models.tasks import TaskMaster
from src.repositories.base import BaseRepository

try:
    from src.models.dataclasses import TaskCandidate
    from src.domain.diagnostic.irt_engine import IRTTaskParams
except ImportError:
    TaskCandidate = None
    IRTTaskParams = None


class TaskRepo(BaseRepository[TaskMaster]):
    model = TaskMaster

    def get_tasks_for_skill(
        self,
        skill_id: str,
        difficulty: Optional[str] = None,
        limit: int = 10,
        exclude_ids: Optional[Set[str]] = None,
    ) -> List[TaskMaster]:
        stmt = select(TaskMaster).where(
            TaskMaster.skill_id == skill_id,
            TaskMaster.is_active.is_(True),
            TaskMaster.verification_status == "verified",
        )
        if difficulty:
            stmt = stmt.where(TaskMaster.difficulty == difficulty)
        if exclude_ids:
            stmt = stmt.where(~TaskMaster.id.in_(exclude_ids))

        stmt = stmt.order_by(TaskMaster.times_used).limit(limit)
        return list(self.session.scalars(stmt))

    def increment_usage(self, task_id: str, is_correct: bool) -> None:
        task = self.get_by_id(task_id)
        if task:
            task.times_used = (task.times_used or 0) + 1
            if is_correct:
                task.times_correct = (task.times_correct or 0) + 1
            self.session.flush()

    def get_all_active_tasks(
        self, exclude_ids: Optional[Set[str]] = None, limit: int = 500
    ) -> List[TaskMaster]:
        stmt = select(TaskMaster).where(
            TaskMaster.is_active.is_(True), TaskMaster.verification_status == "verified"
        )
        if exclude_ids:
            stmt = stmt.where(~TaskMaster.id.in_(exclude_ids))

        stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt))
