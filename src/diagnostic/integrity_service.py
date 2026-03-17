"""
ALGO V1 - Integrity Service
Обнаружение аномалий и нечестного поведения в диагностических сессиях.

Алгоритмы:
- Person-Fit Statistics: l_z (Drasgow et al., 1985)
- Response Time Analysis: Log-Normal RT Model (van der Linden, 2006)
- CUSUM: Cumulative Sum для обнаружения разладки
- Pattern Analysis: Guttman-error, Cheating, Fatigue
"""

import math
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from enum import Enum
import logging

from .irt_engine import IRTTaskParams, ResponseData, ThetaEstimate

logger = logging.getLogger(__name__)


# ============================================================================
# ENUMS & CONFIG
# ============================================================================

class AnomalyType(Enum):
    """Типы аномалий"""
    NONE = "none"
    RAPID_GUESSING = "rapid_guessing"         # Быстрые случайные ответы
    CHEATING = "cheating"                     # Списывание / внешняя помощь
    CREATIVE_ANSWERING = "creative_answering" # Нестандартное поведение
    FATIGUE = "fatigue"                       # Усталость / потеря мотивации
    WARM_UP = "warm_up"                       # Разогрев (улучшение к концу)
    THETA_JUMP = "theta_jump"                 # Резкий скачок θ (подмена)
    INCONSISTENT = "inconsistent"             # Guttman-ошибки: лёгкие неправильно, сложные правильно
    PREKNOWLEDGE = "preknowledge"             # Знание конкретных задач (не навыка)
    RANDOM_MC = "random_mc"                   # Случайный выбор в MC без анализа задачи


class Severity(Enum):
    """Серьёзность аномалии"""
    LOW = "low"           # Информативно, не влияет на оценку
    MEDIUM = "medium"     # Рекомендуется пометить, снизить уверенность
    HIGH = "high"         # Рекомендуется аннулировать / пересдать
    CRITICAL = "critical" # Автоматическое аннулирование


@dataclass
class AnomalyReport:
    """Отчёт об аномалии"""
    anomaly_type: AnomalyType
    severity: Severity
    confidence: float              # 0..1, уверенность в аномалии
    description: str
    evidence: Dict[str, float] = field(default_factory=dict)
    affected_items: List[int] = field(default_factory=list)  # Индексы задач
    recommended_action: str = ""


@dataclass
class IntegrityReport:
    """Полный отчёт о целостности сессии"""
    session_id: str
    is_valid: bool                 # Результат можно доверять
    overall_confidence: float      # Общая уверенность в результате (0..1)
    
    anomalies: List[AnomalyReport] = field(default_factory=list)
    
    # Статистики
    person_fit_lz: Optional[float] = None      # l_z статистика
    person_fit_verdict: str = "acceptable"      # acceptable / suspect / aberrant
    
    avg_response_time_ms: float = 0.0
    rt_anomaly_fraction: float = 0.0            # Доля аномальных RT
    
    guttman_errors: int = 0
    guttman_ratio: float = 0.0                  # Отношение к общему числу пар
    
    theta_volatility: float = 0.0               # Волатильность θ
    max_theta_jump: float = 0.0                 # Максимальный скачок θ


# ============================================================================
# PERSON-FIT: l_z STATISTIC
# ============================================================================

