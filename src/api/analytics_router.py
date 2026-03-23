"""
ALGO V1 - Analytics Router
REST API: Student progress, Group analytics, Enrollment, Live queue
~20 endpoints для owner/admin/teacher dashboards
"""

from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional

from src.core.dependencies import (
    require_org_owner, require_org_admin, require_org_teacher, require_org_member,
    require_superadmin,
)
from src.domain.analytics.student_progress import StudentProgressService
from src.domain.analytics.group_analytics import GroupAnalyticsService

from src.core.database import get_db

router = APIRouter(prefix="/analytics", tags=["Analytics"])


# ╔═══════════════════════════════════════════════════════════════════════╗
# ║  STUDENT PROGRESS — доступ: teacher и выше                           ║
# ╚═══════════════════════════════════════════════════════════════════════╝

@router.get("/students/{student_id}/skill-map")
def student_skill_map(
    student_id: str,
    user: dict = Depends(require_org_teacher),
    db: Session = Depends(get_db)
):
    """Карта навыков ученика — mastery по каждому навыку"""
    svc = StudentProgressService(db)
    return svc.get_skill_map(student_id, user["org_id"])


@router.get("/students/{student_id}/timeline")
def student_timeline(
    student_id: str,
    user: dict = Depends(require_org_teacher),
    db: Session = Depends(get_db)
):
    """Динамика θ ученика по времени"""
    svc = StudentProgressService(db)
    return svc.get_progress_timeline(student_id, user["org_id"])


@router.get("/students/{student_id}/weaknesses")
def student_weaknesses(
    student_id: str,
    user: dict = Depends(require_org_teacher),
    db: Session = Depends(get_db)
):
    """Отчёт о слабых навыках ученика"""
    svc = StudentProgressService(db)
    return svc.get_weakness_report(student_id, user["org_id"])


@router.get("/students/{student_id}/diagnostics")
def student_diagnostic_results(
    student_id: str,
    user: dict = Depends(require_org_teacher),
    db: Session = Depends(get_db)
):
    """Все результаты диагностик ученика"""
    svc = StudentProgressService(db)
    return svc.get_diagnostic_results(student_id, user["org_id"])


# ╔═══════════════════════════════════════════════════════════════════════╗
# ║  GROUP ANALYTICS — доступ: teacher и выше                            ║
# ╚═══════════════════════════════════════════════════════════════════════╝

@router.get("/groups/{group_id}/overview")
def group_overview(
    group_id: str,
    user: dict = Depends(require_org_teacher),
    db: Session = Depends(get_db)
):
    """Обзор группы: средний θ, распределение, список учеников"""
    svc = GroupAnalyticsService(db)
    return svc.get_group_overview(user["org_id"], group_id)


@router.get("/groups/{group_id}/heatmap")
def group_skill_heatmap(
    group_id: str,
    user: dict = Depends(require_org_teacher),
    db: Session = Depends(get_db)
):
    """Тепловая карта навыков группы (students × skills)"""
    svc = GroupAnalyticsService(db)
    return svc.get_group_skill_heatmap(user["org_id"], group_id)


@router.get("/groups/{group_id}/problem-skills")
def group_problem_skills(
    group_id: str,
    user: dict = Depends(require_org_teacher),
    db: Session = Depends(get_db)
):
    """Проблемные навыки группы — где больше всего gap/struggling"""
    svc = GroupAnalyticsService(db)
    return svc.get_problem_skills(user["org_id"], group_id)


# ╔═══════════════════════════════════════════════════════════════════════╗
# ║  ORG-LEVEL ANALYTICS — доступ: admin и выше                          ║
# ╚═══════════════════════════════════════════════════════════════════════╝

@router.get("/org/problem-skills")
def org_problem_skills(
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db)
):
    """Проблемные навыки по всей организации"""
    svc = GroupAnalyticsService(db)
    return svc.get_problem_skills(user["org_id"])


@router.get("/org/enrollment")
def org_enrollment(
    days: int = Query(30, ge=1, le=365),
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db)
):
    """
    Аналитика потока: новые ученики, диагностики за период.
    Для admin dashboard.
    """
    svc = GroupAnalyticsService(db)
    return svc.get_enrollment_analytics(user["org_id"], days)


@router.get("/org/live-queue")
def org_live_queue(
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db)
):
    """
    Живая очередь диагностик.
    Кто тестируется, ожидает, завершил за 24 часа.
    """
    svc = GroupAnalyticsService(db)
    return svc.get_live_queue(user["org_id"])


# ╔═══════════════════════════════════════════════════════════════════════╗
# ║  OWNER OVERVIEW — доступ: owner                                      ║
# ╚═══════════════════════════════════════════════════════════════════════╝

@router.get("/org/overview")
def org_overview(
    user: dict = Depends(require_org_owner),
    db: Session = Depends(get_db)
):
    """
    Обзор организации для владельца.
    Агрегация: группы, учителя, средний уровень, потенциальные проблемы.
    """
    from src.auth.service import AuthService
    from src.models.auth import Group, Student, OrganizationMember
    
    org_id = user["org_id"]
    auth_svc = AuthService(db)
    stats = auth_svc.get_org_stats(org_id)
    
    # Group summaries
    group_svc = GroupAnalyticsService(db)
    groups = db.query(Group).filter(Group.org_id == org_id, Group.status == "active").all()
    
    group_summaries = []
    for g in groups:
        overview = group_svc.get_group_overview(org_id, str(g.id))
        group_summaries.append({
            "group_id": str(g.id),
            "name": g.name,
            "class_level": g.class_level,
            "student_count": overview.get("student_count", 0),
            "avg_theta": overview.get("avg_theta"),
        })
    
    return {
        "org_id": org_id,
        **stats,
        "groups": group_summaries,
    }


# ╔═══════════════════════════════════════════════════════════════════════╗
# ║  GRAPH LEARNING — batch analysis (superadmin only)                    ║
# ╚═══════════════════════════════════════════════════════════════════════╝

@router.post("/graph/run-batch")
def run_graph_batch(
    days: int = Query(30, ge=1, le=365, description="Период анализа в днях"),
    auto_apply: bool = Query(False, description="Применить обновления без ревью"),
    user: dict = Depends(require_superadmin),
    db: Session = Depends(get_db),
):
    """
    Запуск batch-анализа графа знаний.
    Анализирует ответы за *days* дней, обнаруживает корреляции,
    обновляет / предлагает новые пререквизиты.
    """
    from src.domain.graph.pg_data_fetcher import PgDataFetcher
    from src.domain.graph.pg_graph_writer import PgGraphWriter
    from src.domain.graph.learning_updater import BatchGraphUpdater

    fetcher = PgDataFetcher(db)
    writer = PgGraphWriter(db)
    batch = BatchGraphUpdater(data_fetcher=fetcher, graph_writer=writer)

    since = datetime.now() - timedelta(days=days)
    report = batch.run_batch_analysis(since=since, auto_apply=auto_apply)
    return report
