"""
ALGO V1 - Database Repositories
Репозитории для работы с базой данных
"""

from typing import List, Optional, Dict, Tuple, Set
from datetime import datetime, timedelta
from uuid import UUID
import logging

from sqlalchemy import select, and_, or_, func, desc
from sqlalchemy.orm import Session, selectinload

from . import (
    KnowledgeHierarchy, SkillPrerequisite, SkillBenchmark, TaskMaster,
    Textbook, TextbookStructure, TextbookTask,
    AnswerEvent, SkillCorrelation, ErrorPattern, SessionSummary,
    StudentSkillState, GraphUpdateLog, IRTCalibrationHistory
)
from .dataclasses import TaskCandidate, SkillNode
from ..diagnostic.irt_engine import IRTTaskParams
from ..graph.learning_updater import AnswerEvent as AnswerEventDTO, GraphUpdate

logger = logging.getLogger(__name__)


class KnowledgeRepository:
    """Репозиторий для работы с иерархией знаний"""
    
    def __init__(self, session: Session):
        self.session = session
    
    def get_skill_by_id(self, skill_id: str) -> Optional[KnowledgeHierarchy]:
        """Получить навык по ID"""
        return self.session.get(KnowledgeHierarchy, skill_id)
    
    def get_skills_by_level(self, level: str) -> List[KnowledgeHierarchy]:
        """Получить все навыки уровня"""
        stmt = select(KnowledgeHierarchy).where(
            KnowledgeHierarchy.level == level,
            KnowledgeHierarchy.is_active == True
        ).order_by(KnowledgeHierarchy.sequence_order)
        return list(self.session.scalars(stmt))
    
    def get_skills_by_class(self, class_level: int) -> List[KnowledgeHierarchy]:
        """Получить навыки для класса"""
        stmt = select(KnowledgeHierarchy).where(
            KnowledgeHierarchy.level == 'L4',
            KnowledgeHierarchy.class_level_start <= class_level,
            KnowledgeHierarchy.class_level_end >= class_level,
            KnowledgeHierarchy.is_active == True
        )
        return list(self.session.scalars(stmt))
    
    def get_children(self, parent_id: str) -> List[KnowledgeHierarchy]:
        """Получить дочерние узлы"""
        stmt = select(KnowledgeHierarchy).where(
            KnowledgeHierarchy.parent_id == parent_id,
            KnowledgeHierarchy.is_active == True
        ).order_by(KnowledgeHierarchy.sequence_order)
        return list(self.session.scalars(stmt))
    
    def get_full_hierarchy(self) -> Dict[str, KnowledgeHierarchy]:
        """Получить всю иерархию"""
        stmt = select(KnowledgeHierarchy).where(
            KnowledgeHierarchy.is_active == True
        )
        skills = list(self.session.scalars(stmt))
        return {s.id: s for s in skills}
    
    def get_skill_graph(self) -> Dict[str, SkillNode]:
        """Получить граф навыков для диагностики"""
        # Загружаем навыки с пререквизитами
        stmt = select(KnowledgeHierarchy).options(
            selectinload(KnowledgeHierarchy.prerequisites)
        ).where(
            KnowledgeHierarchy.level == 'L4',
            KnowledgeHierarchy.is_active == True
        )
        skills = list(self.session.scalars(stmt))
        
        result = {}
        for skill in skills:
            prereqs = [p.prerequisite_id for p in skill.prerequisites]
            prereq_weights = {
                p.prerequisite_id: float(p.weight or 1.0) 
                for p in skill.prerequisites
            }
            
            result[skill.id] = SkillNode(
                skill_id=skill.id,
                name=skill.name,
                name_ru=skill.name_ru,
                level=skill.level,
                prerequisites=prereqs,
                prerequisite_weights=prereq_weights
            )
        
        return result
    
    def search_skills(
        self,
        query: str,
        level: Optional[str] = None,
        limit: int = 20
    ) -> List[KnowledgeHierarchy]:
        """Поиск навыков по тексту"""
        stmt = select(KnowledgeHierarchy).where(
            or_(
                KnowledgeHierarchy.name.ilike(f"%{query}%"),
                KnowledgeHierarchy.name_ru.ilike(f"%{query}%"),
                KnowledgeHierarchy.id.ilike(f"%{query}%")
            ),
            KnowledgeHierarchy.is_active == True
        )
        
        if level:
            stmt = stmt.where(KnowledgeHierarchy.level == level)
        
        stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt))


