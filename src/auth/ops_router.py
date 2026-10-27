"""LOGIC-B operations endpoints (new routes; the changed old ones stay in ``router.py``).

Mounted into the auth router (``router.include_router(ops_router)``), so the prefix is ``/api/v1/auth``.
Rights are in SPEC 4.10: lesson teacher, group archive, staff handover = owner/admin ("manager"); member removal /
restore = owner only.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.core.database import get_db

from .attendance_access import is_org_manager
from .dependencies import require_org_admin, require_org_owner
from .ops_schemas import (
    ArchiveGroupOut,
    ArchiveGroupRequest,
    ArchiveImpactOut,
    GroupResponseB,
    GroupRestoredB,
    HandoverOut,
    HandoverRequest,
    LessonTeacherCandidatesOut,
    LessonTeacherOut,
    LessonTeacherPut,
    RemovalImpactOut,
    RestoreGroupRequest,
    RestoreMemberOut,
)

router = APIRouter()


def _svc(db: Session):
    from .service import AuthService  # lazy: router.py imports this module while it is being imported itself

    return AuthService(db)


def _manager(user: dict = Depends(require_org_admin)) -> dict:
    """Owner/admin OF THIS centre (a global role or a superadmin without a membership does not count)."""
    if not user.get("org_id") or not is_org_manager(user):
        raise HTTPException(403, "Недостаточно прав. Требуется: owner, admin")
    return user


def _owner(user: dict = Depends(require_org_owner)) -> dict:
    if not user.get("org_id"):
        raise HTTPException(403, "Нет организации")
    return user


# ------------------------------------------------------------------------------------------ lesson teacher


@router.put("/org/groups/{group_id}/lesson-teacher", response_model=LessonTeacherOut, tags=["LOGIC-B lesson teacher"])
def put_lesson_teacher(
    group_id: str,
    request: LessonTeacherPut,
    user: dict = Depends(_manager),
    db: Session = Depends(get_db),
):
    """Назначить или снять замену преподавателя на один урок (owner/admin)."""
    from .lesson_teachers import LessonTeachers

    return LessonTeachers(_svc(db)).put(
        user["org_id"], group_id, request, actor_id=user["user_id"], actor_role=user["org_role"]
    )


@router.get(
    "/org/groups/{group_id}/lesson-teacher-candidates",
    response_model=LessonTeacherCandidatesOut,
    tags=["LOGIC-B lesson teacher"],
)
def get_lesson_teacher_candidates(
    group_id: str,
    lessonId: str = Query(..., min_length=1),
    lessonDate: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    user: dict = Depends(_manager),
    db: Session = Depends(get_db),
):
    """Кто может провести урок: преподаватели центра, занятые отмечены."""
    from datetime import date as _date

    from .lesson_teachers import LessonTeachers

    try:
        _date.fromisoformat(lessonDate)
    except ValueError:  # 2026-13-45 matches the pattern: a 422, not a 500
        raise HTTPException(422, "lessonDate: такой даты нет в календаре")
    return LessonTeachers(_svc(db)).candidates(user["org_id"], group_id, lessonId, lessonDate, is_owner=user.get("org_role") == "owner")


# --------------------------------------------------------------------------------------------- group archive


@router.get(
    "/org/groups/{group_id}/archive-impact",
    response_model=ArchiveImpactOut,
    response_model_exclude_none=True,
    tags=["LOGIC-B group archive"],
)
def get_group_archive_impact(
    group_id: str,
    user: dict = Depends(_manager),
    db: Session = Depends(get_db),
):
    """Что мешает архивации группы и что произойдёт автоматически."""
    from .group_lifecycle import GroupLifecycle

    return GroupLifecycle(_svc(db)).impact(user["org_id"], group_id)


@router.post("/org/groups/{group_id}/archive", response_model=ArchiveGroupOut, tags=["LOGIC-B group archive"])
def archive_group(
    group_id: str,
    request: ArchiveGroupRequest,
    user: dict = Depends(_manager),
    db: Session = Depends(get_db),
):
    """Архивировать группу (операция с предусловиями)."""
    from .group_lifecycle import GroupLifecycle

    return GroupLifecycle(_svc(db)).archive(
        user["org_id"], group_id, request, actor_id=user["user_id"], actor_role=user["org_role"]
    )


@router.post("/org/groups/{group_id}/restore", response_model=GroupRestoredB, tags=["LOGIC-B group archive"])
def restore_group(
    group_id: str,
    request: Optional[RestoreGroupRequest] = None,
    user: dict = Depends(_manager),
    db: Session = Depends(get_db),
):
    """Вернуть группу из архива (ученики не возвращаются)."""
    from .group_lifecycle import GroupLifecycle

    return GroupLifecycle(_svc(db)).restore(
        user["org_id"], group_id, request, actor_id=user["user_id"], actor_role=user["org_role"]
    )


# ------------------------------------------------------------------------------------------------- staff


@router.get("/org/members/{member_id}/removal-impact", response_model=RemovalImpactOut, tags=["LOGIC-B staff"])
def get_member_removal_impact(
    member_id: str,
    user: dict = Depends(_manager),
    db: Session = Depends(get_db),
):
    """Что мешает удалить сотрудника: группы, начатые уроки, замены, пропущенные уроки."""
    from .staff_lifecycle import StaffLifecycle

    return StaffLifecycle(_svc(db)).impact(user["org_id"], member_id)


@router.post("/org/members/{member_id}/handover", response_model=HandoverOut, tags=["LOGIC-B staff"])
def handover_member_groups(
    member_id: str,
    request: HandoverRequest,
    user: dict = Depends(_manager),
    db: Session = Depends(get_db),
):
    """Передать группы сотрудника другим преподавателям (атомарно)."""
    from .staff_lifecycle import StaffLifecycle

    return StaffLifecycle(_svc(db)).handover(
        user["org_id"], member_id, request, actor_id=user["user_id"], actor_role=user["org_role"]
    )


@router.post("/org/members/{member_id}/restore", response_model=RestoreMemberOut, tags=["LOGIC-B staff"])
def restore_member(
    member_id: str,
    user: dict = Depends(_owner),
    db: Session = Depends(get_db),
):
    """Вернуть удалённого сотрудника в команду (только owner); группы не возвращаются."""
    from .staff_lifecycle import StaffLifecycle

    return StaffLifecycle(_svc(db)).restore(user["org_id"], member_id, actor_id=user["user_id"])


# LOGIC-B2: the substitution requests (registered last: it imports ``_manager`` from this module)
from .substitution_router import router as _substitution_router  # noqa: E402

router.include_router(_substitution_router)
