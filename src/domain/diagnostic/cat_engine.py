"""
ALGO V1 - Shadow CAT Engine (Computerized Adaptive Testing)
Продвинутый адаптивный тестовый движок с контент-балансировкой,
контролем экспозиции и многоуровневыми правилами остановки.

Алгоритмы:
- Shadow CAT: Контент-балансировка через теневые тесты (van der Linden, 2005)
- Sympson-Hetter: Контроль экспозиции задач (Sympson & Hetter, 1985)
- CUSUM / Compound Stopping: Правила остановки (SPRT-like)
- Weighted MFI: Взвешенный Maximum Fisher Information
"""

import random
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Set, Tuple, Any
from enum import Enum
from collections import defaultdict
import logging

from .irt_engine import IRTEngine, IRTTaskParams, ResponseData

logger = logging.getLogger(__name__)


# ============================================================================
# CONFIGURATION
# ============================================================================


@dataclass
class CATConfig:
    """Конфигурация CAT-движка"""

    # ─── Фазы ───
    warmup_items: int = 5  # Задач в разогреве
    core_max_items: int = 25  # Максимум задач в основной фазе
    refinement_max_items: int = 10  # Максимум задач в уточнении
    absolute_max_items: int = 40  # Абсолютный потолок
    absolute_min_items: int = 8  # Минимум задач для валидного результата

    # ─── Остановка ───
    se_threshold: float = 0.30  # SE для надёжной оценки
    delta_theta_threshold: float = 0.05  # Минимальное изменение theta
    delta_window: int = 5  # Окно для отслеживания дельты theta

    # ─── Контент-балансировка ───
    content_balance_weight: float = 0.3  # Вес контент-балансировки (0=нет, 1=полностью)
    min_skills_coverage: float = 0.4  # Минимальное покрытие навыков (40%)
    difficulty_distribution: Dict[str, float] = field(
        default_factory=lambda: {
            "A": 0.25,  # Лёгкие
            "B": 0.50,  # Средние
            "C": 0.25,  # Сложные
        }
    )

    # ─── Экспозиция ───
    max_exposure_rate: float = 0.30  # Задача не должна появляться > 30% тестов
    sympson_hetter_target: float = 0.25

    # ─── IRT ───
    prior_mean: float = 0.0
    prior_std: float = 1.0
    theta_range: Tuple[float, float] = (-4.0, 4.0)

    # ─── Безопасность ───
    fast_response_ms: int = 2000  # Подозрительно быстрый ответ
    slow_response_ms: int = 300000  # Слишком медленный (5 мин)

    # ─── Prereq-filter ───
    hard_prereq_weight_threshold: float = 7.0  # >= → жёсткая блокировка
    soft_prereq_penalty: float = 0.3  # множитель при мягком пробеле
    gap_threshold: float = 0.4  # mastery < gap → пробел

    # ─── Deep Scan ───
    deep_scan_max_items: int = 10  # Макс. задач на DEEP_SCAN


class CATPhase(Enum):
    """Фазы адаптивного теста"""

    WARMUP = "warmup"  # Разогрев: фиксированная сложность, определение зоны
    CORE = "core"  # Ядро: полноценный CAT, максимизация информации
    DEEP_SCAN = "deep_scan"  # Глубокое сканирование: проверка пререквизитов пробелов
    REFINEMENT = "refinement"  # Уточнение: точная настройка theta
    COMPLETED = "completed"


# ============================================================================
# CONTENT BLUEPRINTS (Shadow CAT)
# ============================================================================


@dataclass
class ContentBlueprint:
    """
    Blueprint = желаемое распределение контента в тесте.
    Shadow CAT гарантирует, что итоговый тест покрывает все спецификации.
    """

    skill_quotas: Dict[str, int] = field(default_factory=dict)
    # skill_id -> минимум задач

    difficulty_quotas: Dict[str, int] = field(default_factory=dict)
    # "A"/"B"/"C" -> минимум задач

    domain_quotas: Dict[str, int] = field(default_factory=dict)
    # L1/L2 domain -> минимум задач

    total_items: int = 30

    def is_satisfied(
        self,
        skill_counts: Dict[str, int],
        difficulty_counts: Dict[str, int],
        domain_counts: Dict[str, int],
    ) -> bool:
        """Проверить, удовлетворён ли blueprint"""
        for skill_id, quota in self.skill_quotas.items():
            if skill_counts.get(skill_id, 0) < quota:
                return False
        for diff, quota in self.difficulty_quotas.items():
            if difficulty_counts.get(diff, 0) < quota:
                return False
        for domain, quota in self.domain_quotas.items():
            if domain_counts.get(domain, 0) < quota:
                return False
        return True

    def deficit_score(
        self,
        task: "CATTask",
        skill_counts: Dict[str, int],
        difficulty_counts: Dict[str, int],
        domain_counts: Dict[str, int],
    ) -> float:
        """
        Оценка дефицита: насколько задача помогает покрыть blueprint.
        Чем больше дефицит — тем выше score.
        """
        score = 0.0

        # Skill deficit
        skill_quota = self.skill_quotas.get(task.skill_id, 0)
        skill_count = skill_counts.get(task.skill_id, 0)
        if skill_count < skill_quota:
            score += (skill_quota - skill_count) / max(1, skill_quota)

        # Difficulty deficit
        diff_quota = self.difficulty_quotas.get(task.difficulty, 0)
        diff_count = difficulty_counts.get(task.difficulty, 0)
        if diff_count < diff_quota:
            score += (diff_quota - diff_count) / max(1, diff_quota)

        # Domain deficit
        if task.domain_id:
            domain_quota = self.domain_quotas.get(task.domain_id, 0)
            domain_count = domain_counts.get(task.domain_id, 0)
            if domain_count < domain_quota:
                score += (domain_quota - domain_count) / max(1, domain_quota)

        return score


