"""
ALGO V1 — Student & Diagnostic Repositories  (Step 11)
"""

from __future__ import annotations

from typing import List, Optional
from uuid import UUID

from sqlalchemy import select

from src.models.auth import DiagnosticInvite, Student, StudentDiagnosticResult
from src.repositories.base import BaseRepository


class StudentRepo(BaseRepository[Student]):
    model = Student

    def list_by_org(self, org_id: UUID, status: str = None) -> List[Student]:
        stmt = select(Student).where(Student.org_id == org_id)
        if status:
            stmt = stmt.where(Student.status == status)
        return list(self.session.scalars(stmt))

    def list_by_group(self, group_id: UUID) -> List[Student]:
        stmt = select(Student).where(Student.group_id == group_id)
        return list(self.session.scalars(stmt))

    def count_by_org(self, org_id: UUID) -> int:
        stmt = select(Student).where(Student.org_id == org_id)
        return len(list(self.session.scalars(stmt)))

    def set_status(self, student: Student, status: str) -> Student:
        student.status = status
        self.session.flush()
        return student


class DiagnosticInviteRepo(BaseRepository[DiagnosticInvite]):
    model = DiagnosticInvite

    def get_by_code(self, code: str) -> Optional[DiagnosticInvite]:
        stmt = select(DiagnosticInvite).where(DiagnosticInvite.code == code)
        return self.session.scalar(stmt)

    def get_active_by_student(self, student_id: UUID) -> Optional[DiagnosticInvite]:
        stmt = select(DiagnosticInvite).where(
            DiagnosticInvite.student_id == student_id,
            DiagnosticInvite.status.in_(["pending", "confirmed", "in_progress"]),
        )
        return self.session.scalar(stmt)

    def list_by_org(self, org_id: UUID) -> List[DiagnosticInvite]:
        stmt = select(DiagnosticInvite).where(DiagnosticInvite.org_id == org_id)
        return list(self.session.scalars(stmt))


class StudentDiagnosticResultRepo(BaseRepository[StudentDiagnosticResult]):
    model = StudentDiagnosticResult

    def get_latest_for_student(self, student_id: UUID) -> Optional[StudentDiagnosticResult]:
        stmt = (
            select(StudentDiagnosticResult)
            .where(StudentDiagnosticResult.student_id == student_id)
            .order_by(StudentDiagnosticResult.diagnosed_at.desc())
            .limit(1)
        )
        return self.session.scalar(stmt)

    def list_by_student(self, student_id: UUID) -> List[StudentDiagnosticResult]:
        stmt = (
            select(StudentDiagnosticResult)
            .where(StudentDiagnosticResult.student_id == student_id)
            .order_by(StudentDiagnosticResult.diagnosed_at.desc())
        )
        return list(self.session.scalars(stmt))

    def list_by_org(self, org_id: UUID) -> List[StudentDiagnosticResult]:
        stmt = select(StudentDiagnosticResult).where(StudentDiagnosticResult.org_id == org_id)
        return list(self.session.scalars(stmt))
