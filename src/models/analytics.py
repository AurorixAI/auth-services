"""
ALGO V1 — Analytics & Session ORM Models

Tables: answer_events, skill_correlations, error_patterns,
        session_summaries, student_skill_states, skill_response_times
"""

from sqlalchemy import (
    Column,
    String,
    Integer,
    Boolean,
    Text,
    ForeignKey,
    DateTime,
    Numeric,
    BigInteger,
    Index,
    CheckConstraint,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func

from src.models.base import Base


class AnswerEvent(Base):
    """Полная история ответов для ML"""

    __tablename__ = "answer_events"

    id = Column(BigInteger, primary_key=True)

    session_id = Column(UUID(as_uuid=True), nullable=False)
    student_id = Column(UUID(as_uuid=True))

    task_id = Column(String(30), nullable=False)  # no FK — tasks_master lives in content DB
    skill_id = Column(String(30), nullable=False)  # no FK — content DB is separate

    is_correct = Column(Boolean, nullable=False)
    student_answer = Column(Text)
    correct_answer = Column(Text)

    time_spent_ms = Column(Integer, nullable=False)
    started_at = Column(DateTime, nullable=False)
    submitted_at = Column(DateTime, nullable=False, default=func.now())

    difficulty_level = Column(String(1))
    task_order_in_session = Column(Integer)

    task_irt_a = Column(Numeric(6, 4))
    task_irt_b = Column(Numeric(6, 4))
    task_irt_c = Column(Numeric(6, 4))

    student_theta_before = Column(Numeric(6, 4))
    student_theta_after = Column(Numeric(6, 4))

    error_type = Column(String(50))
    error_explanation = Column(Text)
    matched_distractor = Column(Text)

    device_type = Column(String(20))
    client_version = Column(String(20))

    org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"))

    __table_args__ = (
        Index("idx_ae_session", "session_id"),
        Index("idx_ae_student", "student_id"),
        Index("idx_ae_skill", "skill_id"),
        Index("idx_ae_submitted", "submitted_at"),
    )


class SessionSummary(Base):
    """Сводка по диагностическим сессиям"""

    __tablename__ = "session_summaries"

    session_id = Column(UUID(as_uuid=True), primary_key=True)
    student_id = Column(UUID(as_uuid=True))

    started_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime)
    total_duration_ms = Column(Integer)

    total_tasks = Column(Integer, default=0)
    correct_answers = Column(Integer, default=0)

    final_theta = Column(Numeric(6, 4))
    confidence_interval_low = Column(Numeric(6, 4))
    confidence_interval_high = Column(Numeric(6, 4))

    identified_gaps = Column(JSONB, default=[])
    root_gaps = Column(JSONB, default=[])

    recommended_skills = Column(JSONB, default=[])
    priority_order = Column(JSONB, default=[])

    session_type = Column(String(20), default="diagnostic")

    platform = Column(String(20))
    client_version = Column(String(20))

    created_at = Column(DateTime, default=func.now())

    org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"))
    integrity_report = Column(JSONB)
    error_patterns = Column(JSONB)

    __table_args__ = (
        CheckConstraint(
            "session_type IN ('diagnostic', 'quick_check', 'practice', 'exam')"
        ),
        Index("idx_ss_student", "student_id"),
        Index("idx_ss_started", "started_at"),
    )


class StudentSkillState(Base):
    """Байесовское состояние навыков ученика"""

    __tablename__ = "student_skill_states"

    id = Column(Integer, primary_key=True)
    student_id = Column(UUID(as_uuid=True), nullable=False)
    skill_id = Column(String(30), nullable=False)  # FK removed — content DB is separate

    mastery_probability = Column(Numeric(5, 4), nullable=False, default=0.5)
    learn_rate = Column(Numeric(5, 4), default=0.1)
    guess_rate = Column(Numeric(5, 4), default=0.2)
    slip_rate = Column(Numeric(5, 4), default=0.1)

    theta = Column(Numeric(6, 4))
    theta_se = Column(Numeric(6, 4))

    total_attempts = Column(Integer, default=0)
    correct_attempts = Column(Integer, default=0)
    last_attempt_at = Column(DateTime)
    last_correct_at = Column(DateTime)

    trend = Column(String(10))
    consecutive_correct = Column(Integer, default=0)
    consecutive_incorrect = Column(Integer, default=0)

    status = Column(String(20), default="unknown")

    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"))

    __table_args__ = (
        CheckConstraint("trend IN ('improving', 'stable', 'declining', 'unknown')"),
        CheckConstraint(
            "status IN ('mastered', 'learning', 'struggling', 'gap', 'unknown')"
        ),
        UniqueConstraint("student_id", "skill_id"),
        Index("idx_sss_student", "student_id"),
        Index("idx_sss_skill", "skill_id"),
        Index("idx_sss_status", "status"),
    )
