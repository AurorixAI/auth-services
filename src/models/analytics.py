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
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from src.models.base import Base


class AnswerEvent(Base):
    """Полная история ответов для ML"""

    __tablename__ = "answer_events"

    id = Column(BigInteger, primary_key=True)

    session_id = Column(UUID(as_uuid=True), nullable=False)
    student_id = Column(UUID(as_uuid=True))

    task_id = Column(String(30), ForeignKey("tasks_master.id"), nullable=False)
    skill_id = Column(String(30), ForeignKey("knowledge_hierarchy.id"), nullable=False)

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

    task = relationship("TaskMaster", back_populates="answer_events")
    skill = relationship("KnowledgeHierarchy")

    __table_args__ = (
        Index("idx_ae_session", "session_id"),
        Index("idx_ae_student", "student_id"),
        Index("idx_ae_skill", "skill_id"),
        Index("idx_ae_submitted", "submitted_at"),
    )


class SkillCorrelation(Base):
    """Обнаруженные корреляции между навыками"""

    __tablename__ = "skill_correlations"

    id = Column(Integer, primary_key=True)

    skill_a_id = Column(
        String(30), ForeignKey("knowledge_hierarchy.id"), nullable=False
    )
    skill_b_id = Column(
        String(30), ForeignKey("knowledge_hierarchy.id"), nullable=False
    )

    correlation_coefficient = Column(Numeric(6, 4), nullable=False)
    p_value = Column(Numeric(10, 8))
    sample_size = Column(Integer, nullable=False)

    relationship_type = Column(String(20), nullable=False)
    direction = Column(String(10))

    strength = Column(Numeric(4, 2))
    confidence = Column(Numeric(4, 3))

    discovery_method = Column(String(30), nullable=False)

    is_validated = Column(Boolean, default=False)
    validated_by = Column(String(100))
    validated_at = Column(DateTime)

    discovered_at = Column(DateTime, default=func.now())
    last_updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    skill_a = relationship("KnowledgeHierarchy", foreign_keys=[skill_a_id])
    skill_b = relationship("KnowledgeHierarchy", foreign_keys=[skill_b_id])

    __table_args__ = (
        CheckConstraint(
            "relationship_type IN ('prerequisite', 'corequisite', 'diagnostic', 'transfer')"
        ),
        CheckConstraint(
            "discovery_method IN ('expert_defined', 'statistical', 'ml_discovered', 'pattern_mining')"
        ),
        UniqueConstraint("skill_a_id", "skill_b_id", "relationship_type"),
        Index("idx_sc_skill_a", "skill_a_id"),
        Index("idx_sc_skill_b", "skill_b_id"),
    )


class ErrorPattern(Base):
    """Паттерны ошибок"""

    __tablename__ = "error_patterns"

    id = Column(Integer, primary_key=True)
    skill_id = Column(String(30), ForeignKey("knowledge_hierarchy.id"), nullable=False)

    pattern_type = Column(String(30), nullable=False)
    pattern_description = Column(Text, nullable=False)
    error_examples = Column(JSONB, default=[])

    root_cause_skills = Column(JSONB, default=[])

    occurrence_count = Column(Integer, default=0)
    occurrence_rate = Column(Numeric(5, 4))
    recovery_rate = Column(Numeric(5, 4))

    remediation_suggestions = Column(JSONB, default=[])
    related_tasks = Column(JSONB, default=[])

    discovered_at = Column(DateTime, default=func.now())
    last_seen_at = Column(DateTime, default=func.now())

    skill = relationship("KnowledgeHierarchy")

    __table_args__ = (
        CheckConstraint(
            "pattern_type IN ('sign_error', 'order_of_operations', 'formula_confusion', 'concept_gap', 'calculation_error', 'careless_mistake', 'time_pressure', 'partial_knowledge', 'misconception', 'other')"
        ),
        Index("idx_ep_skill", "skill_id"),
        Index("idx_ep_type", "pattern_type"),
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
    skill_id = Column(String(30), ForeignKey("knowledge_hierarchy.id"), nullable=False)

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

    skill = relationship("KnowledgeHierarchy")

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


class SkillResponseTime(Base):
    """Статистика времени ответов по навыкам"""

    __tablename__ = "skill_response_times"

    skill_id = Column(
        String(30), ForeignKey("knowledge_hierarchy.id"), primary_key=True
    )
    difficulty_level = Column(String(1), primary_key=True)

    sample_size = Column(Integer, default=0)
    mean_time_ms = Column(Integer)
    median_time_ms = Column(Integer)
    std_dev_ms = Column(Integer)
    percentile_10 = Column(Integer)
    percentile_25 = Column(Integer)
    percentile_75 = Column(Integer)
    percentile_90 = Column(Integer)
    percentile_99 = Column(Integer)

    min_realistic_time_ms = Column(Integer)

    mean_time_correct_ms = Column(Integer)
    mean_time_incorrect_ms = Column(Integer)

    last_calculated_at = Column(DateTime, default=func.now())

    skill = relationship("KnowledgeHierarchy")

    __table_args__ = (CheckConstraint("difficulty_level IN ('A', 'B', 'C')"),)
