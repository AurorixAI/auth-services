"""
ALGO V1 — Textbook ORM Models

Tables: textbooks, textbook_structure, textbook_tasks, textbook_toc, textbook_skill_map
"""

from uuid import uuid4

from sqlalchemy import (
    Column, String, Integer, Boolean, Text, ForeignKey,
    DateTime, Numeric, ARRAY, Index, CheckConstraint, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from src.models.base import Base


class Textbook(Base):
    """Каталог учебников"""
    __tablename__ = 'textbooks'

    textbook_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)

    isbn = Column(String(20))
    title = Column(String(300), nullable=False)
    subtitle = Column(String(300))
    edition = Column(String(50))

    authors = Column(ARRAY(Text))
    publisher = Column(String(200))

    subject = Column(String(50), nullable=False)
    class_level = Column(Integer, nullable=False)
    country = Column(String(10), default='RU')
    language = Column(String(10), default='ru')

    cover_image_url = Column(Text)
    total_pages = Column(Integer)

    digitization_status = Column(String(20), default='pending')
    digitization_progress = Column(Numeric(5, 2), default=0)
    tasks_extracted = Column(Integer, default=0)

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

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

    node_type = Column(String(20), nullable=False)

    display_number = Column(String(30))
    display_name = Column(String(500), nullable=False)
    full_title = Column(Text)

    page_start = Column(Integer)
    page_end = Column(Integer)
    sort_order = Column(Integer, nullable=False)

    mapped_skill_id = Column(String(30), ForeignKey('knowledge_hierarchy.id'))
    mapped_topic_id = Column(String(30), ForeignKey('knowledge_hierarchy.id'))
    mapped_cluster_id = Column(String(30), ForeignKey('knowledge_hierarchy.id'))
    mapping_confidence = Column(Numeric(3, 2))
    mapping_verified = Column(Boolean, default=False)

    description = Column(Text)
    learning_objectives = Column(ARRAY(Text))
    key_concepts = Column(ARRAY(Text))

    tasks_count = Column(Integer, default=0)
    estimated_hours = Column(Numeric(4, 2))

    created_at = Column(DateTime, default=func.now())

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

    exercise_number = Column(String(30))
    page_number = Column(Integer)
    section_reference = Column(String(100))

    book_context = Column(Text)
    original_text = Column(Text)

    ocr_confidence = Column(Numeric(3, 2))
    manually_verified = Column(Boolean, default=False)

    created_at = Column(DateTime, default=func.now())

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

    level = Column(String(20), nullable=False)
    number = Column(String(30))
    title = Column(String(500), nullable=False)
    full_path = Column(Text)

    page_start = Column(Integer)
    page_end = Column(Integer)

    sort_order = Column(Integer, nullable=False, default=0)

    tasks_extracted = Column(Integer, default=0)
    is_digitized = Column(Boolean, default=False)

    created_at = Column(DateTime, default=func.now())

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

    role = Column(String(20), nullable=False, default='primary')
    confidence = Column(Numeric(3, 2), default=0.8)
    is_main = Column(Boolean, default=False)
    cross_topic_id = Column(Integer, ForeignKey('textbook_toc.id'))

    created_at = Column(DateTime, default=func.now())

    toc_entry = relationship("TextbookToc", foreign_keys=[toc_id], back_populates="skill_maps")
    skill = relationship("KnowledgeHierarchy")

    __table_args__ = (
        CheckConstraint("role IN ('primary', 'secondary', 'prerequisite', 'extension')"),
        UniqueConstraint('toc_id', 'skill_id'),
        Index('idx_tsm_toc', 'toc_id'),
        Index('idx_tsm_skill', 'skill_id'),
    )
