"""
ALGO V1 — Pipeline Data Models
src/pipeline/models.py

Dataclasses, используемые всеми модулями конвейера.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# ExtractedTask — центральная единица конвейера
# ---------------------------------------------------------------------------


@dataclass
class ExtractedTask:
    """Задача, извлечённая из текста учебника."""

    temp_id: str = ""
    exercise_number: str = ""
    page: int = 0
    paragraph_number: str = ""
    paragraph_title: str = ""

    question_text: str = ""
    question_latex: str = ""
    answer_raw: str = ""
    answer_type: str = "exact_number"
    # exact_number, expression, multiple_choice, text, fraction,
    # equation_solution, set, coordinate, inequality
    answer_options: Optional[List[str]] = None

    difficulty: str = "B"  # A, B, C
    cognitive_load: str = "apply"  # recall, apply, analyze
    is_star: bool = False  # ★ задача
    task_marker: str = ""  # оригинальный символ-маркер
    task_category: str = "standard"
    # standard, advanced, olympiad, oral, research, project, with_drawing
    image_description: str = ""

    # --- Заполняются на этапах обработки ---
    sympy_verified: bool = False
    sympy_answer: str = ""
    sympy_confidence: float = 0.0
    solution_steps: Optional[List[str]] = None
    hints: Optional[List[str]] = None
    common_mistakes: Optional[List[Dict]] = None
    distractors: Optional[List[str]] = None
    distractor_meta: Optional[List[Dict]] = (
        None  # [{value, error_type, explanation, plausibility, broken_step}]
    )

    # --- Дуальный маппинг ---
    # skill_id может быть None — задачи без маппинга на ядро (textbook-only)
    skill_id: Optional[str] = None
    toc_id: Optional[int] = None
    tags: Dict[str, Any] = field(default_factory=dict)
    mapping_confidence: float = 0.0


# ---------------------------------------------------------------------------
# DistractorResult
# ---------------------------------------------------------------------------


@dataclass
class DistractorResult:
    """Результат генерации одного дистрактора с метаданными."""

    value: str
    error_type: str  # sign_error, off_by_one, transfer_error, …
    explanation: str  # Почему ученик мог так ответить
    plausibility: float  # 0–1, насколько правдоподобный


# ---------------------------------------------------------------------------
# TemplateResult
# ---------------------------------------------------------------------------


@dataclass
class TemplateResult:
    """Результат генерации параметрического шаблона."""

    template_string: str
    template_latex: Optional[str]
    parameters: List[str]
    constraints: Dict[str, Dict[str, Any]]
    answer_formula: str
    generation_method: str
    quality_score: float
    tested_combinations: int
