"""
ALGO V1 - SQLAlchemy Models
ORM модели для всех таблиц базы данных
"""

from datetime import datetime
from typing import Optional, List
from uuid import uuid4

from sqlalchemy import (
    Column, String, Integer, Boolean, Text, ForeignKey, 
    DateTime, Numeric, ARRAY, Index, CheckConstraint, UniqueConstraint,
    BigInteger, create_engine
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship, declarative_base, Session
from sqlalchemy.sql import func

# Single source of truth — all model files use this Base
from src.models.base import Base  # noqa: F401  (re-exported for backward compat)


# =============================================================================
# CORE SCHEMA - Ядро диагностики
# =============================================================================

class KnowledgeHierarchy(Base):
    """
    Иерархия знаний (L1-L4)
    L1: Domain (Алгебра, Геометрия, Арифметика)
    L2: Cluster (Уравнения, Неравенства, Функции)
    L3: Topic (Линейные уравнения, Квадратные уравнения)
    L4: Atomic Skill (Вычисление дискриминанта, Перенос слагаемых)
    """
    __tablename__ = 'knowledge_hierarchy'
    
    id = Column(String(30), primary_key=True)  # Например: ALG_EQ_QUAD_DISCRIM
    level = Column(String(2), nullable=False)  # L1, L2, L3, L4
    parent_id = Column(String(30), ForeignKey('knowledge_hierarchy.id'))
    
    # Названия
    name = Column(String(200), nullable=False)
    name_ru = Column(String(200), nullable=False)
    name_uz = Column(String(200))
    
    # Метаданные
    description = Column(Text)
    class_level_start = Column(Integer)
    class_level_end = Column(Integer)
    importance = Column(Integer, default=5)
    sequence_order = Column(Integer, default=0)
    
    # Атомарность (только для L4)
    is_composite = Column(Boolean, default=False)
    composite_type = Column(String(20))  # sequential, parallel, conditional
    decomposition_notes = Column(Text)
    
    # Когнитивные характеристики (только для L4)
    cognitive_type = Column(String(20))  # recall, apply, analyze, evaluate, create
    typical_time_seconds = Column(Integer)
    
    # Статус
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    
    # Расширения (миграция 010)
    origin = Column(String(20), default='base')                   # base, textbook, olympiad
    is_advanced = Column(Boolean, default=False)                   # ★-задачи, CAT показывает при θ > 1.5
    source_textbook_id = Column(UUID(as_uuid=True), ForeignKey('textbooks.textbook_id'))
    
    # Relationships
    parent = relationship("KnowledgeHierarchy", remote_side=[id], backref="children")
    prerequisites = relationship(
        "SkillPrerequisite", 
        foreign_keys="SkillPrerequisite.skill_id",
        back_populates="skill"
    )
    dependent_skills = relationship(
        "SkillPrerequisite",
        foreign_keys="SkillPrerequisite.prerequisite_id",
        back_populates="prerequisite"
    )
    tasks = relationship("TaskMaster", back_populates="skill")
    benchmarks = relationship("SkillBenchmark", back_populates="skill", uselist=False)
    
    __table_args__ = (
        CheckConstraint("level IN ('L1', 'L2', 'L3', 'L4')"),
        CheckConstraint("importance BETWEEN 1 AND 10"),
        CheckConstraint("cognitive_type IN ('recall', 'apply', 'analyze', 'evaluate', 'create')"),
        CheckConstraint("composite_type IN ('sequential', 'parallel', 'conditional')"),
        Index('idx_kh_level', 'level'),
        Index('idx_kh_parent', 'parent_id'),
        Index('idx_kh_class_level', 'class_level_start', 'class_level_end'),
    )


class SkillPrerequisite(Base):
    """Граф зависимостей навыков"""
    __tablename__ = 'skill_prerequisites'
    
    id = Column(Integer, primary_key=True)
    skill_id = Column(String(30), ForeignKey('knowledge_hierarchy.id'), nullable=False)
    prerequisite_id = Column(String(30), ForeignKey('knowledge_hierarchy.id'), nullable=False)
    
    # Тип зависимости
    dependency_type = Column(String(10), nullable=False)  # hard, soft
    criticality = Column(Integer, default=5)
    relationship_description = Column(Text)
    
    # Самообучение
    weight = Column(Numeric(4, 2), default=1.0)
    learned_weight = Column(Numeric(4, 2))
    confidence = Column(Numeric(4, 3), default=1.0)
    sample_size = Column(Integer, default=0)
    discovery_source = Column(String(20), default='expert')  # expert, statistical, ml
    last_validated_at = Column(DateTime)
    
    created_at = Column(DateTime, default=func.now())
    
    # Relationships
    skill = relationship("KnowledgeHierarchy", foreign_keys=[skill_id], back_populates="prerequisites")
    prerequisite = relationship("KnowledgeHierarchy", foreign_keys=[prerequisite_id], back_populates="dependent_skills")
    
    __table_args__ = (
        CheckConstraint("dependency_type IN ('hard', 'soft')"),
        CheckConstraint("criticality BETWEEN 1 AND 10"),
        CheckConstraint("skill_id != prerequisite_id"),
        CheckConstraint("discovery_source IN ('expert', 'statistical', 'ml')"),
        UniqueConstraint('skill_id', 'prerequisite_id'),
        Index('idx_sp_skill', 'skill_id'),
        Index('idx_sp_prereq', 'prerequisite_id'),
    )


class SkillBenchmark(Base):
    """Статистика навыков для автокалибровки"""
    __tablename__ = 'skill_benchmarks'
    
    skill_id = Column(String(30), ForeignKey('knowledge_hierarchy.id'), primary_key=True)
    
    # Общая статистика
    total_attempts = Column(Integer, default=0)
    total_correct = Column(Integer, default=0)
    avg_solve_time_seconds = Column(Numeric(10, 2))
    
    # Статистика по классам
    success_rate_grade_5 = Column(Numeric(5, 2))
    success_rate_grade_6 = Column(Numeric(5, 2))
    success_rate_grade_7 = Column(Numeric(5, 2))
    success_rate_grade_8 = Column(Numeric(5, 2))
    success_rate_grade_9 = Column(Numeric(5, 2))
    success_rate_grade_10 = Column(Numeric(5, 2))
    success_rate_grade_11 = Column(Numeric(5, 2))
    
    # Статистика по сложности
    success_rate_level_a = Column(Numeric(5, 2))
    success_rate_level_b = Column(Numeric(5, 2))
    success_rate_level_c = Column(Numeric(5, 2))
    
    # Типичные ошибки
    common_errors = Column(JSONB, default=[])
    
    last_calculated_at = Column(DateTime, default=func.now())
    
    # Relationships
    skill = relationship("KnowledgeHierarchy", back_populates="benchmarks")


class TaskMaster(Base):
    """Банк задач"""
    __tablename__ = 'tasks_master'
    
    id = Column(String(30), primary_key=True)
    skill_id = Column(String(30), ForeignKey('knowledge_hierarchy.id'), nullable=True)
    
    # Контент задачи
    question_text = Column(Text, nullable=False)
    question_latex = Column(Text)
    question_image_url = Column(Text)
    
    # Правильный ответ
    answer_type = Column(String(20), nullable=False)
    correct_answer = Column(Text, nullable=False)
    correct_answer_latex = Column(Text)
    answer_tolerance = Column(Numeric(10, 6))
    answer_options = Column(JSONB)       # Варианты для multiple_choice
    
    # Генерация вариантов
    has_template = Column(Boolean, default=False)
    template_params = Column(JSONB)
    generation_function = Column(Text)
    
    # Сложность (SQL column name: 'difficulty', not 'difficulty_level')
    difficulty = Column(String(1), default='B')  # A, B, C
    difficulty_score = Column(Numeric(3, 2))
    cognitive_load = Column(String(20))   # recall, apply, analyze
    
    # IRT параметры
    irt_discrimination = Column(Numeric(6, 4), default=1.0)
    irt_difficulty = Column(Numeric(6, 4), default=0.0)
    irt_guessing = Column(Numeric(6, 4), default=0.0)
    irt_calibrated = Column(Boolean, default=False)
    irt_sample_size = Column(Integer, default=0)
    
    # Решение
    solution_steps = Column(JSONB)
    solution_explanation = Column(Text)
    hints = Column(JSONB)
    common_mistakes = Column(JSONB)
    distractor_meta = Column(JSONB)      # [{value, error_type, explanation, plausibility}, ...]
    sympy_solution = Column(Text)
    
    # Верификация
    verification_status = Column(String(20), default='pending')
    verified_by = Column(String(100))
    verified_at = Column(DateTime)
    
    # Источник
    source_type = Column(String(20))
    source_reference = Column(Text)
    
    # Статистика
    times_used = Column(Integer, default=0)
    times_correct = Column(Integer, default=0)
    avg_time_seconds = Column(Numeric(10, 2))
    
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    
    # Расширения (миграция 009)
    toc_id = Column(Integer, ForeignKey('textbook_toc.id'))       # FK на textbook_toc
    tags = Column(JSONB, default={})                               # {"page": 42, "exercise": "3.15", ...}
    
    # Relationships
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
        CheckConstraint("verification_status IN ('pending', 'verified', 'conflict', 'rejected', 'needs_review')"),
        Index('idx_tm_skill', 'skill_id'),
        Index('idx_tm_difficulty', 'difficulty'),
        Index('idx_tm_active', 'is_active'),
    )


