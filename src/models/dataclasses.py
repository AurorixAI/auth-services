"""
ALGO V1 - Shared Dataclasses
Датаклассы, используемые несколькими модулями (diagnostic, repositories, api).
Вынесены из professional_engine.py для устранения cross-layer зависимостей.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from enum import Enum
from datetime import datetime

from src.diagnostic.irt_engine import IRTTaskParams


# ============================================================================
# Enums
# ============================================================================

class DiagnosticPhase(Enum):
    """Фазы диагностики"""
    LIFT = "lift"
    ADAPTIVE = "adaptive"
    DEEP_SCAN = "deep_scan"
    SUMMARY = "summary"
    COMPLETED = "completed"


class MasteryLevel(Enum):
    """Уровни владения навыком"""
    MASTERED = "mastered"
    PROFICIENT = "proficient"
    DEVELOPING = "developing"
    STRUGGLING = "struggling"
    GAP = "gap"
    UNKNOWN = "unknown"

    @classmethod
    def from_probability(cls, p: float) -> "MasteryLevel":
        if p >= 0.9:
            return cls.MASTERED
        elif p >= 0.7:
            return cls.PROFICIENT
        elif p >= 0.5:
            return cls.DEVELOPING
        elif p >= 0.3:
            return cls.STRUGGLING
        else:
            return cls.GAP


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class SkillNode:
    """Узел навыка в графе"""
    skill_id: str
    name: str
    name_ru: str
    level: str  # L1, L2, L3, L4

    # Пререквизиты
    prerequisites: List[str] = field(default_factory=list)
    prerequisite_weights: Dict[str, float] = field(default_factory=dict)

    # Состояние
    mastery_probability: float = 0.5
    mastery_level: MasteryLevel = MasteryLevel.UNKNOWN
    theta: Optional[float] = None
    theta_se: Optional[float] = None

    # Метаданные
    attempts: int = 0
    correct: int = 0
    last_attempt: Optional[datetime] = None

    # Флаги
    is_tested: bool = False
    is_gap: bool = False
    is_root_gap: bool = False


@dataclass
class TaskCandidate:
    """Кандидат задачи для диагностики"""
    task_id: str
    skill_id: str
    difficulty: str  # A, B, C

    # IRT параметры
    irt_params: IRTTaskParams

    # Метрики выбора
    information_gain: float = 0.0
    priority_score: float = 0.0

    # Ограничения
    already_used: bool = False
    similar_used: bool = False


@dataclass
class DiagnosticResult:
    """Результат диагностики"""
    session_id: str
    student_id: Optional[str]

    # Временные рамки
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_seconds: int = 0

    # Статистика
    total_tasks: int = 0
    correct_answers: int = 0
    accuracy: float = 0.0

    # Глобальная оценка
    overall_theta: float = 0.0
    overall_theta_se: float = 1.0
    confidence_interval: Tuple[float, float] = (-1.96, 1.96)

    # Оценки по навыкам
    skill_estimates: Dict = field(default_factory=dict)
    skill_levels: Dict = field(default_factory=dict)

    # Пробелы
    identified_gaps: List[str] = field(default_factory=list)
    root_gaps: List[str] = field(default_factory=list)
    learning_path: List[str] = field(default_factory=list)
    priority_skills: List[Tuple[str, float]] = field(default_factory=list)

    # Метаданные
    response_pattern: str = "normal"
    phases_completed: List[str] = field(default_factory=list)