# ============================================================================
# EXPOSURE CONTROL (Sympson-Hetter)
# ============================================================================


def _default_one() -> float:
    """Pickle-совместимая фабрика для defaultdict (вместо lambda: 1.0)."""
    return 1.0


class ExposureController:
    """
    Sympson-Hetter метод контроля экспозиции.

    Каждой задаче назначается вероятность k(i), с которой она будет
    предложена, если выбрана алгоритмом. Если k(i) < 1, задача может
    быть "заблокирована" даже если она оптимальна.

    Это предотвращает сверхиспользование "лучших" задач.

    Параметры персистируются в Redis (ключ ``exposure:params``).
    При недоступности Redis данные хранятся in-memory.
    """

    _REDIS_KEY = "exposure:params"
    _REDIS_META_KEY = "exposure:meta"

    def __init__(self, target_exposure: float = 0.25):
        self.target_exposure = target_exposure

        # k(i): вероятность администрирования (Sympson-Hetter parameter)
        # Ключ = task_id, значение = вероятность [0, 1]
        # NOTE: _default_one вместо lambda для pickle-совместимости
        self.k_params: Dict[str, float] = defaultdict(_default_one)

        # Статистика за текущий цикл калибровки
        self.selection_count: Dict[str, int] = defaultdict(int)  # Сколько раз выбран
        self.admin_count: Dict[str, int] = defaultdict(int)  # Сколько раз предъявлен
        self.total_sessions: int = 0

        # Try loading persisted state from Redis
        self._load_from_redis()

    # ----- Redis persistence ------------------------------------------------

    def _get_redis(self):
        """Lazy import to avoid circular dependency."""
        try:
            from src.api.redis_sessions import _get_redis

            return _get_redis()
        except Exception:
            return None

    def _load_from_redis(self) -> None:
        """Restore k_params, counters and total_sessions from Redis."""
        r = self._get_redis()
        if r is None:
            return
        try:
            # k_params stored as hash  task_id -> k_value
            raw = r.hgetall(self._REDIS_KEY)
            if raw:
                for tid, val in raw.items():
                    key = tid if isinstance(tid, str) else tid.decode()
                    self.k_params[key] = float(val)

            # meta: total_sessions, selection_count, admin_count
            meta = r.hgetall(self._REDIS_META_KEY)
            if meta:
                for field, val in meta.items():
                    field_s = field if isinstance(field, str) else field.decode()
                    val_s = val if isinstance(val, str) else val.decode()
                    if field_s == "total_sessions":
                        self.total_sessions = int(val_s)
                    elif field_s.startswith("sel:"):
                        self.selection_count[field_s[4:]] = int(val_s)
                    elif field_s.startswith("adm:"):
                        self.admin_count[field_s[4:]] = int(val_s)

            logger.debug(
                "ExposureController loaded from Redis: %d params, %d sessions",
                len(raw) if raw else 0,
                self.total_sessions,
            )
        except Exception as exc:
            logger.warning("ExposureController: failed to load from Redis: %s", exc)

    def _save_to_redis(self) -> None:
        """Persist current state to Redis."""
        r = self._get_redis()
        if r is None:
            return
        try:
            pipe = r.pipeline()
            # k_params
            if self.k_params:
                pipe.delete(self._REDIS_KEY)
                pipe.hset(
                    self._REDIS_KEY,
                    mapping={tid: str(val) for tid, val in self.k_params.items()},
                )
            # meta
            meta_mapping: Dict[str, str] = {
                "total_sessions": str(self.total_sessions),
            }
            for tid, cnt in self.selection_count.items():
                meta_mapping[f"sel:{tid}"] = str(cnt)
            for tid, cnt in self.admin_count.items():
                meta_mapping[f"adm:{tid}"] = str(cnt)
            if meta_mapping:
                pipe.delete(self._REDIS_META_KEY)
                pipe.hset(self._REDIS_META_KEY, mapping=meta_mapping)
            pipe.execute()
        except Exception as exc:
            logger.warning("ExposureController: failed to save to Redis: %s", exc)

    def should_administer(self, task_id: str) -> bool:
        """
        Решить, предъявлять ли задачу.
        Вызывается после того как CAT выбрал задачу.
        """
        k = self.k_params[task_id]
        self.selection_count[task_id] += 1

        if random.random() < k:
            self.admin_count[task_id] += 1
            return True
        return False

    def end_session(self) -> None:
        """Обновить статистику после сессии и сохранить в Redis"""
        self.total_sessions += 1
        self._save_to_redis()

    def recalibrate(self) -> None:
        """
        Пересчёт k-параметров по формуле Sympson-Hetter:

        k_new(i) = k_old(i) × (target / actual_exposure(i))

        Вызывается периодически (после каждых N сессий).
        """
        if self.total_sessions < 10:
            return  # Недостаточно данных

        for task_id in self.selection_count:
            selected = self.selection_count[task_id]
            if selected == 0:
                continue

            actual_exposure = self.admin_count[task_id] / self.total_sessions

            if actual_exposure > 0:
                k_old = self.k_params[task_id]
                k_new = k_old * (self.target_exposure / actual_exposure)
                self.k_params[task_id] = min(1.0, max(0.1, k_new))

        logger.info(
            f"Exposure control recalibrated: {self.total_sessions} sessions, "
            f"{len(self.k_params)} tasks adjusted"
        )
        self._save_to_redis()

    def get_exposure_stats(self) -> Dict:
        """Статистика экспозиции"""
        if self.total_sessions == 0:
            return {"sessions": 0}

        exposures = {
            task_id: count / self.total_sessions
            for task_id, count in self.admin_count.items()
        }

        return {
            "sessions": self.total_sessions,
            "avg_exposure": sum(exposures.values()) / max(1, len(exposures)),
            "max_exposure": max(exposures.values()) if exposures else 0,
            "overexposed_items": sum(
                1 for e in exposures.values() if e > self.target_exposure * 1.5
            ),
            "underused_items": sum(
                1 for e in exposures.values() if e < self.target_exposure * 0.2
            ),
        }


