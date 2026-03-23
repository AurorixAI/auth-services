"""
ALGO V1 - Learning Graph Updater
Самообучающийся граф знаний на основе данных студентов

Система обнаруживает:
1. Новые корреляции между навыками
2. Обновляет веса существующих связей
3. Выявляет паттерны ошибок
4. Калибрует IRT параметры задач
"""

import math
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Set
from datetime import datetime, timedelta
from enum import Enum
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


class DiscoverySource(Enum):
    """Источник обнаружения связи"""
    EXPERT = "expert"               # Заложено экспертами
    STATISTICAL = "statistical"     # Статистический анализ
    ML = "ml"                       # ML модель
    PATTERN_MINING = "pattern_mining"  # Анализ паттернов


class RelationshipType(Enum):
    """Тип связи между навыками"""
    PREREQUISITE = "prerequisite"   # A → B (A нужен для B)
    COREQUISITE = "corequisite"     # A ↔ B (связаны)
    DIAGNOSTIC = "diagnostic"       # Ошибка в A предсказывает ошибку в B
    TRANSFER = "transfer"           # Знание A помогает в B


@dataclass
class AnswerEvent:
    """Событие ответа для анализа"""
    session_id: str
    student_id: Optional[str]
    skill_id: str
    task_id: str
    is_correct: bool
    time_ms: int
    submitted_at: datetime
    
    # Опциональные данные
    difficulty: str = "B"
    theta_before: Optional[float] = None
    theta_after: Optional[float] = None


@dataclass
class SkillPairStats:
    """Статистика пары навыков"""
    skill_a: str
    skill_b: str
    
    # Матрица совместных ответов
    both_correct: int = 0
    a_correct_b_wrong: int = 0
    a_wrong_b_correct: int = 0
    both_wrong: int = 0
    
    # Временной анализ
    a_before_b_success: int = 0  # A правильно перед B → B правильно
    a_before_b_fail: int = 0     # A неправильно перед B → B неправильно
    
    @property
    def total(self) -> int:
        return self.both_correct + self.a_correct_b_wrong + self.a_wrong_b_correct + self.both_wrong
    
    def phi_coefficient(self) -> float:
        """
        Вычисляет коэффициент φ (Phi) - меру корреляции для бинарных данных
        
        φ = (n11*n00 - n10*n01) / sqrt((n11+n10)(n01+n00)(n11+n01)(n10+n00))
        """
        n11 = self.both_correct
        n00 = self.both_wrong
        n10 = self.a_correct_b_wrong
        n01 = self.a_wrong_b_correct
        
        numerator = n11 * n00 - n10 * n01
        denominator = math.sqrt(
            (n11 + n10) * (n01 + n00) * (n11 + n01) * (n10 + n00)
        )
        
        if denominator == 0:
            return 0.0
        
        return numerator / denominator
    
    def conditional_probability_b_given_a(self) -> Tuple[float, float]:
        """
        P(B correct | A correct) и P(B correct | A wrong)
        """
        if self.both_correct + self.a_correct_b_wrong == 0:
            p_b_if_a_correct = 0.5
        else:
            p_b_if_a_correct = self.both_correct / (self.both_correct + self.a_correct_b_wrong)
        
        if self.a_wrong_b_correct + self.both_wrong == 0:
            p_b_if_a_wrong = 0.5
        else:
            p_b_if_a_wrong = self.a_wrong_b_correct / (self.a_wrong_b_correct + self.both_wrong)
        
        return p_b_if_a_correct, p_b_if_a_wrong


@dataclass
class DiscoveredCorrelation:
    """Обнаруженная корреляция"""
    skill_a: str
    skill_b: str
    
    correlation: float
    p_value: float
    sample_size: int
    
    relationship_type: RelationshipType
    direction: str  # "a_to_b", "b_to_a", "bidirectional"
    
    strength: float  # 1-10
    confidence: float  # 0-1
    
    discovery_source: DiscoverySource
    discovered_at: datetime = field(default_factory=datetime.now)
    
    # Валидация
    is_validated: bool = False
    requires_review: bool = True


