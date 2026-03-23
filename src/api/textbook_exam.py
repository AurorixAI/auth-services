"""
ALGO V1 — Textbook Exam API
src/api/textbook_exam.py

Модуль 2b: Генерация экзаменов по учебнику (textbook_toc).
Учитель выбирает параграфы → система подбирает задачи с учётом difficulty_distribution.

Endpoints:
  GET  /api/v1/textbook-exams/textbooks          — список учебников
  GET  /api/v1/textbook-exams/textbooks/{id}/toc  — дерево оглавления
  GET  /api/v1/textbook-exams/toc/{id}/skills     — навыки для параграфа
  POST /api/v1/textbook-exams/generate            — генерация экзамена
  GET  /api/v1/textbook-exams/textbooks/{id}/coverage — покрытие учебника
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import text

import logging

logger = logging.getLogger(__name__)

from src.core.database import get_db
from src.models.exam import GeneratedExam

router = APIRouter(prefix="/textbook-exams", tags=["Textbook Exams"])


# ============================================================================
# RESPONSE MODELS
# ============================================================================

class TextbookInfo(BaseModel):
    textbook_id: str
    title: str
    authors: List[str] = []
    class_level: int
    digitization_status: str = "pending"
    digitization_progress: float = 0
    tasks_extracted: int = 0
    cover_image_url: Optional[str] = None


class TocNode(BaseModel):
    id: int
    parent_id: Optional[int] = None
    level: str
    number: Optional[str] = None
    title: str
    page_start: Optional[int] = None
    sort_order: int = 0
    tasks_count: int = 0
    children: List["TocNode"] = []


TocNode.model_rebuild()


class SkillInParagraph(BaseModel):
    skill_id: str
    skill_name: str
    role: str = "primary"
    confidence: float = 0.8
    is_main: bool = False
    tasks_count: int = 0


class ExamTaskResponse(BaseModel):
    task_id: str
    skill_id: str
    skill_name: str
    difficulty: str
    question_text: str
    question_latex: Optional[str] = None
    question_image_url: Optional[str] = None
    answer_type: str = "exact_number"
    toc_title: Optional[str] = None


class GeneratedExamResponse(BaseModel):
    exam_id: str
    title: str
    tasks: List[ExamTaskResponse]
    total_tasks: int
    difficulty_distribution: Dict[str, int]
    skills_covered: List[str]
    paragraphs_covered: List[str]


class ExamRequest(BaseModel):
    title: str = Field(..., description="Название экзамена")
    textbook_id: str = Field(..., description="UUID учебника")
    toc_ids: List[int] = Field(..., description="ID параграфов из textbook_toc")
    tasks_count: int = Field(10, ge=1, le=50)
    difficulty_distribution: Dict[str, float] = Field(
        default={"A": 0.3, "B": 0.5, "C": 0.2},
        description="Пропорция сложности: A/B/C"
    )
    time_minutes: int = Field(45, ge=5, le=180)
    shuffle_tasks: bool = True


class CoverageItem(BaseModel):
    toc_id: int
    level: str
    number: Optional[str] = None
    title: str
    skills_mapped: int
    tasks_available: int
    is_digitized: bool


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.get("/textbooks", response_model=List[TextbookInfo])
async def list_textbooks(
    class_level: Optional[int] = Query(None, ge=5, le=11),
    db: Session = Depends(get_db),
):
    """Список учебников с textbook_toc"""
    query = """
        SELECT DISTINCT t.textbook_id::text, t.title, t.authors, t.class_level,
               t.digitization_status, t.digitization_progress, t.tasks_extracted,
               t.cover_image_url
        FROM textbooks t
        WHERE t.is_active = TRUE
    """
    params = {}
    if class_level:
        query += " AND t.class_level = :cl"
        params["cl"] = class_level

    query += " ORDER BY t.class_level, t.title"
    rows = db.execute(text(query), params).fetchall()

    return [
        TextbookInfo(
            textbook_id=r[0],
            title=r[1],
            authors=r[2] or [],
            class_level=r[3],
            digitization_status=r[4] or "pending",
            digitization_progress=float(r[5] or 0),
            tasks_extracted=r[6] or 0,
            cover_image_url=r[7],
        )
        for r in rows
    ]


@router.get("/textbooks/{textbook_id}/toc", response_model=List[TocNode])
async def get_textbook_toc(
    textbook_id: str,
    db: Session = Depends(get_db),
):
    """Дерево оглавления учебника (flat list, клиент строит дерево по parent_id)"""
    rows = db.execute(text("""
        SELECT toc.id, toc.parent_id, toc.level, toc.number, toc.title,
               toc.page_start, toc.sort_order,
               COALESCE(tc.cnt, 0) AS tasks_count
        FROM textbook_toc toc
        LEFT JOIN (
            SELECT tm.toc_id, COUNT(*) AS cnt
            FROM tasks_master tm
            WHERE tm.toc_id IS NOT NULL AND tm.is_active = TRUE
            GROUP BY tm.toc_id
        ) tc ON tc.toc_id = toc.id
        WHERE toc.textbook_id = :tb_id
        ORDER BY toc.sort_order
    """), {"tb_id": textbook_id}).fetchall()

    if not rows:
        raise HTTPException(404, "Учебник не найден или пуст")

    return [
        TocNode(
            id=r[0], parent_id=r[1], level=r[2], number=r[3],
            title=r[4], page_start=r[5], sort_order=r[6],
            tasks_count=r[7],
        )
        for r in rows
    ]


@router.get("/toc/{toc_id}/skills", response_model=List[SkillInParagraph])
async def get_toc_skills(
    toc_id: int,
    db: Session = Depends(get_db),
):
    """Навыки, привязанные к параграфу"""
    rows = db.execute(text("""
        SELECT kh.id, kh.name_ru, tsm.role, tsm.confidence, tsm.is_main,
               COUNT(tm.id) AS tasks_count
        FROM textbook_skill_map tsm
        JOIN knowledge_hierarchy kh ON kh.id = tsm.skill_id
        LEFT JOIN tasks_master tm ON tm.skill_id = tsm.skill_id
            AND tm.toc_id = tsm.toc_id AND tm.is_active = TRUE
        WHERE tsm.toc_id = :toc_id
        GROUP BY kh.id, kh.name_ru, tsm.role, tsm.confidence, tsm.is_main
        ORDER BY tsm.is_main DESC, tsm.role
    """), {"toc_id": toc_id}).fetchall()

    return [
        SkillInParagraph(
            skill_id=r[0], skill_name=r[1], role=r[2],
            confidence=float(r[3]), is_main=bool(r[4]), tasks_count=r[5],
        )
        for r in rows
    ]


@router.post("/generate", response_model=GeneratedExamResponse)
async def generate_textbook_exam(
    request: ExamRequest,
    db: Session = Depends(get_db),
):
    """
    Генерирует экзамен из выбранных параграфов учебника.
    Учитывает difficulty_distribution для баланса A/B/C.
    """
    if not request.toc_ids:
        raise HTTPException(400, "Укажите хотя бы один параграф (toc_ids)")

    # Вычисляем количество задач по сложностям
    dist = request.difficulty_distribution
    total = request.tasks_count
    count_a = round(total * dist.get("A", 0.3))
    count_b = round(total * dist.get("B", 0.5))
    count_c = total - count_a - count_b

    # Собираем toc_ids включая дочерние
    toc_ids_expanded = _expand_toc_ids(db, request.toc_ids)

    # Получаем задачи с учётом сложности
    all_tasks = []
    for difficulty, count in [("A", count_a), ("B", count_b), ("C", count_c)]:
        if count <= 0:
            continue
        rows = db.execute(text("""
            SELECT tm.id, tm.skill_id, kh.name_ru, tm.difficulty,
                   tm.question_text, tm.question_latex, tm.question_image_url,
                   tm.answer_type, toc.title AS toc_title
            FROM tasks_master tm
            JOIN knowledge_hierarchy kh ON kh.id = tm.skill_id
            LEFT JOIN textbook_toc toc ON toc.id = tm.toc_id
            WHERE tm.toc_id = ANY(:toc_ids)
              AND tm.is_active = TRUE
              AND tm.verification_status = 'verified'
              AND tm.difficulty = :diff
            ORDER BY RANDOM()
            LIMIT :lim
        """), {"toc_ids": toc_ids_expanded, "diff": difficulty, "lim": count}).fetchall()
        all_tasks.extend(rows)

    if not all_tasks:
        raise HTTPException(400, "Нет задач для выбранных параграфов. Оцифруйте учебник.")

    # Shuffle
    if request.shuffle_tasks:
        import random
        random.shuffle(all_tasks)

    # Build response
    tasks_resp = [
        ExamTaskResponse(
            task_id=r[0], skill_id=r[1], skill_name=r[2],
            difficulty=r[3], question_text=r[4],
            question_latex=r[5], question_image_url=r[6],
            answer_type=r[7], toc_title=r[8],
        )
        for r in all_tasks
    ]

    difficulty_dist = {}
    skills_set = set()
    paragraphs_set = set()
    for t in tasks_resp:
        difficulty_dist[t.difficulty] = difficulty_dist.get(t.difficulty, 0) + 1
        skills_set.add(t.skill_id)
        if t.toc_title:
            paragraphs_set.add(t.toc_title)

    exam_id = f"TBEXAM_{uuid4().hex[:8]}"

    # ── Persist to generated_exams so sessions can reference it ──
    variant_tasks = [
        {
            "task_id": t.task_id,
            "skill_id": t.skill_id,
            "skill_name": t.skill_name,
            "difficulty": t.difficulty,
            "question_text": t.question_text,
            "question_latex": t.question_latex,
            "question_image_url": t.question_image_url,
            "answer_type": t.answer_type,
            "toc_title": t.toc_title,
        }
        for t in tasks_resp
    ]
    exam_record = GeneratedExam(
        id=exam_id,
        title=request.title,
        variants=[{"variant_number": 1, "tasks": variant_tasks}],
        settings={
            "time_minutes": request.time_minutes,
            "shuffle_tasks": request.shuffle_tasks,
            "difficulty_distribution": request.difficulty_distribution,
            "source": "textbook",
            "textbook_id": request.textbook_id,
            "toc_ids": request.toc_ids,
        },
        skills_covered=list(skills_set),
        status="draft",
        total_time_minutes=request.time_minutes,
        variants_count=1,
        tasks_per_variant=len(tasks_resp),
    )
    db.add(exam_record)
    db.commit()
    logger.info(f"Textbook exam persisted: {exam_id}, {len(tasks_resp)} tasks")

    return GeneratedExamResponse(
        exam_id=exam_id,
        title=request.title,
        tasks=tasks_resp,
        total_tasks=len(tasks_resp),
        difficulty_distribution=difficulty_dist,
        skills_covered=list(skills_set),
        paragraphs_covered=list(paragraphs_set),
    )


@router.get("/textbooks/{textbook_id}/coverage", response_model=List[CoverageItem])
async def get_textbook_coverage(
    textbook_id: str,
    db: Session = Depends(get_db),
):
    """Покрытие учебника: сколько навыков / задач по каждому параграфу"""
    rows = db.execute(text("""
        SELECT toc.id, toc.level, toc.number, toc.title,
               COUNT(DISTINCT tsm.skill_id) AS skills_mapped,
               COUNT(DISTINCT tm.id) AS tasks_available,
               toc.is_digitized
        FROM textbook_toc toc
        LEFT JOIN textbook_skill_map tsm ON tsm.toc_id = toc.id
        LEFT JOIN tasks_master tm ON tm.toc_id = toc.id AND tm.is_active = TRUE
        WHERE toc.textbook_id = :tb_id
        GROUP BY toc.id, toc.level, toc.number, toc.title, toc.is_digitized
        ORDER BY toc.sort_order
    """), {"tb_id": textbook_id}).fetchall()

    return [
        CoverageItem(
            toc_id=r[0], level=r[1], number=r[2], title=r[3],
            skills_mapped=r[4], tasks_available=r[5],
            is_digitized=bool(r[6]),
        )
        for r in rows
    ]


# ============================================================================
# HELPERS
# ============================================================================

def _expand_toc_ids(db: Session, toc_ids: List[int]) -> List[int]:
    """
    Расширяет список toc_ids: если выбрана глава, включает все её параграфы.
    """
    rows = db.execute(text("""
        WITH RECURSIVE children AS (
            SELECT id FROM textbook_toc WHERE id = ANY(:ids)
            UNION ALL
            SELECT t.id FROM textbook_toc t
            JOIN children c ON t.parent_id = c.id
        )
        SELECT DISTINCT id FROM children
    """), {"ids": toc_ids}).fetchall()

    return [r[0] for r in rows]