# Global singleton — shared across all CAT sessions.
_global_exposure: Optional[ExposureController] = None


def get_exposure_controller(target: float = 0.25) -> ExposureController:
    """Return the process-wide ExposureController (lazy-init, Redis-backed)."""
    global _global_exposure
    if _global_exposure is None:
        _global_exposure = ExposureController(target)
    return _global_exposure


# ============================================================================
# STOPPING RULES
# ============================================================================


class StoppingRule:
    """Базовый класс правила остановки"""

    def should_stop(self, state: "CATSessionState") -> bool:
        raise NotImplementedError

    def reason(self) -> str:
        raise NotImplementedError


class MinItemsRule(StoppingRule):
    """Минимальное количество задач"""

    def __init__(self, min_items: int = 8):
        self.min_items = min_items

    def should_stop(self, state: "CATSessionState") -> bool:
        return False  # Это "анти-стоп" правило — не останавливаться рано

    def can_stop(self, state: "CATSessionState") -> bool:
        """Позволяет ли правило остановиться"""
        return state.total_items >= self.min_items

    def reason(self) -> str:
        return f"min_items ({self.min_items})"


class MaxItemsRule(StoppingRule):
    """Максимальное количество задач"""

    def __init__(self, max_items: int = 40):
        self.max_items = max_items

    def should_stop(self, state: "CATSessionState") -> bool:
        return state.total_items >= self.max_items

    def reason(self) -> str:
        return f"max_items ({self.max_items})"


class SERule(StoppingRule):
    """Остановка по стандартной ошибке"""

    def __init__(self, se_threshold: float = 0.30):
        self.se_threshold = se_threshold

    def should_stop(self, state: "CATSessionState") -> bool:
        if state.current_se is None:
            return False
        return state.current_se <= self.se_threshold

    def reason(self) -> str:
        return f"SE <= {self.se_threshold}"


class DeltaThetaRule(StoppingRule):
    """Остановка по стабилизации theta"""

    def __init__(self, delta: float = 0.05, window: int = 5):
        self.delta = delta
        self.window = window

    def should_stop(self, state: "CATSessionState") -> bool:
        if len(state.theta_history) < self.window:
            return False

        recent = state.theta_history[-self.window :]
        max_theta = max(recent)
        min_theta = min(recent)

        return (max_theta - min_theta) < self.delta

    def reason(self) -> str:
        return f"delta_theta < {self.delta} over {self.window} items"


class ContentCoverageRule(StoppingRule):
    """Остановка при достаточном покрытии контента"""

    def __init__(self, blueprint: Optional[ContentBlueprint] = None):
        self.blueprint = blueprint

    def should_stop(self, state: "CATSessionState") -> bool:
        if not self.blueprint:
            return True  # Нет ограничений
        return self.blueprint.is_satisfied(
            state.skill_counts, state.difficulty_counts, state.domain_counts
        )

    def reason(self) -> str:
        return "content_coverage_met"


class CompoundStoppingRule:
    """
    Составное правило остановки:

    Остановиться если:
      (min_items выполнен) AND (
        (SE_rule OR delta_theta_rule) OR max_items_rule
      ) AND (content_coverage ИЛИ max_items)
    """

    def __init__(self, config: CATConfig, blueprint: Optional[ContentBlueprint] = None):
        self.min_rule = MinItemsRule(config.absolute_min_items)
        self.max_rule = MaxItemsRule(config.absolute_max_items)
        self.se_rule = SERule(config.se_threshold)
        self.delta_rule = DeltaThetaRule(
            config.delta_theta_threshold, config.delta_window
        )
        self.content_rule = ContentCoverageRule(blueprint)

    def should_stop(self, state: "CATSessionState") -> Tuple[bool, str]:
        """
        Returns:
            (should_stop, reason)
        """
        # Абсолютный потолок
        if self.max_rule.should_stop(state):
            return True, self.max_rule.reason()

        # Минимум не достигнут — продолжаем
        if not self.min_rule.can_stop(state):
            return False, "min_items_not_reached"

        # Проверяем точность
        precision_ok = self.se_rule.should_stop(state) or self.delta_rule.should_stop(
            state
        )
        content_ok = self.content_rule.should_stop(state)

        if precision_ok and content_ok:
            reason_parts = []
            if self.se_rule.should_stop(state):
                reason_parts.append(self.se_rule.reason())
            if self.delta_rule.should_stop(state):
                reason_parts.append(self.delta_rule.reason())
            return True, " & ".join(reason_parts)

        return False, "continue"