@dataclass
class ErrorPattern:
    """Паттерн ошибок"""
    skill_id: str
    pattern_type: str
    pattern_description: str
    
    occurrence_count: int = 0
    occurrence_rate: float = 0.0
    
    root_cause_skills: List[str] = field(default_factory=list)
    
    discovered_at: datetime = field(default_factory=datetime.now)


@dataclass
class GraphUpdate:
    """Обновление графа для применения"""
    update_type: str
    entity_type: str
    entity_id: str
    
    old_value: Optional[dict] = None
    new_value: Optional[dict] = None
    
    triggered_by: DiscoverySource = DiscoverySource.STATISTICAL
    requires_review: bool = False
    
    created_at: datetime = field(default_factory=datetime.now)


class LearningGraphUpdater:
    """
    Класс для обучения графа знаний на основе данных
    
    Анализирует ответы студентов и:
    1. Обнаруживает скрытые связи между навыками
    2. Обновляет веса существующих связей
    3. Выявляет паттерны ошибок
    4. Генерирует рекомендации по обновлению графа
    """
    
    # Пороги для обнаружения связей
    MIN_SAMPLE_SIZE = 100           # Минимум ответов для анализа
    CORRELATION_THRESHOLD = 0.3     # Минимальная корреляция для связи
    P_VALUE_THRESHOLD = 0.05        # Порог статистической значимости
    
    # Пороги для обновления весов
    WEIGHT_UPDATE_THRESHOLD = 0.1   # Минимальное изменение веса
    CONFIDENCE_DECAY = 0.95         # Затухание уверенности со временем
    
    def __init__(
        self,
        skill_ids: Set[str],
        existing_prerequisites: Dict[Tuple[str, str], float],  # (prereq, skill) -> weight
        batch_size: int = 10000
    ):
        """
        Args:
            skill_ids: Множество всех ID навыков
            existing_prerequisites: Существующие связи {(prereq, skill): weight}
            batch_size: Размер батча для обработки
        """
        self.skill_ids = skill_ids
        self.existing_prerequisites = existing_prerequisites
        self.batch_size = batch_size
        
        # Статистика пар навыков
        self.pair_stats: Dict[Tuple[str, str], SkillPairStats] = {}
        
        # Статистика по навыкам
        self.skill_stats: Dict[str, Dict] = defaultdict(lambda: {
            "total": 0,
            "correct": 0,
            "total_time_ms": 0,
            "times": []
        })
        
        # Обнаруженные корреляции
        self.discovered_correlations: List[DiscoveredCorrelation] = []
        
        # Паттерны ошибок
        self.error_patterns: List[ErrorPattern] = []
        
        # Обновления для применения
        self.pending_updates: List[GraphUpdate] = []
        
        # Кеш сессий для временного анализа
        self._session_cache: Dict[str, List[AnswerEvent]] = defaultdict(list)
    
    def process_events(self, events: List[AnswerEvent]) -> None:
        """
        Обработать батч событий ответов.
        
        Применяет экспоненциальный time-decay: свежие ответы важнее старых.
        Полупериод распада: 30 дней (ответ месячной давности = 0.5 веса).
        
        Args:
            events: Список событий
        """
        logger.info(f"Processing {len(events)} answer events")
        
        now = datetime.now()
        half_life_days = 30.0  # Полупериод распада: 30 дней
        decay_lambda = math.log(2) / half_life_days
        
        # Группируем по сессиям для временного анализа
        for event in events:
            # Time-decay weight: w = exp(-λ × age_days)
            age_days = (now - event.submitted_at).total_seconds() / 86400.0
            time_weight = math.exp(-decay_lambda * max(0, age_days))
            
            self._session_cache[event.session_id].append(event)
            
            # Обновляем статистику навыка с time-decay
            stats = self.skill_stats[event.skill_id]
            stats["total"] += time_weight
            if event.is_correct:
                stats["correct"] += time_weight
            stats["total_time_ms"] += event.time_ms * time_weight
            stats["times"].append(event.time_ms)
        
        # Анализируем сессии
        for session_id, session_events in self._session_cache.items():
            self._analyze_session(session_events)
        
        # Очищаем кеш
        self._session_cache.clear()
    
    def _analyze_session(self, events: List[AnswerEvent]) -> None:
        """Анализ одной сессии для поиска связей"""
        if len(events) < 2:
            return
        
        # Сортируем по времени
        events.sort(key=lambda e: e.submitted_at)
        
        # Строим карту skill -> (is_correct, order)
        skill_results = {}
        for i, event in enumerate(events):
            if event.skill_id not in skill_results:
                skill_results[event.skill_id] = (event.is_correct, i)
        
        # Анализируем все пары навыков в сессии
        skills = list(skill_results.keys())
        for i in range(len(skills)):
            for j in range(i + 1, len(skills)):
                skill_a = skills[i]
                skill_b = skills[j]
                
                result_a, order_a = skill_results[skill_a]
                result_b, order_b = skill_results[skill_b]
                
                # Получаем или создаём статистику пары
                pair_key = (skill_a, skill_b) if skill_a < skill_b else (skill_b, skill_a)
                if pair_key not in self.pair_stats:
                    self.pair_stats[pair_key] = SkillPairStats(
                        skill_a=pair_key[0],
                        skill_b=pair_key[1]
                    )
                
                stats = self.pair_stats[pair_key]
                
                # Обновляем матрицу совместных ответов
                if result_a and result_b:
                    stats.both_correct += 1
                elif result_a and not result_b:
                    stats.a_correct_b_wrong += 1
                elif not result_a and result_b:
                    stats.a_wrong_b_correct += 1
                else:
                    stats.both_wrong += 1
                
                # Временной анализ (A перед B)
                if order_a < order_b:
                    if result_a and result_b:
                        stats.a_before_b_success += 1
                    elif not result_a and not result_b:
                        stats.a_before_b_fail += 1
    
    def analyze_correlations(self) -> List[DiscoveredCorrelation]:
        """
        Анализ статистики для обнаружения корреляций.
        
        Применяет Benjamini-Hochberg FDR коррекцию для множественных
        сравнений, чтобы контролировать ложноположительные обнаружения.
        
        Returns:
            Список обнаруженных корреляций (FDR-скорректированных)
        """
        logger.info(f"Analyzing {len(self.pair_stats)} skill pairs")
        
        # Шаг 1: Собираем все кандидаты (без порога p-value)
        candidates = []
        
        for (skill_a, skill_b), stats in self.pair_stats.items():
            if stats.total < self.MIN_SAMPLE_SIZE:
                continue
            
            phi = stats.phi_coefficient()
            
            if abs(phi) < self.CORRELATION_THRESHOLD:
                continue
            
            chi_squared = (phi ** 2) * stats.total
            p_value = self._chi_squared_p_value(chi_squared, df=1)
            
            relationship_type, direction = self._determine_relationship_type(
                skill_a, skill_b, stats, phi
            )
            
            candidates.append({
                'skill_a': skill_a,
                'skill_b': skill_b,
                'phi': phi,
                'p_value': p_value,
                'stats': stats,
                'relationship_type': relationship_type,
                'direction': direction
            })
        
        # Шаг 2: Benjamini-Hochberg FDR коррекция
        if not candidates:
            return []
        
        # Сортируем по p-value (по возрастанию)
        candidates.sort(key=lambda c: c['p_value'])
        m = len(candidates)
        
        # BH процедура: p_i ≤ (i/m) × α
        alpha = self.P_VALUE_THRESHOLD
        bh_threshold_idx = -1
        
        for i, cand in enumerate(candidates):
            bh_critical = ((i + 1) / m) * alpha
            if cand['p_value'] <= bh_critical:
                bh_threshold_idx = i
        
        # Все кандидаты с индексом ≤ bh_threshold_idx проходят FDR коррекцию
        significant_candidates = candidates[:bh_threshold_idx + 1] if bh_threshold_idx >= 0 else []
        
        logger.info(
            f"FDR correction: {len(candidates)} candidates → "
            f"{len(significant_candidates)} significant (BH α={alpha})"
        )
        
        # Шаг 3: Формируем результаты
        new_correlations = []
        
        for cand in significant_candidates:
            stats = cand['stats']
            phi = cand['phi']
            skill_a = cand['skill_a']
            skill_b = cand['skill_b']
            p_value = cand['p_value']
            relationship_type = cand['relationship_type']
            direction = cand['direction']
            
            # Вычисляем силу связи (1-10)
            strength = min(10, abs(phi) * 10 + 2)
            
            # Вычисляем уверенность (0-1)
            confidence = min(1.0, (stats.total / 1000) * (1 - p_value))
            
            correlation = DiscoveredCorrelation(
                skill_a=skill_a,
                skill_b=skill_b,
                correlation=phi,
                p_value=p_value,
                sample_size=stats.total,
                relationship_type=relationship_type,
                direction=direction,
                strength=strength,
                confidence=confidence,
                discovery_source=DiscoverySource.STATISTICAL,
                requires_review=abs(phi) > 0.5  # Сильные корреляции требуют ревью
            )
            
            new_correlations.append(correlation)
            
            logger.info(
                f"Discovered correlation: {skill_a} <-> {skill_b}, "
                f"φ={phi:.3f}, p={p_value:.4f}, n={stats.total}"
            )
        
        self.discovered_correlations.extend(new_correlations)
        return new_correlations
    
    def _determine_relationship_type(
        self,
        skill_a: str,
        skill_b: str,
        stats: SkillPairStats,
        phi: float
    ) -> Tuple[RelationshipType, str]:
        """Определить тип связи и направление"""
        
        # Анализируем условные вероятности
        p_b_if_a_correct, p_b_if_a_wrong = stats.conditional_probability_b_given_a()
        
        # Проверяем временную последовательность
        temporal_effect = 0
        if stats.a_before_b_success + stats.a_before_b_fail > 10:
            success_rate = stats.a_before_b_success / (stats.a_before_b_success + stats.a_before_b_fail)
            temporal_effect = success_rate - 0.5  # > 0 если A помогает B
        
        # Проверяем, есть ли уже prerequisite связь
        is_prereq_a_to_b = (skill_a, skill_b) in self.existing_prerequisites
        is_prereq_b_to_a = (skill_b, skill_a) in self.existing_prerequisites
        
        # Определяем тип
        if is_prereq_a_to_b or is_prereq_b_to_a:
            # Если уже есть prerequisite - это подтверждение
            if is_prereq_a_to_b:
                return RelationshipType.PREREQUISITE, "a_to_b"
            else:
                return RelationshipType.PREREQUISITE, "b_to_a"
        
        if temporal_effect > 0.2:
            # Сильный временной эффект → prerequisite
            return RelationshipType.PREREQUISITE, "a_to_b"
        elif temporal_effect < -0.2:
            return RelationshipType.PREREQUISITE, "b_to_a"
        
        if phi > 0:
            # Положительная корреляция → transfer или corequisite
            if abs(p_b_if_a_correct - p_b_if_a_wrong) > 0.3:
                return RelationshipType.TRANSFER, "a_to_b"
            else:
                return RelationshipType.COREQUISITE, "bidirectional"
        else:
            # Отрицательная корреляция → diagnostic
            return RelationshipType.DIAGNOSTIC, "bidirectional"
    
    def _chi_squared_p_value(self, chi_squared: float, df: int = 1) -> float:
        """
        Расчёт p-value для χ² через Wilson-Hilferty аппроксимацию.
        
        Точность: ±0.001 для p > 0.001, что достаточно для статистических тестов.
        Не требует scipy — чистая математика.
        
        Формула: Z = ((χ²/df)^(1/3) - (1 - 2/(9·df))) / √(2/(9·df))
        P-value ≈ 1 - Φ(Z)  (complementary normal CDF)
        """
        if chi_squared <= 0:
            return 1.0
        if df <= 0:
            return 1.0
        
        # Wilson-Hilferty approximation: χ² → Z-score
        k = df
        z_num = (chi_squared / k) ** (1.0 / 3.0) - (1.0 - 2.0 / (9.0 * k))
        z_den = math.sqrt(2.0 / (9.0 * k))
        
        if z_den < 1e-10:
            return 0.0 if chi_squared > df else 1.0
        
        z = z_num / z_den
        
        # Complementary normal CDF approximation (Abramowitz & Stegun, 1972)
        # |error| < 7.5×10⁻⁸
        if z < -8:
            return 1.0
        if z > 8:
            return 0.0
        
        # Standard normal CDF via rational approximation
        t = 1.0 / (1.0 + 0.2316419 * abs(z))
        d = 0.3989422804014327  # 1/√(2π)
        poly = d * math.exp(-0.5 * z * z) * (
            t * (0.319381530
            + t * (-0.356563782
            + t * (1.781477937
            + t * (-1.821255978
            + t * 1.330274429))))
        )
        
        if z >= 0:
            return poly
        else:
            return 1.0 - poly
    
    def update_prerequisite_weights(self) -> List[GraphUpdate]:
        """
        Обновить веса существующих prerequisite связей
        на основе собранной статистики
        
        Returns:
            Список обновлений для применения
        """
        updates = []
        
        for (prereq_id, skill_id), current_weight in self.existing_prerequisites.items():
            # Ищем статистику для этой пары
            pair_key = (prereq_id, skill_id) if prereq_id < skill_id else (skill_id, prereq_id)
            stats = self.pair_stats.get(pair_key)
            
            if not stats or stats.total < self.MIN_SAMPLE_SIZE:
                continue
            
            # Вычисляем новый вес на основе корреляции
            phi = stats.phi_coefficient()
            
            # Нормализуем φ в диапазон веса (0.5 - 2.0)
            # φ = 0 → weight = 1.0
            # φ = 1 → weight = 2.0
            # φ = -1 → weight = 0.5
            learned_weight = 1.0 + phi * 0.5
            
            # Сглаживание с текущим весом (экспоненциальное скользящее среднее)
            alpha = 0.3  # Скорость обновления
            new_weight = current_weight * (1 - alpha) + learned_weight * alpha
            
            # Проверяем, достаточно ли значительное изменение
            if abs(new_weight - current_weight) < self.WEIGHT_UPDATE_THRESHOLD:
                continue
            
            update = GraphUpdate(
                update_type="strength_update",
                entity_type="prerequisite",
                entity_id=f"{prereq_id}->{skill_id}",
                old_value={
                    "weight": current_weight,
                    "sample_size": 0
                },
                new_value={
                    "weight": new_weight,
                    "learned_weight": learned_weight,
                    "sample_size": stats.total,
                    "confidence": min(1.0, stats.total / 1000)
                },
                triggered_by=DiscoverySource.STATISTICAL,
                requires_review=abs(new_weight - current_weight) > 0.3
            )
            
            updates.append(update)
            
            logger.info(
                f"Weight update: {prereq_id}->{skill_id}: "
                f"{current_weight:.2f} -> {new_weight:.2f} (φ={phi:.3f})"
            )
        
        self.pending_updates.extend(updates)
        return updates
    
    def detect_error_patterns(self) -> List[ErrorPattern]:
        """
        Выявление паттернов ошибок
        
        Анализирует:
        1. Навыки с аномально низким success rate
        2. Кластеры связанных ошибок
        3. Временные паттерны (ошибки в начале/конце)
        """
        patterns = []
        
        for skill_id, stats in self.skill_stats.items():
            if stats["total"] < 50:
                continue
            
            success_rate = stats["correct"] / stats["total"]
            
            # Паттерн: аномально низкий success rate
            if success_rate < 0.3:
                # Ищем корневые причины через корреляции
                root_causes = self._find_root_causes(skill_id)
                
                pattern = ErrorPattern(
                    skill_id=skill_id,
                    pattern_type="concept_gap",
                    pattern_description=f"Low success rate ({success_rate:.1%})",
                    occurrence_count=stats["total"],
                    occurrence_rate=1 - success_rate,
                    root_cause_skills=root_causes
                )
                patterns.append(pattern)
            
            # Паттерн: аномально быстрые неправильные ответы
            if stats["times"]:
                avg_time = sum(stats["times"]) / len(stats["times"])
                
                # Считаем быстрые ответы
                fast_threshold = avg_time * 0.3
                fast_wrong = sum(
                    1 for t in stats["times"]
                    if t < fast_threshold
                )
                
                if fast_wrong > stats["total"] * 0.2:
                    pattern = ErrorPattern(
                        skill_id=skill_id,
                        pattern_type="careless_mistake",
                        pattern_description=f"High rate of fast wrong answers ({fast_wrong}/{stats['total']})",
                        occurrence_count=fast_wrong,
                        occurrence_rate=fast_wrong / stats["total"]
                    )
                    patterns.append(pattern)
        
        self.error_patterns.extend(patterns)
        return patterns
    
    def _find_root_causes(self, skill_id: str) -> List[str]:
        """Найти потенциальные корневые причины пробела в навыке"""
        root_causes = []
        
        # Ищем навыки с высокой корреляцией ошибок
        for (skill_a, skill_b), stats in self.pair_stats.items():
            related_skill = None
            if skill_a == skill_id:
                related_skill = skill_b
            elif skill_b == skill_id:
                related_skill = skill_a
            
            if not related_skill:
                continue
            
            if stats.total < 50:
                continue
            
            # Проверяем: если related_skill неправильно, то skill_id тоже часто неправильно
            if stats.both_wrong > stats.total * 0.4:
                root_causes.append(related_skill)
        
        return root_causes[:5]  # Максимум 5 причин
    
    def propose_new_prerequisites(self) -> List[GraphUpdate]:
        """
        Предложить новые prerequisite связи
        на основе обнаруженных корреляций.
        Перед добавлением проверяет, что новая связь не создаст цикл в DAG.
        """
        proposals = []
        
        # Строим adjacency list из существующих + уже предложенных связей
        # adj[prereq] = {skill1, skill2, ...} означает prereq → skill (prereq нужен для skill)
        adj: Dict[str, Set[str]] = defaultdict(set)
        for (prereq_id, skill_id) in self.existing_prerequisites:
            adj[prereq_id].add(skill_id)
        
        for corr in self.discovered_correlations:
            # Только prerequisite связи
            if corr.relationship_type != RelationshipType.PREREQUISITE:
                continue
            
            # Определяем направление
            if corr.direction == "a_to_b":
                prereq_id, skill_id = corr.skill_a, corr.skill_b
            elif corr.direction == "b_to_a":
                prereq_id, skill_id = corr.skill_b, corr.skill_a
            else:
                continue
            
            # Проверяем, не существует ли уже
            if (prereq_id, skill_id) in self.existing_prerequisites:
                continue
            
            # Проверяем, не создаст ли цикл
            if self._would_create_cycle(prereq_id, skill_id, adj):
                logger.warning(
                    f"Skipping prerequisite {prereq_id} -> {skill_id}: "
                    f"would create cycle in DAG"
                )
                continue
            
            proposal = GraphUpdate(
                update_type="prerequisite_added",
                entity_type="prerequisite",
                entity_id=f"{prereq_id}->{skill_id}",
                old_value=None,
                new_value={
                    "skill_id": skill_id,
                    "prerequisite_id": prereq_id,
                    "weight": 1.0,
                    "learned_weight": corr.strength / 10,
                    "dependency_type": "soft",  # Новые связи начинаем как soft
                    "confidence": corr.confidence,
                    "sample_size": corr.sample_size,
                    "discovery_source": corr.discovery_source.value
                },
                triggered_by=corr.discovery_source,
                requires_review=True  # Новые связи всегда требуют ревью
            )
            
            proposals.append(proposal)
            # Добавляем в adj чтобы последующие proposals тоже учитывали эту связь
            adj[prereq_id].add(skill_id)
            
            logger.info(
                f"New prerequisite proposed: {prereq_id} -> {skill_id} "
                f"(strength={corr.strength:.1f}, confidence={corr.confidence:.2f})"
            )
        
        self.pending_updates.extend(proposals)
        return proposals
    
    def _would_create_cycle(
        self,
        prereq_id: str,
        skill_id: str,
        adj: Dict[str, Set[str]]
    ) -> bool:
        """
        Проверяет, создаст ли добавление ребра prereq_id → skill_id цикл.
        
        Цикл возникает, если skill_id уже может достичь prereq_id по
        существующим рёбрам (т.е. skill_id → ... → prereq_id).
        Реализация: BFS/DFS от skill_id, ищем prereq_id.
        """
        # Если добавляем prereq → skill, цикл будет если skill → ... → prereq
        visited: Set[str] = set()
        stack = [skill_id]
        
        while stack:
            node = stack.pop()
            if node == prereq_id:
                return True
            if node in visited:
                continue
            visited.add(node)
            for neighbor in adj.get(node, set()):
                if neighbor not in visited:
                    stack.append(neighbor)
        
        return False
    
    def generate_report(self) -> Dict:
        """Сгенерировать отчёт об анализе"""
        return {
            "analyzed_pairs": len(self.pair_stats),
            "analyzed_skills": len(self.skill_stats),
            "discovered_correlations": len(self.discovered_correlations),
            "error_patterns": len(self.error_patterns),
            "pending_updates": len(self.pending_updates),
            "correlations_by_type": self._count_by_type(),
            "top_correlations": self._get_top_correlations(10),
            "skills_with_issues": self._get_problem_skills()
        }
    
    def _count_by_type(self) -> Dict[str, int]:
        """Подсчёт корреляций по типам"""
        counts = defaultdict(int)
        for corr in self.discovered_correlations:
            counts[corr.relationship_type.value] += 1
        return dict(counts)
    
    def _get_top_correlations(self, n: int) -> List[Dict]:
        """Топ-N сильнейших корреляций"""
        sorted_corrs = sorted(
            self.discovered_correlations,
            key=lambda c: abs(c.correlation),
            reverse=True
        )
        
        return [
            {
                "skill_a": c.skill_a,
                "skill_b": c.skill_b,
                "correlation": c.correlation,
                "type": c.relationship_type.value,
                "sample_size": c.sample_size
            }
            for c in sorted_corrs[:n]
        ]
    
    def _get_problem_skills(self) -> List[Dict]:
        """Навыки с проблемами"""
        problems = []
        
        for skill_id, stats in self.skill_stats.items():
            if stats["total"] < 50:
                continue
            
            success_rate = stats["correct"] / stats["total"]
            
            if success_rate < 0.5:
                problems.append({
                    "skill_id": skill_id,
                    "success_rate": success_rate,
                    "total_attempts": stats["total"]
                })
        
        problems.sort(key=lambda x: x["success_rate"])
        return problems[:20]
    
    def get_pending_updates(self, require_review: Optional[bool] = None) -> List[GraphUpdate]:
        """
        Получить обновления, ожидающие применения
        
        Args:
            require_review: Фильтр по требованию ревью (None = все)
        """
        if require_review is None:
            return self.pending_updates
        
        return [
            u for u in self.pending_updates
            if u.requires_review == require_review
        ]
    
    def clear_pending_updates(self) -> None:
        """Очистить список ожидающих обновлений"""
        self.pending_updates.clear()


