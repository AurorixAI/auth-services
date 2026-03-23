"""
ALGO V1 — Group & Membership Repositories  (Step 11)
"""

from __future__ import annotations

from typing import List, Optional
from uuid import UUID

from sqlalchemy import select

from src.models.auth import Group, StudentGroup
from src.repositories.base import BaseRepository


class GroupRepo(BaseRepository[Group]):
    model = Group

    def list_by_org(self, org_id: UUID, status: str = "active") -> List[Group]:
        stmt = select(Group).where(Group.org_id == org_id)
        if status:
            stmt = stmt.where(Group.status == status)
        return list(self.session.scalars(stmt))

    def list_by_teacher(self, teacher_id: UUID) -> List[Group]:
        stmt = select(Group).where(Group.teacher_id == teacher_id, Group.status == "active")
        return list(self.session.scalars(stmt))

    def count_students(self, group_id: UUID) -> int:
        stmt = select(StudentGroup).where(
            StudentGroup.group_id == group_id,
            StudentGroup.is_active.is_(True),
        )
        return len(list(self.session.scalars(stmt)))


class StudentGroupRepo(BaseRepository[StudentGroup]):
    model = StudentGroup

    def get_membership(self, student_id: UUID, group_id: UUID) -> Optional[StudentGroup]:
        stmt = select(StudentGroup).where(
            StudentGroup.student_id == student_id,
            StudentGroup.group_id == group_id,
        )
        return self.session.scalar(stmt)

    def list_active_students(self, group_id: UUID) -> List[StudentGroup]:
        stmt = select(StudentGroup).where(
            StudentGroup.group_id == group_id,
            StudentGroup.is_active.is_(True),
        )
        return list(self.session.scalars(stmt))

    def remove_student(self, student_id: UUID, group_id: UUID) -> bool:
        membership = self.get_membership(student_id, group_id)
        if not membership:
            return False
        from sqlalchemy.sql import func

        membership.is_active = False
        membership.left_at = func.now()
        self.session.flush()
        return True
