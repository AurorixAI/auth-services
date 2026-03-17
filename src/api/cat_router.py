"""
ALGO V1 - CAT Router
REST API эндпоинты для адаптивного тестирования (Shadow CAT Engine)
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime
from uuid import uuid4
import logging

from src.diagnostic.cat_engine import (
    ShadowCATEngine, CATConfig, CATPhase, CATTask,
    ContentBlueprint, ExposureController, build_blueprint_from_skills
)
from src.diagnostic.integrity_service import (
    IntegrityService, IntegrityReport
)
from src.diagnostic.irt_engine import IRTTaskParams
from src.auth.dependencies import get_optional_user, require_org_teacher
from src.auth.service import AuthService
from src.utils.answer_check import check_answer as _check_answer
from src.models.repositories import (
    KnowledgeRepository as _KnowledgeRepo,
    TaskRepository as _DBTaskRepo,
    AnswerEventRepository,
    SessionRepository,
    StudentSkillRepository,
)

logger = logging.getLogger(__name__)

from src.api.database import get_db

router = APIRouter(prefix="/cat", tags=["CAT - Adaptive Testing"])


# ============================================================================
# SCHEMAS
# ============================================================================

class CATStartRequest(BaseModel):
    """Запрос на старт CAT-сессии"""
    class_level: Optional[int] = Field(None, ge=5, le=11, description="Класс ученика")
    skill_ids: Optional[List[str]] = Field(None, description="Фильтр по навыкам (опционально)")
    max_items: int = Field(30, ge=5, le=50, description="Максимум задач")
    prior_theta: Optional[float] = Field(None, description="Предварительная оценка θ")
    diagnostic_code: Optional[str] = Field(None, description="Код диагностики (QR)")


class CATAnswerRequest(BaseModel):
    """Ответ на CAT-задачу"""
    task_id: str
    answer: str
    time_ms: int = Field(..., ge=0)


class CATTaskResponse(BaseModel):
    """Задача CAT"""
    task_id: str
    skill_id: str
    difficulty: str
    question_text: str
    question_latex: Optional[str] = None
    question_image_url: Optional[str] = None
    answer_type: str
    phase: str
    item_number: int
    options: Optional[List[str]] = None  # MC варианты (только в WARMUP)


class CATStartResponse(BaseModel):
    """Ответ на старт CAT"""
    session_id: str
    phase: str
    config: Dict[str, Any]
    first_task: CATTaskResponse


class CATAnswerResponse(BaseModel):
    """Ответ после сабмита"""
    is_correct: bool
    correct_answer: Optional[str] = None
    theta: float
    theta_se: float
    phase: str
    item_number: int
    accuracy: float
    next_task: Optional[CATTaskResponse] = None
    is_complete: bool = False
    flags: List[str] = []


class CATResultResponse(BaseModel):
    """Полный результат CAT"""
    session_id: str
    student_id: Optional[str]
    
    # Оценка
    theta_eap: float
    theta_mle: Optional[float]
    se: float
    ci_low: float
    ci_high: float
    
    # Статистика
    total_items: int
    correct: int
    accuracy: float
    
    # Контент
    skills_tested: List[str]
    difficulty_distribution: Dict[str, int]
    
    # Integrity
    response_pattern: str
    fast_responses: int
    slow_responses: int
    
    # Trajectories
    theta_trajectory: List[float]
    se_trajectory: List[float]
    
    # Integrity report
    integrity: Dict[str, Any]


class CATProgressResponse(BaseModel):
    """Прогресс CAT"""
    phase: str
    total_items: int
    items_in_phase: int
    theta: float
    se: Optional[float]
    accuracy: float
    skills_tested: int
    available_items: int


# ============================================================================
# SESSION STORAGE (Redis with in-memory fallback)
# ============================================================================

from src.api.redis_sessions import session_store as _sessions


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.post("/start", response_model=CATStartResponse, status_code=201)
def start_cat_session(
    request: CATStartRequest,
    db: Session = Depends(get_db),
    user: Optional[dict] = Depends(get_optional_user)
):
    """
    Начать новую CAT-сессию.
    
    Создаёт Shadow CAT Engine с контент-балансировкой,
    загружает задачи из БД и возвращает первую задачу.
    """
    student_id = user.get("sub") if user else None
    session_id = str(uuid4())
    diagnostic_invite_info = None
    
    # Если есть diagnostic_code — связываем с диагностикой
    if request.diagnostic_code:
        auth_svc = AuthService(db)
        try:
            diagnostic_invite_info = auth_svc.start_diagnostic(request.diagnostic_code, session_id)
            # Получим student_id и org_id из диагностики
            diag_info = auth_svc.get_diagnostic_by_code(request.diagnostic_code)
            if diag_info:
                student_id = diag_info.get("student_id")
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    
    # Rate limiting
    rate_key = student_id or "anon"
    if not _sessions.check_rate_limit(rate_key):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Слишком много стартов сессий. Подождите минуту."
        )
    
    # Конфигурация CAT
    config = CATConfig(
        absolute_max_items=request.max_items,
        prior_mean=request.prior_theta or 0.0,
    )
    
    # Загружаем навыки и задачи
    knowledge_repo = _KnowledgeRepo(db)
    task_repo = _DBTaskRepo(db)
    
    # Получаем граф навыков
    skill_graph = knowledge_repo.get_skill_graph()
    if not skill_graph:
        raise HTTPException(503, "Граф навыков пуст")
    
    # Загружаем задачи из БД и конвертируем в CATTask
    cat_tasks = _load_cat_tasks(task_repo, skill_graph, request.skill_ids, request.class_level)
    
    if not cat_tasks:
        raise HTTPException(400, "Нет доступных задач для указанных навыков")
    
    # Строим blueprint
    skills_dict = {
        sid: {"level": node.level, "domain_id": getattr(node, 'parent_id', None)}
        for sid, node in skill_graph.items()
    }
    blueprint = build_blueprint_from_skills(skills_dict, request.max_items)
    
    # Создаём CAT Engine
    engine = ShadowCATEngine(
        config=config,
        blueprint=blueprint,
        task_pool=cat_tasks,
        skill_graph=skill_graph,
    )
    
    engine.start_session(session_id, student_id, request.prior_theta)
    
    # Выбираем первую задачу
    first_task = engine.select_next_item()
    if not first_task:
        raise HTTPException(503, "Не удалось выбрать первую задачу")
    
    # Сохраняем сессию в Redis
    _sessions.save(session_id, {
        "engine": engine,
        "current_task": first_task,
        "created_at": datetime.now().isoformat(),
        "student_id": student_id,
        "diagnostic_code": request.diagnostic_code,
    })
    
    return CATStartResponse(
        session_id=session_id,
        phase=engine.state.phase.value,
        config={
            "max_items": config.absolute_max_items,
            "warmup_items": config.warmup_items,
            "se_threshold": config.se_threshold,
        },
        first_task=_task_to_response(first_task, engine.state)
    )


@router.post("/{session_id}/answer", response_model=CATAnswerResponse)
def submit_cat_answer(
    session_id: str,
    request: CATAnswerRequest,
    db: Session = Depends(get_db),
    user: Optional[dict] = Depends(get_optional_user)
):
    """
    Отправить ответ на CAT-задачу.
    
    Обновляет θ через IRT, проверяет ответ,
    выбирает следующую оптимальную задачу.
    """
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(404, "CAT-сессия не найдена или истекла")
    
    engine: ShadowCATEngine = session["engine"]
    current_task: CATTask = session["current_task"]
    
    if current_task.task_id != request.task_id:
        raise HTTPException(400, "Неверный ID задачи")
    
    # Theta before
    theta_before = engine.state.theta if hasattr(engine.state, 'theta') else 0.0
    
    # Проверяем ответ
    is_correct = _check_answer(
        request.answer,
        current_task.correct_answer,
        current_task.answer_type
    )
    
    # Обрабатываем в CAT Engine
    result = engine.process_response(
        task_id=request.task_id,
        is_correct=is_correct,
        time_ms=request.time_ms,
        student_answer=request.answer
    )
    
    # ────── ERROR CLASSIFICATION (если неправильный) ──────
    error_type = None
    error_explanation = None
    matched_distractor = None
    
    if not is_correct:
        match = _match_distractor(request.answer, current_task)
        if match:
            error_type = match["error_type"]
            error_explanation = match["explanation"]
            matched_distractor = match["matched_value"]
        else:
            error_type = "unclassified"
            error_explanation = "Ответ не совпал ни с одним дистрактором"
    
    # ────── SAVE answer_event to DB ──────
    try:
        student_id = session.get("student_id")
        answer_repo = AnswerEventRepository(db)
        answer_repo.save_event(
            session_id=session_id,
            task_id=request.task_id,
            skill_id=current_task.skill_id,
            is_correct=is_correct,
            time_spent_ms=request.time_ms,
            started_at=datetime.now(),
            student_id=student_id,
            student_answer=request.answer,
            correct_answer=current_task.correct_answer,
            difficulty_level=current_task.difficulty,
            task_order=result.get("total_items", 0),
            theta_before=theta_before,
            theta_after=result.get("theta"),
            irt_params=current_task.irt_params if hasattr(current_task, 'irt_params') else None,
            error_type=error_type,
            error_explanation=error_explanation,
            matched_distractor=matched_distractor,
        )
        db.commit()
    except Exception as e:
        logger.error(f"Failed to save answer_event: {e}")
        db.rollback()
    # ─────────────────────────────────────
    
    # Выбираем следующую задачу
    next_task = engine.select_next_item()
    is_complete = next_task is None
    
    if next_task:
        session["current_task"] = next_task
        _sessions.save(session_id, session)  # Обновляем в Redis
    
    next_task_resp = _task_to_response(next_task, engine.state) if next_task else None
    
    return CATAnswerResponse(
        is_correct=is_correct,
        correct_answer=None,  # Не показывать правильный ответ при диагностике
        theta=result["theta"],
        theta_se=result["theta_se"],
        phase=result["phase"],
        item_number=result["total_items"] + 1,
        accuracy=result["accuracy"],
        next_task=next_task_resp,
        is_complete=is_complete,
        flags=result.get("flags", [])
    )


@router.get("/{session_id}/result", response_model=CATResultResponse)
def get_cat_result(
    session_id: str,
    db: Session = Depends(get_db),
    user: Optional[dict] = Depends(get_optional_user)
):
    """
    Получить полный результат CAT-сессии.
    
    Включает:
    - Финальную оценку θ (EAP + MLE)
    - Integrity report (l_z, Guttman, RT-анализ)
    - Траекторию θ
    - Покрытие контента
    """
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(404, "CAT-сессия не найдена или истекла")
    
    engine: ShadowCATEngine = session["engine"]
    
    # Завершаем если ещё не
    if engine.state.phase != CATPhase.COMPLETED:
        engine.state.phase = CATPhase.COMPLETED
    
    # Получаем результат CAT
    result = engine.get_result()
    
    # Integrity analysis
    integrity_service = IntegrityService()
    integrity_report = integrity_service.analyze(
        session_id=session_id,
        responses=engine.state.responses,
        theta=result["theta_eap"],
        theta_history=result["theta_trajectory"],
        se_history=result["se_trajectory"]
    )
    
    # ────── SAVE to DB ──────
    student_id = session.get("student_id")
    diagnostic_code = session.get("diagnostic_code")
    
    try:
        # 1. Session summary
        session_repo = SessionRepository(db)
        created_at_str = session.get("created_at", datetime.now().isoformat())
        started_at = datetime.fromisoformat(created_at_str)
        session_repo.save_summary(
            session_id=session_id,
            started_at=started_at,
            completed_at=datetime.now(),
            student_id=student_id,
            total_tasks=result["total_items"],
            correct_answers=result["correct"],
            final_theta=result["theta_eap"],
            ci_low=result["ci_low"],
            ci_high=result["ci_high"],
            identified_gaps=result.get("identified_gaps", []),
            root_gaps=result.get("root_gaps", []),
            session_type="diagnostic",
        )
        
        # 2. Student skill states (per skill)
        if student_id:
            skill_repo = StudentSkillRepository(db)
            skill_results = result.get("skill_results", {})
            for skill_id, skill_data in skill_results.items():
                mastery = skill_data.get("mastery", 0.5)
                skill_repo.upsert_state(
                    student_id=student_id,
                    skill_id=skill_id,
                    mastery_probability=mastery,
                    theta=result["theta_eap"],
                    theta_se=result["se"],
                )
        
        # 3. Complete diagnostic invite (if diagnostic_code)
        if diagnostic_code:
            auth_svc = AuthService(db)
            auth_svc.complete_diagnostic(diagnostic_code, {
                "final_theta": result["theta_eap"],
                "se": result["se"],
                "accuracy": result["accuracy"],
                "total_items": result["total_items"],
                "identified_gaps": result.get("identified_gaps", []),
                "root_gaps": result.get("root_gaps", []),
                "error_summary": result.get("error_summary"),
                "integrity_report": {
                    "is_valid": integrity_report.is_valid,
                    "overall_confidence": integrity_report.overall_confidence,
                    "person_fit_lz": integrity_report.person_fit_lz,
                    "guttman_errors": integrity_report.guttman_errors,
                    "rt_anomaly_fraction": integrity_report.rt_anomaly_fraction,
                },
                "recommended_level": result.get("recommended_level"),
            })
        
        db.commit()
        logger.info(f"CAT result saved to DB: session={session_id}, theta={result['theta_eap']:.3f}")
    except Exception as e:
        logger.error(f"Failed to save CAT result to DB: {e}")
        db.rollback()
    # ──────────────────────
    
    # Cleanup — удаляем сессию из Redis
    _sessions.delete(session_id)
    
    return CATResultResponse(
        session_id=session_id,
        student_id=result.get("student_id"),
        theta_eap=result["theta_eap"],
        theta_mle=result.get("theta_mle"),
        se=result["se"],
        ci_low=result["ci_low"],
        ci_high=result["ci_high"],
        total_items=result["total_items"],
        correct=result["correct"],
        accuracy=result["accuracy"],
        skills_tested=result["skills_tested"],
        difficulty_distribution=result["difficulty_distribution"],
        response_pattern=result["response_pattern"],
        fast_responses=result["fast_responses"],
        slow_responses=result["slow_responses"],
        theta_trajectory=result["theta_trajectory"],
        se_trajectory=result["se_trajectory"],
        integrity={
            "is_valid": integrity_report.is_valid,
            "overall_confidence": integrity_report.overall_confidence,
            "person_fit_lz": integrity_report.person_fit_lz,
            "person_fit_verdict": integrity_report.person_fit_verdict,
            "guttman_errors": integrity_report.guttman_errors,
            "guttman_ratio": integrity_report.guttman_ratio,
            "rt_anomaly_fraction": integrity_report.rt_anomaly_fraction,
            "theta_volatility": integrity_report.theta_volatility,
            "anomalies": [
                {
                    "type": a.anomaly_type.value,
                    "severity": a.severity.value,
                    "confidence": a.confidence,
                    "description": a.description,
                }
                for a in integrity_report.anomalies
            ]
        }
    )


@router.get("/{session_id}/progress", response_model=CATProgressResponse)
def get_cat_progress(session_id: str):
    """Получить текущий прогресс CAT"""
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(404, "CAT-сессия не найдена или истекла")
    
    engine: ShadowCATEngine = session["engine"]
    progress = engine.get_progress()
    
    return CATProgressResponse(**progress)


# ============================================================================
# HELPERS
# ============================================================================

def _load_cat_tasks(
    task_repo,
    skill_graph: Dict,
    skill_filter: Optional[List[str]] = None,
    class_level: Optional[int] = None
) -> List[CATTask]:
    """Загрузить задачи из БД и конвертировать в CATTask"""
    cat_tasks = []
    
    # Получаем все активные задачи (TaskMaster objects)
    all_tasks = task_repo.get_all_active_tasks(limit=500)
    
    for task in all_tasks:
        # Фильтр по навыкам
        if skill_filter and task.skill_id not in skill_filter:
            continue
        
        # Фильтр по классу (через связь skill → class_level)
        if class_level:
            skill_node = skill_graph.get(task.skill_id)
            if skill_node:
                cls_start = getattr(skill_node, 'class_level_start', None)
                cls_end = getattr(skill_node, 'class_level_end', None)
                if cls_start and cls_end and not (cls_start <= class_level <= cls_end):
                    continue
                elif cls_start and not cls_end and cls_start != class_level:
                    continue
        
        # Определяем domain_id из графа
        domain_id = None
        skill_node = skill_graph.get(task.skill_id)
        if skill_node and hasattr(skill_node, 'parent_id'):
            domain_id = skill_node.parent_id
        
        # IRT параметры (convert Decimal from DB to float)
        irt_params = IRTTaskParams(
            task_id=task.id,
            skill_id=task.skill_id,
            discrimination=float(getattr(task, 'irt_discrimination', 1.0) or 1.0),
            difficulty=float(getattr(task, 'irt_difficulty', 0.0) or 0.0),
            guessing=float(getattr(task, 'irt_guessing', 0.0) or 0.0),
        )
        
        # Дистракторы и типичные ошибки
        raw_dm = getattr(task, 'distractor_meta', None)
        raw_cm = getattr(task, 'common_mistakes', None)
        
        cat_task = CATTask(
            task_id=task.id,
            skill_id=task.skill_id,
            domain_id=domain_id,
            difficulty=task.difficulty_level or "B",
            irt_params=irt_params,
            question_text=task.question_text or "",
            question_latex=getattr(task, 'question_latex', None),
            question_image_url=getattr(task, 'question_image_url', None),
            correct_answer=task.correct_answer or "",
            answer_type=task.answer_type or "exact_number",
            answer_options=getattr(task, 'answer_options', None),
            distractor_meta=raw_dm if isinstance(raw_dm, list) else None,
            common_mistakes=raw_cm if isinstance(raw_cm, list) else None,
        )
        
        cat_tasks.append(cat_task)
    
    return cat_tasks


def _task_to_response(task: CATTask, state) -> CATTaskResponse:
    """Конвертировать CATTask в CATTaskResponse.
    
    Правило формата (только для диагностики — модуль 1):
      • exact_number / decimal с len(correct_answer) ≤ 5 → открытый ввод
      • Всё остальное → MC (правильный + дистракторы, перемешано)
    
    Дистракторы генерируются через StepDistractorEngine если distractor_meta пуст.
    """
    import random as _rnd
    
    answer_fmt = _determine_answer_format(task)
    
    options = None
    if answer_fmt == "multiple_choice":
        options = _build_mc_options(task)
    
    return CATTaskResponse(
        task_id=task.task_id,
        skill_id=task.skill_id,
        difficulty=task.difficulty,
        question_text=task.question_text,
        question_latex=task.question_latex,
        question_image_url=task.question_image_url,
        answer_type=task.answer_type if answer_fmt == "open_input" else "multiple_choice",
        phase=state.phase.value,
        item_number=state.total_items + 1,
        options=options,
    )


def _determine_answer_format(task: CATTask) -> str:
    """
    Определяет формат ответа для диагностики.
    
    Правило:
      • exact_number / decimal с len(correct_answer) ≤ 5 → open_input
      • Всё остальное → multiple_choice
    """
    answer_type = (task.answer_type or "").lower().strip()
    correct = (task.correct_answer or "").strip()
    
    if answer_type in ("exact_number", "decimal") and len(correct) <= 5:
        return "open_input"
    
    return "multiple_choice"


def _build_mc_options(task: CATTask, count: int = 4) -> List[str]:
    """
    Собирает MC-варианты: правильный ответ + дистракторы.
    
    Приоритет:
      1. distractor_meta (предрассчитанные)
      2. Генерация на лету через StepDistractorEngine
      3. Fallback из answer_options (если есть в БД)
    """
    import random as _rnd
    
    correct = (task.correct_answer or "").strip()
    distractors: List[str] = []
    
    # 1. Из distractor_meta
    if task.distractor_meta:
        for d in task.distractor_meta:
            val = str(d.get("value", "")).strip()
            if val and val != correct:
                distractors.append(val)
    
    # 2. Генерация на лету через StepDistractorEngine.generate_for_answer (ВСЕ типы)
    if len(distractors) < count - 1:
        try:
            from src.exam_builder.sympy_step_solver import StepDistractorEngine
            
            engine = StepDistractorEngine()
            gen_distractors = engine.generate_for_answer(
                correct_answer=correct,
                answer_type=task.answer_type or "exact_number",
                count=count - 1 - len(distractors),
            )
            for gd in gen_distractors:
                val = str(gd.value).strip()
                if val and val != correct and val not in distractors:
                    distractors.append(val)
        except Exception:
            pass
    
    # 3. Fallback из answer_options (старый путь)
    if len(distractors) < count - 1 and task.answer_options:
        for opt in task.answer_options:
            val = str(opt).strip()
            if val and val != correct and val not in distractors:
                distractors.append(val)
    
    # Собираем: правильный + до (count-1) дистракторов
    opts = [correct] + distractors[:count - 1]
    _rnd.shuffle(opts)
    return opts


def _is_numeric(s: str) -> bool:
    """Проверка: строка — число."""
    try:
        float(s)
        return True
    except (ValueError, TypeError):
        return False


def _match_distractor(
    student_answer: str,
    task: CATTask,
    tolerance: float = 0.01,
) -> Optional[Dict[str, str]]:
    """
    Сопоставляет ответ ученика с дистрактором для классификации ошибки.
    
    Проверяет:
      1. distractor_meta задачи (предрассчитанные)
      2. Генерирует на лету через StepDistractorEngine
    
    Returns:
        {"error_type": ..., "explanation": ..., "matched_value": ...} или None
    """
    answer = student_answer.strip()
    answer_type = (task.answer_type or "exact_number").lower().strip()
    
    # Сначала проверяем distractor_meta
    if task.distractor_meta:
        for d in task.distractor_meta:
            d_val = str(d.get("value", "")).strip()
            if not d_val:
                continue
            
            if _match_answer_value(answer, d_val, answer_type, tolerance):
                return {
                    "error_type": d.get("error_type", "unknown"),
                    "explanation": d.get("explanation", ""),
                    "matched_value": d_val,
                }
    
    # Генерация на лету через generate_for_answer (все типы)
    correct_str = (task.correct_answer or "").strip()
    answer_type = (task.answer_type or "exact_number").lower().strip()
    if correct_str:
        try:
            from src.exam_builder.sympy_step_solver import StepDistractorEngine
            
            engine = StepDistractorEngine()
            distractors = engine.generate_for_answer(
                correct_answer=correct_str,
                answer_type=answer_type,
                count=6,
            )
            for gd in distractors:
                gd_val = str(gd.value).strip()
                if _match_answer_value(answer, gd_val, answer_type, tolerance):
                    return {
                        "error_type": gd.error_type,
                        "explanation": gd.explanation,
                        "matched_value": gd_val,
                    }
        except Exception:
            pass
    
    return None


def _match_answer_value(
    student: str, distractor: str, answer_type: str, tolerance: float = 0.01
) -> bool:
    """
    Сравнивает ответ ученика с дистрактором с учётом типа ответа.
    
      exact_number/decimal → числовое сравнение с tolerance
      fraction            → fractions.Fraction
      expression          → sympy simplify
      остальное           → строковое (lower/strip)
    """
    s = student.strip().lower().replace(" ", "")
    d = distractor.strip().lower().replace(" ", "")
    
    # Точное строковое
    if s == d:
        return True
    
    if answer_type in ("exact_number", "decimal"):
        try:
            return abs(float(s.replace(",", ".")) - float(d.replace(",", "."))) <= tolerance
        except ValueError:
            return False
    
    if answer_type == "fraction":
        try:
            from fractions import Fraction
            return Fraction(s) == Fraction(d)
        except Exception:
            return False
    
    if answer_type in ("expression", "equation_solution", "inequality"):
        try:
            from sympy import simplify, parse_expr
            return simplify(parse_expr(s.replace("^", "**")) - parse_expr(d.replace("^", "**"))) == 0
        except Exception:
            return False
    
    return False