def compute_lz(
    responses: List[ResponseData],
    theta: float
) -> float:
    """
    Вычисляет стандартизированную log-likelihood статистику l_z.
    
    l_z < -2: Аберрантный паттерн (подозрительный)
    l_z ∈ [-2, 2]: Нормальный
    l_z > 2: Слишком хороший fit (подозрительно идеальный)
    
    Формула:
    l_0 = Σ [u_i × ln P_i + (1-u_i) × ln(1-P_i)]
    E(l_0) = Σ [P_i × ln P_i + (1-P_i) × ln(1-P_i)]
    Var(l_0) = Σ [P_i × (1-P_i) × (ln(P_i / (1-P_i)))²]
    l_z = (l_0 - E(l_0)) / sqrt(Var(l_0))
    """
    if not responses:
        return 0.0
    
    l_0 = 0.0     # Наблюдаемый log-likelihood
    e_l0 = 0.0    # Ожидаемый
    var_l0 = 0.0  # Дисперсия
    
    for resp in responses:
        p = resp.task_params.probability(theta)
        p = max(0.001, min(0.999, p))  # Клиппинг для численной стабильности
        q = 1 - p
        
        # Наблюдаемый
        u = 1.0 if resp.is_correct else 0.0
        l_0 += u * math.log(p) + (1 - u) * math.log(q)
        
        # Ожидаемый
        e_l0 += p * math.log(p) + q * math.log(q)
        
        # Дисперсия
        log_ratio = math.log(p / q)
        var_l0 += p * q * (log_ratio ** 2)
    
    if var_l0 < 1e-10:
        return 0.0
    
    lz = (l_0 - e_l0) / math.sqrt(var_l0)
    return lz


# ============================================================================
# GUTTMAN ERROR ANALYSIS
# ============================================================================

def count_guttman_errors(responses: List[ResponseData]) -> Tuple[int, int]:
    """
    Подсчёт Guttman-ошибок: пар (i, j) где:
    - b_i < b_j (задача i легче j)
    - u_i = 0, u_j = 1 (ответ на лёгкую неправильный, на сложную — правильный)
    
    Returns:
        (guttman_errors, total_comparable_pairs)
    """
    errors = 0
    total = 0
    
    for i in range(len(responses)):
        for j in range(i + 1, len(responses)):
            b_i = responses[i].task_params.difficulty
            b_j = responses[j].task_params.difficulty
            u_i = responses[i].is_correct
            u_j = responses[j].is_correct
            
            # Пара сравнима только если сложности существенно различаются
            if abs(b_i - b_j) < 0.3:
                continue
            
            total += 1
            
            if b_i < b_j:
                # i легче j
                if not u_i and u_j:
                    errors += 1
            else:
                # j легче i
                if u_i and not u_j:
                    errors += 1
    
    return errors, total


# ============================================================================
# RESPONSE TIME ANALYSIS
# ============================================================================

@dataclass
class RTAnalysis:
    """Анализ времени ответа"""
    rapid_guessing_count: int = 0
    rapid_guessing_fraction: float = 0.0
    
    too_slow_count: int = 0
    too_slow_fraction: float = 0.0
    
    avg_time_ms: float = 0.0
    median_time_ms: float = 0.0
    cv: float = 0.0  # Коэффициент вариации
    
    # Подозрительные паттерны
    decreasing_rt_trend: bool = False    # Время сокращается → может привыкать
    bimodal_distribution: bool = False   # Два пика: быстрые (угадал) + медленные (решал)