# ============================================================================
# CAT TASK & SESSION STATE
# ============================================================================


@dataclass
class CATTask:
    """Задача для CAT"""

    task_id: str
    skill_id: str
    domain_id: Optional[str]  # L1/L2 домен
    difficulty: str  # A, B, C
    irt_params: IRTTaskParams

    # Метаданные
    content_tags: List[str] = field(default_factory=list)
    exposure_k: float = 1.0  # Sympson-Hetter k

    # Текстовые данные (для отображения)
    question_text: str = ""
    question_latex: Optional[str] = None
    question_image_url: Optional[str] = None
    correct_answer: str = ""
    answer_type: str = "exact_number"
    answer_options: Optional[List[str]] = None  # MC варианты (если есть)

    # Дистракторы и ошибки
    distractor_meta: Optional[List[Dict]] = (
        None  # [{value, error_type, explanation, plausibility}]
    )
    common_mistakes: Optional[List[Dict]] = None  # [{mistake, frequency}]


@dataclass
class CATSessionState:
    """Состояние CAT-сессии"""

    session_id: str
    student_id: Optional[str]

    # Фаза
    phase: CATPhase = CATPhase.WARMUP

    # Счётчики
    total_items: int = 0
    items_in_phase: int = 0
    correct_count: int = 0

    # Theta tracking
    current_theta: float = 0.0
    current_se: Optional[float] = None
    theta_history: List[float] = field(default_factory=list)
    se_history: List[float] = field(default_factory=list)

    # Контент tracking
    used_task_ids: Set[str] = field(default_factory=set)
    skill_counts: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    difficulty_counts: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    domain_counts: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    tested_skills: Set[str] = field(default_factory=set)

    # История ответов
    responses: List[ResponseData] = field(default_factory=list)

    # Integrity
    fast_response_count: int = 0
    slow_response_count: int = 0

    # Timing
    started_at: Optional[float] = None

    # Skill-level mastery (for prereq-filter)
    skill_mastery: Dict[str, float] = field(
        default_factory=dict
    )  # skill_id → P(mastery)
    skills_to_deep_scan: Set[str] = field(default_factory=set)  # gap skills queue

    @property
    def accuracy(self) -> float:
        if self.total_items == 0:
            return 0.0
        return self.correct_count / self.total_items


# ============================================================================
# MAIN CAT ENGINE
# ============================================================================