class PrerequisiteRepository:
    """Репозиторий для работы с зависимостями навыков"""
    
    def __init__(self, session: Session):
        self.session = session
    
    def get_prerequisites(self, skill_id: str) -> List[SkillPrerequisite]:
        """Получить пререквизиты навыка"""
        stmt = select(SkillPrerequisite).where(
            SkillPrerequisite.skill_id == skill_id
        )
        return list(self.session.scalars(stmt))
    
    def get_dependents(self, skill_id: str) -> List[SkillPrerequisite]:
        """Получить навыки, зависящие от данного"""
        stmt = select(SkillPrerequisite).where(
            SkillPrerequisite.prerequisite_id == skill_id
        )
        return list(self.session.scalars(stmt))
    
    def get_all_prerequisites(self) -> Dict[Tuple[str, str], float]:
        """Получить все пререквизиты как словарь"""
        stmt = select(SkillPrerequisite)
        prereqs = list(self.session.scalars(stmt))
        
        return {
            (p.prerequisite_id, p.skill_id): float(p.weight or 1.0)
            for p in prereqs
        }
    
    def add_prerequisite(
        self,
        skill_id: str,
        prerequisite_id: str,
        dependency_type: str = 'soft',
        weight: float = 1.0,
        discovery_source: str = 'expert'
    ) -> SkillPrerequisite:
        """Добавить новый пререквизит"""
        prereq = SkillPrerequisite(
            skill_id=skill_id,
            prerequisite_id=prerequisite_id,
            dependency_type=dependency_type,
            weight=weight,
            discovery_source=discovery_source
        )
        self.session.add(prereq)
        self.session.flush()
        return prereq
    
    def update_weight(
        self,
        skill_id: str,
        prerequisite_id: str,
        new_weight: float,
        learned_weight: Optional[float] = None,
        sample_size: Optional[int] = None,
        confidence: Optional[float] = None
    ) -> bool:
        """Обновить вес связи"""
        stmt = select(SkillPrerequisite).where(
            SkillPrerequisite.skill_id == skill_id,
            SkillPrerequisite.prerequisite_id == prerequisite_id
        )
        prereq = self.session.scalar(stmt)
        
        if not prereq:
            return False
        
        prereq.weight = new_weight
        if learned_weight is not None:
            prereq.learned_weight = learned_weight
        if sample_size is not None:
            prereq.sample_size = sample_size
        if confidence is not None:
            prereq.confidence = confidence
        prereq.last_validated_at = datetime.now()
        
        return True


