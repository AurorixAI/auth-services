"""
ALGO V1 — Student Service  (Step 13)

Extracted from src/auth/service.py — student management operations.
"""

from __future__ import annotations

from typing import List, Optional
from uuid import UUID, uuid4

from src.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from src.models.auth import DiagnosticInvite, Student
from src.repositories.student_repo import DiagnosticInviteRepo, StudentRepo, StudentDiagnosticResultRepo


class StudentService:
    def __init__(
        self,
        student_repo: StudentRepo,
        invite_repo: DiagnosticInviteRepo,
        result_repo: StudentDiagnosticResultRepo,
    ) -> None:
        self.student_repo = student_repo
        self.invite_repo = invite_repo
        self.result_repo = result_repo

    def create_student(
        self,
        *,
        org_id: UUID,
        first_name: str,
        last_name: str,
        class_level: int,
        student_phone: str,
        student_number: str = None,
        comment: str = None,
        group_id: UUID = None,
        created_by: UUID = None,
    ) -> dict:
        student = Student(
            org_id=org_id,
            first_name=first_name,
            last_name=last_name,
            class_level=class_level,
            student_phone=student_phone,
            student_number=student_number,
            comment=comment,
            group_id=group_id,
            created_by=created_by,
            status="new",
        )
        self.student_repo.add(student)
        return student.to_dict()

    def get_student(self, student_id: UUID, org_id: UUID) -> dict:
        student = self.student_repo.get_by_id_or_raise(student_id, "Student")
        if student.org_id != org_id:
            raise ForbiddenError("Student does not belong to your organization")
        return student.to_dict()

    def list_students(self, org_id: UUID, status: str = None) -> List[dict]:
        return [s.to_dict() for s in self.student_repo.list_by_org(org_id, status)]

    def update_student(self, student_id: UUID, org_id: UUID, **updates) -> dict:
        student = self.student_repo.get_by_id_or_raise(student_id, "Student")
        if student.org_id != org_id:
            raise ForbiddenError("Student does not belong to your organization")
        for field, value in updates.items():
            if hasattr(student, field) and value is not None:
                setattr(student, field, value)
        self.student_repo.session.flush()
        return student.to_dict()

    def delete_student(self, student_id: UUID, org_id: UUID) -> None:
        student = self.student_repo.get_by_id_or_raise(student_id, "Student")
        if student.org_id != org_id:
            raise ForbiddenError("Student does not belong to your organization")
        self.student_repo.delete(student)

    def create_diagnostic_invite(
        self,
        *,
        org_id: UUID,
        student_id: UUID,
        class_level: int,
        created_by: UUID,
        diagnostic_type: str = "initial",
        expires_in_hours: int = 48,
    ) -> dict:
        from datetime import datetime, timedelta

        # Check no active invite already exists
        existing = self.invite_repo.get_active_by_student(student_id)
        if existing:
            raise ConflictError("Student already has an active diagnostic invite")

        code = _gen_invite_code()
        expires_at = datetime.utcnow() + timedelta(hours=expires_in_hours)

        invite = DiagnosticInvite(
            org_id=org_id,
            student_id=student_id,
            created_by=created_by,
            code=code,
            class_level=class_level,
            diagnostic_type=diagnostic_type,
            status="pending",
            expires_at=expires_at,
        )
        self.invite_repo.add(invite)
        return invite.to_dict()

    def get_student_results(self, student_id: UUID) -> List[dict]:
        return [r.to_dict() for r in self.result_repo.list_by_student(student_id)]


def _gen_invite_code(length: int = 8) -> str:
    import random, string
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))
