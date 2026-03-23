"""
ALGO V1 - IRT (Item Response Theory) Engine
3PL модель для точной оценки способностей ученика

Теория:
- P(θ) = c + (1-c) / (1 + exp(-a(θ - b)))
- θ: способность ученика (обычно от -4 до +4)
- a: дискриминация задачи (насколько хорошо различает способности)
- b: сложность задачи (в тех же единицах что и θ)
- c: параметр угадывания (для тестов с выбором ответа)
"""

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class ResponsePattern(Enum):
    """Паттерны ответов для детекции аномалий"""

    NORMAL = "normal"
    GUESSING = "guessing"  # Слишком быстро, нестабильно
    CHEATING = "cheating"  # Неправдоподобно высокий результат
    CARELESS = "careless"  # Ошибки на лёгких после правильных на сложных
    FATIGUE = "fatigue"  # Ухудшение к концу
    WARMUP = "warmup"  # Улучшение после первых задач


@dataclass
class IRTTaskParams:
    """IRT параметры задачи (3PL модель)"""

    task_id: str
    skill_id: str

    # 3PL параметры
    discrimination: float = 1.0  # a: от 0.5 до 2.5 (типично 1.0)
    difficulty: float = 0.0  # b: от -3 до +3
    guessing: float = 0.0  # c: от 0 до 0.35 (0 для открытых ответов)

    # Метаданные
    calibrated: bool = False
    sample_size: int = 0
    fit_statistic: Optional[float] = None

    def probability(self, theta: float) -> float:
        """
        Вычисляет P(correct | theta) по 3PL модели

        Args:
            theta: способность ученика

        Returns:
            Вероятность правильного ответа
        """
        a, b, c = self.discrimination, self.difficulty, self.guessing

        # Ограничение экспоненты для численной стабильности
        exponent = -a * (theta - b)
        exponent = max(-50, min(50, exponent))

        return c + (1 - c) / (1 + math.exp(exponent))

    def information(self, theta: float) -> float:
        """
        Вычисляет информационную функцию I(θ)
        Показывает, насколько задача информативна для данной способности

        Максимальная информация достигается когда θ ≈ b
        """
        a, _b, c = self.discrimination, self.difficulty, self.guessing
        p = self.probability(theta)
        q = 1 - p

        if p <= c or p >= 1:
            return 0.0

        # I(θ) = a² * ((p - c)² / ((1 - c)² * p * q))
        numerator = (a**2) * ((p - c) ** 2)
        denominator = ((1 - c) ** 2) * p * q

        if denominator < 1e-10:
            return 0.0

        return numerator / denominator


@dataclass
class ResponseData:
    """Данные об ответе ученика"""

    task_id: str
    skill_id: str
    is_correct: bool
    time_ms: int
    task_params: IRTTaskParams

    # Опциональные метаданные
    student_answer: Optional[str] = None
    order_in_session: int = 0


@dataclass
class ThetaEstimate:
    """Оценка способности ученика"""

    theta: float  # Точечная оценка
    standard_error: float  # Стандартная ошибка
    confidence_low: float  # 95% CI нижняя граница
    confidence_high: float  # 95% CI верхняя граница

    # Метаданные
    n_responses: int = 0
    estimation_method: str = "EAP"  # EAP, MLE, MAP

    @property
    def is_reliable(self) -> bool:
        """Достаточно ли надёжна оценка"""
        return self.standard_error < 0.5 and self.n_responses >= 5