class TaskRepository:
    """Репозиторий для работы с задачами"""
    
    def __init__(self, session: Session):
        self.session = session
    
    def get_task_by_id(self, task_id: str) -> Optional[TaskMaster]:
        """Получить задачу по ID"""
        return self.session.get(TaskMaster, task_id)
    
    def get_task_candidate_by_id(self, task_id: str) -> Optional[TaskCandidate]:
        """Получить задачу как TaskCandidate для диагностики"""
        task = self.get_task_by_id(task_id)
        if not task:
            return None
        
        return self._to_candidate(task)
    
    def get_tasks_for_skill(
        self,
        skill_id: str,
        difficulty: Optional[str] = None,
        limit: int = 10,
        exclude_ids: Optional[Set[str]] = None
    ) -> List[TaskMaster]:
        """Получить задачи для навыка"""
        stmt = select(TaskMaster).where(
            TaskMaster.skill_id == skill_id,
            TaskMaster.is_active == True,
            TaskMaster.verification_status == 'verified'
        )
        
        if difficulty:
            stmt = stmt.where(TaskMaster.difficulty == difficulty)
        
        if exclude_ids:
            stmt = stmt.where(~TaskMaster.id.in_(exclude_ids))
        
        stmt = stmt.order_by(TaskMaster.times_used).limit(limit)
        return list(self.session.scalars(stmt))
    
    def get_task_for_skill(
        self,
        skill_id: str,
        difficulty: str = "B",
        exclude_ids: Optional[Set[str]] = None
    ) -> Optional[TaskCandidate]:
        """Получить одну задачу для навыка (для диагностики)"""
        tasks = self.get_tasks_for_skill(
            skill_id=skill_id,
            difficulty=difficulty,
            limit=1,
            exclude_ids=exclude_ids
        )
        
        if not tasks:
            # Пробуем другие сложности
            tasks = self.get_tasks_for_skill(
                skill_id=skill_id,
                difficulty=None,
                limit=1,
                exclude_ids=exclude_ids
            )
        
        return self._to_candidate(tasks[0]) if tasks else None
    
    def get_all_active_tasks(
        self,
        exclude_ids: Optional[Set[str]] = None,
        limit: int = 500
    ) -> List[TaskMaster]:
        """Получить все активные верифицированные задачи (TaskMaster)"""
        stmt = select(TaskMaster).where(
            TaskMaster.is_active == True,
            TaskMaster.verification_status == 'verified'
        )
        
        if exclude_ids:
            stmt = stmt.where(~TaskMaster.id.in_(exclude_ids))
        
        stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt))
    
    def get_available_tasks(
        self,
        exclude_ids: Optional[Set[str]] = None,
        limit: int = 100
    ) -> List[TaskCandidate]:
        """Получить доступные задачи"""
        stmt = select(TaskMaster).where(
            TaskMaster.is_active == True,
            TaskMaster.verification_status == 'verified'
        )
        
        if exclude_ids:
            stmt = stmt.where(~TaskMaster.id.in_(exclude_ids))
        
        stmt = stmt.limit(limit)
        tasks = list(self.session.scalars(stmt))
        
        return [self._to_candidate(t) for t in tasks]
    
    def _to_candidate(self, task: TaskMaster) -> TaskCandidate:
        """Преобразовать TaskMaster в TaskCandidate"""
        irt_params = IRTTaskParams(
            task_id=task.id,
            skill_id=task.skill_id,
            discrimination=float(task.irt_discrimination or 1.0),
            difficulty=float(task.irt_difficulty or 0.0),
            guessing=float(task.irt_guessing or 0.0),
            calibrated=task.irt_calibrated or False,
            sample_size=task.irt_sample_size or 0
        )
        
        return TaskCandidate(
            task_id=task.id,
            skill_id=task.skill_id,
            difficulty=task.difficulty_level or 'B',
            irt_params=irt_params
        )
    
    def update_irt_params(
        self,
        task_id: str,
        discrimination: float,
        difficulty: float,
        guessing: float,
        sample_size: int
    ) -> bool:
        """Обновить IRT параметры задачи"""
        task = self.get_task_by_id(task_id)
        if not task:
            return False
        
        # Сохраняем историю
        history = IRTCalibrationHistory(
            task_id=task_id,
            old_discrimination=task.irt_discrimination,
            old_difficulty=task.irt_difficulty,
            old_guessing=task.irt_guessing,
            new_discrimination=discrimination,
            new_difficulty=difficulty,
            new_guessing=guessing,
            sample_size=sample_size
        )
        self.session.add(history)
        
        # Обновляем задачу
        task.irt_discrimination = discrimination
        task.irt_difficulty = difficulty
        task.irt_guessing = guessing
        task.irt_calibrated = True
        task.irt_sample_size = sample_size
        
        return True
    
    def increment_usage(self, task_id: str, is_correct: bool) -> None:
        """Увеличить счётчик использования"""
        task = self.get_task_by_id(task_id)
        if task:
            task.times_used = (task.times_used or 0) + 1
            if is_correct:
                task.times_correct = (task.times_correct or 0) + 1