class ShadowCATEngine:
    """
    Shadow CAT Engine — продвинутый адаптивный тестовый движок.

    Workflow:
    1. Warmup: фиксированные задачи для определения зоны θ
    2. Core: полноценный CAT с Shadow Test + MFI
    3. Refinement: тонкая настройка θ при SE > порога

    Отличия от базового CAT:
    - Shadow Test гарантирует контент-балансировку
    - Sympson-Hetter предотвращает сверхиспользование задач
    - Составные правила остановки (SE + delta + min/max + coverage)
    - Integrity checks встроены в процесс
    """

    def __init__(
        self,
        config: CATConfig,
        blueprint: Optional[ContentBlueprint] = None,
        exposure_controller: Optional[ExposureController] = None,
        task_pool: Optional[List[CATTask]] = None,
        skill_graph: Optional[Dict[str, Any]] = None,
    ):
        self.config = config
        self.blueprint = blueprint or ContentBlueprint()
        self.exposure = exposure_controller or get_exposure_controller(
            config.sympson_hetter_target
        )
        self.task_pool = {t.task_id: t for t in (task_pool or [])}
        self.skill_graph = skill_graph or {}  # skill_id → SkillNode

        # IRT движок
        self.irt = IRTEngine(
            prior_mean=config.prior_mean,
            prior_std=config.prior_std,
            theta_min=config.theta_range[0],
            theta_max=config.theta_range[1],
        )

        # Правила остановки
        self.stopping = CompoundStoppingRule(config, blueprint)

        # Состояние
        self.state: Optional[CATSessionState] = None

    # =========================================================================
    # SESSION LIFECYCLE
    # =========================================================================

    def start_session(
        self,
        session_id: str,
        student_id: Optional[str] = None,
        prior_theta: Optional[float] = None,
    ) -> CATSessionState:
        """
        Начать новую CAT-сессию.

        Args:
            session_id: Уникальный ID сессии
            student_id: ID студента
            prior_theta: Предварительная оценка θ (из предыдущих сессий)
        """
        import time

        self.irt.reset()

        if prior_theta is not None:
            self.irt.prior_mean = prior_theta

        self.state = CATSessionState(
            session_id=session_id,
            student_id=student_id,
            current_theta=self.irt.prior_mean,
            started_at=time.time(),
        )

        logger.info(
            f"CAT session started: {session_id}, prior_theta={self.irt.prior_mean:.2f}"
        )

        return self.state

    def select_next_item(self) -> Optional[CATTask]:
        """
        Выбрать следующую задачу.

        Логика зависит от фазы:
        - WARMUP: Средне-сложные задачи, покрытие навыков
        - CORE: Shadow CAT + Weighted MFI
        - REFINEMENT: Максимальная информация на текущем θ
        """
        if not self.state or self.state.phase == CATPhase.COMPLETED:
            return None

        # Проверяем остановку
        should_stop, reason = self.stopping.should_stop(self.state)
        if should_stop:
            self.state.phase = CATPhase.COMPLETED
            logger.info(f"CAT stopped: {reason}")
            return None

        # Проверяем переход фазы
        self._check_phase_transition()

        if self.state.phase == CATPhase.COMPLETED:
            return None

        # Получаем доступные задачи
        available = self._get_available_tasks()

        if not available:
            logger.warning("No available tasks")
            self.state.phase = CATPhase.COMPLETED
            return None

        # Выбор зависит от фазы
        if self.state.phase == CATPhase.WARMUP:
            selected = self._select_warmup_item(available)
        elif self.state.phase == CATPhase.CORE:
            selected = self._select_core_item(available)
        elif self.state.phase == CATPhase.DEEP_SCAN:
            selected = self._select_deep_scan_item(available)
        else:  # REFINEMENT
            selected = self._select_refinement_item(available)

        if selected:
            # Sympson-Hetter exposure check
            attempts = 0
            while selected and not self.exposure.should_administer(selected.task_id):
                available = [t for t in available if t.task_id != selected.task_id]
                if not available:
                    break
                selected = self._select_by_information(available)
                attempts += 1
                if attempts > 10:
                    break

        return selected

    def process_response(
        self,
        task_id: str,
        is_correct: bool,
        time_ms: int,
        student_answer: Optional[str] = None,
    ) -> Dict:
        """
        Обработать ответ и обновить состояние.

        Returns:
            Словарь с theta, se, phase, flags
        """
        if not self.state:
            raise RuntimeError("No active CAT session")

        task = self.task_pool.get(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not in pool")

        # Создаём ResponseData
        response = ResponseData(
            task_id=task_id,
            skill_id=task.skill_id,
            is_correct=is_correct,
            time_ms=time_ms,
            task_params=task.irt_params,
            student_answer=student_answer,
            order_in_session=self.state.total_items,
        )

        # Integrity checks
        flags = []
        if time_ms < self.config.fast_response_ms:
            self.state.fast_response_count += 1
            flags.append("fast_response")
        if time_ms > self.config.slow_response_ms:
            self.state.slow_response_count += 1
            flags.append("slow_response")

        # IRT update
        estimate = self.irt.add_response(response)

        # Обновляем состояние
        self.state.total_items += 1
        self.state.items_in_phase += 1
        self.state.responses.append(response)
        self.state.used_task_ids.add(task_id)

        if is_correct:
            self.state.correct_count += 1

        self.state.current_theta = estimate.theta
        self.state.current_se = estimate.standard_error
        self.state.theta_history.append(estimate.theta)
        self.state.se_history.append(estimate.standard_error)

        # Контент tracking
        self.state.skill_counts[task.skill_id] += 1
        self.state.difficulty_counts[task.difficulty] += 1
        self.state.tested_skills.add(task.skill_id)
        if task.domain_id:
            self.state.domain_counts[task.domain_id] += 1

        # Skill-level mastery estimate (Bayesian posterior)
        self._update_skill_mastery(task.skill_id, is_correct)

        # Queue skills with confirmed gaps for DEEP_SCAN
        if (
            self.skill_graph
            and task.skill_id in self.state.skill_mastery
            and self.state.skill_mastery[task.skill_id] < self.config.gap_threshold
        ):
            node = self.skill_graph.get(task.skill_id)
            if node:
                untested_prereqs = [
                    pid
                    for pid in getattr(node, "prerequisites", [])
                    if pid not in self.state.tested_skills
                ]
                if untested_prereqs:
                    self.state.skills_to_deep_scan.add(task.skill_id)

        return {
            "theta": estimate.theta,
            "theta_se": estimate.standard_error,
            "ci_low": estimate.confidence_low,
            "ci_high": estimate.confidence_high,
            "phase": self.state.phase.value,
            "total_items": self.state.total_items,
            "is_correct": is_correct,
            "flags": flags,
            "accuracy": self.state.accuracy,
            "skills_tested": len(self.state.tested_skills),
        }

    def get_result(self) -> Dict:
        """Получить финальный результат CAT"""
        if not self.state:
            return {}

        estimate = self.irt.get_current_estimate()
        pattern = self.irt.detect_response_pattern()

        # Финальная проверка: MLE для сравнения с EAP
        mle = self.irt.compute_mle()

        return {
            "session_id": self.state.session_id,
            "student_id": self.state.student_id,
            # Оценка
            "theta_eap": estimate.theta,
            "theta_mle": mle.theta if mle else None,
            "se": estimate.standard_error,
            "ci_low": estimate.confidence_low,
            "ci_high": estimate.confidence_high,
            # Статистика
            "total_items": self.state.total_items,
            "correct": self.state.correct_count,
            "accuracy": self.state.accuracy,
            # Контент
            "skills_tested": list(self.state.tested_skills),
            "skill_counts": dict(self.state.skill_counts),
            "difficulty_distribution": dict(self.state.difficulty_counts),
            # Integrity
            "response_pattern": pattern.value,
            "fast_responses": self.state.fast_response_count,
            "slow_responses": self.state.slow_response_count,
            # Theta trajectory
            "theta_trajectory": self.state.theta_history,
            "se_trajectory": self.state.se_history,
        }

    # =========================================================================
    # PREREQUISITE FILTER & SKILL MASTERY
    # =========================================================================

    def _update_skill_mastery(self, skill_id: str, is_correct: bool) -> None:
        """
        Bayesian update of skill-level mastery estimate P(mastery).

        Uses a simple Beta-binomial model: prior Beta(1,1),
        posterior after n correct / m wrong → Beta(1+n, 1+m).
        We store only the mean of the posterior.
        """
        prior = self.state.skill_mastery.get(skill_id)
        n_attempts = self.state.skill_counts.get(skill_id, 1)

        if prior is None:
            # First observation: P = 1 if correct, 0 if wrong (+ slight shrinkage)
            self.state.skill_mastery[skill_id] = 0.75 if is_correct else 0.25
        else:
            # Incremental Bayesian update (Beta mean)
            alpha = prior * (n_attempts - 1) + (1.0 if is_correct else 0.0) + 1
            beta_param = (
                (1 - prior) * (n_attempts - 1) + (0.0 if is_correct else 1.0) + 1
            )
            self.state.skill_mastery[skill_id] = alpha / (alpha + beta_param)

    def _is_skill_eligible(self, skill_id: str) -> Tuple[bool, float]:
        """
        Проверяет, выполнены ли зависимости навыка.

        Returns:
            (eligible, penalty_multiplier)
            - eligible=False → жёсткие зависимости (weight >= threshold) с пробелом → BLOCK
            - penalty_multiplier < 1.0 → мягкие пробелы → штраф
        """
        if not self.skill_graph:
            return True, 1.0

        node = self.skill_graph.get(skill_id)
        if not node:
            return True, 1.0

        penalty = 1.0
        prereqs = getattr(node, "prerequisites", [])
        prereq_weights = getattr(node, "prerequisite_weights", {})

        for prereq_id in prereqs:
            if prereq_id not in self.state.tested_skills:
                continue  # Неизвестные пререквизиты не блокируют

            mastery = self.state.skill_mastery.get(prereq_id, 1.0)
            if mastery < self.config.gap_threshold:
                weight = prereq_weights.get(prereq_id, 5.0)
                if weight >= self.config.hard_prereq_weight_threshold:
                    return False, 0.0  # HARD BLOCK
                else:
                    penalty *= self.config.soft_prereq_penalty

        return True, penalty

    def _select_deep_scan_item(self, available: List[CATTask]) -> Optional[CATTask]:
        """
        DEEP SCAN: проверка непротестированных пререквизитов пробельных навыков.
        """
        while self.state.skills_to_deep_scan:
            target_skill = next(iter(self.state.skills_to_deep_scan))
            node = self.skill_graph.get(target_skill) if self.skill_graph else None

            if not node:
                self.state.skills_to_deep_scan.discard(target_skill)
                continue

            untested_prereqs = [
                pid
                for pid in getattr(node, "prerequisites", [])
                if pid not in self.state.tested_skills
            ]

            if not untested_prereqs:
                self.state.skills_to_deep_scan.discard(target_skill)
                continue

            # Find a medium-difficulty task for the first untested prereq
            prereq_id = untested_prereqs[0]
            candidates = [t for t in available if t.skill_id == prereq_id]
            if not candidates:
                # No task in pool for this prereq — skip
                self.state.skills_to_deep_scan.discard(target_skill)
                continue

            # Prefer medium difficulty
            medium = [t for t in candidates if t.difficulty == "B"]
            return (medium or candidates)[0]

        # Nothing left in deep scan → fall back to core selection
        return self._select_core_item(available)

    # =========================================================================
    # ITEM SELECTION STRATEGIES
    # =========================================================================

    def _select_warmup_item(self, available: List[CATTask]) -> Optional[CATTask]:
        """
        Warmup: a-stratification (Chang & Ying, 1999).

        В начале теста НЕ даём высоко-дискриминантные задачи — они слишком
        чувствительны к ошибкам оценки θ. Используем низко-дискриминантные
        задачи для грубой оценки зоны, постепенно увеличивая a.

        Стратификация: разбиваем пул на страты по discrimination,
        в i-й задаче берём из i-й страты.
        """
        # Предпочитаем непроверенные навыки
        untested = [t for t in available if t.skill_id not in self.state.tested_skills]
        pool = untested if untested else available

        # a-stratification: разбиваем по дискриминации
        n_strata = max(1, self.config.warmup_items)
        item_idx = self.state.items_in_phase  # 0-based

        # Сортируем по discrimination
        sorted_pool = sorted(pool, key=lambda t: t.irt_params.discrimination)

        # Определяем страту для текущей задачи
        stratum_size = max(1, len(sorted_pool) // n_strata)
        stratum_start = min(item_idx * stratum_size, len(sorted_pool) - 1)
        stratum_end = min(stratum_start + stratum_size, len(sorted_pool))
        stratum = sorted_pool[stratum_start:stratum_end]

        if not stratum:
            stratum = sorted_pool

        # Из страты выбираем задачу средней сложности (B)
        medium = [t for t in stratum if t.difficulty == "B"]
        final_pool = medium if medium else stratum

        # Из оставшихся выбираем с наибольшей информацией
        return self._select_by_information(final_pool)

    def _select_core_item(self, available: List[CATTask]) -> Optional[CATTask]:
        """
        Core: Shadow CAT — взвешенный выбор с prereq-filter:
        score = ((1 - w) × information + w × content_deficit + novelty) × penalty

        Где w = content_balance_weight, penalty из prereq-filter.
        Задачи с HARD-блоком пропускаются. При мягком пробеле score снижается.
        """
        theta = self.state.current_theta
        w = self.config.content_balance_weight

        best_task = None
        best_score = -float("inf")

        for task in available:
            # Prereq filter
            eligible, penalty = self._is_skill_eligible(task.skill_id)
            if not eligible:
                continue  # Hard-blocked

            # Информация
            info = task.irt_params.information(theta)

            # Нормализуем информацию (0..1)
            a = task.irt_params.discrimination
            c = task.irt_params.guessing
            max_possible_info = (
                (a**2) * (1 - c) / (4.0 * (1 + c)) if (1 + c) > 0 else (a**2) / 4.0
            )
            norm_info = info / max(0.01, max_possible_info)

            # Контент-дефицит (0..1)
            deficit = self.blueprint.deficit_score(
                task,
                dict(self.state.skill_counts),
                dict(self.state.difficulty_counts),
                dict(self.state.domain_counts),
            )

            # Бонус за непроверенный навык
            novelty_bonus = (
                0.3 if task.skill_id not in self.state.tested_skills else 0.0
            )

            # Штраф за "слишком лёгкие/сложные" задачи
            p = task.irt_params.probability(theta)
            optimality = 1.0 - 2 * abs(p - 0.5)  # Максимум при p=0.5

            # Итоговый score с prereq penalty
            score = (
                (1 - w) * norm_info * (0.7 + 0.3 * optimality)
                + w * deficit
                + novelty_bonus
            ) * penalty

            if score > best_score:
                best_score = score
                best_task = task

        if best_task is None and available:
            # All tasks blocked by prereq filter → fallback without filter
            logger.warning("CORE prereq-filter: all tasks blocked, falling back")
            return self._select_by_information(available)

        return best_task

    def _select_refinement_item(self, available: List[CATTask]) -> Optional[CATTask]:
        """
        Refinement: KL-divergence для точной настройки θ.
        KL лучше MFI на этом этапе, т.к. учитывает окрестность θ.
        Без контент-балансировки.
        """
        return self._select_by_kl_divergence(available, delta=0.05)

    def _select_by_information(self, tasks: List[CATTask]) -> Optional[CATTask]:
        """Выбрать задачу с максимальной информацией Фишера"""
        if not tasks:
            return None

        # Применяем enemy item constraint
        filtered = self._filter_enemy_items(tasks)
        pool = filtered if filtered else tasks

        theta = self.state.current_theta if self.state else 0.0
        return max(pool, key=lambda t: t.irt_params.information(theta))

    def _select_by_kl_divergence(
        self, tasks: List[CATTask], delta: float = 0.1
    ) -> Optional[CATTask]:
        """
        KL-divergence item selection (Chang & Ying, 1996).

        Выбирает задачу, максимизирующую KL-информацию в окрестности θ,
        а не точечно при θ. Лучше чем MFI на экстремах θ.

        KL_i = ∫ KL(f(x|θ) || f(x|θ+δ)) × π(θ) dθ

        Для 3PL упрощение: KL ≈ I(θ) × δ² / 2 + I'(θ) × δ / 2
        Но точнее считать как среднее I(θ) по окрестности [θ-δ, θ+δ].

        Args:
            tasks: Доступные задачи
            delta: Размер окрестности (по умолчанию 0.1)
        """
        if not tasks:
            return None

        # Применяем enemy item constraint
        filtered = self._filter_enemy_items(tasks)
        pool = filtered if filtered else tasks

        theta = self.state.current_theta if self.state else 0.0

        # KL как среднее информации в окрестности θ ± δ
        def kl_score(task: CATTask) -> float:
            params = task.irt_params
            info_center = params.information(theta)
            info_left = params.information(theta - delta)
            info_right = params.information(theta + delta)
            # Средневзвешенное: центр имеет вес 2, края по 1
            return (2 * info_center + info_left + info_right) / 4.0

        return max(pool, key=kl_score)

    def _filter_enemy_items(self, tasks: List[CATTask]) -> List[CATTask]:
        """
        Enemy item constraint: нельзя давать задачу из того же навыка,
        что и предыдущая задача (если есть альтернативы).

        Также нельзя давать >2 задачи подряд на один skill_id.
        """
        if not self.state or not self.state.responses:
            return tasks

        # Навык последней задачи
        last_skill = self.state.responses[-1].skill_id
        # Навык предпоследней задачи (если есть)
        prev_skill = (
            self.state.responses[-2].skill_id
            if len(self.state.responses) >= 2
            else None
        )

        # Если последние 2 задачи на один навык — обязательно сменить
        if prev_skill and prev_skill == last_skill:
            filtered = [t for t in tasks if t.skill_id != last_skill]
            if filtered:
                return filtered

        # Иначе просто предпочитаем другой навык (мягкое ограничение)
        different_skill = [t for t in tasks if t.skill_id != last_skill]
        return different_skill if different_skill else tasks

    # =========================================================================
    # PHASE MANAGEMENT
    # =========================================================================

    def _check_phase_transition(self) -> None:
        """Проверить и выполнить переход фазы"""
        if not self.state:
            return

        phase = self.state.phase

        if phase == CATPhase.WARMUP:
            if self.state.items_in_phase >= self.config.warmup_items:
                self._transition(CATPhase.CORE)

        elif phase == CATPhase.CORE:
            if self.state.items_in_phase >= self.config.core_max_items:
                # Check for deep scan candidates before moving on
                if self.state.skills_to_deep_scan and self.skill_graph:
                    self._transition(CATPhase.DEEP_SCAN)
                elif (
                    self.state.current_se
                    and self.state.current_se > self.config.se_threshold
                ):
                    self._transition(CATPhase.REFINEMENT)
                else:
                    self._transition(CATPhase.COMPLETED)

        elif phase == CATPhase.DEEP_SCAN:
            if (
                self.state.items_in_phase >= self.config.deep_scan_max_items
                or not self.state.skills_to_deep_scan
            ):
                if (
                    self.state.current_se
                    and self.state.current_se > self.config.se_threshold
                ):
                    self._transition(CATPhase.REFINEMENT)
                else:
                    self._transition(CATPhase.COMPLETED)

        elif phase == CATPhase.REFINEMENT:
            if self.state.items_in_phase >= self.config.refinement_max_items:
                self._transition(CATPhase.COMPLETED)

    def _transition(self, new_phase: CATPhase) -> None:
        """Переход в новую фазу"""
        old = self.state.phase
        self.state.phase = new_phase
        self.state.items_in_phase = 0

        logger.info(
            f"CAT phase transition: {old.value} -> {new_phase.value}, "
            f"theta={self.state.current_theta:.3f}, "
            f"items={self.state.total_items}"
        )

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _get_available_tasks(self) -> List[CATTask]:
        """Получить доступные задачи (не использованные)"""
        return [
            task
            for task in self.task_pool.values()
            if task.task_id not in self.state.used_task_ids
        ]

    def load_task_pool(self, tasks: List[CATTask]) -> None:
        """Загрузить пул задач"""
        self.task_pool = {t.task_id: t for t in tasks}
        logger.info(f"Task pool loaded: {len(tasks)} items")

    def add_task(self, task: CATTask) -> None:
        """Добавить задачу в пул"""
        self.task_pool[task.task_id] = task

    def get_progress(self) -> Dict:
        """Текущий прогресс"""
        if not self.state:
            return {"status": "no_session"}

        should_stop, reason = self.stopping.should_stop(self.state)

        return {
            "phase": self.state.phase.value,
            "total_items": self.state.total_items,
            "items_in_phase": self.state.items_in_phase,
            "theta": self.state.current_theta,
            "se": self.state.current_se,
            "accuracy": self.state.accuracy,
            "skills_tested": len(self.state.tested_skills),
            "available_items": len(self._get_available_tasks()),
            "should_stop": should_stop,
            "stop_reason": reason,
        }


# ============================================================================
# BLUEPRINT BUILDER
# ============================================================================


def build_blueprint_from_skills(
    skills: Dict[str, Dict],
    total_items: int = 30,
    difficulty_dist: Optional[Dict[str, float]] = None,
) -> ContentBlueprint:
    """
    Построить ContentBlueprint из графа навыков.

    Args:
        skills: {skill_id: {"level": "L4", "domain_id": "G5_S01", ...}}
        total_items: Общее количество задач
        difficulty_dist: Распределение сложности {"A": 0.25, "B": 0.50, "C": 0.25}
    """
    if difficulty_dist is None:
        difficulty_dist = {"A": 0.25, "B": 0.50, "C": 0.25}

    # Подсчёт L4-навыков
    l4_skills = {sid: s for sid, s in skills.items() if s.get("level") == "L4"}

    # Распределяем квоты по навыкам (хотя бы 1 задача на L4-навык)
    skill_quotas = {}
    for skill_id in l4_skills:
        skill_quotas[skill_id] = 1

    # Сложность
    difficulty_quotas = {
        diff: max(1, int(total_items * ratio))
        for diff, ratio in difficulty_dist.items()
    }

    # Домены (L1)
    domains = set()
    for s in skills.values():
        domain = s.get("domain_id")
        if domain:
            domains.add(domain)

    domain_quotas = {d: max(1, total_items // max(1, len(domains))) for d in domains}

    return ContentBlueprint(
        skill_quotas=skill_quotas,
        difficulty_quotas=difficulty_quotas,
        domain_quotas=domain_quotas,
        total_items=total_items,
    )