class TaskTemplateDB(Base):
    """
    Кэшированные параметрические шаблоны задач.

    Заполняется при оцифровке (AI/методист) — содержит буквенную формулу,
    параметры и constraints.  SymPy использует эти данные только для
    подстановки новых чисел и верификации ответа.
    """
    __tablename__ = 'task_templates'

    id = Column(String(30), primary_key=True)
    original_task_id = Column(String(30), ForeignKey('tasks_master.id'), nullable=False)
    skill_id = Column(String(30), ForeignKey('knowledge_hierarchy.id'), nullable=False)

    # Шаблон текста (два формата хранения)
    template_text = Column(Text, nullable=False)       # «Решите: {{a}}x + {{b}} = {{c}}»
    template_latex = Column(Text)

    # Параметры / ограничения (legacy JSONB из 001)
    parameters = Column(JSONB, nullable=False)          # ["a", "b", "c"]
    constraints = Column(JSONB, nullable=False)         # {"a": {"min": 1, ...}}

    # Формула ответа (текстовая)
    answer_formula = Column(Text, nullable=False)       # "(c - b) / a"

    generation_method = Column(String(20))              # simple_substitution / constrained / sympy_verified
    quality_score = Column(Numeric(3, 2))
    tested_combinations = Column(Integer, default=0)
    successful_combinations = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())

    # ── Расширения из 012_variant_engine ──
    answer_formula_sympy = Column(Text)                 # srepr() SymPy-выражение
    constraints_repr = Column(JSONB, default=[])        # ["a != 0", ...]
    task_type = Column(String(30), default='arithmetic')
    text_template = Column(Text)                        # «Вычислите: {a} + {b}»
    original_values = Column(JSONB, default={})         # {"a": 5, "b": 3}
    param_details = Column(JSONB, default={})           # {"a": {"min":1, "max":50, ...}}

    # Relationships
    task = relationship("TaskMaster", backref="templates")
    skill = relationship("KnowledgeHierarchy")

    __table_args__ = (
        Index('idx_tt_skill', 'skill_id'),
        Index('idx_tt_quality', quality_score.desc()),
        Index('idx_tt_task_type', 'task_type'),
    )