class AnswerEventRepository:
    """Репозиторий для работы с событиями ответов"""
    
    def __init__(self, session: Session):
        self.session = session
    
    def save_event(
        self,
        session_id: UUID,
        task_id: str,
        skill_id: str,
        is_correct: bool,
        time_spent_ms: int,
        started_at: datetime,
        student_id: Optional[UUID] = None,
        student_answer: Optional[str] = None,
        correct_answer: Optional[str] = None,
        difficulty_level: Optional[str] = None,
        task_order: Optional[int] = None,
        theta_before: Optional[float] = None,
        theta_after: Optional[float] = None,
        irt_params: Optional[IRTTaskParams] = None,
        error_type: Optional[str] = None,
        error_explanation: Optional[str] = None,
        matched_distractor: Optional[str] = None,
    ) -> AnswerEvent:
        """Сохранить событие ответа"""
        event = AnswerEvent(
            session_id=session_id,
            student_id=student_id,
            task_id=task_id,
            skill_id=skill_id,
            is_correct=is_correct,
            student_answer=student_answer,
            correct_answer=correct_answer,
            time_spent_ms=time_spent_ms,
            started_at=started_at,
            difficulty_level=difficulty_level,
            task_order_in_session=task_order,
            student_theta_before=theta_before,
            student_theta_after=theta_after,
            error_type=error_type,
            error_explanation=error_explanation,
            matched_distractor=matched_distractor,
        )
        
        if irt_params:
            event.task_irt_a = irt_params.discrimination
            event.task_irt_b = irt_params.difficulty
            event.task_irt_c = irt_params.guessing
        
        self.session.add(event)
        self.session.flush()
        return event
    
    def get_events_for_period(
        self,
        since: datetime,
        until: datetime,
        offset: int = 0,
        limit: int = 10000
    ) -> List[AnswerEventDTO]:
        """Получить события за период"""
        stmt = select(AnswerEvent).where(
            AnswerEvent.submitted_at >= since,
            AnswerEvent.submitted_at < until
        ).order_by(AnswerEvent.submitted_at).offset(offset).limit(limit)
        
        events = list(self.session.scalars(stmt))
        
        return [
            AnswerEventDTO(
                session_id=str(e.session_id),
                student_id=str(e.student_id) if e.student_id else None,
                skill_id=e.skill_id,
                task_id=e.task_id,
                is_correct=e.is_correct,
                time_ms=e.time_spent_ms,
                submitted_at=e.submitted_at,
                difficulty=e.difficulty_level or 'B',
                theta_before=float(e.student_theta_before) if e.student_theta_before else None,
                theta_after=float(e.student_theta_after) if e.student_theta_after else None
            )
            for e in events
        ]
    
    def get_events_for_session(self, session_id: UUID) -> List[AnswerEvent]:
        """Получить все события сессии"""
        stmt = select(AnswerEvent).where(
            AnswerEvent.session_id == session_id
        ).order_by(AnswerEvent.submitted_at)
        return list(self.session.scalars(stmt))
    
    def get_events_for_student(
        self,
        student_id: UUID,
        limit: int = 100
    ) -> List[AnswerEvent]:
        """Получить последние события ученика"""
        stmt = select(AnswerEvent).where(
            AnswerEvent.student_id == student_id
        ).order_by(desc(AnswerEvent.submitted_at)).limit(limit)
        return list(self.session.scalars(stmt))


