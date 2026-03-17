"""
ALGO V1 - Exam Models
SQLAlchemy models for generated exams and exam sessions
"""

from datetime import datetime
from sqlalchemy import (
    Column, String, Integer, Text, ForeignKey,
    DateTime, Index,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func

from src.models import Base


class GeneratedExam(Base):
    """Сгенерированный экзамен (учителем)"""
    __tablename__ = 'generated_exams'

    id = Column(String(50), primary_key=True)           # EXAM_{hex8}
    org_id = Column(String(50), nullable=True)
    teacher_id = Column(String(50), nullable=True)
    teacher_name = Column(String(200), nullable=True)

    title = Column(String(300), nullable=False)
    access_code = Column(String(20), unique=True, nullable=True, index=True)  # null = draft, set on publish
    subject = Column(String(100), default="Математика")

    # Content stored as JSONB
    variants = Column(JSONB, nullable=False, default=[])       # List of variant dicts with tasks
    settings = Column(JSONB, nullable=False, default={})       # time_minutes, shuffle_tasks, etc.
    skills_covered = Column(JSONB, nullable=False, default=[]) # List of skill_ids

    status = Column(String(20), default="draft")  # draft, active, closed
    total_time_minutes = Column(Integer, default=45)
    variants_count = Column(Integer, default=1)
    tasks_per_variant = Column(Integer, default=10)

    created_at = Column(DateTime, default=func.now())
    expires_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index('ix_generated_exams_org', 'org_id'),
        Index('ix_generated_exams_teacher', 'teacher_id'),
        Index('ix_generated_exams_code', 'access_code', unique=True),
    )


class ExamSession(Base):
    """Сессия прохождения экзамена учеником"""
    __tablename__ = 'exam_sessions'

    id = Column(String(50), primary_key=True)            # SES_{hex8}
    exam_id = Column(String(50), ForeignKey('generated_exams.id'), nullable=False)

    student_name = Column(String(200), nullable=False)
    class_level = Column(Integer, nullable=True)
    variant_number = Column(Integer, default=1)

    # Answers stored as JSONB: { "task_id": "student_answer", ... }
    answers = Column(JSONB, nullable=False, default={})

    # Scoring
    score = Column(Integer, default=0)
    max_score = Column(Integer, default=0)
    answered_count = Column(Integer, default=0)
    correct_count = Column(Integer, default=0)

    status = Column(String(20), default="in_progress")  # in_progress, submitted
    started_at = Column(DateTime, default=func.now())
    submitted_at = Column(DateTime, nullable=True)
    time_spent_seconds = Column(Integer, default=0)

    __table_args__ = (
        Index('ix_exam_sessions_exam', 'exam_id'),
        Index('ix_exam_sessions_status', 'status'),
    )
