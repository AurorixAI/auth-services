"""
ALGO V1 — Analytics Service  (Step 16)

Extracted from src/api/analytics_router.py
Provides dashboards and data for students, groups, and organizations.
"""

from __future__ import annotations

from typing import List, Optional
from uuid import UUID

from src.core.exceptions import NotFoundError
from src.repositories.analytics_repo import SessionSummaryRepo
from src.repositories.student_repo import StudentRepo


class AnalyticsService:
    def __init__(
        self,
        session_repo: SessionSummaryRepo,
        student_repo: StudentRepo,
    ) -> None:
        self.session_repo = session_repo
        self.student_repo = student_repo

    def get_student_dashboard(self, student_id: UUID, org_id: UUID) -> dict:
        student = self.student_repo.get_by_id_or_raise(student_id, "Student")
        if student.org_id != org_id:
            raise NotFoundError("Student", str(student_id))
            
        sessions = self.session_repo.get_student_sessions(student_id)
        return {
            "student_id": str(student_id),
            "recent_sessions": [s.to_dict() for s in sessions if hasattr(s, 'to_dict')]
        }

    def get_group_dashboard(self, group_id: UUID, org_id: UUID) -> dict:
        return {"group_id": str(group_id), "metrics": {}}

    def get_org_dashboard(self, org_id: UUID) -> dict:
        return {"org_id": str(org_id), "metrics": {}}
