"""
ALGO V1 — Exam Service  (Step 15)

Extracted from src/api/exam_router.py
Manages generated exams.
"""

from __future__ import annotations

from uuid import UUID

# from src.models.exam import GeneratedExam, ExamSession
# from src.repositories.exam_repo import ExamRepo


class ExamService:
    def __init__(self, session) -> None:
        self.session = session
        # self.exam_repo = ExamRepo(session)

    def generate_exam(self, spec: dict) -> dict:
        """Generate a new exam from textbook structure or skills."""
        # Mock logic
        return {"exam_id": "mock-exam-id", "tasks": []}

    def get_exam(self, exam_id: str) -> dict:
        """Fetch exam details."""
        return {"exam_id": exam_id, "status": "ready"}

    def submit_exam(self, session_id: UUID, answers: dict) -> dict:
        """Submit all answers for a linear exam."""
        return {"session_id": str(session_id), "score": 100}