# =============================================================================
# PRESENTATION SCHEMA - Слой учебников
# =============================================================================

class Textbook(Base):
    """Каталог учебников"""
    __tablename__ = 'textbooks'
    
    textbook_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    
    # Идентификация
    isbn = Column(String(20))
    title = Column(String(300), nullable=False)
    subtitle = Column(String(300))
    edition = Column(String(50))
    
    # Авторы
    authors = Column(ARRAY(Text))
    publisher = Column(String(200))
    
    # Классификация
    subject = Column(String(50), nullable=False)
    class_level = Column(Integer, nullable=False)
    country = Column(String(10), default='RU')
    language = Column(String(10), default='ru')
    
    # Медиа
    cover_image_url = Column(Text)
    total_pages = Column(Integer)
    
    # Статус оцифровки
    digitization_status = Column(String(20), default='pending')
    digitization_progress = Column(Numeric(5, 2), default=0)
    tasks_extracted = Column(Integer, default=0)
    
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    
    # Relationships
    structure = relationship("TextbookStructure", back_populates="textbook", cascade="all, delete-orphan")
    tasks = relationship("TextbookTask", back_populates="textbook", cascade="all, delete-orphan")
    toc_entries = relationship("TextbookToc", backref="textbook", cascade="all, delete-orphan")
    
    __table_args__ = (
        CheckConstraint("digitization_status IN ('pending', 'in_progress', 'completed', 'verified')"),
        Index('idx_tb_class', 'class_level'),
        Index('idx_tb_subject', 'subject'),
    )


