"""
ALGO V1 — Task Bank ORM Models

Tables: tasks_master, task_templates
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
    Index,
    CheckConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from src.models.base import Base


class TaskMaster(Base):
    """Банк задач"""

    __tablename__ = "tasks_master"

    id = Column(String(30), primary_key=True)
    skill_id = Column(String(30), ForeignKey("knowledge_hierarchy.id"), nullable=True)

    question_text = Column(Text, nullable=False)
    question_latex = Column(Text)
    question_image_url = Column(Text)

    answer_type = Column(String(20), nullable=False)
    correct_answer = Column(Text, nullable=False)
    correct_answer_latex = Column(Text)
    answer_tolerance = Column(Numeric(10, 6))
    answer_options = Column(JSONB)

    has_template = Column(Boolean, default=False)
    template_params = Column(JSONB)
    generation_function = Column(Text)

    # Сложность (SQL column name: 'difficulty', not 'difficulty_level')
    difficulty = Column(String(1), nullable=False, default="B")
    difficulty_score = Column(Numeric(3, 2))
    cognitive_load = Column(String(20))

    irt_discrimination = Column(Numeric(6, 4), default=1.0)
    irt_difficulty = Column(Numeric(6, 4), default=0.0)
    irt_guessing = Column(Numeric(6, 4), default=0.0)
    irt_calibrated = Column(Boolean, default=False)
    irt_sample_size = Column(Integer, default=0)

    solution_steps = Column(JSONB)
    solution_explanation = Column(Text)
    hints = Column(JSONB)
    common_mistakes = Column(JSONB)
    distractor_meta = Column(JSONB)
    sympy_solution = Column(Text)

    verification_status = Column(String(20), default="pending")
    verified_by = Column(String(100))
    verified_at = Column(DateTime)

    source_type = Column(String(20))
    source_reference = Column(Text)

    times_used = Column(Integer, default=0)
    times_correct = Column(Integer, default=0)
    avg_time_seconds = Column(Numeric(10, 2))

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # Расширения (миграция 009)
    toc_id = Column(Integer, ForeignKey("textbook_toc.id"))
    tags = Column(JSONB, default={})

    skill = relationship("KnowledgeHierarchy", back_populates="tasks")
    textbook_tasks = relationship("TextbookTask", back_populates="task")
    answer_events = relationship("AnswerEvent", back_populates="task")
    toc_entry = relationship("TextbookToc", back_populates="tasks")

    # Backward-compat property
    @property
    def difficulty_level(self):
        return self.difficulty

    __table_args__ = (
        CheckConstraint("difficulty IN ('A', 'B', 'C')"),
        CheckConstraint(
            "verification_status IN ('pending', 'verified', 'conflict', 'rejected', 'needs_review')"
        ),
        Index("idx_tm_skill", "skill_id"),
        Index("idx_tm_difficulty", "difficulty"),
        Index("idx_tm_active", "is_active"),
    )


class TaskTemplateDB(Base):
    """
    Кэшированные параметрические шаблоны задач.

    Заполняется при оцифровке (AI/методист) — содержит буквенную формулу,
    параметры и constraints.  SymPy использует эти данные только для
    подстановки новых чисел и верификации ответа.
    """

    __tablename__ = "task_templates"

    id = Column(String(30), primary_key=True)
    original_task_id = Column(String(30), ForeignKey("tasks_master.id"), nullable=False)
    skill_id = Column(String(30), ForeignKey("knowledge_hierarchy.id"), nullable=True)

    template_text = Column(Text, nullable=False)
    template_latex = Column(Text)

    parameters = Column(JSONB, nullable=False)
    constraints = Column(JSONB, nullable=False)

    answer_formula = Column(Text, nullable=False)

    generation_method = Column(String(20))
    quality_score = Column(Numeric(3, 2))
    tested_combinations = Column(Integer, default=0)
    successful_combinations = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())

    # Расширения из 012_variant_engine
    answer_formula_sympy = Column(Text)
    constraints_repr = Column(JSONB, default=[])
    task_type = Column(String(30), default="arithmetic")
    text_template = Column(Text)
    original_values = Column(JSONB, default={})
    param_details = Column(JSONB, default={})

    task = relationship("TaskMaster", backref="templates")
    skill = relationship("KnowledgeHierarchy")

    __table_args__ = (
        Index("idx_tt_skill", "skill_id"),
        Index("idx_tt_quality", quality_score.desc()),
        Index("idx_tt_task_type", "task_type"),
    )
