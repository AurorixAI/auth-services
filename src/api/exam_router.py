"""
ALGO V1 - Exam Router
Full exam lifecycle: generate в†’ save в†’ take в†’ submit в†’ results
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4
import random

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import text as sa_text

from src.models.exam import GeneratedExam, ExamSession
from src.models import TaskMaster
from src.utils.answer_check import check_answer as _check_answer
from src.core.database import get_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
router = APIRouter(tags=["Exams"])


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class ExamTaskPublic(BaseModel):
    """Task shown to student (no correct_answer)"""
    task_id: str
    skill_id: str = ""
    skill_name: str = ""
    difficulty: str = "medium"
    text: str
    options: List[str] = []
    image_url: Optional[str] = None
    answer_type: str = "multiple_choice"
    points: int = 10


class ExamInfoResponse(BaseModel):
    """Public info about an exam (by access code)"""
    exam_id: str
    title: str
    subject: str
    teacher_name: Optional[str]
    total_time_minutes: int
    tasks_per_variant: int
    variants_count: int
    status: str


class StartExamRequest(BaseModel):
    student_name: str = Field(..., min_length=1)
    class_level: Optional[int] = None


class StartExamResponse(BaseModel):
    session_id: str
    variant_number: int
    time_minutes: int
    tasks: List[ExamTaskPublic]


class SubmitExamRequest(BaseModel):
    session_id: str
    answers: Dict[str, str]  # {task_id: answer}
    time_spent_seconds: int = 0


class SubmitExamResponse(BaseModel):
    session_id: str
    score: int
    max_score: int
    answered_count: int
    correct_count: int
    total_tasks: int
    percentage: float


class TaskResultItem(BaseModel):
    task_id: str
    text: str
    options: List[str]
    student_answer: Optional[str]
    correct_answer: str
    is_correct: bool
    skill_name: str = ""
    error_type: Optional[str] = None
    error_explanation: Optional[str] = None


class SessionResultResponse(BaseModel):
    session_id: str
    exam_title: str
    student_name: str
    variant_number: int
    score: int
    max_score: int
    answered_count: int
    correct_count: int
    total_tasks: int
    percentage: float
    time_spent_seconds: int
    submitted_at: Optional[datetime]
    tasks: List[TaskResultItem] = []


class ExamListItem(BaseModel):
    exam_id: str
    title: str
    access_code: Optional[str] = None
    status: str
    tasks_count: int
    variants_count: int
    sessions_count: int
    created_at: datetime


class SessionListItem(BaseModel):
    session_id: str
    student_name: str
    class_level: Optional[int]
    variant_number: int
    score: int
    max_score: int
    status: str
    started_at: datetime
    submitted_at: Optional[datetime]


# ---------------------------------------------------------------------------
# Helper: generate access code
# ---------------------------------------------------------------------------

def _generate_access_code() -> str:
    import string
    chars = string.ascii_uppercase + string.digits
    code = "EX" + "".join(random.choices(chars, k=8))
    return code


# ---------------------------------------------------------------------------
# MC vs Open question selection logic
# ---------------------------------------------------------------------------

def should_show_as_mc(task: dict, mc_mode: str = "auto") -> bool:
    """
    РћРїСЂРµРґРµР»СЏРµС‚, РїРѕРєР°Р·С‹РІР°С‚СЊ Р»Рё Р·Р°РґР°С‡Сѓ РєР°Рє MC (СЃ РІР°СЂРёР°РЅС‚Р°РјРё РѕС‚РІРµС‚Р°)
    РёР»Рё РєР°Рє РѕС‚РєСЂС‹С‚С‹Р№ РІРѕРїСЂРѕСЃ (С‚РµРєСЃС‚РѕРІС‹Р№ РІРІРѕРґ).

    mc_mode values:
      - "auto"     вЂ” СЃРёСЃС‚РµРјРЅРѕРµ СЂРµС€РµРЅРёРµ (РїРѕ answer_type + difficulty + РЅР°Р»РёС‡РёРµ options)
      - "all_mc"   вЂ” РІСЃРµРіРґР° MC (РµСЃР»Рё РµСЃС‚СЊ РґРёСЃС‚СЂР°РєС‚РѕСЂС‹/options)
      - "all_open"вЂ” РІСЃРµРіРґР° РѕС‚РєСЂС‹С‚С‹Р№ РІРѕРїСЂРѕСЃ
      - "50", "70"  вЂ” РїСЂРѕС†РµРЅС‚ Р·Р°РґР°С‡ MC (int as string)

    Returns True if task should be shown as MC.
    """
    if mc_mode == "all_open":
        return False

    has_options = bool(task.get("answer_options") or task.get("options") or task.get("distractors"))

    if mc_mode == "all_mc":
        return has_options

    # "auto" mode: MC for easy/medium + numeric types + when distractors exist
    answer_type = task.get("answer_type", "exact_number")
    difficulty = task.get("difficulty", "medium").lower()

    mc_eligible_types = {"exact_number", "decimal", "multiple_choice", "true_false", "fraction"}
    mc_eligible_diffs = {"a", "b", "easy", "medium", "low", "normal"}

    if answer_type in mc_eligible_types and difficulty in mc_eligible_diffs and has_options:
        return True

    # For answer_type=multiple_choice, always show as MC if options exist
    if answer_type == "multiple_choice" and has_options:
        return True

    return False


def _build_mc_options(task: dict, correct_answer: str) -> List[str]:
    """
    РЎРѕР±РёСЂР°РµС‚ СЃРїРёСЃРѕРє РІР°СЂРёР°РЅС‚РѕРІ (correct + distractors), РїРµСЂРµРјРµС€РёРІР°РµС‚.
    """
    options = task.get("answer_options") or task.get("options") or []
    if isinstance(options, list) and len(options) > 0 and correct_answer:
        # Ensure correct answer is included
        correct_norm = correct_answer.strip()
        if correct_norm not in [o.strip() for o in options]:
            options = [correct_norm] + list(options)
        random.shuffle(options)
        return options

    # Try distractors + correct
    distractors = task.get("distractors") or []
    if distractors and correct_answer:
        options = [correct_answer.strip()] + [str(d) for d in distractors[:3]]
        random.shuffle(options)
        return options

    return []


# ---------------------------------------------------------------------------
# Request model for exam generation
# ---------------------------------------------------------------------------

class GenerateExamRequest(BaseModel):
    """Request body from frontend exam-creation wizard"""
    title: str = Field(..., min_length=1, max_length=300)
    textbook_id: Optional[str] = None
    skill_ids: List[str] = []           # "topics" mode
    paragraph_ids: List[str] = []       # "textbook" mode (toc_ids)
    tasks_count: int = Field(10, ge=1, le=50)
    difficulty_distribution: Optional[Dict[str, float]] = None  # {easy,medium,hard} %
    time_minutes: int = Field(45, ge=5, le=180)
    variants_count: int = Field(1, ge=1, le=30)
    generate_variants: bool = False
    shuffle_tasks: bool = True
    shuffle_options: bool = True
    mc_mode: str = "auto"


# ---------------------------------------------------------------------------
# HELPER: expand toc_ids to include children (recursive CTE)
# ---------------------------------------------------------------------------

def _expand_toc_ids(db: Session, toc_ids: List[int]) -> List[int]:
    """Recursively expand selected toc_ids to include child paragraphs."""
    rows = db.execute(sa_text("""
        WITH RECURSIVE children AS (
            SELECT id FROM textbook_toc WHERE id = ANY(:ids)
            UNION ALL
            SELECT t.id FROM textbook_toc t
            JOIN children c ON t.parent_id = c.id
        )
        SELECT DISTINCT id FROM children
    """), {"ids": toc_ids}).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# TEACHER: Generate exam  (POST /exams/generate)
# ---------------------------------------------------------------------------

@router.post(
    "/exams/generate",
    summary="Сгенерировать экзамен из выбранных навыков или параграфов учебника",
)
async def generate_exam(
    req: GenerateExamRequest,
    db: Session = Depends(get_db),
):
    """
    Единый эндпоинт генерации экзамена.

    Режим A — skill_ids ("по программе"): выбираются задачи по skill_id.
    Режим B — paragraph_ids ("по учебнику"): выбираются задачи по toc_id.
    """
    if not req.skill_ids and not req.paragraph_ids:
        raise HTTPException(
            status_code=400,
            detail="Укажите skill_ids (темы) или paragraph_ids (параграфы учебника)",
        )

    # ── Difficulty distribution ──────────────────────────────────────────
    dist = req.difficulty_distribution or {"easy": 30, "medium": 50, "hard": 20}

    # Normalise keys: frontend sends easy/medium/hard, DB uses A/B/C
    total_pct = (dist.get("easy", 0) + dist.get("medium", 0) + dist.get("hard", 0)) or 100
    count_a = round(req.tasks_count * dist.get("easy", 30) / total_pct)   # A = easy
    count_b = round(req.tasks_count * dist.get("medium", 50) / total_pct) # B = medium
    count_c = req.tasks_count - count_a - count_b                          # C = hard
    if count_c < 0:
        count_c = 0
        count_b = req.tasks_count - count_a

    all_tasks: list = []

    # ── Branch A: by skill_ids ──────────────────────────────────────────
    if req.skill_ids:
        for difficulty, count in [("A", count_a), ("B", count_b), ("C", count_c)]:
            if count <= 0:
                continue
            rows = db.execute(sa_text("""
                SELECT tm.id, tm.skill_id, kh.name_ru, tm.difficulty,
                       tm.question_text, tm.question_latex, tm.question_image_url,
                       tm.answer_type, tm.correct_answer, tm.answer_options,
                       tm.distractor_meta
                FROM tasks_master tm
                JOIN knowledge_hierarchy kh ON kh.id = tm.skill_id
                WHERE tm.skill_id = ANY(:skill_ids)
                  AND tm.is_active = TRUE
                  AND tm.difficulty = :diff
                ORDER BY RANDOM()
                LIMIT :lim
            """), {"skill_ids": req.skill_ids, "diff": difficulty, "lim": count}).fetchall()
            all_tasks.extend(rows)

        # If not enough tasks with strict difficulty, fill remainder
        if len(all_tasks) < req.tasks_count:
            existing_ids = [r[0] for r in all_tasks]
            fill = db.execute(sa_text("""
                SELECT tm.id, tm.skill_id, kh.name_ru, tm.difficulty,
                       tm.question_text, tm.question_latex, tm.question_image_url,
                       tm.answer_type, tm.correct_answer, tm.answer_options,
                       tm.distractor_meta
                FROM tasks_master tm
                JOIN knowledge_hierarchy kh ON kh.id = tm.skill_id
                WHERE tm.skill_id = ANY(:skill_ids)
                  AND tm.is_active = TRUE
                  AND tm.id != ALL(:used)
                ORDER BY RANDOM()
                LIMIT :lim
            """), {
                "skill_ids": req.skill_ids,
                "used": existing_ids or [""],
                "lim": req.tasks_count - len(all_tasks),
            }).fetchall()
            all_tasks.extend(fill)

    # ── Branch B: by paragraph_ids (textbook toc) ───────────────────────
    elif req.paragraph_ids:
        toc_int_ids = []
        for pid in req.paragraph_ids:
            try:
                toc_int_ids.append(int(pid))
            except (ValueError, TypeError):
                pass
        if not toc_int_ids:
            raise HTTPException(400, "paragraph_ids должны быть числовыми")

        toc_ids_expanded = _expand_toc_ids(db, toc_int_ids)
        if not toc_ids_expanded:
            raise HTTPException(400, "Параграфы не найдены в textbook_toc")

        for difficulty, count in [("A", count_a), ("B", count_b), ("C", count_c)]:
            if count <= 0:
                continue
            rows = db.execute(sa_text("""
                SELECT tm.id, tm.skill_id, kh.name_ru, tm.difficulty,
                       tm.question_text, tm.question_latex, tm.question_image_url,
                       tm.answer_type, tm.correct_answer, tm.answer_options,
                       tm.distractor_meta
                FROM tasks_master tm
                JOIN knowledge_hierarchy kh ON kh.id = tm.skill_id
                WHERE tm.toc_id = ANY(:toc_ids)
                  AND tm.is_active = TRUE
                  AND tm.difficulty = :diff
                ORDER BY RANDOM()
                LIMIT :lim
            """), {"toc_ids": toc_ids_expanded, "diff": difficulty, "lim": count}).fetchall()
            all_tasks.extend(rows)

        # Fill remainder
        if len(all_tasks) < req.tasks_count:
            existing_ids = [r[0] for r in all_tasks]
            fill = db.execute(sa_text("""
                SELECT tm.id, tm.skill_id, kh.name_ru, tm.difficulty,
                       tm.question_text, tm.question_latex, tm.question_image_url,
                       tm.answer_type, tm.correct_answer, tm.answer_options,
                       tm.distractor_meta
                FROM tasks_master tm
                JOIN knowledge_hierarchy kh ON kh.id = tm.skill_id
                WHERE tm.toc_id = ANY(:toc_ids)
                  AND tm.is_active = TRUE
                  AND tm.id != ALL(:used)
                ORDER BY RANDOM()
                LIMIT :lim
            """), {
                "toc_ids": toc_ids_expanded,
                "used": existing_ids or [""],
                "lim": req.tasks_count - len(all_tasks),
            }).fetchall()
            all_tasks.extend(fill)

    if not all_tasks:
        raise HTTPException(
            400,
            "Не найдено задач для выбранных тем/параграфов. "
            "Проверьте, что задачи оцифрованы и активны.",
        )

    # ── Shuffle ─────────────────────────────────────────────────────────
    if req.shuffle_tasks:
        random.shuffle(all_tasks)

    # ── Build variant tasks list ────────────────────────────────────────
    skills_set: set = set()
    variant_tasks: list = []
    for r in all_tasks:
        task_id, skill_id, skill_name, difficulty = r[0], r[1], r[2], r[3]
        question_text = r[4]
        question_latex = r[5]
        question_image_url = r[6]
        answer_type = r[7]
        correct_answer = r[8]
        answer_options = r[9]
        distractor_meta = r[10]

        skills_set.add(skill_id)

        td = {
            "task_id": task_id,
            "skill_id": skill_id,
            "skill_name": skill_name or "",
            "difficulty": difficulty or "B",
            "question_text": question_text or "",
            "question_latex": question_latex,
            "question_image_url": question_image_url,
            "answer_type": answer_type or "exact_number",
            "correct_answer": correct_answer or "",
            "answer_options": answer_options,
            "distractor_meta": distractor_meta,
            "is_template_generated": False,
        }

        # Build MC options if missing but distractor_meta exists
        if not td["answer_options"] and td["distractor_meta"] and td["correct_answer"]:
            dms = td["distractor_meta"]
            if isinstance(dms, list) and len(dms) > 0:
                opts = [td["correct_answer"]] + [str(d.get("value", "")) for d in dms[:3]]
                random.shuffle(opts)
                td["answer_options"] = opts

        variant_tasks.append(td)

    # ── Create GeneratedExam record ─────────────────────────────────────
    exam_id = f"EXAM_{uuid4().hex[:8]}"
    source = "topics" if req.skill_ids else "textbook"

    exam = GeneratedExam(
        id=exam_id,
        title=req.title,
        variants=[{"variant_number": 1, "tasks": variant_tasks}],
        settings={
            "time_minutes": req.time_minutes,
            "shuffle_tasks": req.shuffle_tasks,
            "shuffle_options": req.shuffle_options,
            "mc_mode": req.mc_mode,
            "source": source,
            "skill_ids": req.skill_ids,
            "paragraph_ids": req.paragraph_ids,
            "difficulty_distribution": req.difficulty_distribution,
        },
        skills_covered=list(skills_set),
        status="draft",
        total_time_minutes=req.time_minutes,
        variants_count=1,
        tasks_per_variant=len(variant_tasks),
    )
    db.add(exam)
    db.flush()  # Get ID before potential variant generation

    # ── Optional: auto-generate variants ────────────────────────────────
    if req.generate_variants and req.variants_count > 1:
        try:
            from src.domain.exam_builder.variant_orchestrator import VariantOrchestrator
            orch = VariantOrchestrator(db=db)
            report = orch.generate_variants(
                draft_tasks=variant_tasks,
                num_variants=req.variants_count,
                seed=42,
            )
            exam.variants = [v.to_dict() for v in report.variants]
            exam.variants_count = report.total_variants
            exam.status = "variants_ready"
            logger.info(
                f"Exam {exam_id}: generated {report.total_variants} variants "
                f"({report.tasks_templatized} templatized, {report.tasks_fallback} fallback)"
            )
        except Exception as exc:
            logger.warning(f"Variant generation failed for {exam_id}, keeping single variant: {exc}")
            db.rollback()
            # Re-add exam after rollback since the previous add was lost
            db.add(exam)

    db.commit()
    logger.info(f"Exam created: {exam_id}, {len(variant_tasks)} tasks, source={source}")

    return {
        "exam_id": exam_id,
        "status": exam.status,
        "tasks_count": len(variant_tasks),
        "variants_count": exam.variants_count,
        "skills_covered": list(skills_set),
    }


# ---------------------------------------------------------------------------
# PUBLIC: Get exam info by code
# ---------------------------------------------------------------------------

@router.get(
    "/exams/by-code/{code}",
    response_model=ExamInfoResponse,
    summary="РџРѕР»СѓС‡РёС‚СЊ РёРЅС„РѕСЂРјР°С†РёСЋ РѕР± СЌРєР·Р°РјРµРЅРµ РїРѕ РєРѕРґСѓ"
)
async def get_exam_by_code(code: str, db: Session = Depends(get_db)):
    exam = db.query(GeneratedExam).filter(GeneratedExam.access_code == code).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Р­РєР·Р°РјРµРЅ РЅРµ РЅР°Р№РґРµРЅ")
    if exam.status != "active":
        raise HTTPException(status_code=410, detail="Р­РєР·Р°РјРµРЅ Р·Р°РєСЂС‹С‚")

    variants = exam.variants or []
    tasks_count = len(variants[0].get("tasks", [])) if variants else 0

    return ExamInfoResponse(
        exam_id=exam.id,
        title=exam.title,
        subject=exam.subject or "РњР°С‚РµРјР°С‚РёРєР°",
        teacher_name=exam.teacher_name,
        total_time_minutes=exam.total_time_minutes,
        tasks_per_variant=tasks_count,
        variants_count=exam.variants_count,
        status=exam.status,
    )


# ---------------------------------------------------------------------------
# PUBLIC: Start exam session
# ---------------------------------------------------------------------------

@router.post(
    "/exams/by-code/{code}/start",
    response_model=StartExamResponse,
    summary="РќР°С‡Р°С‚СЊ СЌРєР·Р°РјРµРЅ"
)
async def start_exam_session(
    code: str,
    req: StartExamRequest,
    db: Session = Depends(get_db),
):
    exam = db.query(GeneratedExam).filter(GeneratedExam.access_code == code).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Р­РєР·Р°РјРµРЅ РЅРµ РЅР°Р№РґРµРЅ")
    if exam.status == "draft":
        raise HTTPException(status_code=403, detail="Р­РєР·Р°РјРµРЅ РµС‰С‘ РЅРµ РѕРїСѓР±Р»РёРєРѕРІР°РЅ")
    if exam.status not in ("active",):
        raise HTTPException(status_code=410, detail="Р­РєР·Р°РјРµРЅ Р·Р°РєСЂС‹С‚")

    variants = exam.variants or []
    if not variants:
        raise HTTPException(status_code=400, detail="Р’ СЌРєР·Р°РјРµРЅРµ РЅРµС‚ РІР°СЂРёР°РЅС‚РѕРІ")

    # Assign random variant
    variant_idx = random.randint(0, len(variants) - 1)
    variant = variants[variant_idx]

    session_id = f"SES_{uuid4().hex[:8]}"
    tasks_raw = variant.get("tasks", [])

    session = ExamSession(
        id=session_id,
        exam_id=exam.id,
        student_name=req.student_name,
        class_level=req.class_level,
        variant_number=variant.get("variant_number", variant_idx + 1),
        answers={},
        max_score=len(tasks_raw) * 10,
        status="in_progress",
    )
    db.add(session)
    db.commit()

    # Build task list without correct answers
    # в”Ђв”Ђ MC / Open logic в”Ђв”Ђ
    settings = exam.settings or {}
    mc_mode = settings.get("mc_mode", "auto")

    # If mc_mode is a percentage (e.g. "70"), we decide per-task randomly
    mc_pct: Optional[int] = None
    if mc_mode not in ("auto", "all_mc", "all_open"):
        try:
            mc_pct = int(mc_mode)
        except (ValueError, TypeError):
            mc_pct = None

    tasks_public = []
    for t in tasks_raw:
        raw_options = t.get("answer_options", t.get("options", [])) or []
        answer_type_val = t.get("answer_type", "multiple_choice")

        # Determine if this task should be MC
        if mc_pct is not None:
            show_mc = random.randint(1, 100) <= mc_pct and bool(raw_options or t.get("distractors"))
        else:
            show_mc = should_show_as_mc(t, mc_mode)

        if show_mc:
            options = _build_mc_options(t, t.get("correct_answer", ""))
            if not options:
                options = raw_options
            if options:
                answer_type_val = "multiple_choice"
        else:
            options = []
            if answer_type_val == "multiple_choice":
                answer_type_val = "exact_number"

        tasks_public.append(ExamTaskPublic(
            task_id=t.get("task_id", ""),
            skill_id=t.get("skill_id", ""),
            skill_name=t.get("skill_name", ""),
            difficulty=t.get("difficulty", "medium"),
            text=t.get("question_text", t.get("text", "")),
            options=options,
            image_url=t.get("question_image_url", t.get("image_url")),
            answer_type=answer_type_val,
            points=10,
        ))

    return StartExamResponse(
        session_id=session_id,
        variant_number=session.variant_number,
        time_minutes=exam.total_time_minutes,
        tasks=tasks_public,
    )


# ---------------------------------------------------------------------------
# PUBLIC: Submit exam
# ---------------------------------------------------------------------------

@router.post(
    "/exams/by-code/{code}/submit",
    response_model=SubmitExamResponse,
    summary="РЎРґР°С‚СЊ СЌРєР·Р°РјРµРЅ"
)
async def submit_exam(
    code: str,
    req: SubmitExamRequest,
    db: Session = Depends(get_db),
):
    session = db.query(ExamSession).filter(ExamSession.id == req.session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="РЎРµСЃСЃРёСЏ РЅРµ РЅР°Р№РґРµРЅР°")
    if session.status == "submitted":
        raise HTTPException(status_code=409, detail="Р­РєР·Р°РјРµРЅ СѓР¶Рµ СЃРґР°РЅ")

    exam = db.query(GeneratedExam).filter(GeneratedExam.id == session.exam_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Р­РєР·Р°РјРµРЅ РЅРµ РЅР°Р№РґРµРЅ")

    # Get the variant tasks to check answers
    variants = exam.variants or []
    variant = None
    for v in variants:
        if v.get("variant_number") == session.variant_number:
            variant = v
            break
    if not variant and variants:
        variant = variants[0]

    tasks_by_id = {}
    if variant:
        for t in variant.get("tasks", []):
            tasks_by_id[t.get("task_id")] = t

    # Grade answers + error analytics
    answers_map = req.answers  # already a dict
    answers_detail = {}  # Enhanced answers with error_type
    correct_count = 0
    score = 0
    for task_id, student_answer in answers_map.items():
        task = tasks_by_id.get(task_id)
        if task and task.get("correct_answer") and student_answer.strip():
            is_correct = _check_answer(
                student_answer,
                task["correct_answer"],
                task.get("answer_type", "multiple_choice")
            )
            if is_correct:
                correct_count += 1
                score += 10
                answers_detail[task_id] = {"answer": student_answer, "correct": True}
            else:
                # Check if answer matches a known distractor
                error_info = {"answer": student_answer, "correct": False}
                distractor_meta = task.get("distractor_meta") or []
                student_norm = student_answer.strip().lower().replace(",", ".").replace(" ", "")
                for dm in distractor_meta:
                    dm_val = str(dm.get("value", "")).strip().lower().replace(",", ".").replace(" ", "")
                    if student_norm == dm_val:
                        error_info["error_type"] = dm.get("error_type", "unknown")
                        error_info["error_explanation"] = dm.get("explanation", "")
                        break
                answers_detail[task_id] = error_info
        else:
            answers_detail[task_id] = {"answer": student_answer, "correct": False, "skipped": not student_answer.strip()}

    total_tasks = len(tasks_by_id)
    answered = sum(1 for v in answers_map.values() if v.strip())

    session.answers = answers_detail  # Now stores detailed info with error_type
    session.score = score
    session.correct_count = correct_count
    session.answered_count = answered
    session.status = "submitted"
    session.submitted_at = datetime.utcnow()
    session.time_spent_seconds = req.time_spent_seconds
    db.commit()

    return SubmitExamResponse(
        session_id=session.id,
        score=score,
        max_score=total_tasks * 10,
        answered_count=answered,
        correct_count=correct_count,
        total_tasks=total_tasks,
        percentage=round((correct_count / total_tasks * 100) if total_tasks > 0 else 0, 1),
    )


# ---------------------------------------------------------------------------
# PUBLIC: Get session result
# ---------------------------------------------------------------------------

@router.get(
    "/exams/sessions/{session_id}/result",
    response_model=SessionResultResponse,
    summary="Р РµР·СѓР»СЊС‚Р°С‚ СЌРєР·Р°РјРµРЅР°"
)
async def get_session_result(session_id: str, db: Session = Depends(get_db)):
    session = db.query(ExamSession).filter(ExamSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="РЎРµСЃСЃРёСЏ РЅРµ РЅР°Р№РґРµРЅР°")

    exam = db.query(GeneratedExam).filter(GeneratedExam.id == session.exam_id).first()
    exam_title = exam.title if exam else "Р­РєР·Р°РјРµРЅ"

    total_tasks = session.max_score // 10 if session.max_score else 0

    # Build detailed task results
    task_results = []
    if exam:
        variants = exam.variants or []
        variant = None
        for v in variants:
            if v.get("variant_number") == session.variant_number:
                variant = v
                break
        if not variant and variants:
            variant = variants[0]
        if variant:
            answers_map = session.answers or {}
            for t in variant.get("tasks", []):
                tid = t.get("task_id", "")
                raw_ans = answers_map.get(tid)
                correct_ans = t.get("correct_answer", "")

                # answers may be stored as dict (new format) or str (legacy)
                if isinstance(raw_ans, dict):
                    student_ans = raw_ans.get("answer")
                    is_correct = raw_ans.get("correct", False)
                    error_type = raw_ans.get("error_type")
                    error_explanation = raw_ans.get("error_explanation")
                else:
                    student_ans = raw_ans
                    is_correct = _check_answer(
                        student_ans or "", correct_ans,
                        t.get("answer_type", "multiple_choice")
                    ) if student_ans else False
                    error_type = None
                    error_explanation = None

                task_results.append(TaskResultItem(
                    task_id=tid,
                    text=t.get("question_text", t.get("text", "")),
                    options=t.get("answer_options", t.get("options", [])) or [],
                    student_answer=student_ans,
                    correct_answer=correct_ans,
                    is_correct=is_correct,
                    skill_name=t.get("skill_name", ""),
                    error_type=error_type,
                    error_explanation=error_explanation,
                ))

    return SessionResultResponse(
        session_id=session.id,
        exam_title=exam_title,
        student_name=session.student_name,
        variant_number=session.variant_number,
        score=session.score or 0,
        max_score=session.max_score or 0,
        answered_count=session.answered_count or 0,
        correct_count=session.correct_count or 0,
        total_tasks=total_tasks,
        percentage=round(
            (session.correct_count / total_tasks * 100) if total_tasks > 0 else 0, 1
        ),
        time_spent_seconds=session.time_spent_seconds or 0,
        submitted_at=session.submitted_at,
        tasks=task_results,
    )


# ---------------------------------------------------------------------------
# TEACHER: List exams
# ---------------------------------------------------------------------------

@router.get(
    "/exams",
    response_model=List[ExamListItem],
    summary="РЎРїРёСЃРѕРє СЌРєР·Р°РјРµРЅРѕРІ РѕСЂРіР°РЅРёР·Р°С†РёРё"
)
async def list_exams(db: Session = Depends(get_db)):
    exams = db.query(GeneratedExam).order_by(GeneratedExam.created_at.desc()).all()
    result = []
    for e in exams:
        sessions_count = db.query(ExamSession).filter(ExamSession.exam_id == e.id).count()
        variants = e.variants or []
        tasks_count = len(variants[0].get("tasks", [])) if variants else 0
        result.append(ExamListItem(
            exam_id=e.id,
            title=e.title,
            access_code=e.access_code,
            status=e.status,
            tasks_count=tasks_count,
            variants_count=e.variants_count or 1,
            sessions_count=sessions_count,
            created_at=e.created_at,
        ))
    return result


# ---------------------------------------------------------------------------
# TEACHER: List sessions for an exam
# ---------------------------------------------------------------------------

@router.get(
    "/exams/{exam_id}/sessions",
    response_model=List[SessionListItem],
    summary="РЎРїРёСЃРѕРє СЃРґР°РІС€РёС… СЌРєР·Р°РјРµРЅ"
)
async def list_exam_sessions(exam_id: str, db: Session = Depends(get_db)):
    sessions = db.query(ExamSession).filter(
        ExamSession.exam_id == exam_id
    ).order_by(ExamSession.started_at.desc()).all()

    return [
        SessionListItem(
            session_id=s.id,
            student_name=s.student_name,
            class_level=s.class_level,
            variant_number=s.variant_number,
            score=s.score or 0,
            max_score=s.max_score or 0,
            status=s.status,
            started_at=s.started_at,
            submitted_at=s.submitted_at,
        )
        for s in sessions
    ]


# ---------------------------------------------------------------------------
# TEACHER: Get exam draft (full data with answers)
# ---------------------------------------------------------------------------

class DraftTaskItem(BaseModel):
    task_id: str
    skill_id: str = ""
    skill_name: str = ""
    difficulty: str = "B"
    question_text: str
    question_latex: Optional[str] = None
    question_image_url: Optional[str] = None
    answer_type: str = "exact_number"
    correct_answer: str = ""
    answer_options: Optional[List[str]] = None
    distractor_meta: Optional[List[Dict[str, Any]]] = None
    is_template_generated: bool = False


class DraftVariantItem(BaseModel):
    variant_id: str
    variant_number: int
    total_points: int
    tasks: List[DraftTaskItem]


class DraftResponse(BaseModel):
    exam_id: str
    title: str
    status: str
    total_time_minutes: int
    variants_count: int
    tasks_per_variant: int
    settings: Dict[str, Any] = {}
    skills_covered: List[str] = []
    variants: List[DraftVariantItem] = []
    created_at: Optional[datetime] = None


@router.get(
    "/exams/{exam_id}/draft",
    response_model=DraftResponse,
    summary="РџРѕР»СѓС‡РёС‚СЊ С‡РµСЂРЅРѕРІРёРє СЌРєР·Р°РјРµРЅР° (РґР»СЏ СѓС‡РёС‚РµР»СЏ)"
)
async def get_exam_draft(exam_id: str, db: Session = Depends(get_db)):
    exam = db.query(GeneratedExam).filter(GeneratedExam.id == exam_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Р­РєР·Р°РјРµРЅ РЅРµ РЅР°Р№РґРµРЅ")

    variants_data = []
    for v in (exam.variants or []):
        tasks = []
        for t in v.get("tasks", []):
            tasks.append(DraftTaskItem(
                task_id=t.get("task_id", ""),
                skill_id=t.get("skill_id", ""),
                skill_name=t.get("skill_name", ""),
                difficulty=t.get("difficulty", "B"),
                question_text=t.get("question_text", ""),
                question_latex=t.get("question_latex"),
                question_image_url=t.get("question_image_url"),
                answer_type=t.get("answer_type", "exact_number"),
                correct_answer=t.get("correct_answer", ""),
                answer_options=t.get("answer_options"),
                distractor_meta=t.get("distractor_meta"),
                is_template_generated=t.get("is_template_generated", False),
            ))
        variants_data.append(DraftVariantItem(
            variant_id=v.get("variant_id", ""),
            variant_number=v.get("variant_number", 1),
            total_points=v.get("total_points", 0),
            tasks=tasks,
        ))

    return DraftResponse(
        exam_id=exam.id,
        title=exam.title,
        status=exam.status,
        total_time_minutes=exam.total_time_minutes or 45,
        variants_count=exam.variants_count or 1,
        tasks_per_variant=exam.tasks_per_variant or 0,
        settings=exam.settings or {},
        skills_covered=exam.skills_covered or [],
        variants=variants_data,
        created_at=exam.created_at,
    )


# ---------------------------------------------------------------------------
# TEACHER: Replace task in draft
# ---------------------------------------------------------------------------

class ReplaceTaskRequest(BaseModel):
    variant_number: int
    task_id: str


@router.post(
    "/exams/{exam_id}/draft/replace-task",
    summary="Р—Р°РјРµРЅРёС‚СЊ Р·Р°РґР°С‡Сѓ РІ С‡РµСЂРЅРѕРІРёРєРµ"
)
async def replace_task_in_draft(
    exam_id: str,
    req: ReplaceTaskRequest,
    db: Session = Depends(get_db),
):
    exam = db.query(GeneratedExam).filter(GeneratedExam.id == exam_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Р­РєР·Р°РјРµРЅ РЅРµ РЅР°Р№РґРµРЅ")
    if exam.status != "draft":
        raise HTTPException(status_code=400, detail="РњРѕР¶РЅРѕ СЂРµРґР°РєС‚РёСЂРѕРІР°С‚СЊ С‚РѕР»СЊРєРѕ С‡РµСЂРЅРѕРІРёРє")

    variants = exam.variants or []
    variant = None
    variant_idx = -1
    for i, v in enumerate(variants):
        if v.get("variant_number") == req.variant_number:
            variant = v
            variant_idx = i
            break
    if variant is None:
        raise HTTPException(status_code=404, detail="Р’Р°СЂРёР°РЅС‚ РЅРµ РЅР°Р№РґРµРЅ")

    # Find the task to replace
    tasks = variant.get("tasks", [])
    task_idx = -1
    old_task = None
    for i, t in enumerate(tasks):
        if t.get("task_id") == req.task_id:
            task_idx = i
            old_task = t
            break
    if old_task is None:
        raise HTTPException(status_code=404, detail="Р—Р°РґР°С‡Р° РЅРµ РЅР°Р№РґРµРЅР° РІ РІР°СЂРёР°РЅС‚Рµ")

    # Find replacement: same skill + same difficulty, not used in this variant
    from src.models import TaskMaster
    used_ids = {t.get("task_id") for t in tasks}
    skill_id = old_task.get("skill_id", "")
    difficulty = old_task.get("difficulty", "B")

    candidates = db.query(TaskMaster).filter(
        TaskMaster.skill_id == skill_id,
        TaskMaster.difficulty == difficulty,
        TaskMaster.is_active == True,
        ~TaskMaster.id.in_(used_ids),
    ).limit(10).all()

    if not candidates:
        # Try any difficulty
        candidates = db.query(TaskMaster).filter(
            TaskMaster.skill_id == skill_id,
            TaskMaster.is_active == True,
            ~TaskMaster.id.in_(used_ids),
        ).limit(10).all()

    if not candidates:
        raise HTTPException(status_code=404, detail="РќРµС‚ Р°Р»СЊС‚РµСЂРЅР°С‚РёРІРЅС‹С… Р·Р°РґР°С‡ РґР»СЏ СЌС‚РѕРіРѕ РЅР°РІС‹РєР°")

    new_task = random.choice(candidates)

    # Build new task dict with distractors
    new_td = {
        "task_id": new_task.id,
        "skill_id": new_task.skill_id,
        "skill_name": old_task.get("skill_name", ""),
        "difficulty": new_task.difficulty or "B",
        "question_text": new_task.question_text or "",
        "question_latex": new_task.question_latex,
        "question_image_url": new_task.question_image_url,
        "answer_type": new_task.answer_type or "exact_number",
        "correct_answer": new_task.correct_answer or "",
        "answer_options": new_task.answer_options,
        "is_template_generated": False,
    }

    # Generate distractors if missing
    if not new_td["answer_options"]:
        try:
            from src.domain.exam_builder.sympy_step_solver import StepSolver, StepDistractorEngine
            solver = StepSolver()
            distractor_engine = StepDistractorEngine()

            answer_str = new_td["correct_answer"]
            try:
                answer_val = float(answer_str.replace(",", "."))
            except ValueError:
                answer_val = None

            if answer_val is not None:
                # Решаем по шагам, получаем дистракторы
                steps = solver.solve(
                    "arithmetic",  # generic fallback
                    "",  # no formula
                    {},  # no params
                    new_td["question_text"],
                )
                distractors = distractor_engine.generate(steps, answer_val, "arithmetic", count=3)
                if distractors:
                    opts = [answer_str] + [d.value for d in distractors[:3]]
                    random.shuffle(opts)
                    new_td["answer_options"] = opts
                    new_td["distractor_meta"] = [
                        {
                            "value": d.value,
                            "error_type": d.error_type,
                            "broken_step": d.broken_step,
                            "plausibility": d.plausibility,
                            "explanation": d.explanation,
                        }
                        for d in distractors[:3]
                    ]
        except Exception:
            pass

    # Replace in JSONB
    tasks[task_idx] = new_td
    variant["tasks"] = tasks
    variants[variant_idx] = variant
    exam.variants = variants

    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(exam, "variants")
    db.commit()

    return {"status": "ok", "new_task": new_td}


# ---------------------------------------------------------------------------
# TEACHER: Get alternative tasks for replacement
# ---------------------------------------------------------------------------

@router.get(
    "/exams/{exam_id}/draft/alternatives",
    summary="РђР»СЊС‚РµСЂРЅР°С‚РёРІРЅС‹Рµ Р·Р°РґР°С‡Рё РґР»СЏ Р·Р°РјРµРЅС‹"
)
async def get_alternatives(
    exam_id: str,
    variant_number: int = Query(1),
    task_id: str = Query(...),
    db: Session = Depends(get_db),
):
    exam = db.query(GeneratedExam).filter(GeneratedExam.id == exam_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Р­РєР·Р°РјРµРЅ РЅРµ РЅР°Р№РґРµРЅ")

    # Find current task to know skill + difficulty
    for v in (exam.variants or []):
        if v.get("variant_number") == variant_number:
            for t in v.get("tasks", []):
                if t.get("task_id") == task_id:
                    skill_id = t.get("skill_id", "")
                    difficulty = t.get("difficulty", "B")
                    used_ids = {tt.get("task_id") for tt in v.get("tasks", [])}

                    from src.models import TaskMaster
                    candidates = db.query(TaskMaster).filter(
                        TaskMaster.skill_id == skill_id,
                        TaskMaster.is_active == True,
                        ~TaskMaster.id.in_(used_ids),
                    ).order_by(
                        # Same difficulty first
                        (TaskMaster.difficulty != difficulty).asc()
                    ).limit(10).all()

                    return [
                        {
                            "task_id": c.id,
                            "question_text": c.question_text,
                            "question_latex": c.question_latex,
                            "difficulty": c.difficulty,
                            "correct_answer": c.correct_answer,
                            "answer_type": c.answer_type,
                        }
                        for c in candidates
                    ]

    raise HTTPException(status_code=404, detail="Р—Р°РґР°С‡Р° РЅРµ РЅР°Р№РґРµРЅР°")


# ---------------------------------------------------------------------------
# TEACHER: Generate variants (SymPy engine)
# ---------------------------------------------------------------------------

class GenerateVariantsRequest(BaseModel):
    num_variants: int = Field(default=2, ge=1, le=30)
    seed: int = Field(default=42)


class GenerateVariantsResponse(BaseModel):
    exam_id: str
    total_variants: int
    tasks_templatized: int
    tasks_fallback: int
    tasks_total: int
    status: str
    errors: List[str] = []


@router.post(
    "/exams/{exam_id}/generate-variants",
    response_model=GenerateVariantsResponse,
    summary="РЎРіРµРЅРµСЂРёСЂРѕРІР°С‚СЊ РІР°СЂРёР°РЅС‚С‹ СЌРєР·Р°РјРµРЅР° С‡РµСЂРµР· SymPy-РґРІРёР¶РѕРє",
)
async def generate_variants(
    exam_id: str,
    body: GenerateVariantsRequest,
    db: Session = Depends(get_db),
):
    """
    Р“РµРЅРµСЂРёСЂСѓРµС‚ N СѓРЅРёРєР°Р»СЊРЅС‹С… РІР°СЂРёР°РЅС‚РѕРІ РґР»СЏ С‡РµСЂРЅРѕРІРёРєР° СЌРєР·Р°РјРµРЅР°.

    - РљР°Р¶РґР°СЏ Р·Р°РґР°С‡Р° РїР°СЂР°РјРµС‚СЂРёР·СѓРµС‚СЃСЏ С‡РµСЂРµР· SymPy
    - Р”Р»СЏ РєР°Р¶РґРѕРіРѕ РІР°СЂРёР°РЅС‚Р° РіРµРЅРµСЂРёСЂСѓСЋС‚СЃСЏ СѓРЅРёРєР°Р»СЊРЅС‹Рµ РЅР°Р±РѕСЂС‹ РїР°СЂР°РјРµС‚СЂРѕРІ
    - Р”РёСЃС‚СЂР°РєС‚РѕСЂС‹ СЃРѕР·РґР°СЋС‚СЃСЏ С‡РµСЂРµР· РїРѕС€Р°РіРѕРІРѕРµ СЂРµС€РµРЅРёРµ
    - Р•СЃР»Рё Р·Р°РґР°С‡Сѓ РЅРµР»СЊР·СЏ РїР°СЂР°РјРµС‚СЂРёР·РѕРІР°С‚СЊ вЂ” РѕСЃС‚Р°С‘С‚СЃСЏ РѕСЂРёРіРёРЅР°Р»СЊРЅРѕР№

    Р”РѕСЃС‚СѓРїРµРЅ РўРћР›Р¬РљРћ РґР»СЏ draft СЌРєР·Р°РјРµРЅРѕРІ.
    """
    import time as _time

    exam = db.query(GeneratedExam).filter(GeneratedExam.id == exam_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Р­РєР·Р°РјРµРЅ РЅРµ РЅР°Р№РґРµРЅ")
    if exam.status not in ("draft", "variants_ready"):
        raise HTTPException(
            status_code=400,
            detail="Р“РµРЅРµСЂР°С†РёСЏ РґРѕСЃС‚СѓРїРЅР° С‚РѕР»СЊРєРѕ РґР»СЏ С‡РµСЂРЅРѕРІРёРєРѕРІ",
        )

    variants_jsonb = exam.variants or []
    if not variants_jsonb:
        raise HTTPException(status_code=400, detail="РќРµС‚ Р·Р°РґР°С‡ РІ С‡РµСЂРЅРѕРІРёРєРµ")

    # Р‘РµСЂС‘Рј Р·Р°РґР°С‡Рё РёР· РїРµСЂРІРѕРіРѕ РІР°СЂРёР°РЅС‚Р° (РѕСЃРЅРѕРІРЅРѕР№ draft)
    base_variant = variants_jsonb[0] if isinstance(variants_jsonb, list) else {}
    draft_tasks = base_variant.get("tasks", [])
    if not draft_tasks:
        raise HTTPException(status_code=400, detail="РќРµС‚ Р·Р°РґР°С‡ РІ С‡РµСЂРЅРѕРІРёРєРµ")

    # Р“РµРЅРµСЂРёСЂСѓРµРј РІР°СЂРёР°РЅС‚С‹
    from src.domain.exam_builder.variant_orchestrator import VariantOrchestrator

    t0 = _time.perf_counter()
    orch = VariantOrchestrator(db=db)
    report = orch.generate_variants(
        draft_tasks=draft_tasks,
        num_variants=body.num_variants,
        seed=body.seed,
    )
    elapsed_ms = int((_time.perf_counter() - t0) * 1000)

    # РЎРѕС…СЂР°РЅСЏРµРј РІР°СЂРёР°РЅС‚С‹ РІ JSONB
    new_variants = [v.to_dict() for v in report.variants]
    exam.variants = new_variants
    exam.variants_count = report.total_variants
    exam.status = "variants_ready"
    db.commit()

    return GenerateVariantsResponse(
        exam_id=exam_id,
        total_variants=report.total_variants,
        tasks_templatized=report.tasks_templatized,
        tasks_fallback=report.tasks_fallback,
        tasks_total=report.tasks_total,
        status="variants_ready",
        errors=report.errors[:20],  # limit errors to 20
    )


# ---------------------------------------------------------------------------
# TEACHER: Publish draft в†’ active
# ---------------------------------------------------------------------------

@router.post(
    "/exams/{exam_id}/publish",
    summary="РћРїСѓР±Р»РёРєРѕРІР°С‚СЊ С‡РµСЂРЅРѕРІРёРє СЌРєР·Р°РјРµРЅР°"
)
async def publish_exam(exam_id: str, db: Session = Depends(get_db)):
    exam = db.query(GeneratedExam).filter(GeneratedExam.id == exam_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Р­РєР·Р°РјРµРЅ РЅРµ РЅР°Р№РґРµРЅ")
    if exam.status not in ("draft", "variants_ready"):
        raise HTTPException(status_code=400, detail="Р­РєР·Р°РјРµРЅ СѓР¶Рµ РѕРїСѓР±Р»РёРєРѕРІР°РЅ")

    # Generate access code
    access_code = _generate_access_code()

    # Check uniqueness
    while db.query(GeneratedExam).filter(GeneratedExam.access_code == access_code).first():
        access_code = _generate_access_code()

    exam.access_code = access_code
    exam.status = "active"
    db.commit()

    return {
        "status": "active",
        "access_code": access_code,
        "exam_id": exam.id,
        "url": f"/e/{access_code}",
    }


# ---------------------------------------------------------------------------
# TEACHER: Close exam
# ---------------------------------------------------------------------------

@router.post(
    "/exams/{exam_id}/close",
    summary="Р—Р°РєСЂС‹С‚СЊ СЌРєР·Р°РјРµРЅ"
)
async def close_exam(exam_id: str, db: Session = Depends(get_db)):
    exam = db.query(GeneratedExam).filter(GeneratedExam.id == exam_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Р­РєР·Р°РјРµРЅ РЅРµ РЅР°Р№РґРµРЅ")
    exam.status = "closed"
    db.commit()
    return {"status": "closed"}


# ---------------------------------------------------------------------------
# TEACHER: Exam analytics (post-exam skill gap analysis)
# ---------------------------------------------------------------------------

@router.get(
    "/exams/{exam_id}/analytics",
    summary="Аналитика экзамена (skill gaps, дистрактор-анализ)",
    tags=["Exams"],
)
async def exam_analytics(
    exam_id: str,
    session_id: Optional[str] = Query(None, description="Конкретная сессия (иначе аггрегация по всем)"),
    db: Session = Depends(get_db),
):
    """
    Пост-экзаменная аналитика.

    Если указан session_id — анализ одного ученика.
    Если не указан — агрегация по всем submitted сессиям экзамена.
    """
    from src.domain.exam_builder.analytics import ExamAnalytics, StudentAnswer

    exam = db.query(GeneratedExam).filter(GeneratedExam.id == exam_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Экзамен не найден")

    # Resolve sessions
    if session_id:
        sessions = [db.query(ExamSession).filter(ExamSession.id == session_id).first()]
        if not sessions[0]:
            raise HTTPException(status_code=404, detail="Сессия не найдена")
    else:
        sessions = (
            db.query(ExamSession)
            .filter(ExamSession.exam_id == exam_id, ExamSession.status == "submitted")
            .all()
        )
    if not sessions:
        return {"exam_id": exam_id, "reports": [], "message": "Нет сданных сессий"}

    variants = exam.variants or []
    analytics = ExamAnalytics(db_session=db)
    reports = []

    for ses in sessions:
        # Find the variant the student took
        variant = None
        for v in variants:
            if v.get("variant_number") == ses.variant_number:
                variant = v
                break
        if not variant and variants:
            variant = variants[0]
        if not variant:
            continue

        # Build tasks_meta from variant tasks
        tasks_meta = []
        for t in variant.get("tasks", []):
            tasks_meta.append({
                "task_id": t.get("task_id", ""),
                "skill_id": t.get("skill_id") or None,
                "skill_name": t.get("skill_name", ""),
                "correct_answer": t.get("correct_answer", ""),
                "answer_type": t.get("answer_type", "text"),
                "distractors": t.get("distractor_meta", []),
                "common_mistakes": t.get("common_mistakes", []),
            })

        # Build answers from session
        answers_raw = ses.answers or {}
        student_answers = []
        for task_id, ans_data in answers_raw.items():
            if isinstance(ans_data, dict):
                student_text = ans_data.get("answer", "")
            else:
                student_text = str(ans_data) if ans_data else ""
            student_answers.append(StudentAnswer(
                task_id=task_id,
                student_answer=student_text,
            ))

        report = analytics.analyze(
            exam_id=exam_id,
            student_id=ses.student_name,
            answers=student_answers,
            tasks_meta=tasks_meta,
        )
        reports.append(report.to_dict())

    return {
        "exam_id": exam_id,
        "reports": reports,
        "sessions_analyzed": len(reports),
    }