class TextbookStructure(Base):
    """Структура учебника (рекурсивная)"""
    __tablename__ = 'textbook_structure'
    
    id = Column(Integer, primary_key=True)
    textbook_id = Column(UUID(as_uuid=True), ForeignKey('textbooks.textbook_id', ondelete='CASCADE'), nullable=False)
    parent_id = Column(Integer, ForeignKey('textbook_structure.id', ondelete='CASCADE'))
    
    # Тип узла
    node_type = Column(String(20), nullable=False)  # part, chapter, section, subsection, topic
    
    # Отображение
    display_number = Column(String(30))
    display_name = Column(String(500), nullable=False)
    full_title = Column(Text)
    
    # Позиция
    page_start = Column(Integer)
    page_end = Column(Integer)
    sort_order = Column(Integer, nullable=False)
    
    # Маппинг на CORE
    mapped_skill_id = Column(String(30), ForeignKey('knowledge_hierarchy.id'))
    mapped_topic_id = Column(String(30), ForeignKey('knowledge_hierarchy.id'))
    mapped_cluster_id = Column(String(30), ForeignKey('knowledge_hierarchy.id'))
    mapping_confidence = Column(Numeric(3, 2))
    mapping_verified = Column(Boolean, default=False)
    
    # Описание
    description = Column(Text)
    learning_objectives = Column(ARRAY(Text))
    key_concepts = Column(ARRAY(Text))
    
    # Статистика
    tasks_count = Column(Integer, default=0)
    estimated_hours = Column(Numeric(4, 2))
    
    created_at = Column(DateTime, default=func.now())
    
    # Relationships
    textbook = relationship("Textbook", back_populates="structure")
    parent = relationship("TextbookStructure", remote_side=[id], backref="children")
    mapped_skill = relationship("KnowledgeHierarchy", foreign_keys=[mapped_skill_id])
    tasks = relationship("TextbookTask", back_populates="structure")
    
    __table_args__ = (
        CheckConstraint("node_type IN ('part', 'chapter', 'section', 'subsection', 'topic')"),
        Index('idx_ts_textbook', 'textbook_id'),
        Index('idx_ts_parent', 'parent_id'),
        Index('idx_ts_mapped_skill', 'mapped_skill_id'),
    )


class TextbookTask(Base):
    """Привязка задач к учебникам"""
    __tablename__ = 'textbook_tasks'
    
    id = Column(Integer, primary_key=True)
    textbook_id = Column(UUID(as_uuid=True), ForeignKey('textbooks.textbook_id', ondelete='CASCADE'), nullable=False)
    task_id = Column(String(30), ForeignKey('tasks_master.id', ondelete='CASCADE'), nullable=False)
    structure_id = Column(Integer, ForeignKey('textbook_structure.id'))
    
    # Как задача выглядит в книге
    exercise_number = Column(String(30))
    page_number = Column(Integer)
    section_reference = Column(String(100))
    
    # Контекст
    book_context = Column(Text)
    original_text = Column(Text)
    
    # Оцифровка
    ocr_confidence = Column(Numeric(3, 2))
    manually_verified = Column(Boolean, default=False)
    
    created_at = Column(DateTime, default=func.now())
    
    # Relationships
    textbook = relationship("Textbook", back_populates="tasks")
    task = relationship("TaskMaster", back_populates="textbook_tasks")
    structure = relationship("TextbookStructure", back_populates="tasks")
    
    __table_args__ = (
        UniqueConstraint('textbook_id', 'task_id'),
        Index('idx_tbt_textbook', 'textbook_id'),
        Index('idx_tbt_task', 'task_id'),
    )