def analyze_response_times(
    responses: List[ResponseData],
    rapid_threshold_ms: int = 2000,
    slow_threshold_ms: int = 300000
) -> RTAnalysis:
    """
    Анализ паттернов времени ответа.
    """
    if not responses:
        return RTAnalysis()
    
    times = [r.time_ms for r in responses]
    n = len(times)
    
    # Базовые метрики
    avg = sum(times) / n
    sorted_times = sorted(times)
    median = sorted_times[n // 2]
    
    variance = sum((t - avg) ** 2 for t in times) / max(1, n - 1)
    std = math.sqrt(variance)
    cv = std / avg if avg > 0 else 0
    
    # Быстрые / медленные
    rapid = sum(1 for t in times if t < rapid_threshold_ms)
    slow = sum(1 for t in times if t > slow_threshold_ms)
    
    # Тренд: линейная регрессия по индексам
    decreasing = False
    if n >= 5:
        x_mean = (n - 1) / 2
        y_mean = avg
        cov_xy = sum((i - x_mean) * (t - y_mean) for i, t in enumerate(times))
        var_x = sum((i - x_mean) ** 2 for i in range(n))
        
        if var_x > 0:
            slope = cov_xy / var_x
            # Если RT уменьшается > 10% за тест — тренд
            total_change = slope * n
            decreasing = total_change < -avg * 0.1
    
    # Бимодальность через Kernel Density Estimation (better than cv > 0.8)
    bimodal = False
    if n >= 10:
        # Silverman's test approximation: check if log-times have two modes
        log_times = [math.log(max(100, t)) for t in times]
        log_mean = sum(log_times) / n
        log_var = sum((lt - log_mean) ** 2 for lt in log_times) / max(1, n - 1)
        log_std = math.sqrt(log_var) if log_var > 0 else 0
        
        if log_std > 0:
            # Count how many points are > 1.5 SDs from mean on each side
            low_cluster = sum(1 for lt in log_times if lt < log_mean - 1.0 * log_std)
            high_cluster = sum(1 for lt in log_times if lt > log_mean + 1.0 * log_std)
            middle = n - low_cluster - high_cluster
            # Bimodal if both tails have substantial mass and middle is depleted
            bimodal = (low_cluster >= n * 0.15 and high_cluster >= n * 0.15 
                      and middle < n * 0.5)
    
    return RTAnalysis(
        rapid_guessing_count=rapid,
        rapid_guessing_fraction=rapid / n,
        too_slow_count=slow,
        too_slow_fraction=slow / n,
        avg_time_ms=avg,
        median_time_ms=float(median),
        cv=cv,
        decreasing_rt_trend=decreasing,
        bimodal_distribution=bimodal
    )


# ============================================================================
# LOG-NORMAL RESPONSE TIME MODEL (van der Linden, 2006)
# ============================================================================

@dataclass
class LogNormalRTResult:
    """Результат анализа по Log-Normal RT модели"""
    speed_tau: float                    # τ: параметр скорости ученика
    speed_se: float                     # SE оценки τ
    item_residuals: List[float]         # Остатки по каждой задаче
    flagged_items: List[int]            # Индексы подозрительных задач
    model_fit: float                    # -2LL / N (fit statistic)


def fit_lognormal_rt(
    responses: List[ResponseData],
    item_time_params: Optional[Dict[str, Tuple[float, float]]] = None
) -> LogNormalRTResult:
    """
    Log-Normal RT model (van der Linden, 2006):
    
    log(T_ij) = β_j - τ_i + ε_ij
    
    Где:
    - T_ij: время ответа ученика i на задачу j
    - β_j: параметр времени задачи (log-time intensity)  
    - τ_i: параметр скорости ученика (выше = быстрее)
    - ε_ij ~ N(0, σ²_j)
    
    Если item_time_params не заданы, β_j и σ_j оцениваются из данных.
    
    Args:
        responses: Ответы с time_ms
        item_time_params: {task_id: (beta_j, sigma_j)} — параметры задач (опционально)
    
    Returns:
        LogNormalRTResult с оценкой скорости и подозрительными задачами
    """
    if len(responses) < 3:
        return LogNormalRTResult(
            speed_tau=0.0, speed_se=1.0, item_residuals=[],
            flagged_items=[], model_fit=0.0
        )
    
    log_times = []
    betas = []
    sigmas = []
    
    for resp in responses:
        t = max(100, resp.time_ms)  # Минимум 100мс
        log_t = math.log(t / 1000.0)  # Переводим в секунды
        log_times.append(log_t)
        
        if item_time_params and resp.task_id in item_time_params:
            beta_j, sigma_j = item_time_params[resp.task_id]
        else:
            # Эвристика: β зависит от сложности задачи
            # Типичное время: exp(β) секунд
            b = resp.task_params.difficulty
            beta_j = 2.5 + 0.5 * b  # ~12с для b=0, ~30с для b=2
            sigma_j = 0.8  # Типичный разброс
        
        betas.append(beta_j)
        sigmas.append(sigma_j)
    
    n = len(log_times)
    
    # Оценка τ (MLE): τ = mean(β_j - log(T_j))
    tau_estimates = [betas[i] - log_times[i] for i in range(n)]
    tau = sum(tau_estimates) / n
    
    # SE τ
    tau_var = sum((te - tau) ** 2 for te in tau_estimates) / max(1, n - 1)
    tau_se = math.sqrt(tau_var / n) if tau_var > 0 else 1.0
    
    # Остатки: r_j = (log(T_j) - (β_j - τ)) / σ_j
    residuals = []
    flagged = []
    neg_2ll = 0.0
    
    for i in range(n):
        expected_log_t = betas[i] - tau
        residual = (log_times[i] - expected_log_t) / max(0.1, sigmas[i])
        residuals.append(residual)
        
        # Подозрительно если |r| > 2.5 (задача аномально быстрая/медленная)
        if abs(residual) > 2.5:
            flagged.append(i)
        
        # -2 Log Likelihood
        neg_2ll += residual ** 2 + 2 * math.log(max(0.1, sigmas[i]))
    
    model_fit = neg_2ll / n
    
    return LogNormalRTResult(
        speed_tau=tau,
        speed_se=tau_se,
        item_residuals=residuals,
        flagged_items=flagged,
        model_fit=model_fit
    )


# ============================================================================
# CUSUM — Cumulative Sum для online detection
# ============================================================================

@dataclass
class CUSUMResult:
    """Результат CUSUM-анализа"""
    change_detected: bool
    change_point: Optional[int]         # Индекс точки разладки
    max_cusum_pos: float                # Макс. положительный CUSUM
    max_cusum_neg: float                # Макс. отрицательный CUSUM
    cusum_trajectory: List[float]       # Траектория CUSUM


def cusum_detect(
    responses: List[ResponseData],
    theta: float,
    h: float = 4.0,
    k: float = 0.5
) -> CUSUMResult:
    """
    CUSUM (Cumulative Sum) для online обнаружения смены поведения.
    
    Отслеживает кумулятивную сумму отклонений от ожидаемого паттерна.
    Если CUSUM превышает порог h — обнаружена "разладка" (change point).
    
    Типичные причины разладки:
    - Начало списывания (внезапно правильные ответы на сложные задачи)
    - Усталость (внезапно неправильные)
    - Подмена тестируемого
    
    Формула:
    S_n^+ = max(0, S_{n-1}^+ + (z_n - k))    # Детекция положительного сдвига  
    S_n^- = max(0, S_{n-1}^- + (-z_n - k))    # Детекция отрицательного сдвига
    
    z_n = (u_n - P_n) / sqrt(P_n × Q_n)       # Стандартизированный остаток
    
    Alarm if S_n^+ > h or S_n^- > h
    
    Args:
        responses: Ответы сессии
        theta: Текущая оценка θ
        h: Порог срабатывания (по умолчанию 4.0, АRL ≈ 168 при in-control)
        k: Параметр допуска (allowance, обычно 0.5)
    
    Returns:
        CUSUMResult с точкой разладки
    """
    if len(responses) < 5:
        return CUSUMResult(
            change_detected=False, change_point=None,
            max_cusum_pos=0.0, max_cusum_neg=0.0, cusum_trajectory=[]
        )
    
    s_pos = 0.0  # Положительный CUSUM
    s_neg = 0.0  # Отрицательный CUSUM
    
    max_pos = 0.0
    max_neg = 0.0
    change_point = None
    trajectory = []
    
    for i, resp in enumerate(responses):
        p = resp.task_params.probability(theta)
        p = max(0.01, min(0.99, p))
        q = 1 - p
        
        # Стандартизированный остаток
        u = 1.0 if resp.is_correct else 0.0
        z = (u - p) / math.sqrt(p * q)
        
        # Обновление CUSUM
        s_pos = max(0.0, s_pos + (z - k))
        s_neg = max(0.0, s_neg + (-z - k))
        
        trajectory.append(s_pos - s_neg)  # Net CUSUM для визуализации
        
        max_pos = max(max_pos, s_pos)
        max_neg = max(max_neg, s_neg)
        
        # Проверка alarm
        if change_point is None and (s_pos > h or s_neg > h):
            change_point = i
    
    return CUSUMResult(
        change_detected=change_point is not None,
        change_point=change_point,
        max_cusum_pos=max_pos,
        max_cusum_neg=max_neg,
        cusum_trajectory=trajectory
    )


# ============================================================================
# THETA TRAJECTORY ANALYSIS
# ============================================================================

def analyze_theta_trajectory(
    theta_history: List[float],
    se_history: Optional[List[float]] = None
) -> Dict:
    """
    Анализ траектории θ для обнаружения аномалий.
    """
    if len(theta_history) < 3:
        return {"volatility": 0, "max_jump": 0, "stable": True}
    
    # Скачки
    jumps = [abs(theta_history[i] - theta_history[i-1]) for i in range(1, len(theta_history))]
    max_jump = max(jumps)
    avg_jump = sum(jumps) / len(jumps)
    
    # Волатильность (std скачков)
    jump_var = sum((j - avg_jump) ** 2 for j in jumps) / max(1, len(jumps) - 1)
    volatility = math.sqrt(jump_var)
    
    # Проверка на резкий jump (> 3 SD от среднего скачка)
    suspect_jumps = []
    if volatility > 0:
        threshold = avg_jump + 3 * volatility
        for i, jump in enumerate(jumps):
            if jump > threshold and jump > 1.0:
                suspect_jumps.append({
                    "position": i + 1,
                    "from_theta": theta_history[i],
                    "to_theta": theta_history[i + 1],
                    "jump_size": jump
                })
    
    # Финальная стабильность: последние 5 значений
    stable = True
    if len(theta_history) >= 5:
        last_5 = theta_history[-5:]
        range_5 = max(last_5) - min(last_5)
        stable = range_5 < 0.5
    
    return {
        "volatility": volatility,
        "max_jump": max_jump,
        "avg_jump": avg_jump,
        "suspect_jumps": suspect_jumps,
        "stable": stable,
        "converged": stable and len(theta_history) >= 8
    }


# ============================================================================
# MAIN INTEGRITY SERVICE
# ============================================================================

class IntegrityService:
    """
    Сервис проверки целостности диагностической сессии.
    
    Объединяет все анализы и выдаёт единый отчёт.
    
    Использование:
        service = IntegrityService()
        report = service.analyze(session_id, responses, theta, theta_history)
        
        if not report.is_valid:
            # Пометить сессию как подозрительную
    """
    
    def __init__(
        self,
        lz_suspect_threshold: float = -2.0,
        lz_aberrant_threshold: float = -3.0,
        guttman_max_ratio: float = 0.3,
        rapid_guessing_max_fraction: float = 0.3,
        theta_jump_threshold: float = 2.0,
        rapid_threshold_ms: int = 2000,
        slow_threshold_ms: int = 300000
    ):
        self.lz_suspect = lz_suspect_threshold
        self.lz_aberrant = lz_aberrant_threshold
        self.guttman_max_ratio = guttman_max_ratio
        self.rapid_guessing_max = rapid_guessing_max_fraction
        self.theta_jump_threshold = theta_jump_threshold
        self.rapid_threshold_ms = rapid_threshold_ms
        self.slow_threshold_ms = slow_threshold_ms
    
    def analyze(
        self,
        session_id: str,
        responses: List[ResponseData],
        theta: float,
        theta_history: Optional[List[float]] = None,
        se_history: Optional[List[float]] = None
    ) -> IntegrityReport:
        """
        Полный анализ целостности сессии.
        
        Args:
            session_id: ID сессии
            responses: Все ответы
            theta: Финальная оценка θ
            theta_history: История θ после каждого ответа
            se_history: История SE после каждого ответа
        """
        anomalies: List[AnomalyReport] = []
        
        if not responses:
            return IntegrityReport(
                session_id=session_id,
                is_valid=False,
                overall_confidence=0.0,
                anomalies=[AnomalyReport(
                    anomaly_type=AnomalyType.NONE,
                    severity=Severity.HIGH,
                    confidence=1.0,
                    description="Нет ответов в сессии"
                )]
            )
        
        # 1. Person-fit (l_z)
        lz = compute_lz(responses, theta)
        pf_verdict = "acceptable"
        
        if lz < self.lz_aberrant:
            pf_verdict = "aberrant"
            anomalies.append(AnomalyReport(
                anomaly_type=AnomalyType.INCONSISTENT,
                severity=Severity.HIGH,
                confidence=min(1.0, abs(lz) / 5.0),
                description=f"Аберрантный паттерн ответов (l_z={lz:.2f}). "
                            f"Ответы не соответствуют оценке способности.",
                evidence={"l_z": lz, "threshold": self.lz_aberrant},
                recommended_action="Рекомендуется пересдача или дополнительная проверка"
            ))
        elif lz < self.lz_suspect:
            pf_verdict = "suspect"
            anomalies.append(AnomalyReport(
                anomaly_type=AnomalyType.INCONSISTENT,
                severity=Severity.MEDIUM,
                confidence=min(1.0, abs(lz) / 4.0),
                description=f"Подозрительный паттерн ответов (l_z={lz:.2f}). "
                            f"Паттерн слабо соответствует оценке.",
                evidence={"l_z": lz, "threshold": self.lz_suspect}
            ))
        
        # 2. Guttman errors
        guttman_errors, guttman_total = count_guttman_errors(responses)
        guttman_ratio = guttman_errors / max(1, guttman_total)
        
        if guttman_ratio > self.guttman_max_ratio:
            anomalies.append(AnomalyReport(
                anomaly_type=AnomalyType.INCONSISTENT,
                severity=Severity.MEDIUM,
                confidence=min(1.0, guttman_ratio / 0.5),
                description=f"Много Guttman-ошибок ({guttman_errors}/{guttman_total}). "
                            f"Ученик ошибается на лёгких, но верно решает сложные.",
                evidence={
                    "errors": float(guttman_errors),
                    "total_pairs": float(guttman_total),
                    "ratio": guttman_ratio
                }
            ))
        
        # 3. Response time
        rt_analysis = analyze_response_times(
            responses, self.rapid_threshold_ms, self.slow_threshold_ms
        )
        
        if rt_analysis.rapid_guessing_fraction > self.rapid_guessing_max:
            severity = Severity.HIGH if rt_analysis.rapid_guessing_fraction > 0.5 else Severity.MEDIUM
            anomalies.append(AnomalyReport(
                anomaly_type=AnomalyType.RAPID_GUESSING,
                severity=severity,
                confidence=rt_analysis.rapid_guessing_fraction,
                description=f"{rt_analysis.rapid_guessing_count} из {len(responses)} "
                            f"ответов подозрительно быстрые (< {self.rapid_threshold_ms}мс). "
                            f"Возможно угадывание.",
                evidence={
                    "rapid_count": float(rt_analysis.rapid_guessing_count),
                    "fraction": rt_analysis.rapid_guessing_fraction,
                    "avg_time_ms": rt_analysis.avg_time_ms
                },
                recommended_action="Снизить уверенность в оценке" if severity == Severity.MEDIUM
                    else "Рекомендуется пересдача"
            ))
        
        if rt_analysis.bimodal_distribution:
            anomalies.append(AnomalyReport(
                anomaly_type=AnomalyType.CREATIVE_ANSWERING,
                severity=Severity.LOW,
                confidence=0.6,
                description="Бимодальное распределение времени ответа. "
                            "Часть задач решены быстро, часть — медленно.",
                evidence={"cv": rt_analysis.cv}
            ))
        
        # 4. Theta trajectory
        theta_volatility = 0.0
        max_theta_jump = 0.0
        
        if theta_history and len(theta_history) >= 3:
            trajectory = analyze_theta_trajectory(theta_history, se_history)
            theta_volatility = trajectory["volatility"]
            max_theta_jump = trajectory["max_jump"]
            
            for jump_info in trajectory.get("suspect_jumps", []):
                anomalies.append(AnomalyReport(
                    anomaly_type=AnomalyType.THETA_JUMP,
                    severity=Severity.HIGH,
                    confidence=min(1.0, jump_info["jump_size"] / 3.0),
                    description=f"Резкий скачок θ на позиции {jump_info['position']}: "
                                f"{jump_info['from_theta']:.2f} → {jump_info['to_theta']:.2f} "
                                f"(Δ={jump_info['jump_size']:.2f}).",
                    evidence={
                        "position": float(jump_info["position"]),
                        "jump_size": jump_info["jump_size"]
                    },
                    affected_items=[jump_info["position"]],
                    recommended_action="Проверить, не было ли внешней помощи"
                ))
        
        # 5. Fatigue / Warm-up detection
        if len(responses) >= 10:
            half = len(responses) // 2
            first_half_acc = sum(1 for r in responses[:half] if r.is_correct) / half
            second_half_acc = sum(1 for r in responses[half:] if r.is_correct) / (len(responses) - half)
            
            if second_half_acc < first_half_acc - 0.25:
                anomalies.append(AnomalyReport(
                    anomaly_type=AnomalyType.FATIGUE,
                    severity=Severity.LOW,
                    confidence=min(1.0, (first_half_acc - second_half_acc) / 0.4),
                    description=f"Возможная усталость: точность упала с "
                                f"{first_half_acc:.0%} до {second_half_acc:.0%}.",
                    evidence={
                        "first_half_accuracy": first_half_acc,
                        "second_half_accuracy": second_half_acc,
                        "drop": first_half_acc - second_half_acc
                    }
                ))
            elif second_half_acc > first_half_acc + 0.25:
                anomalies.append(AnomalyReport(
                    anomaly_type=AnomalyType.WARM_UP,
                    severity=Severity.LOW,
                    confidence=min(1.0, (second_half_acc - first_half_acc) / 0.4),
                    description=f"Эффект разогрева: точность выросла с "
                                f"{first_half_acc:.0%} до {second_half_acc:.0%}.",
                    evidence={
                        "first_half_accuracy": first_half_acc,
                        "second_half_accuracy": second_half_acc,
                        "improvement": second_half_acc - first_half_acc
                    }
                ))
        
        # 6. Cheating detection: высокий θ + быстрые ответы + высокая точность
        accuracy = sum(1 for r in responses if r.is_correct) / len(responses)
        if (
            theta > 2.0 and
            accuracy > 0.85 and
            rt_analysis.avg_time_ms < 10000 and
            len(responses) >= 10
        ):
            anomalies.append(AnomalyReport(
                anomaly_type=AnomalyType.CHEATING,
                severity=Severity.HIGH,
                confidence=0.7,
                description=f"Подозрение на списывание: высокий θ={theta:.2f}, "
                            f"точность {accuracy:.0%}, быстрые ответы ({rt_analysis.avg_time_ms:.0f}мс).",
                evidence={
                    "theta": theta,
                    "accuracy": accuracy,
                    "avg_time_ms": rt_analysis.avg_time_ms
                },
                recommended_action="Рекомендуется контрольная пересдача"
            ))
        
        # 7. Log-Normal RT model — обнаружение аномальных времён по модели
        if len(responses) >= 5:
            ln_rt = fit_lognormal_rt(responses)
            if ln_rt.flagged_items:
                anomalies.append(AnomalyReport(
                    anomaly_type=AnomalyType.CREATIVE_ANSWERING,
                    severity=Severity.MEDIUM if len(ln_rt.flagged_items) >= 3 else Severity.LOW,
                    confidence=min(1.0, len(ln_rt.flagged_items) / max(1, len(responses)) * 3),
                    description=f"Log-Normal RT модель: {len(ln_rt.flagged_items)} задач с "
                                f"аномальным временем (|z| > 2.5). τ={ln_rt.speed_tau:.2f} ± {ln_rt.speed_se:.2f}.",
                    evidence={
                        "speed_tau": ln_rt.speed_tau,
                        "speed_se": ln_rt.speed_se,
                        "flagged_count": len(ln_rt.flagged_items),
                        "flagged_indices": ln_rt.flagged_items[:10],
                        "model_fit": ln_rt.model_fit
                    },
                    affected_items=ln_rt.flagged_items
                ))
        
        # 8. CUSUM — online обнаружение разладки (смены поведения)
        if len(responses) >= 8:
            cusum = cusum_detect(responses, theta)
            if cusum.change_detected:
                severity = Severity.HIGH if cusum.change_point is not None and cusum.change_point < len(responses) * 0.5 else Severity.MEDIUM
                anomalies.append(AnomalyReport(
                    anomaly_type=AnomalyType.THETA_JUMP,
                    severity=severity,
                    confidence=min(1.0, max(cusum.max_cusum_pos, cusum.max_cusum_neg) / 8.0),
                    description=f"CUSUM: обнаружена разладка на позиции {cusum.change_point}. "
                                f"S+={cusum.max_cusum_pos:.2f}, S-={cusum.max_cusum_neg:.2f}. "
                                f"Возможна смена стратегии ответа.",
                    evidence={
                        "change_point": cusum.change_point,
                        "max_cusum_pos": cusum.max_cusum_pos,
                        "max_cusum_neg": cusum.max_cusum_neg,
                        "trajectory_len": len(cusum.cusum_trajectory)
                    },
                    affected_items=[cusum.change_point] if cusum.change_point is not None else [],
                    recommended_action="Проверить ответы после точки разладки"
                ))
        
        # 9. Random MC Pattern — случайное нажатие по MC без анализа задачи
        # Комбинация: низкая точность + быстрые ответы + нет паттерна в ошибках
        if len(responses) >= 8:
            wrong_responses = [r for r in responses if not r.is_correct]
            wrong_fast = [
                r for r in wrong_responses
                if r.time_ms < self.rapid_threshold_ms * 2  # < 2× порога быстрых
            ]
            wrong_ratio = len(wrong_responses) / len(responses)
            fast_wrong_ratio = len(wrong_fast) / max(1, len(wrong_responses))
            
            # Условия: >60% неправильных И >50% из них быстрые И θ < −1
            if wrong_ratio > 0.60 and fast_wrong_ratio > 0.50 and theta < -1.0:
                conf = min(1.0, wrong_ratio * fast_wrong_ratio * 1.5)
                sev = Severity.HIGH if conf > 0.7 else Severity.MEDIUM
                anomalies.append(AnomalyReport(
                    anomaly_type=AnomalyType.RANDOM_MC,
                    severity=sev,
                    confidence=conf,
                    description=f"Подозрение на случайный выбор MC: "
                                f"{len(wrong_responses)}/{len(responses)} неправильных, "
                                f"{len(wrong_fast)} из них быстрые. θ={theta:.2f}.",
                    evidence={
                        "wrong_ratio": wrong_ratio,
                        "fast_wrong_ratio": fast_wrong_ratio,
                        "theta": theta,
                        "total_wrong": len(wrong_responses),
                        "fast_wrong": len(wrong_fast),
                    },
                    recommended_action="Результат ненадёжен — рекомендуется пересдача"
                ))
        
        # ─── Собираем итоговый отчёт ───
        
        # Определяем валидность
        high_anomalies = [a for a in anomalies if a.severity in (Severity.HIGH, Severity.CRITICAL)]
        is_valid = len(high_anomalies) == 0
        
        # Общая уверенность
        if not anomalies:
            overall_confidence = 1.0
        else:
            # Снижаем уверенность пропорционально аномалиям
            penalty = sum(
                a.confidence * (0.3 if a.severity == Severity.LOW else
                               0.5 if a.severity == Severity.MEDIUM else
                               0.8 if a.severity == Severity.HIGH else 1.0)
                for a in anomalies
            )
            overall_confidence = max(0.0, 1.0 - min(1.0, penalty))
        
        return IntegrityReport(
            session_id=session_id,
            is_valid=is_valid,
            overall_confidence=overall_confidence,
            anomalies=anomalies,
            person_fit_lz=lz,
            person_fit_verdict=pf_verdict,
            avg_response_time_ms=rt_analysis.avg_time_ms,
            rt_anomaly_fraction=rt_analysis.rapid_guessing_fraction + rt_analysis.too_slow_fraction,
            guttman_errors=guttman_errors,
            guttman_ratio=guttman_ratio,
            theta_volatility=theta_volatility,
            max_theta_jump=max_theta_jump
        )
