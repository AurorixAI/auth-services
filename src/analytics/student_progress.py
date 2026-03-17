"""
ALGO V1 - Student Progress Service
Персональная аналитика ученика: skill map, timeline, weakness report
"""

from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, timezone
from uuid import UUID
import logging

from sqlalchemy.orm import Session
from sqlalchemy import func, desc

from src.auth.models import Student, StudentDiagnosticResult, DiagnosticInvite
from src.models import AnswerEvent, SessionSummary, StudentSkillState

logger = logging.getLogger(__name__)


class StudentProgressService:
    """Аналитика прогресса отдельного ученика"""
    
    def __init__(self, db: Session):
        self.db = db
    
    # ─────────────────────────────────────────
    # 1. SKILL MAP — карта навыков
    # ─────────────────────────────────────────
    
    def get_skill_map(self, student_id: str, org_id: str) -> Dict[str, Any]:
        """
        Полная карта навыков ученика.
        Для каждого навыка: mastery, status, trend, attempts.
        """
        states = (
            self.db.query(StudentSkillState)
            .filter(StudentSkillState.student_id == student_id)
            .all()
        )
        
        skills = []
        total_mastered = 0
        total_learning = 0
        total_gap = 0
        
        for s in states:
            skill_info = {
                "skill_id": s.skill_id,
                "mastery": float(s.mastery_probability) if s.mastery_probability else 0.0,
                "status": s.status or "unknown",
                "trend": s.trend or "stable",
                "total_attempts": s.total_attempts or 0,
                "correct_attempts": s.correct_attempts or 0,
                "accuracy": (
                    (s.correct_attempts / s.total_attempts * 100)
                    if s.total_attempts and s.total_attempts > 0
                    else 0
                ),
                "last_attempt": s.last_attempt_at.isoformat() if s.last_attempt_at else None,
            }
            skills.append(skill_info)
            
            if s.status == "mastered":
                total_mastered += 1
            elif s.status in ("learning", "struggling"):
                total_learning += 1
            elif s.status == "gap":
                total_gap += 1
        
        return {
            "student_id": student_id,
            "total_skills": len(skills),
            "mastered": total_mastered,
            "learning": total_learning,
            "gaps": total_gap,
            "mastery_rate": (total_mastered / len(skills) * 100) if skills else 0,
            "skills": sorted(skills, key=lambda x: x["mastery"]),
        }
    
    # ─────────────────────────────────────────
    # 2. PROGRESS TIMELINE — динамика θ
    # ─────────────────────────────────────────
    
    def get_progress_timeline(self, student_id: str, org_id: str) -> Dict[str, Any]:
        """
        Прогресс ученика по времени.
        Данные из diagnostic_results + session_summaries.
        """
        # Диагностические результаты
        diag_results = (
            self.db.query(StudentDiagnosticResult)
            .filter(StudentDiagnosticResult.student_id == student_id)
            .order_by(StudentDiagnosticResult.diagnosed_at)
            .all()
        )
        
        # Сессии
        sessions = (
            self.db.query(SessionSummary)
            .filter(SessionSummary.student_id == student_id)
            .order_by(SessionSummary.started_at)
            .all()
        )
        
        timeline = []
        
        for d in diag_results:
            timeline.append({
                "type": "diagnostic",
                "date": d.diagnosed_at.isoformat() if d.diagnosed_at else None,
                "theta": float(d.final_theta) if d.final_theta else None,
                "se": float(d.se) if d.se else None,
                "accuracy": float(d.accuracy) if d.accuracy else None,
                "total_items": d.total_tasks,
                "diagnostic_type": d.diagnostic_type,
                "recommended_level": d.recommended_level,
            })
        
        for s in sessions:
            timeline.append({
                "type": "session",
                "date": s.started_at.isoformat() if s.started_at else None,
                "theta": float(s.final_theta) if s.final_theta else None,
                "total_tasks": s.total_tasks,
                "correct": s.correct_answers,
                "accuracy": (
                    s.correct_answers / s.total_tasks * 100
                    if s.total_tasks and s.total_tasks > 0
                    else 0
                ),
            })
        
        # Sort by date
        timeline.sort(key=lambda x: x.get("date") or "")
        
        # Compute theta trend
        thetas = [t["theta"] for t in timeline if t.get("theta") is not None]
        trend = "stable"
        if len(thetas) >= 2:
            if thetas[-1] > thetas[0] + 0.3:
                trend = "improving"
            elif thetas[-1] < thetas[0] - 0.3:
                trend = "declining"
        
        return {
            "student_id": student_id,
            "timeline": timeline,
            "total_sessions": len(timeline),
            "latest_theta": thetas[-1] if thetas else None,
            "theta_trend": trend,
        }
    
    # ─────────────────────────────────────────
    # 3. WEAKNESS REPORT — слабые навыки
    # ─────────────────────────────────────────
    
    def get_weakness_report(self, student_id: str, org_id: str) -> Dict[str, Any]:
        """
        Отчёт о слабых местах:
        - gap skills (mastery < 0.3)
        - struggling skills (0.3 - 0.5)
        - frequent error types
        - root causes from diagnostic
        """
        # Gap/struggling skills
        weak_states = (
            self.db.query(StudentSkillState)
            .filter(
                StudentSkillState.student_id == student_id,
                StudentSkillState.status.in_(["gap", "struggling"])
            )
            .order_by(StudentSkillState.mastery_probability)
            .all()
        )
        
        gaps = []
        struggling = []
        for s in weak_states:
            info = {
                "skill_id": s.skill_id,
                "mastery": float(s.mastery_probability) if s.mastery_probability else 0,
                "attempts": s.total_attempts or 0,
                "accuracy": (
                    (s.correct_attempts / s.total_attempts * 100)
                    if s.total_attempts and s.total_attempts > 0
                    else 0
                ),
                "trend": s.trend or "stable",
            }
            if s.status == "gap":
                gaps.append(info)
            else:
                struggling.append(info)
        
        # Latest diagnostic root gaps
        latest_diag = (
            self.db.query(StudentDiagnosticResult)
            .filter(StudentDiagnosticResult.student_id == student_id)
            .order_by(desc(StudentDiagnosticResult.diagnosed_at))
            .first()
        )
        
        root_gaps = latest_diag.root_gaps if latest_diag and latest_diag.root_gaps else []
        identified_gaps = latest_diag.identified_gaps if latest_diag and latest_diag.identified_gaps else []
        error_summary = latest_diag.error_summary if latest_diag else None
        
        # Error frequency from answer_events (last 100 answers)
        recent_errors = (
            self.db.query(
                AnswerEvent.skill_id,
                func.count(AnswerEvent.id).label("error_count"),
            )
            .filter(
                AnswerEvent.student_id == student_id,
                AnswerEvent.is_correct == False,
            )
            .group_by(AnswerEvent.skill_id)
            .order_by(desc("error_count"))
            .limit(10)
            .all()
        )
        
        error_skills = []
        for row in recent_errors:
            error_skills.append({
                "skill_id": row[0],
                "error_count": row[1] or 0,
            })
        
        return {
            "student_id": student_id,
            "gaps": gaps,
            "struggling": struggling,
            "root_gaps": root_gaps,
            "identified_gaps": identified_gaps,
            "error_summary": error_summary,
            "top_error_skills": error_skills,
            "total_weak_skills": len(gaps) + len(struggling),
        }
    
    # ─────────────────────────────────────────
    # 4. DIAGNOSTIC RESULTS
    # ─────────────────────────────────────────
    
    def get_diagnostic_results(self, student_id: str, org_id: str) -> list:
        """Все результаты диагностик ученика"""
        results = (
            self.db.query(StudentDiagnosticResult)
            .filter(
                StudentDiagnosticResult.student_id == student_id,
                StudentDiagnosticResult.org_id == org_id,
            )
            .order_by(desc(StudentDiagnosticResult.diagnosed_at))
            .all()
        )
        
        return [
            {
                "id": str(r.id),
                "diagnostic_type": r.diagnostic_type,
                "final_theta": float(r.final_theta) if r.final_theta else None,
                "se": float(r.se) if r.se else None,
                "accuracy": float(r.accuracy) if r.accuracy else None,
                "total_items": r.total_tasks,
                "identified_gaps": r.identified_gaps,
                "root_gaps": r.root_gaps,
                "recommended_level": r.recommended_level,
                "integrity_report": r.integrity_report,
                "diagnosed_at": r.diagnosed_at.isoformat() if r.diagnosed_at else None,
            }
            for r in results
        ]