class TextbookToc(Base):
    """Рекурсивное оглавление учебника (миграция 009)"""
    __tablename__ = 'textbook_toc'

    id = Column(Integer, primary_key=True)
    textbook_id = Column(UUID(as_uuid=True), ForeignKey('textbooks.textbook_id', ondelete='CASCADE'), nullable=False)
    parent_id = Column(Integer, ForeignKey('textbook_toc.id', ondelete='CASCADE'))

    # Уровень: textbook > part > chapter > section > paragraph
    level = Column(String(20), nullable=False)
    number = Column(String(30))           # "§3.2", "Глава 5", "1.1"
    title = Column(String(500), nullable=False)
    full_path = Column(Text)               # auto-gen: "Глава 3 / §3.2 / Упражнения"

    # Страницы
    page_start = Column(Integer)
    page_end = Column(Integer)

    # Сортировка
    sort_order = Column(Integer, nullable=False, default=0)

    # Статус оцифровки
    tasks_extracted = Column(Integer, default=0)
    is_digitized = Column(Boolean, default=False)

    created_at = Column(DateTime, default=func.now())

    # Relationships
    parent = relationship("TextbookToc", remote_side=[id], backref="children")
    skill_maps = relationship(
        "TextbookSkillMap",
        foreign_keys="[TextbookSkillMap.toc_id]",
        back_populates="toc_entry",
        cascade="all, delete-orphan",
    )
    tasks = relationship("TaskMaster", back_populates="toc_entry")

    __table_args__ = (
        CheckConstraint("level IN ('textbook', 'part', 'chapter', 'section', 'paragraph')"),
        Index('idx_toc_textbook', 'textbook_id'),
        Index('idx_toc_parent', 'parent_id'),
        Index('idx_toc_level', 'level'),
    )


class TextbookSkillMap(Base):
    """Мост: параграф учебника ↔ навыки скелета (миграция 009)"""
    __tablename__ = 'textbook_skill_map'

    id = Column(Integer, primary_key=True)
    toc_id = Column(Integer, ForeignKey('textbook_toc.id', ondelete='CASCADE'), nullable=False)
    skill_id = Column(String(30), ForeignKey('knowledge_hierarchy.id', ondelete='CASCADE'), nullable=False)

    # Роль навыка в параграфе
    role = Column(String(20), nullable=False, default='primary')
    confidence = Column(Numeric(3, 2), default=0.8)
    is_main = Column(Boolean, default=False)
    cross_topic_id = Column(Integer, ForeignKey('textbook_toc.id'))

    created_at = Column(DateTime, default=func.now())

    # Relationships
    toc_entry = relationship("TextbookToc", foreign_keys=[toc_id], back_populates="skill_maps")
    skill = relationship("KnowledgeHierarchy")

    __table_args__ = (
        CheckConstraint("role IN ('primary', 'secondary', 'prerequisite', 'extension')"),
        UniqueConstraint('toc_id', 'skill_id'),
        Index('idx_tsm_toc', 'toc_id'),
        Index('idx_tsm_skill', 'skill_id'),
    )


# =============================================================================
# ANALYTICS SCHEMA - Самообучающийся граф
# =============================================================================

