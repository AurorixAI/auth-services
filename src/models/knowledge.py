"""
ALGO V1 — Knowledge & Skill ORM Models

Tables: knowledge_hierarchy, skill_prerequisites, skill_benchmarks
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
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from src.models.base import Base


class KnowledgeHierarchy(Base):
    """
    Иерархия знаний (L1-L4)
    L1: Domain (Алгебра, Геометрия, Арифметика)
    L2: Cluster (Уравнения, Неравенства, Функции)
    L3: Topic (Линейные уравнения, Квадратные уравнения)
    L4: Atomic Skill (Вычисление дискриминанта, Перенос слагаемых)
    """

    __tablename__ = "knowledge_hierarchy"

    id = Column(String(30), primary_key=True)
    level = Column(String(2), nullable=False)
    parent_id = Column(String(30), ForeignKey("knowledge_hierarchy.id"))

    name = Column(String(200), nullable=False)
    name_ru = Column(String(200), nullable=False)
    name_uz = Column(String(200))

    description = Column(Text)
    class_level_start = Column(Integer)
    class_level_end = Column(Integer)
    importance = Column(Integer, default=5)
    sequence_order = Column(Integer, default=0)

    is_composite = Column(Boolean, default=False)
    composite_type = Column(String(20))
    decomposition_notes = Column(Text)

    cognitive_type = Column(String(20))
    typical_time_seconds = Column(Integer)

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # Расширения (миграция 010)
    origin = Column(String(20), default="base")
    is_advanced = Column(Boolean, default=False)
    source_textbook_id = Column(UUID(as_uuid=True), ForeignKey("textbooks.textbook_id"))

    parent = relationship("KnowledgeHierarchy", remote_side=[id], backref="children")
    prerequisites = relationship(
        "SkillPrerequisite",
        foreign_keys="SkillPrerequisite.skill_id",
        back_populates="skill",
    )
    dependent_skills = relationship(
        "SkillPrerequisite",
        foreign_keys="SkillPrerequisite.prerequisite_id",
        back_populates="prerequisite",
    )
    tasks = relationship("TaskMaster", back_populates="skill")
    benchmarks = relationship("SkillBenchmark", back_populates="skill", uselist=False)

    __table_args__ = (
        CheckConstraint("level IN ('L1', 'L2', 'L3', 'L4')"),
        CheckConstraint("importance BETWEEN 1 AND 10"),
        CheckConstraint(
            "cognitive_type IN ('recall', 'apply', 'analyze', 'evaluate', 'create')"
        ),
        CheckConstraint("composite_type IN ('sequential', 'parallel', 'conditional')"),
        Index("idx_kh_level", "level"),
        Index("idx_kh_parent", "parent_id"),
        Index("idx_kh_class_level", "class_level_start", "class_level_end"),
    )


class SkillPrerequisite(Base):
    """Граф зависимостей навыков"""

    __tablename__ = "skill_prerequisites"

    id = Column(Integer, primary_key=True)
    skill_id = Column(String(30), ForeignKey("knowledge_hierarchy.id"), nullable=False)
    prerequisite_id = Column(
        String(30), ForeignKey("knowledge_hierarchy.id"), nullable=False
    )

    dependency_type = Column(String(10), nullable=False)
    criticality = Column(Integer, default=5)
    relationship_description = Column(Text)

    weight = Column(Numeric(4, 2), default=1.0)
    learned_weight = Column(Numeric(4, 2))
    confidence = Column(Numeric(4, 3), default=1.0)
    sample_size = Column(Integer, default=0)
    discovery_source = Column(String(20), default="expert")
    last_validated_at = Column(DateTime)

    created_at = Column(DateTime, default=func.now())

    skill = relationship(
        "KnowledgeHierarchy", foreign_keys=[skill_id], back_populates="prerequisites"
    )
    prerequisite = relationship(
        "KnowledgeHierarchy",
        foreign_keys=[prerequisite_id],
        back_populates="dependent_skills",
    )

    __table_args__ = (
        CheckConstraint("dependency_type IN ('hard', 'soft')"),
        CheckConstraint("criticality BETWEEN 1 AND 10"),
        CheckConstraint("skill_id != prerequisite_id"),
        CheckConstraint("discovery_source IN ('expert', 'statistical', 'ml')"),
        UniqueConstraint("skill_id", "prerequisite_id"),
        Index("idx_sp_skill", "skill_id"),
        Index("idx_sp_prereq", "prerequisite_id"),
    )


class SkillBenchmark(Base):
    """Статистика навыков для автокалибровки"""

    __tablename__ = "skill_benchmarks"

    skill_id = Column(
        String(30), ForeignKey("knowledge_hierarchy.id"), primary_key=True
    )

    total_attempts = Column(Integer, default=0)
    total_correct = Column(Integer, default=0)
    avg_solve_time_seconds = Column(Numeric(10, 2))

    success_rate_grade_5 = Column(Numeric(5, 2))
    success_rate_grade_6 = Column(Numeric(5, 2))
    success_rate_grade_7 = Column(Numeric(5, 2))
    success_rate_grade_8 = Column(Numeric(5, 2))
    success_rate_grade_9 = Column(Numeric(5, 2))
    success_rate_grade_10 = Column(Numeric(5, 2))
    success_rate_grade_11 = Column(Numeric(5, 2))

    success_rate_level_a = Column(Numeric(5, 2))
    success_rate_level_b = Column(Numeric(5, 2))
    success_rate_level_c = Column(Numeric(5, 2))

    common_errors = Column(JSONB, default=[])

    last_calculated_at = Column(DateTime, default=func.now())

    skill = relationship("KnowledgeHierarchy", back_populates="benchmarks")
