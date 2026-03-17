"""
ALGO V1 - Group Analytics Service
Групповая аналитика: overview, skill heatmap, problem skills, enrollment
"""

from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, timezone
import logging

from sqlalchemy.orm import Session
from sqlalchemy import func, desc, case, and_

from src.auth.models import (
    Organization, Student, Group, StudentGroup,
    DiagnosticInvite, StudentDiagnosticResult,
)
from src.models import AnswerEvent, SessionSummary, StudentSkillState

logger = logging.getLogger(__name__)


class GroupAnalyticsService:
    """Аналитика на уровне групп и организации"""
    
    def __init__(self, db: Session):
        self.db = db
    
    # ═══════════════════════════════════════════
    # 1. GROUP OVERVIEW
    # ═══════════════════════════════════════════
    
    def get_group_overview(self, org_id: str, group_id: str) -> Dict[str, Any]:
        """
        Обзор группы: кол-во учеников, средний θ, распределение mastery.
        """
        group = self.db.query(Group).filter(Group.id == group_id, Group.org_id == org_id).first()
        if not group:
            return {"error": "Группа не найдена"}
        
        # Student IDs in group
        student_ids = [
            sg.student_id for sg in
            self.db.query(StudentGroup)
            .filter(StudentGroup.group_id == group_id, StudentGroup.is_active == True)
            .all()
        ]
        
        if not student_ids:
            return {
                "group_id": str(group.id),
                "group_name": group.name,
                "student_count": 0,
                "avg_theta": None,
                "mastery_distribution": {},
                "students": [],
            }
        
        # Get latest diagnostic results per student
        student_data = []
        thetas = []
        status_counts = {"mastered": 0, "learning": 0, "struggling": 0, "gap": 0, "no_data": 0}
        
        for sid in student_ids:
            student = self.db.query(Student).filter(Student.id == sid).first()
            if not student:
                continue
            
            # Latest diagnostic
            latest = (
                self.db.query(StudentDiagnosticResult)
                .filter(StudentDiagnosticResult.student_id == sid)
                .order_by(desc(StudentDiagnosticResult.diagnosed_at))
                .first()
            )
            
            theta = float(latest.final_theta) if latest and latest.final_theta else None
            if theta is not None:
                thetas.append(theta)
            
            # Classify student level by theta
            level = "no_data"
            if theta is not None:
                if theta >= 1.5:
                    level = "mastered"
                elif theta >= 0.0:
                    level = "learning"
                elif theta >= -1.0:
                    level = "struggling"
                else:
                    level = "gap"
            status_counts[level] += 1
            
            student_data.append({
                "student_id": str(sid),
                "name": f"{student.first_name} {student.last_name}",
                "class_level": student.class_level,
                "status": student.status,
                "theta": theta,
                "level": level,
                "accuracy": float(latest.accuracy) if latest and latest.accuracy else None,
            })
        
        avg_theta = sum(thetas) / len(thetas) if thetas else None
        
        return {
            "group_id": str(group.id),
            "group_name": group.name,
            "class_level": group.class_level,
            "student_count": len(student_ids),
            "avg_theta": round(avg_theta, 3) if avg_theta is not None else None,
            "mastery_distribution": status_counts,
            "students": sorted(student_data, key=lambda x: x.get("theta") or -99),
        }
    
    # ═══════════════════════════════════════════
    # 2. GROUP SKILL HEATMAP
    # ═══════════════════════════════════════════
    
    def get_group_skill_heatmap(self, org_id: str, group_id: str) -> Dict[str, Any]:
        """
        Тепловая карта навыков группы.
        Rows = students, Columns = skills, Values = mastery_probability
        """
        student_ids = [
            sg.student_id for sg in
            self.db.query(StudentGroup)
            .filter(StudentGroup.group_id == group_id, StudentGroup.is_active == True)
            .all()
        ]
        
        if not student_ids:
            return {"group_id": group_id, "heatmap": [], "skills": [], "students": []}
        
        # Get all skill states for these students
        states = (
            self.db.query(StudentSkillState)
            .filter(StudentSkillState.student_id.in_(student_ids))
            .all()
        )
        
        # Collect all unique skills
        all_skills = sorted(set(s.skill_id for s in states))
        
        # Build matrix
        student_names = {}
        for sid in student_ids:
            student = self.db.query(Student).filter(Student.id == sid).first()
            if student:
                student_names[str(sid)] = f"{student.first_name} {student.last_name}"
        
        heatmap = []
        for sid in student_ids:
            sid_str = str(sid)
            row = {
                "student_id": sid_str,
                "student_name": student_names.get(sid_str, "—"),
                "skills": {},
            }
            for s in states:
                if str(s.student_id) == sid_str:
                    row["skills"][s.skill_id] = {
                        "mastery": float(s.mastery_probability) if s.mastery_probability else 0,
                        "status": s.status or "unknown",
                    }
            heatmap.append(row)
        
        # Aggregate per-skill averages
        skill_averages = {}
        for skill_id in all_skills:
            values = [
                float(s.mastery_probability)
                for s in states
                if s.skill_id == skill_id and s.mastery_probability is not None
            ]
            skill_averages[skill_id] = round(sum(values) / len(values), 3) if values else 0
        
        return {
            "group_id": group_id,
            "skills": all_skills,
            "skill_averages": skill_averages,
            "students": list(student_names.values()),
            "heatmap": heatmap,
        }
    
    # ═══════════════════════════════════════════
    # 3. PROBLEM SKILLS — навыки с наибольшими проблемами
    # ═══════════════════════════════════════════
    
    def get_problem_skills(self, org_id: str, group_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Навыки, где больше всего учеников имеют gap/struggling.
        Если group_id = None — по всей организации.
        """
        if group_id:
            student_ids = [
                sg.student_id for sg in
                self.db.query(StudentGroup)
                .filter(StudentGroup.group_id == group_id, StudentGroup.is_active == True)
                .all()
            ]
        else:
            student_ids = [
                s.id for s in
                self.db.query(Student.id)
                .filter(Student.org_id == org_id)
                .all()
            ]
        
        if not student_ids:
            return {"skills": []}
        
        # Count gap/struggling per skill
        problem_query = (
            self.db.query(
                StudentSkillState.skill_id,
                func.count(StudentSkillState.id).label("total_students"),
                func.sum(
                    case((StudentSkillState.status == "gap", 1), else_=0)
                ).label("gap_count"),
                func.sum(
                    case((StudentSkillState.status == "struggling", 1), else_=0)
                ).label("struggling_count"),
                func.avg(StudentSkillState.mastery_probability).label("avg_mastery"),
            )
            .filter(StudentSkillState.student_id.in_(student_ids))
            .group_by(StudentSkillState.skill_id)
            .having(
                func.sum(
                    case(
                        (StudentSkillState.status.in_(["gap", "struggling"]), 1),
                        else_=0
                    )
                ) > 0
            )
            .order_by(desc("gap_count"))
            .limit(20)
            .all()
        )
        
        skills = []
        for row in problem_query:
            skills.append({
                "skill_id": row[0],
                "total_students": row[1] or 0,
                "gap_count": row[2] or 0,
                "struggling_count": row[3] or 0,
                "avg_mastery": round(float(row[4]), 3) if row[4] else 0,
                "problem_rate": round(
                    ((row[2] or 0) + (row[3] or 0)) / (row[1] or 1) * 100, 1
                ),
            })
        
        return {"group_id": group_id, "org_id": org_id, "problem_skills": skills}
    
    # ═══════════════════════════════════════════
    # 4. ENROLLMENT ANALYTICS — поток учеников
    # ═══════════════════════════════════════════
    
    def get_enrollment_analytics(self, org_id: str, days: int = 30) -> Dict[str, Any]:
        """
        Аналитика потока: новые ученики, диагностики, группы за период.
        Для admin dashboard.
        """
        since = datetime.now(timezone.utc) - timedelta(days=days)
        
        # New students per day
        new_students_query = (
            self.db.query(
                func.date(Student.created_at).label("day"),
                func.count(Student.id).label("count"),
            )
            .filter(Student.org_id == org_id, Student.created_at >= since)
            .group_by(func.date(Student.created_at))
            .order_by("day")
            .all()
        )
        
        # Diagnostics per day
        diagnostics_query = (
            self.db.query(
                func.date(DiagnosticInvite.created_at).label("day"),
                func.count(DiagnosticInvite.id).label("total"),
                func.sum(
                    case((DiagnosticInvite.status == "completed", 1), else_=0)
                ).label("completed"),
            )
            .filter(DiagnosticInvite.org_id == org_id, DiagnosticInvite.created_at >= since)
            .group_by(func.date(DiagnosticInvite.created_at))
            .order_by("day")
            .all()
        )
        
        # Student status breakdown
        status_breakdown = (
            self.db.query(Student.status, func.count(Student.id))
            .filter(Student.org_id == org_id)
            .group_by(Student.status)
            .all()
        )
        
        # Class level distribution
        class_dist = (
            self.db.query(Student.class_level, func.count(Student.id))
            .filter(Student.org_id == org_id)
            .group_by(Student.class_level)
            .order_by(Student.class_level)
            .all()
        )
        
        return {
            "org_id": org_id,
            "period_days": days,
            "new_students_daily": [
                {"day": str(row[0]), "count": row[1]} for row in new_students_query
            ],
            "diagnostics_daily": [
                {"day": str(row[0]), "total": row[1], "completed": row[2] or 0}
                for row in diagnostics_query
            ],
            "student_status_breakdown": {
                str(row[0]): row[1] for row in status_breakdown
            },
            "class_level_distribution": {
                str(row[0]): row[1] for row in class_dist
            },
            "total_students": sum(row[1] for row in status_breakdown),
        }
    
    # ═══════════════════════════════════════════
    # 5. LIVE QUEUE — текущие тестирования
    # ═══════════════════════════════════════════
    
    def get_live_queue(self, org_id: str) -> Dict[str, Any]:
        """
        Живая очередь: кто сейчас тестируется, ожидает, завершил.
        Для admin dashboard.
        """
        active_statuses = ["pending", "confirmed", "in_progress"]
        
        active = (
            self.db.query(DiagnosticInvite)
            .filter(
                DiagnosticInvite.org_id == org_id,
                DiagnosticInvite.status.in_(active_statuses),
            )
            .order_by(DiagnosticInvite.created_at.desc())
            .limit(50)
            .all()
        )
        
        recently_completed = (
            self.db.query(DiagnosticInvite)
            .filter(
                DiagnosticInvite.org_id == org_id,
                DiagnosticInvite.status == "completed",
                DiagnosticInvite.completed_at >= datetime.now(timezone.utc) - timedelta(hours=24),
            )
            .order_by(DiagnosticInvite.completed_at.desc())
            .limit(20)
            .all()
        )
        
        def _invite_to_dict(inv):
            student = self.db.query(Student).filter(Student.id == inv.student_id).first()
            # Get result if completed
            result = None
            if inv.status == "completed":
                result_obj = (
                    self.db.query(StudentDiagnosticResult)
                    .filter(StudentDiagnosticResult.invite_id == inv.id)
                    .first()
                )
                if result_obj:
                    result = {
                        "theta": float(result_obj.final_theta) if result_obj.final_theta else None,
                        "accuracy": float(result_obj.accuracy) if result_obj.accuracy else None,
                        "recommended_level": result_obj.recommended_level,
                    }
            
            return {
                "id": str(inv.id),
                "code": inv.code,
                "student_name": f"{student.first_name} {student.last_name}" if student else "—",
                "class_level": inv.class_level,
                "status": inv.status,
                "diagnostic_type": inv.diagnostic_type,
                "created_at": inv.created_at.isoformat() if inv.created_at else None,
                "started_at": inv.started_at.isoformat() if inv.started_at else None,
                "completed_at": inv.completed_at.isoformat() if inv.completed_at else None,
                "result": result,
            }
        
        return {
            "org_id": org_id,
            "active": [_invite_to_dict(i) for i in active],
            "active_count": len(active),
            "recently_completed": [_invite_to_dict(i) for i in recently_completed],
            "completed_today": len(recently_completed),
        }