class AnswerEvent(Base):
    """Полная история ответов для ML"""
    __tablename__ = 'answer_events'
    
    id = Column(BigInteger, primary_key=True)
    
    # Идентификация
    session_id = Column(UUID(as_uuid=True), nullable=False)
    student_id = Column(UUID(as_uuid=True))
    
    # Что решали
    task_id = Column(String(30), ForeignKey('tasks_master.id'), nullable=False)
    skill_id = Column(String(30), ForeignKey('knowledge_hierarchy.id'), nullable=False)
    
    # Результат
    is_correct = Column(Boolean, nullable=False)
    student_answer = Column(Text)
    correct_answer = Column(Text)
    
    # Временные метрики
    time_spent_ms = Column(Integer, nullable=False)
    started_at = Column(DateTime, nullable=False)
    submitted_at = Column(DateTime, nullable=False, default=func.now())
    
    # Контекст
    difficulty_level = Column(String(1))
    task_order_in_session = Column(Integer)
    
    # IRT параметры на момент ответа
    task_irt_a = Column(Numeric(6, 4))
    task_irt_b = Column(Numeric(6, 4))
    task_irt_c = Column(Numeric(6, 4))
    
    # Оценка способности
    student_theta_before = Column(Numeric(6, 4))
    student_theta_after = Column(Numeric(6, 4))
    
    # Классификация ошибки
    error_type = Column(String(50))          # sign_error, partial_result, formula_error, ...
    error_explanation = Column(Text)          # Описание ошибки ученика
    matched_distractor = Column(Text)         # Значение совпавшего дистрактора
    
    # Метаданные
    device_type = Column(String(20))
    client_version = Column(String(20))
    
    # Relationships
    task = relationship("TaskMaster", back_populates="answer_events")
    skill = relationship("KnowledgeHierarchy")
    
    __table_args__ = (
        Index('idx_ae_session', 'session_id'),
        Index('idx_ae_student', 'student_id'),
        Index('idx_ae_skill', 'skill_id'),
        Index('idx_ae_submitted', 'submitted_at'),
    )


class SkillCorrelation(Base):
    """Обнаруженные корреляции между навыками"""
    __tablename__ = 'skill_correlations'
    
    id = Column(Integer, primary_key=True)
    
    # Пара навыков
    skill_a_id = Column(String(30), ForeignKey('knowledge_hierarchy.id'), nullable=False)
    skill_b_id = Column(String(30), ForeignKey('knowledge_hierarchy.id'), nullable=False)
    
    # Статистика
    correlation_coefficient = Column(Numeric(6, 4), nullable=False)
    p_value = Column(Numeric(10, 8))
    sample_size = Column(Integer, nullable=False)
    
    # Тип связи
    relationship_type = Column(String(20), nullable=False)  # prerequisite, corequisite, diagnostic, transfer
    direction = Column(String(10))  # a_to_b, b_to_a, bidirectional
    
    # Сила связи
    strength = Column(Numeric(4, 2))
    confidence = Column(Numeric(4, 3))
    
    # Источник
    discovery_method = Column(String(30), nullable=False)  # expert_defined, statistical, ml_discovered, pattern_mining
    
    # Валидация
    is_validated = Column(Boolean, default=False)
    validated_by = Column(String(100))
    validated_at = Column(DateTime)
    
    discovered_at = Column(DateTime, default=func.now())
    last_updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    
    # Relationships
    skill_a = relationship("KnowledgeHierarchy", foreign_keys=[skill_a_id])
    skill_b = relationship("KnowledgeHierarchy", foreign_keys=[skill_b_id])
    
    __table_args__ = (
        CheckConstraint("relationship_type IN ('prerequisite', 'corequisite', 'diagnostic', 'transfer')"),
        CheckConstraint("discovery_method IN ('expert_defined', 'statistical', 'ml_discovered', 'pattern_mining')"),
        UniqueConstraint('skill_a_id', 'skill_b_id', 'relationship_type'),
        Index('idx_sc_skill_a', 'skill_a_id'),
        Index('idx_sc_skill_b', 'skill_b_id'),
    )