class SessionRepository:
    """Репозиторий для работы с сессиями"""
    
    def __init__(self, session: Session):
        self.session = session
    
    def save_summary(
        self,
        session_id: UUID,
        started_at: datetime,
        completed_at: Optional[datetime] = None,
        student_id: Optional[UUID] = None,
        total_tasks: int = 0,
        correct_answers: int = 0,
        final_theta: Optional[float] = None,
        ci_low: Optional[float] = None,
        ci_high: Optional[float] = None,
        identified_gaps: Optional[List[str]] = None,
        root_gaps: Optional[List[str]] = None,
        recommended_skills: Optional[List[str]] = None,
        priority_order: Optional[List[str]] = None,
        session_type: str = 'diagnostic'
    ) -> SessionSummary:
        """Сохранить сводку сессии"""
        summary = SessionSummary(
            session_id=session_id,
            student_id=student_id,
            started_at=started_at,
            completed_at=completed_at,
            total_duration_ms=int((completed_at - started_at).total_seconds() * 1000) if completed_at else None,
            total_tasks=total_tasks,
            correct_answers=correct_answers,
            final_theta=final_theta,
            confidence_interval_low=ci_low,
            confidence_interval_high=ci_high,
            identified_gaps=identified_gaps or [],
            root_gaps=root_gaps or [],
            recommended_skills=recommended_skills or [],
            priority_order=priority_order or [],
            session_type=session_type
        )
        self.session.add(summary)
        self.session.flush()
        return summary
    
    def get_summary(self, session_id: UUID) -> Optional[SessionSummary]:
        """Получить сводку по ID"""
        return self.session.get(SessionSummary, session_id)
    
    def get_student_sessions(
        self,
        student_id: UUID,
        limit: int = 10
    ) -> List[SessionSummary]:
        """Получить сессии ученика"""
        stmt = select(SessionSummary).where(
            SessionSummary.student_id == student_id
        ).order_by(desc(SessionSummary.started_at)).limit(limit)
        return list(self.session.scalars(stmt))


class StudentSkillRepository:
    """Репозиторий для работы с состоянием навыков ученика"""
    
    def __init__(self, session: Session):
        self.session = session
    
    def get_skill_state(
        self,
        student_id: UUID,
        skill_id: str
    ) -> Optional[StudentSkillState]:
        """Получить состояние навыка ученика"""
        stmt = select(StudentSkillState).where(
            StudentSkillState.student_id == student_id,
            StudentSkillState.skill_id == skill_id
        )
        return self.session.scalar(stmt)
    
    def get_all_states(self, student_id: UUID) -> List[StudentSkillState]:
        """Получить все состояния навыков ученика"""
        stmt = select(StudentSkillState).where(
            StudentSkillState.student_id == student_id
        )
        return list(self.session.scalars(stmt))
    
    def upsert_state(
        self,
        student_id: UUID,
        skill_id: str,
        mastery_probability: float,
        theta: Optional[float] = None,
        theta_se: Optional[float] = None,
        is_correct: Optional[bool] = None
    ) -> StudentSkillState:
        """Обновить или создать состояние навыка"""
        state = self.get_skill_state(student_id, skill_id)
        
        if not state:
            state = StudentSkillState(
                student_id=student_id,
                skill_id=skill_id,
                mastery_probability=mastery_probability
            )
            self.session.add(state)
        else:
            state.mastery_probability = mastery_probability
        
        if theta is not None:
            state.theta = theta
        if theta_se is not None:
            state.theta_se = theta_se
        
        # Обновляем статистику если есть ответ
        if is_correct is not None:
            state.total_attempts = (state.total_attempts or 0) + 1
            state.last_attempt_at = datetime.now()
            
            if is_correct:
                state.correct_attempts = (state.correct_attempts or 0) + 1
                state.consecutive_correct = (state.consecutive_correct or 0) + 1
                state.consecutive_incorrect = 0
                state.last_correct_at = datetime.now()
            else:
                state.consecutive_incorrect = (state.consecutive_incorrect or 0) + 1
                state.consecutive_correct = 0
        
        # Определяем статус
        if mastery_probability >= 0.9:
            state.status = 'mastered'
        elif mastery_probability >= 0.5:
            state.status = 'learning'
        elif mastery_probability >= 0.3:
            state.status = 'struggling'
        else:
            state.status = 'gap'
        
        # Определяем тренд
        if state.consecutive_correct and state.consecutive_correct >= 3:
            state.trend = 'improving'
        elif state.consecutive_incorrect and state.consecutive_incorrect >= 3:
            state.trend = 'declining'
        else:
            state.trend = 'stable'
        
        self.session.flush()
        return state


