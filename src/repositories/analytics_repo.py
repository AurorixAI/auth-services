"""
ALGO V1 — Analytics Repositories  (Step 10)

AnswerEvent, SessionSummary, StudentSkillState, SkillCorrelation
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from sqlalchemy import desc, or_, select

from src.models.analytics import (
    AnswerEvent, SessionSummary, SkillCorrelation, StudentSkillState
)
from src.repositories.base import BaseRepository


class AnswerEventRepo(BaseRepository[AnswerEvent]):
    model = AnswerEvent

    def get_events_for_session(self, session_id: UUID) -> List[AnswerEvent]:
        stmt = select(AnswerEvent).where(
            AnswerEvent.session_id == session_id
        ).order_by(AnswerEvent.submitted_at)
        return list(self.session.scalars(stmt))

    def get_events_for_student(self, student_id: UUID, limit: int = 100) -> List[AnswerEvent]:
        stmt = select(AnswerEvent).where(
            AnswerEvent.student_id == student_id
        ).order_by(desc(AnswerEvent.submitted_at)).limit(limit)
        return list(self.session.scalars(stmt))


class SessionSummaryRepo(BaseRepository[SessionSummary]):
    model = SessionSummary

    def get_student_sessions(self, student_id: UUID, limit: int = 10) -> List[SessionSummary]:
        stmt = select(SessionSummary).where(
            SessionSummary.student_id == student_id
        ).order_by(desc(SessionSummary.started_at)).limit(limit)
        return list(self.session.scalars(stmt))


class StudentSkillRepo(BaseRepository[StudentSkillState]):
    model = StudentSkillState

    def get_skill_state(self, student_id: UUID, skill_id: str) -> Optional[StudentSkillState]:
        stmt = select(StudentSkillState).where(
            StudentSkillState.student_id == student_id,
            StudentSkillState.skill_id == skill_id,
        )
        return self.session.scalar(stmt)

    def get_all_states(self, student_id: UUID) -> List[StudentSkillState]:
        stmt = select(StudentSkillState).where(
            StudentSkillState.student_id == student_id
        )
        return list(self.session.scalars(stmt))

    def upsert_state(
        self,
        student_id: UUID,
        skill_id: str,
        mastery_probability: float,
        theta: Optional[float] = None,
        theta_se: Optional[float] = None,
    ) -> StudentSkillState:
        state = self.get_skill_state(student_id, skill_id)
        if not state:
            state = StudentSkillState(
                student_id=student_id,
                skill_id=skill_id,
                mastery_probability=mastery_probability
            )
            self.session.add(state)
        else:
            state.mastery_probability = mastery_probability
        
        if theta is not None:
            state.theta = theta
        if theta_se is not None:
            state.theta_se = theta_se
            
        self.session.flush()
        return state


class CorrelationRepo(BaseRepository[SkillCorrelation]):
    model = SkillCorrelation

    def get_strong_correlations(self, min_correlation: float = 0.5, limit: int = 100) -> List[SkillCorrelation]:
        from sqlalchemy import func
        stmt = select(SkillCorrelation).where(
            func.abs(SkillCorrelation.correlation_coefficient) >= min_correlation
        ).order_by(
            desc(func.abs(SkillCorrelation.correlation_coefficient))
        ).limit(limit)
        return list(self.session.scalars(stmt))