class ErrorPattern(Base):
    """Паттерны ошибок"""
    __tablename__ = 'error_patterns'
    
    id = Column(Integer, primary_key=True)
    skill_id = Column(String(30), ForeignKey('knowledge_hierarchy.id'), nullable=False)
    
    # Описание
    pattern_type = Column(String(30), nullable=False)
    pattern_description = Column(Text, nullable=False)
    error_examples = Column(JSONB, default=[])
    
    # Корневые причины
    root_cause_skills = Column(JSONB, default=[])
    
    # Статистика
    occurrence_count = Column(Integer, default=0)
    occurrence_rate = Column(Numeric(5, 4))
    recovery_rate = Column(Numeric(5, 4))
    
    # Рекомендации
    remediation_suggestions = Column(JSONB, default=[])
    related_tasks = Column(JSONB, default=[])
    
    discovered_at = Column(DateTime, default=func.now())
    last_seen_at = Column(DateTime, default=func.now())
    
    # Relationships
    skill = relationship("KnowledgeHierarchy")
    
    __table_args__ = (
        CheckConstraint("pattern_type IN ('sign_error', 'order_of_operations', 'formula_confusion', 'concept_gap', 'calculation_error', 'careless_mistake', 'time_pressure', 'partial_knowledge', 'misconception', 'other')"),
        Index('idx_ep_skill', 'skill_id'),
        Index('idx_ep_type', 'pattern_type'),
    )


class SessionSummary(Base):
    """Сводка по диагностическим сессиям"""
    __tablename__ = 'session_summaries'
    
    session_id = Column(UUID(as_uuid=True), primary_key=True)
    student_id = Column(UUID(as_uuid=True))
    
    # Временные рамки
    started_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime)
    total_duration_ms = Column(Integer)
    
    # Результаты
    total_tasks = Column(Integer, default=0)
    correct_answers = Column(Integer, default=0)
    
    # Финальные оценки
    final_theta = Column(Numeric(6, 4))
    confidence_interval_low = Column(Numeric(6, 4))
    confidence_interval_high = Column(Numeric(6, 4))
    
    # Пробелы
    identified_gaps = Column(JSONB, default=[])
    root_gaps = Column(JSONB, default=[])
    
    # Рекомендации
    recommended_skills = Column(JSONB, default=[])
    priority_order = Column(JSONB, default=[])
    
    # Тип сессии
    session_type = Column(String(20), default='diagnostic')
    
    # Метаданные
    platform = Column(String(20))
    client_version = Column(String(20))
    
    created_at = Column(DateTime, default=func.now())
    
    __table_args__ = (
        CheckConstraint("session_type IN ('diagnostic', 'quick_check', 'practice', 'exam')"),
        Index('idx_ss_student', 'student_id'),
        Index('idx_ss_started', 'started_at'),
    )


class StudentSkillState(Base):
    """Байесовское состояние навыков ученика"""
    __tablename__ = 'student_skill_states'
    
    id = Column(Integer, primary_key=True)
    student_id = Column(UUID(as_uuid=True), nullable=False)
    skill_id = Column(String(30), ForeignKey('knowledge_hierarchy.id'), nullable=False)
    
    # Байесовские оценки
    mastery_probability = Column(Numeric(5, 4), nullable=False, default=0.5)
    learn_rate = Column(Numeric(5, 4), default=0.1)
    guess_rate = Column(Numeric(5, 4), default=0.2)
    slip_rate = Column(Numeric(5, 4), default=0.1)
    
    # IRT оценка
    theta = Column(Numeric(6, 4))
    theta_se = Column(Numeric(6, 4))
    
    # История
    total_attempts = Column(Integer, default=0)
    correct_attempts = Column(Integer, default=0)
    last_attempt_at = Column(DateTime)
    last_correct_at = Column(DateTime)
    
    # Тренд
    trend = Column(String(10))  # improving, stable, declining, unknown
    consecutive_correct = Column(Integer, default=0)
    consecutive_incorrect = Column(Integer, default=0)
    
    # Статус
    status = Column(String(20), default='unknown')  # mastered, learning, struggling, gap, unknown
    
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    
    # Relationships
    skill = relationship("KnowledgeHierarchy")
    
    __table_args__ = (
        CheckConstraint("trend IN ('improving', 'stable', 'declining', 'unknown')"),
        CheckConstraint("status IN ('mastered', 'learning', 'struggling', 'gap', 'unknown')"),
        UniqueConstraint('student_id', 'skill_id'),
        Index('idx_sss_student', 'student_id'),
        Index('idx_sss_skill', 'skill_id'),
        Index('idx_sss_status', 'status'),
    )


