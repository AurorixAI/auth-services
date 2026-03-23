"""
ALGO V1 — Group Service  (Step 13)

Extracted from src/auth/service.py — group management operations.
"""

from __future__ import annotations

from typing import List
from uuid import UUID

from src.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from src.models.auth import Group, StudentGroup
from src.repositories.group_repo import GroupRepo, StudentGroupRepo
from src.repositories.student_repo import StudentRepo


class GroupService:
    def __init__(
        self,
        group_repo: GroupRepo,
        student_group_repo: StudentGroupRepo,
        student_repo: StudentRepo,
    ) -> None:
        self.group_repo = group_repo
        self.student_group_repo = student_group_repo
        self.student_repo = student_repo

    def create_group(
        self,
        *,
        org_id: UUID,
        name: str,
        class_level: int = None,
        teacher_id: UUID = None,
        description: str = None,
        max_students: int = 30,
    ) -> dict:
        group = Group(
            org_id=org_id,
            name=name,
            class_level=class_level,
            teacher_id=teacher_id,
            description=description,
            max_students=max_students,
        )
        self.group_repo.add(group)
        return group.to_dict()

    def get_group(self, group_id: UUID, org_id: UUID) -> dict:
        group = self.group_repo.get_by_id_or_raise(group_id, "Group")
        if group.org_id != org_id:
            raise ForbiddenError("Group does not belong to your organization")
        return group.to_dict()

    def list_groups(self, org_id: UUID) -> List[dict]:
        return [g.to_dict() for g in self.group_repo.list_by_org(org_id)]

    def list_teacher_groups(self, teacher_id: UUID) -> List[dict]:
        return [g.to_dict() for g in self.group_repo.list_by_teacher(teacher_id)]

    def delete_group(self, group_id: UUID, org_id: UUID) -> None:
        group = self.group_repo.get_by_id_or_raise(group_id, "Group")
        if group.org_id != org_id:
            raise ForbiddenError("Group does not belong to your organization")
        group.status = "archived"
        self.group_repo.session.flush()

    def add_student(self, group_id: UUID, student_id: UUID, org_id: UUID) -> dict:
        group = self.group_repo.get_by_id_or_raise(group_id, "Group")
        if group.org_id != org_id:
            raise ForbiddenError("Group does not belong to your organization")

        existing = self.student_group_repo.get_membership(student_id, group_id)
        if existing and existing.is_active:
            raise ConflictError("Student is already in this group")

        if existing:
            # Re-activate
            existing.is_active = True
            existing.left_at = None
            self.student_group_repo.session.flush()
            return {"student_id": str(student_id), "group_id": str(group_id), "status": "re-added"}

        membership = StudentGroup(student_id=student_id, group_id=group_id)
        self.student_group_repo.add(membership)
        return {"student_id": str(student_id), "group_id": str(group_id), "status": "added"}

    def remove_student(self, group_id: UUID, student_id: UUID, org_id: UUID) -> None:
        group = self.group_repo.get_by_id_or_raise(group_id, "Group")
        if group.org_id != org_id:
            raise ForbiddenError("Group does not belong to your organization")
        removed = self.student_group_repo.remove_student(student_id, group_id)
        if not removed:
            raise NotFoundError("StudentGroup", str(student_id))

    def list_group_students(self, group_id: UUID, org_id: UUID) -> List[dict]:
        group = self.group_repo.get_by_id_or_raise(group_id, "Group")
        if group.org_id != org_id:
            raise ForbiddenError("Group does not belong to your organization")
        return [
            s.to_dict()
            for s in self.student_repo.list_by_group(group_id)
        ]