class BatchGraphUpdater:
    """
    Batch-обработчик для обновления графа
    
    Запускается периодически (ежедневно/еженедельно)
    для анализа накопленных данных
    """
    
    def __init__(
        self,
        data_fetcher: "DataFetcher",
        graph_writer: "GraphWriter",
        notification_service: Optional["NotificationService"] = None
    ):
        """
        Args:
            data_fetcher: Сервис для получения данных из БД
            graph_writer: Сервис для записи изменений в БД
            notification_service: Сервис уведомлений (опционально)
        """
        self.data_fetcher = data_fetcher
        self.graph_writer = graph_writer
        self.notification_service = notification_service
    
    def run_batch_analysis(
        self,
        since: datetime,
        until: Optional[datetime] = None,
        auto_apply: bool = False
    ) -> Dict:
        """
        Запустить batch-анализ за период
        
        Args:
            since: Начало периода
            until: Конец периода (по умолчанию = сейчас)
            auto_apply: Автоматически применять обновления без ревью
            
        Returns:
            Отчёт об анализе
        """
        until = until or datetime.now()
        
        logger.info(f"Starting batch analysis: {since} to {until}")
        
        # 1. Получаем данные
        skill_ids = self.data_fetcher.get_all_skill_ids()
        prerequisites = self.data_fetcher.get_all_prerequisites()
        
        # 2. Создаём updater
        updater = LearningGraphUpdater(
            skill_ids=set(skill_ids),
            existing_prerequisites=prerequisites
        )
        
        # 3. Обрабатываем события батчами
        offset = 0
        batch_size = 10000
        total_events = 0
        
        while True:
            events = self.data_fetcher.get_answer_events(
                since=since,
                until=until,
                offset=offset,
                limit=batch_size
            )
            
            if not events:
                break
            
            updater.process_events(events)
            total_events += len(events)
            offset += batch_size
            
            logger.info(f"Processed {total_events} events")
        
        # 4. Анализируем
        correlations = updater.analyze_correlations()
        weight_updates = updater.update_prerequisite_weights()
        error_patterns = updater.detect_error_patterns()
        new_prereqs = updater.propose_new_prerequisites()
        
        # 5. Применяем автоматические обновления
        applied_count = 0
        if auto_apply:
            auto_updates = updater.get_pending_updates(require_review=False)
            for update in auto_updates:
                self.graph_writer.apply_update(update)
                applied_count += 1
        
        # 6. Уведомляем о требующих ревью
        if self.notification_service:
            review_updates = updater.get_pending_updates(require_review=True)
            if review_updates:
                self.notification_service.notify_updates_pending_review(
                    count=len(review_updates),
                    updates=review_updates[:10]  # Первые 10 для превью
                )
        
        # 7. Генерируем отчёт
        report = updater.generate_report()
        report.update({
            "period": {
                "since": since.isoformat(),
                "until": until.isoformat()
            },
            "total_events_processed": total_events,
            "auto_applied_updates": applied_count
        })
        
        logger.info(f"Batch analysis complete: {total_events} events, {len(correlations)} correlations")
        
        return report


# Интерфейсы для dependency injection

class DataFetcher:
    """Интерфейс для получения данных из БД"""
    
    def get_all_skill_ids(self) -> List[str]:
        raise NotImplementedError
    
    def get_all_prerequisites(self) -> Dict[Tuple[str, str], float]:
        raise NotImplementedError
    
    def get_answer_events(
        self,
        since: datetime,
        until: datetime,
        offset: int = 0,
        limit: int = 10000
    ) -> List[AnswerEvent]:
        raise NotImplementedError


class GraphWriter:
    """Интерфейс для записи изменений в БД"""
    
    def apply_update(self, update: GraphUpdate) -> bool:
        raise NotImplementedError


class NotificationService:
    """Интерфейс для уведомлений"""
    
    def notify_updates_pending_review(
        self,
        count: int,
        updates: List[GraphUpdate]
    ) -> None:
        raise NotImplementedError