class GraphUpdateLog(Base):
    """Лог изменений графа знаний"""
    __tablename__ = 'graph_updates_log'
    
    id = Column(BigInteger, primary_key=True)
    
    # Что изменилось
    update_type = Column(String(30), nullable=False)
    entity_type = Column(String(20), nullable=False)
    entity_id = Column(String(50), nullable=False)
    
    # Детали
    old_value = Column(JSONB)
    new_value = Column(JSONB)
    change_reason = Column(Text)
    
    # Источник
    triggered_by = Column(String(30), nullable=False)
    
    # Валидация
    requires_review = Column(Boolean, default=False)
    reviewed_by = Column(String(100))
    reviewed_at = Column(DateTime)
    review_status = Column(String(20))  # pending, approved, rejected
    
    created_at = Column(DateTime, default=func.now())
    
    __table_args__ = (
        CheckConstraint("update_type IN ('new_correlation', 'strength_update', 'correlation_removed', 'prerequisite_added', 'prerequisite_removed', 'irt_calibration', 'benchmark_update')"),
        CheckConstraint("triggered_by IN ('ml_system', 'statistical_batch', 'admin_manual', 'expert_review')"),
        CheckConstraint("review_status IN ('pending', 'approved', 'rejected')"),
        Index('idx_gul_type', 'update_type'),
        Index('idx_gul_entity', 'entity_type', 'entity_id'),
        Index('idx_gul_created', 'created_at'),
    )


class IRTCalibrationHistory(Base):
    """История калибровки IRT параметров"""
    __tablename__ = 'irt_calibration_history'
    
    id = Column(Integer, primary_key=True)
    task_id = Column(String(30), ForeignKey('tasks_master.id'), nullable=False)
    
    # Параметры до
    old_discrimination = Column(Numeric(6, 4))
    old_difficulty = Column(Numeric(6, 4))
    old_guessing = Column(Numeric(6, 4))
    
    # Параметры после
    new_discrimination = Column(Numeric(6, 4))
    new_difficulty = Column(Numeric(6, 4))
    new_guessing = Column(Numeric(6, 4))
    
    # Статистика
    sample_size = Column(Integer, nullable=False)
    fit_statistic = Column(Numeric(8, 4))
    
    calibration_method = Column(String(30))  # mmle, jmle, bayesian
    calibrated_at = Column(DateTime, default=func.now())
    
    # Relationships
    task = relationship("TaskMaster")
    
    __table_args__ = (
        Index('idx_ich_task', 'task_id'),
        Index('idx_ich_date', 'calibrated_at'),
    )


class SkillResponseTime(Base):
    """Статистика времени ответов по навыкам"""
    __tablename__ = 'skill_response_times'
    
    skill_id = Column(String(30), ForeignKey('knowledge_hierarchy.id'), primary_key=True)
    difficulty_level = Column(String(1), primary_key=True)
    
    # Статистика (в миллисекундах)
    sample_size = Column(Integer, default=0)
    mean_time_ms = Column(Integer)
    median_time_ms = Column(Integer)
    std_dev_ms = Column(Integer)
    percentile_10 = Column(Integer)
    percentile_25 = Column(Integer)
    percentile_75 = Column(Integer)
    percentile_90 = Column(Integer)
    percentile_99 = Column(Integer)
    
    # Время для определения угадывания
    min_realistic_time_ms = Column(Integer)
    
    # Разбивка по результату
    mean_time_correct_ms = Column(Integer)
    mean_time_incorrect_ms = Column(Integer)
    
    last_calculated_at = Column(DateTime, default=func.now())
    
    # Relationships
    skill = relationship("KnowledgeHierarchy")
    
    __table_args__ = (
        CheckConstraint("difficulty_level IN ('A', 'B', 'C')"),
    )


# =============================================================================
# DATABASE UTILITIES
# =============================================================================

def create_tables(engine):
    """Создание всех таблиц"""
    # Import exam models so they register with Base.metadata
    from src.models.exam import GeneratedExam, ExamSession  # noqa: F401
    Base.metadata.create_all(engine)


def drop_tables(engine):
    """Удаление всех таблиц"""
    Base.metadata.drop_all(engine)


def get_engine(database_url: str):
    """Создание движка БД"""
    return create_engine(database_url, echo=False)