class IRTEngine:
    """
    Движок IRT для оценки способностей ученика

    Использует:
    - 3PL модель для вычисления вероятностей
    - EAP (Expected A Posteriori) для оценки θ
    - Байесовское обновление после каждого ответа
    """

    def __init__(
        self,
        prior_mean: float = 0.0,
        prior_std: float = 1.0,
        theta_min: float = -4.0,
        theta_max: float = 4.0,
        quadrature_points: int = 61,
    ):
        """
        Args:
            prior_mean: Априорное среднее θ
            prior_std: Априорное стандартное отклонение θ
            theta_min: Минимальное значение θ
            theta_max: Максимальное значение θ
            quadrature_points: Количество точек квадратуры для EAP
        """
        self.prior_mean = prior_mean
        self.prior_std = prior_std
        self.theta_min = theta_min
        self.theta_max = theta_max
        self.quadrature_points = quadrature_points

        # Сетка θ для численного интегрирования
        self._theta_grid = [
            theta_min + i * (theta_max - theta_min) / (quadrature_points - 1)
            for i in range(quadrature_points)
        ]

        # История ответов текущей сессии
        self.responses: List[ResponseData] = []

        # Текущая оценка
        self._current_theta: Optional[ThetaEstimate] = None

        # Кеш апостериорного распределения
        self._posterior: List[float] = []
        self._initialize_posterior()

    def _initialize_posterior(self) -> None:
        """Инициализация априорного распределения (нормальное)"""
        self._posterior = []
        total = 0.0

        for theta in self._theta_grid:
            # Нормальное распределение как априорное
            z = (theta - self.prior_mean) / self.prior_std
            prob = math.exp(-0.5 * z * z)
            self._posterior.append(prob)
            total += prob

        # Нормализация
        self._posterior = [p / total for p in self._posterior]

    def reset(self) -> None:
        """Сброс для новой сессии"""
        self.responses = []
        self._current_theta = None
        self._initialize_posterior()

    def add_response(self, response: ResponseData) -> ThetaEstimate:
        """
        Добавить ответ и обновить оценку θ

        Args:
            response: Данные об ответе

        Returns:
            Обновлённая оценка θ
        """
        self.responses.append(response)
        self._update_posterior(response)
        self._current_theta = self._compute_eap()

        logger.debug(
            f"Response added: {response.task_id}, correct={response.is_correct}, "
            f"theta={self._current_theta.theta:.3f} ± {self._current_theta.standard_error:.3f}"
        )

        return self._current_theta

    def _update_posterior(self, response: ResponseData) -> None:
        """
        Байесовское обновление апостериорного распределения

        posterior ∝ prior × likelihood
        """
        new_posterior = []
        total = 0.0

        for i, theta in enumerate(self._theta_grid):
            # Вычисляем likelihood P(response | θ)
            p_correct = response.task_params.probability(theta)

            if response.is_correct:
                likelihood = p_correct
            else:
                likelihood = 1 - p_correct

            # Байесовское обновление
            updated = self._posterior[i] * likelihood
            new_posterior.append(updated)
            total += updated

        # Нормализация (чтобы сумма = 1)
        if total > 0:
            self._posterior = [p / total for p in new_posterior]
        else:
            logger.warning("Posterior collapsed to zero, reinitializing")
            self._initialize_posterior()

    def _compute_eap(self) -> ThetaEstimate:
        """
        Expected A Posteriori (EAP) оценка θ

        θ_EAP = ∫ θ × p(θ|data) dθ
        SE = √(∫ (θ - θ_EAP)² × p(θ|data) dθ)
        """
        # Ожидаемое значение
        theta_eap = sum(
            theta * prob for theta, prob in zip(self._theta_grid, self._posterior)
        )

        # Дисперсия
        variance = sum(
            prob * (theta - theta_eap) ** 2
            for theta, prob in zip(self._theta_grid, self._posterior)
        )

        se = math.sqrt(variance)

        # 95% доверительный интервал
        ci_low = theta_eap - 1.96 * se
        ci_high = theta_eap + 1.96 * se

        return ThetaEstimate(
            theta=theta_eap,
            standard_error=se,
            confidence_low=max(self.theta_min, ci_low),
            confidence_high=min(self.theta_max, ci_high),
            n_responses=len(self.responses),
            estimation_method="EAP",
        )

    def compute_mle(self) -> Optional[ThetaEstimate]:
        """
        Maximum Likelihood Estimation (MLE) θ

        Используется Newton-Raphson для нахождения максимума
        """
        if not self.responses:
            return None

        # Начальное приближение
        theta = self.prior_mean

        for iteration in range(50):
            # Вычисляем первую и вторую производные log-likelihood
            dl = 0.0  # первая производная
            d2l = 0.0  # вторая производная

            for resp in self.responses:
                params = resp.task_params
                a, _b, c = params.discrimination, params.difficulty, params.guessing

                p = params.probability(theta)

                # Избегаем деления на ноль
                if p < 1e-10 or p > 1 - 1e-10:
                    continue

                # Производные для 3PL (корректные формулы)
                1 - p
                p_star = (p - c) / (1 - c)  # P* — вероятность без угадывания

                if resp.is_correct:
                    dl += a * (1 - p) * p_star / p
                else:
                    dl += -a * p_star

                # Fisher Scoring: d2l = -I(θ)
                # I(θ) = a² × (P - c)² / ((1 - c)² × P × Q)
                # Это математически точная формула для 3PL
                info = params.information(theta)
                d2l += -info

            # Newton-Raphson шаг
            if abs(d2l) < 1e-10:
                break

            step = dl / (-d2l)
            theta_new = theta + step

            # Ограничиваем диапазон
            theta_new = max(self.theta_min, min(self.theta_max, theta_new))

            # Проверка сходимости
            if abs(theta_new - theta) < 0.001:
                theta = theta_new
                break

            theta = theta_new

        # Вычисляем стандартную ошибку из информации Фишера
        info = sum(resp.task_params.information(theta) for resp in self.responses)
        se = 1 / math.sqrt(info) if info > 0 else 1.0

        return ThetaEstimate(
            theta=theta,
            standard_error=se,
            confidence_low=max(self.theta_min, theta - 1.96 * se),
            confidence_high=min(self.theta_max, theta + 1.96 * se),
            n_responses=len(self.responses),
            estimation_method="MLE",
        )

    def compute_map(self) -> Optional[ThetaEstimate]:
        """
        Maximum A Posteriori (MAP) estimation of θ

        MAP = argmax[log L(θ|data) + log π(θ)]

        Combines likelihood with prior — more stable than MLE with few responses,
        converges to MLE with many responses. Uses Newton-Raphson with Fisher scoring.
        """
        if not self.responses:
            return None

        theta = self.prior_mean

        for iteration in range(50):
            dl = 0.0
            d2l = 0.0

            # Likelihood component
            for resp in self.responses:
                params = resp.task_params
                a, _b, c = params.discrimination, params.difficulty, params.guessing
                p = params.probability(theta)

                if p < 1e-10 or p > 1 - 1e-10:
                    continue

                1 - p
                p_star = (p - c) / (1 - c)

                if resp.is_correct:
                    dl += a * (1 - p) * p_star / p
                else:
                    dl += -a * p_star

                d2l += -params.information(theta)

            # Prior component: d/dθ log N(θ; μ, σ²) = -(θ - μ) / σ²
            dl += -(theta - self.prior_mean) / (self.prior_std**2)
            d2l += -1.0 / (self.prior_std**2)

            if abs(d2l) < 1e-10:
                break

            step = dl / (-d2l)
            theta_new = max(self.theta_min, min(self.theta_max, theta + step))

            if abs(theta_new - theta) < 0.001:
                theta = theta_new
                break

            theta = theta_new

        # SE from observed information + prior
        total_info = sum(resp.task_params.information(theta) for resp in self.responses)
        total_info += 1.0 / (self.prior_std**2)  # Prior contribution
        se = 1 / math.sqrt(total_info) if total_info > 0 else 1.0

        return ThetaEstimate(
            theta=theta,
            standard_error=se,
            confidence_low=max(self.theta_min, theta - 1.96 * se),
            confidence_high=min(self.theta_max, theta + 1.96 * se),
            n_responses=len(self.responses),
            estimation_method="MAP",
        )

    def get_current_estimate(self) -> ThetaEstimate:
        """Получить текущую оценку θ"""
        if self._current_theta is None:
            return ThetaEstimate(
                theta=self.prior_mean,
                standard_error=self.prior_std,
                confidence_low=self.prior_mean - 1.96 * self.prior_std,
                confidence_high=self.prior_mean + 1.96 * self.prior_std,
                n_responses=0,
            )
        return self._current_theta

    def select_optimal_task(
        self, available_tasks: List[IRTTaskParams], method: str = "max_info"
    ) -> Optional[IRTTaskParams]:
        """
        Выбрать оптимальную следующую задачу

        Args:
            available_tasks: Доступные задачи
            method: Метод выбора
                - "max_info": Максимум информации (точность)
                - "progressive": Прогрессивная сложность
                - "balanced": Баланс информации и времени

        Returns:
            Оптимальная задача или None
        """
        if not available_tasks:
            return None

        theta = self.get_current_estimate().theta

        if method == "max_info":
            # Выбираем задачу с максимальной информацией при текущем θ
            return max(available_tasks, key=lambda t: t.information(theta))

        elif method == "progressive":
            # Постепенно увеличиваем сложность
            n = len(self.responses)
            target_difficulty = self.prior_mean + (n / 10) * 0.5  # Постепенный рост

            # Находим задачу ближайшую к target_difficulty
            return min(
                available_tasks, key=lambda t: abs(t.difficulty - target_difficulty)
            )

        elif method == "balanced":
            # Баланс: информация + избегаем слишком сложных/лёгких
            def score(task: IRTTaskParams) -> float:
                info = task.information(theta)
                p = task.probability(theta)

                # Штраф за слишком высокую/низкую вероятность
                penalty = abs(p - 0.5)

                return info * (1 - 0.5 * penalty)

            return max(available_tasks, key=score)

        return available_tasks[0]

    def expected_information_gain(self, task: IRTTaskParams) -> float:
        """
        Вычисляет ожидаемый прирост информации от задачи
        (для адаптивного выбора)
        """
        theta = self.get_current_estimate().theta
        current_se = self.get_current_estimate().standard_error

        # Вероятность правильного ответа
        task.probability(theta)

        # Информация от задачи
        info = task.information(theta)

        # Ожидаемое уменьшение дисперсии
        current_var = current_se**2
        new_var = 1 / (1 / current_var + info)

        return current_var - new_var

    def detect_response_pattern(self) -> ResponsePattern:
        """
        Анализ паттернов ответов для обнаружения аномалий
        """
        if len(self.responses) < 5:
            return ResponsePattern.NORMAL

        # Анализ времени
        times = [r.time_ms for r in self.responses]
        sum(times) / len(times)

        # Анализ правильности
        correct = [r.is_correct for r in self.responses]

        # Детекция угадывания: быстрые ответы + низкая точность
        fast_responses = sum(1 for t in times if t < 3000)
        if fast_responses > len(times) * 0.7 and sum(correct) / len(correct) < 0.4:
            return ResponsePattern.GUESSING

        # Детекция усталости: ухудшение к концу
        if len(self.responses) >= 10:
            first_half = sum(correct[: len(correct) // 2]) / (len(correct) // 2)
            second_half = sum(correct[len(correct) // 2 :]) / (
                len(correct) - len(correct) // 2
            )

            if second_half < first_half - 0.2:
                return ResponsePattern.FATIGUE

            if second_half > first_half + 0.2:
                return ResponsePattern.WARMUP

        # Детекция небрежности: ошибки на лёгких после правильных на сложных
        careless_count = 0
        for i in range(1, len(self.responses)):
            curr = self.responses[i]
            prev = self.responses[i - 1]

            if (
                prev.is_correct
                and not curr.is_correct
                and curr.task_params.difficulty < prev.task_params.difficulty - 0.5
            ):
                careless_count += 1

        if careless_count >= 3:
            return ResponsePattern.CARELESS

        return ResponsePattern.NORMAL

    def get_diagnostics(self) -> dict:
        """Диагностическая информация о текущей сессии"""
        if not self.responses:
            return {"status": "no_data"}

        estimate = self.get_current_estimate()
        pattern = self.detect_response_pattern()

        correct_count = sum(1 for r in self.responses if r.is_correct)

        return {
            "n_responses": len(self.responses),
            "correct_count": correct_count,
            "accuracy": correct_count / len(self.responses),
            "theta": estimate.theta,
            "standard_error": estimate.standard_error,
            "is_reliable": estimate.is_reliable,
            "response_pattern": pattern.value,
            "avg_time_ms": sum(r.time_ms for r in self.responses) / len(self.responses),
            "confidence_interval": (estimate.confidence_low, estimate.confidence_high),
        }


class SkillIRTEstimator:
    """
    Оценщик θ для отдельных навыков

    Использует отдельный IRT Engine для каждого навыка,
    но учитывает корреляции между навыками
    """

    def __init__(self, skill_correlations: Optional[dict] = None):
        """
        Args:
            skill_correlations: Словарь корреляций {(skill_a, skill_b): correlation}
        """
        self.engines: dict[str, IRTEngine] = {}
        self.skill_correlations = skill_correlations or {}

    def get_engine(self, skill_id: str) -> IRTEngine:
        """Получить или создать движок для навыка"""
        if skill_id not in self.engines:
            self.engines[skill_id] = IRTEngine()
        return self.engines[skill_id]

    def add_response(self, skill_id: str, response: ResponseData) -> ThetaEstimate:
        """Добавить ответ для конкретного навыка"""
        engine = self.get_engine(skill_id)
        estimate = engine.add_response(response)

        # Обновляем связанные навыки через корреляции
        self._propagate_to_related_skills(skill_id, estimate)

        return estimate

    def _propagate_to_related_skills(
        self, skill_id: str, estimate: ThetaEstimate
    ) -> None:
        """
        Пропагация информации на связанные навыки
        через корреляции (байесовское обновление)
        """
        for (skill_a, skill_b), correlation in self.skill_correlations.items():
            related_skill = None

            if skill_a == skill_id:
                related_skill = skill_b
            elif skill_b == skill_id:
                related_skill = skill_a

            if related_skill and related_skill in self.engines:
                # Влияем на априорное распределение связанного навыка
                related_engine = self.engines[related_skill]

                # Сила влияния пропорциональна корреляции
                influence = correlation * 0.3  # Коэффициент затухания

                # Смещаем prior в сторону текущей оценки
                related_engine.prior_mean = (
                    related_engine.prior_mean * (1 - abs(influence))
                    + estimate.theta * influence
                )

    def get_skill_estimates(self) -> dict[str, ThetaEstimate]:
        """Получить оценки по всем навыкам"""
        return {
            skill_id: engine.get_current_estimate()
            for skill_id, engine in self.engines.items()
        }

    def identify_weak_skills(
        self, threshold: float = -0.5
    ) -> List[Tuple[str, ThetaEstimate]]:
        """Определить слабые навыки (θ ниже порога)"""
        weak = []
        for skill_id, engine in self.engines.items():
            estimate = engine.get_current_estimate()
            if estimate.theta < threshold and estimate.is_reliable:
                weak.append((skill_id, estimate))

        # Сортируем по θ (самые слабые первыми)
        weak.sort(key=lambda x: x[1].theta)
        return weak

    def identify_strong_skills(
        self, threshold: float = 0.5
    ) -> List[Tuple[str, ThetaEstimate]]:
        """Определить сильные навыки (θ выше порога)"""
        strong = []
        for skill_id, engine in self.engines.items():
            estimate = engine.get_current_estimate()
            if estimate.theta > threshold and estimate.is_reliable:
                strong.append((skill_id, estimate))

        # Сортируем по θ (самые сильные первыми)
        strong.sort(key=lambda x: -x[1].theta)
        return strong


# Утилиты для калибровки


def calibrate_task_params(
    responses: List[Tuple[float, bool]],  # [(theta, is_correct), ...]
    initial_a: float = 1.0,
    initial_b: float = 0.0,
    initial_c: float = 0.0,
) -> IRTTaskParams:
    """
    Калибровка IRT параметров задачи по истории ответов

    Использует MMLE (Marginal Maximum Likelihood Estimation)
    """
    if len(responses) < 30:
        logger.warning(f"Too few responses ({len(responses)}) for reliable calibration")

    a, b, c = initial_a, initial_b, initial_c

    # Простая итеративная калибровка
    for iteration in range(100):
        da, db, dc = 0.0, 0.0, 0.0

        for theta, is_correct in responses:
            # Вычисляем P(correct) для текущих параметров
            exp_val = math.exp(-a * (theta - b))
            p = c + (1 - c) / (1 + exp_val)

            # Избегаем деления на ноль
            if p < 0.01:
                p = 0.01
            if p > 0.99:
                p = 0.99

            # Градиенты (3PL корректные частные производные)
            q = 1 - p
            p_star = (p - c) / (1 - c)  # P*: 2PL компонента
            w = p_star / p if p > 0.01 else 0.0  # Вес для 3PL

            if is_correct:
                # ∂ ln P / ∂ a = w · (1 - p_star) · (θ - b)
                da += w * (1 - p_star) * (theta - b)
                # ∂ ln P / ∂ b = -a · w · (1 - p_star)
                db += -a * w * (1 - p_star)
                # ∂ ln P / ∂ c = 1 / (P · (1 - c))
                dc += 1.0 / (p * (1 - c)) if p > 0.01 else 0.0
            else:
                # ∂ ln Q / ∂ a = -(p_star · (1 - p_star) · (θ - b)) / q
                da += -(p_star * (1 - p_star) * (theta - b)) / q if q > 0.01 else 0.0
                # ∂ ln Q / ∂ b = a · p_star · (1 - p_star) / q
                db += a * p_star * (1 - p_star) / q if q > 0.01 else 0.0
                # ∂ ln Q / ∂ c = -p_star / (q · (1 - c))
                dc += -p_star / (q * (1 - c)) if q > 0.01 else 0.0

        # Градиентный шаг
        learning_rate = 0.01
        a += learning_rate * da
        b += learning_rate * db
        c += learning_rate * dc

        # Ограничения
        a = max(0.5, min(2.5, a))
        b = max(-3, min(3, b))
        c = max(0, min(0.35, c))

        # Проверка сходимости
        if abs(da) + abs(db) + abs(dc) < 0.001:
            break

    return IRTTaskParams(
        task_id="calibrated",
        skill_id="unknown",
        discrimination=a,
        difficulty=b,
        guessing=c,
        calibrated=True,
        sample_size=len(responses),
    )