class CorrelationRepository:
    """Репозиторий для работы с корреляциями"""
    
    def __init__(self, session: Session):
        self.session = session
    
    def save_correlation(
        self,
        skill_a_id: str,
        skill_b_id: str,
        correlation: float,
        p_value: float,
        sample_size: int,
        relationship_type: str,
        direction: str,
        strength: float,
        confidence: float,
        discovery_method: str
    ) -> SkillCorrelation:
        """Сохранить корреляцию"""
        corr = SkillCorrelation(
            skill_a_id=skill_a_id,
            skill_b_id=skill_b_id,
            correlation_coefficient=correlation,
            p_value=p_value,
            sample_size=sample_size,
            relationship_type=relationship_type,
            direction=direction,
            strength=strength,
            confidence=confidence,
            discovery_method=discovery_method
        )
        self.session.add(corr)
        self.session.flush()
        return corr
    
    def get_correlations_for_skill(
        self,
        skill_id: str,
        min_strength: float = 0
    ) -> List[SkillCorrelation]:
        """Получить корреляции для навыка"""
        stmt = select(SkillCorrelation).where(
            or_(
                SkillCorrelation.skill_a_id == skill_id,
                SkillCorrelation.skill_b_id == skill_id
            ),
            SkillCorrelation.strength >= min_strength
        ).order_by(desc(SkillCorrelation.strength))
        return list(self.session.scalars(stmt))
    
    def get_strong_correlations(
        self,
        min_correlation: float = 0.5,
        limit: int = 100
    ) -> List[SkillCorrelation]:
        """Получить сильные корреляции"""
        stmt = select(SkillCorrelation).where(
            func.abs(SkillCorrelation.correlation_coefficient) >= min_correlation
        ).order_by(
            desc(func.abs(SkillCorrelation.correlation_coefficient))
        ).limit(limit)
        return list(self.session.scalars(stmt))


class GraphUpdateRepository:
    """Репозиторий для работы с логом обновлений графа"""
    
    def __init__(self, session: Session):
        self.session = session
    
    def log_update(self, update: GraphUpdate) -> GraphUpdateLog:
        """Записать обновление в лог"""
        log_entry = GraphUpdateLog(
            update_type=update.update_type,
            entity_type=update.entity_type,
            entity_id=update.entity_id,
            old_value=update.old_value,
            new_value=update.new_value,
            triggered_by=update.triggered_by.value,
            requires_review=update.requires_review,
            review_status='pending' if update.requires_review else None
        )
        self.session.add(log_entry)
        self.session.flush()
        return log_entry
    
    def get_pending_reviews(self, limit: int = 50) -> List[GraphUpdateLog]:
        """Получить обновления, ожидающие ревью"""
        stmt = select(GraphUpdateLog).where(
            GraphUpdateLog.requires_review == True,
            GraphUpdateLog.review_status == 'pending'
        ).order_by(GraphUpdateLog.created_at).limit(limit)
        return list(self.session.scalars(stmt))
    
    def approve_update(
        self,
        update_id: int,
        reviewed_by: str
    ) -> bool:
        """Одобрить обновление"""
        log_entry = self.session.get(GraphUpdateLog, update_id)
        if not log_entry:
            return False
        
        log_entry.review_status = 'approved'
        log_entry.reviewed_by = reviewed_by
        log_entry.reviewed_at = datetime.now()
        return True
    
    def reject_update(
        self,
        update_id: int,
        reviewed_by: str,
        reason: Optional[str] = None
    ) -> bool:
        """Отклонить обновление"""
        log_entry = self.session.get(GraphUpdateLog, update_id)
        if not log_entry:
            return False
        
        log_entry.review_status = 'rejected'
        log_entry.reviewed_by = reviewed_by
        log_entry.reviewed_at = datetime.now()
        if reason:
            log_entry.change_reason = reason
        return True
