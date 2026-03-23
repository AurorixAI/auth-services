"""
ALGO V1 - Textbook API Router

CRUD для учебников, структуры (TOC), и задач учебника.
Заменяет хардкод из frontend/topics-tree.ts.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional, List, Dict, Any
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

from src.core.database import get_db

router = APIRouter(prefix="/textbooks", tags=["textbooks"])

# ============================================================================
# RESPONSE MODELS
# ============================================================================

class TextbookBrief(BaseModel):
    textbook_id: str
    title: str
    subtitle: Optional[str] = None
    authors: List[str] = []
    class_level: int
    publisher: Optional[str] = None
    cover_image_url: Optional[str] = None
    digitization_status: str = "pending"
    digitization_progress: float = 0
    tasks_extracted: int = 0

class TextbookDetail(TextbookBrief):
    isbn: Optional[str] = None
    edition: Optional[str] = None
    country: str = "RU"
    language: str = "ru"
    total_pages: Optional[int] = None
    is_active: bool = True

class TOCNode(BaseModel):
    id: int
    parent_id: Optional[int] = None
    node_type: str
    display_number: Optional[str] = None
    display_name: str
    full_title: Optional[str] = None
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    sort_order: int = 0
    mapped_skill_id: Optional[str] = None
    mapped_topic_id: Optional[str] = None
    mapping_confidence: Optional[float] = None
    tasks_count: int = 0
    children: List['TOCNode'] = []

class TextbookTaskResponse(BaseModel):
    task_id: str
    structure_id: Optional[int] = None
    exercise_number: Optional[str] = None
    skill_id: str
    difficulty: str = "B"
    question_text: str
    question_latex: Optional[str] = None
    answer_type: str = "exact_number"
    correct_answer: Optional[str] = None  # Скрыт для учеников
    has_template: bool = False

class TextbookListResponse(BaseModel):
    textbooks: List[TextbookBrief]
    total: int

# ============================================================================
# ENDPOINTS
# ============================================================================

@router.get("", response_model=TextbookListResponse)
def list_textbooks(
    class_level: Optional[int] = Query(None, ge=1, le=11),
    country: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """
    Список учебников с фильтрацией по классу, стране, статусу.
    """
    query = "SELECT * FROM textbooks WHERE is_active = TRUE"
    params = {}
    
    if class_level:
        query += " AND class_level = :class_level"
        params["class_level"] = class_level
    if country:
        query += " AND country = :country"
        params["country"] = country
    if status:
        query += " AND digitization_status = :status"
        params["status"] = status
    
    query += " ORDER BY class_level, title"
    
    result = db.execute(text(query), params)
    rows = result.mappings().all()
    
    textbooks = [
        TextbookBrief(
            textbook_id=str(r["textbook_id"]),
            title=r["title"],
            subtitle=r.get("subtitle"),
            authors=r.get("authors", []) or [],
            class_level=r["class_level"],
            publisher=r.get("publisher"),
            cover_image_url=r.get("cover_image_url"),
            digitization_status=r.get("digitization_status", "pending"),
            digitization_progress=float(r.get("digitization_progress", 0)),
            tasks_extracted=r.get("tasks_extracted", 0),
        )
        for r in rows
    ]
    
    return TextbookListResponse(textbooks=textbooks, total=len(textbooks))


# ============================================================================
# SKILL TREE API (для режима "По программе") — MUST be above /{textbook_id}
# ============================================================================

class SkillTreeNode(BaseModel):
    id: str
    level: str
    name: str
    name_ru: str
    parent_id: Optional[str] = None
    class_level_start: Optional[int] = None
    class_level_end: Optional[int] = None
    importance: int = 5
    children: List['SkillTreeNode'] = []
    tasks_count: int = 0


@router.get("/skills/tree", response_model=List[SkillTreeNode])
def get_skill_tree(
    class_level: Optional[int] = Query(None, ge=1, le=11),
    db: Session = Depends(get_db)
):
    """
    Дерево навыков (L1→L4) для режима "По программе".
    """
    query = """
        SELECT kh.id, kh.level, kh.name, kh.name_ru, kh.parent_id,
               kh.class_level_start, kh.class_level_end, kh.importance,
               COUNT(tm.id) as tasks_count
        FROM knowledge_hierarchy kh
        LEFT JOIN tasks_master tm ON tm.skill_id = kh.id AND tm.is_active = TRUE
        WHERE kh.is_active = TRUE
    """
    params = {}

    if class_level:
        query += " AND (kh.class_level_start IS NULL OR kh.class_level_start <= :cl)"
        query += " AND (kh.class_level_end IS NULL OR kh.class_level_end >= :cl)"
        params["cl"] = class_level

    query += " GROUP BY kh.id ORDER BY kh.sequence_order"

    result = db.execute(text(query), params)
    rows = result.mappings().all()

    nodes = [
        SkillTreeNode(
            id=r["id"],
            level=r["level"],
            name=r["name"],
            name_ru=r["name_ru"] or r["name"],
            parent_id=r.get("parent_id"),
            class_level_start=r.get("class_level_start"),
            class_level_end=r.get("class_level_end"),
            importance=r.get("importance", 5),
            tasks_count=r.get("tasks_count", 0),
        )
        for r in rows
    ]

    # Build hierarchical tree from flat list using parent_id
    by_id = {n.id: n for n in nodes}
    roots = []
    for node in nodes:
        if node.parent_id and node.parent_id in by_id:
            by_id[node.parent_id].children.append(node)
        else:
            roots.append(node)

    # If grade-specific L1 nodes exist (e.g. G5_NAT), exclude generic ones (ARITH, ALG, GEOM)
    if class_level:
        prefix = f"G{class_level}_"
        has_grade_specific = any(r.level == "L1" and r.id.startswith(prefix) for r in roots)
        if has_grade_specific:
            roots = [r for r in roots if r.level != "L1" or r.id.startswith(prefix)]

    return roots


@router.get("/{textbook_id}", response_model=TextbookDetail)
def get_textbook(textbook_id: str, db: Session = Depends(get_db)):
    """Полная информация об учебнике."""
    result = db.execute(
        text("SELECT * FROM textbooks WHERE textbook_id = :id"),
        {"id": textbook_id}
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, "Учебник не найден")
    
    return TextbookDetail(
        textbook_id=str(row["textbook_id"]),
        title=row["title"],
        subtitle=row.get("subtitle"),
        authors=row.get("authors", []) or [],
        class_level=row["class_level"],
        publisher=row.get("publisher"),
        isbn=row.get("isbn"),
        edition=row.get("edition"),
        country=row.get("country", "RU"),
        language=row.get("language", "ru"),
        cover_image_url=row.get("cover_image_url"),
        total_pages=row.get("total_pages"),
        digitization_status=row.get("digitization_status", "pending"),
        digitization_progress=float(row.get("digitization_progress", 0)),
        tasks_extracted=row.get("tasks_extracted", 0),
        is_active=row.get("is_active", True),
    )


@router.get("/{textbook_id}/toc", response_model=List[TOCNode])
def get_textbook_toc(textbook_id: str, db: Session = Depends(get_db)):
    """
    Оглавление учебника (рекурсивное дерево).
    Возвращает плоский список — фронтенд строит дерево по parent_id.
    """
    result = db.execute(
        text("""
            SELECT id, parent_id, node_type, display_number, display_name,
                   full_title, page_start, page_end, sort_order,
                   mapped_skill_id, mapped_topic_id, mapping_confidence,
                   tasks_count
            FROM textbook_structure
            WHERE textbook_id = :textbook_id
            ORDER BY sort_order
        """),
        {"textbook_id": textbook_id}
    )
    rows = result.mappings().all()
    
    nodes = [
        TOCNode(
            id=r["id"],
            parent_id=r["parent_id"],
            node_type=r["node_type"],
            display_number=r.get("display_number"),
            display_name=r["display_name"],
            full_title=r.get("full_title"),
            page_start=r.get("page_start"),
            page_end=r.get("page_end"),
            sort_order=r.get("sort_order", 0),
            mapped_skill_id=r.get("mapped_skill_id"),
            mapped_topic_id=r.get("mapped_topic_id"),
            mapping_confidence=float(r["mapping_confidence"]) if r.get("mapping_confidence") else None,
            tasks_count=r.get("tasks_count", 0),
        )
        for r in rows
    ]
    
    return nodes


@router.get("/{textbook_id}/toc/tree", response_model=List[TOCNode])
def get_textbook_toc_tree(textbook_id: str, db: Session = Depends(get_db)):
    """
    Оглавление в виде вложенного дерева (children заполнены).
    """
    flat = get_textbook_toc(textbook_id, db)
    return _build_tree(flat)


@router.get("/{textbook_id}/tasks", response_model=List[TextbookTaskResponse])
def get_textbook_tasks(
    textbook_id: str, 
    structure_id: Optional[int] = Query(None, description="Фильтр по разделу TOC"),
    skill_id: Optional[str] = Query(None),
    difficulty: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db)
):
    """
    Задачи из учебника с фильтрацией.
    """
    query = """
        SELECT tt.task_id, tt.structure_id, tt.exercise_number,
               tm.skill_id, tm.difficulty, tm.question_text,
               tm.question_latex, tm.answer_type, tm.has_template
        FROM textbook_tasks tt
        JOIN tasks_master tm ON tt.task_id = tm.id
        WHERE tt.textbook_id = :textbook_id
        AND tm.is_active = TRUE
    """
    params: Dict[str, Any] = {"textbook_id": textbook_id}
    
    if structure_id:
        # Include children of this structure node
        query += """ AND (tt.structure_id = :structure_id 
                     OR tt.structure_id IN (
                         SELECT id FROM textbook_structure 
                         WHERE parent_id = :structure_id
                     ))"""
        params["structure_id"] = structure_id
    
    if skill_id:
        query += " AND tm.skill_id = :skill_id"
        params["skill_id"] = skill_id
    
    if difficulty:
        query += " AND tm.difficulty = :difficulty"
        params["difficulty"] = difficulty
    
    query += " ORDER BY tt.exercise_number LIMIT :limit OFFSET :offset"
    params["limit"] = limit
    params["offset"] = offset
    
    result = db.execute(text(query), params)
    rows = result.mappings().all()
    
    return [
        TextbookTaskResponse(
            task_id=r["task_id"],
            structure_id=r.get("structure_id"),
            exercise_number=r.get("exercise_number"),
            skill_id=r["skill_id"],
            difficulty=r.get("difficulty", "B"),
            question_text=r.get("question_text", ""),
            question_latex=r.get("question_latex"),
            answer_type=r.get("answer_type", "exact_number"),
            has_template=r.get("has_template", False),
        )
        for r in rows
    ]


# ============================================================================
# HELPERS
# ============================================================================

def _build_tree(flat_nodes: List[TOCNode]) -> List[TOCNode]:
    """Строит вложенное дерево из плоского списка."""
    by_id = {n.id: n for n in flat_nodes}
    roots = []
    
    for node in flat_nodes:
        if node.parent_id and node.parent_id in by_id:
            by_id[node.parent_id].children.append(node)
        else:
            roots.append(node)
    
    return roots
