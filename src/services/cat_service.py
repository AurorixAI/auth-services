"""
ALGO V1 — CAT Session Service  (Step 14)

Extracted from src/api/cat_router.py
Manages Computerized Adaptive Testing session lifecycle.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from src.core.exceptions import ConflictError, NotFoundError
from src.repositories.analytics_repo import SessionSummaryRepo, AnswerEventRepo
from src.repositories.student_repo import DiagnosticInviteRepo


class CATService:
    def __init__(
        self,
        session_repo: SessionSummaryRepo,
        answer_event_repo: AnswerEventRepo,
        invite_repo: DiagnosticInviteRepo,
    ) -> None:
        self.session_repo = session_repo
        self.answer_event_repo = answer_event_repo
        self.invite_repo = invite_repo

    def init_session(self, invite_code: str) -> dict:
        """Initialize a new CAT session via an invite code."""
        invite = self.invite_repo.get_by_code(invite_code)
        if not invite:
            raise NotFoundError("DiagnosticInvite", invite_code)

        if invite.status not in ["pending", "confirmed"]:
            raise ConflictError("Invite is no longer valid or already used")

        # In a real implementation, we would call the IRT engine here
        # to select the first task.
        import uuid

        session_id = uuid.uuid4()

        # Start a session
        from src.models.analytics import SessionSummary

        summary = SessionSummary(
            session_id=session_id,
            student_id=invite.student_id,
            started_at=datetime.utcnow(),
            session_type=invite.diagnostic_type,
        )
        self.session_repo.add(summary)

        invite.status = "in_progress"
        invite.cat_session_id = session_id
        invite.started_at = datetime.utcnow()
        self.invite_repo.session.flush()

        return {
            "session_id": str(session_id),
            "student_id": str(invite.student_id),
            "status": "started",
        }

    def submit_answer(
        self, session_id: UUID, task_id: str, student_answer: str, time_spent_ms: int
    ) -> dict:
        """Submit an answer and get the next task or final result."""
        session = self.session_repo.get_by_id_or_raise(session_id, "CAT Session")

        if session.completed_at:
            raise ConflictError("Session is already completed")

        # In a real implementation, we would evaluate the answer via an engine,
        # save the AnswerEvent, update IRT Theta, and fetch the next task.

        return {
            "session_id": str(session_id),
            "status": "in_progress",
            "next_task": None,  # Mock
        }

    def terminate_session(self, session_id: UUID) -> dict:
        """Force terminate a session and calculate final theta."""
        session = self.session_repo.get_by_id_or_raise(session_id, "CAT Session")

        if session.completed_at:
            return session.to_dict()  # already done

        session.completed_at = datetime.utcnow()
        session.total_duration_ms = int(
            (session.completed_at - session.started_at).total_seconds() * 1000
        )
        self.session_repo.session.flush()

        return {"session_id": str(session_id), "status": "completed"}
