"""/api/v1/platform/orgs*: the centres - list with health metrics, card, block / unblock, limits."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.models.auth import Group, GroupAttendance, Organization, OrganizationMember, Student, StudentPayment, User
from src.models.platform import DemoRequest

from . import platform_audit as audit
from .platform_common import contains, iso, not_found, page_params, page_response, query_form, utcnow
from .platform_errors import PlatformError
from .platform_security import Principal, require_platform
from .platform_validation import Form, parse_uuid_or_404

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orgs")

STATUSES = ("active", "blocked", "pending", "rejected", "suspended")


def _count(model, *conditions):
    return select(func.count()).select_from(model).where(*conditions).correlate(Organization).scalar_subquery()


def _members_active_since(days: int):
    cutoff = utcnow() - timedelta(days=days)
    return (
        select(func.count()).select_from(OrganizationMember).join(User, User.id == OrganizationMember.user_id)
        .where(OrganizationMember.org_id == Organization.id, OrganizationMember.is_active.is_(True), User.last_login_at >= cutoff)
        .correlate(Organization).scalar_subquery()
    )


def metric_columns() -> dict:
    return {
        "students": _count(Student, Student.org_id == Organization.id, Student.status != "archived").label("students"),
        "teachers": _count(
            OrganizationMember, OrganizationMember.org_id == Organization.id, OrganizationMember.org_role == "teacher",
            OrganizationMember.is_active.is_(True),
        ).label("teachers"),
        "groups": _count(Group, Group.org_id == Organization.id, Group.status == "active").label("groups"),
        "active_users_7d": _members_active_since(7).label("active_users_7d"),
        "active_users_30d": _members_active_since(30).label("active_users_30d"),
        "last_login_at": (
            select(func.max(User.last_login_at)).select_from(OrganizationMember).join(User, User.id == OrganizationMember.user_id)
            .where(OrganizationMember.org_id == Organization.id).correlate(Organization).scalar_subquery()
        ).label("last_login_at"),
    }


def _state(org: Organization) -> str:
    return "blocked" if org.blocked_at is not None else org.status


def _row(org: Organization, owner: Optional[User], m: dict) -> dict:
    return {
        "id": str(org.id),
        "name": org.name,
        "city": org.city,
        "status": _state(org),
        "blocked_at": iso(org.blocked_at),
        "plan": None,
        "owner": {"id": str(owner.id), "name": f"{owner.first_name} {owner.last_name}".strip(), "email": owner.email} if owner else None,
        "students": m["students"],
        "teachers": m["teachers"],
        "groups": m["groups"],
        "active_users_7d": m["active_users_7d"],
        "active_users_30d": m["active_users_30d"],
        "last_login_at": iso(m["last_login_at"]),
        "max_students": org.max_students,
        "max_teachers": org.max_teachers,
        "created_at": iso(org.created_at),
    }


def _select(db: Session):
    cols = metric_columns()
    return db.query(Organization, User, *cols.values()).outerjoin(User, User.id == Organization.owner_id), cols


def _unpack(result, names) -> dict:
    org, owner, *values = result
    return _row(org, owner, dict(zip(names, values)))


def _load(db: Session, org_id) -> dict:
    query, cols = _select(db)
    found = query.filter(Organization.id == org_id).first()
    if found is None:
        raise not_found("Центр")
    return _unpack(found, list(cols))


def _get(db: Session, org_id_text: str, *, lock: bool = False) -> Organization:
    uid = parse_uuid_or_404(org_id_text, "Центр")
    q = db.query(Organization).filter(Organization.id == uid)
    if lock:
        q = q.with_for_update()
    org = q.first()
    if org is None:
        raise not_found("Центр")
    return org


@router.get("")
def list_orgs(request: Request, p: Principal = Depends(require_platform("orgs.view")), db: Session = Depends(get_db)):
    form = query_form(request)
    sorts = ("name", "created_at", "students", "teachers", "groups", "last_login_at", "active_users_7d")
    page = page_params(request, sorts, "created_at")
    q = form.text("q", "Поиск", required=False, max_len=100)
    status = form.choice("status", "Статус", STATUSES, required=False)
    city = form.text("city", "Город", required=False, max_len=100)
    form.done()

    query, cols = _select(db)
    names = list(cols)
    if q:
        like = contains(q)
        query = query.filter(
            or_(
                Organization.name.ilike(like, escape="\\"), Organization.city.ilike(like, escape="\\"),
                Organization.email.ilike(like, escape="\\"), Organization.phone.ilike(like, escape="\\"),
            )
        )
    if status == "blocked":
        query = query.filter(Organization.blocked_at.isnot(None))
    elif status == "active":
        query = query.filter(Organization.status == "active", Organization.blocked_at.is_(None))
    elif status:
        query = query.filter(Organization.status == status)
    if city:
        query = query.filter(Organization.city.ilike(contains(city), escape="\\"))
    total = query.count()
    if page.sort in ("name", "created_at"):
        key = func.lower(Organization.name) if page.sort == "name" else Organization.created_at
    else:
        key = cols[page.sort]
    ordered = key.asc() if page.order == "asc" else key.desc()
    nulls = ordered.nulls_last()
    results = query.order_by(nulls, Organization.id).offset(page.offset).limit(page.size).all()
    return page_response([_unpack(r, names) for r in results], total, page)


def _usage_30d(db: Session, org_id) -> dict:
    now = utcnow()
    since = now - timedelta(days=30)
    marks = (
        db.query(func.count(GroupAttendance.id))
        .filter(GroupAttendance.org_id == org_id, GroupAttendance.status == "completed", GroupAttendance.lesson_date >= since.date().isoformat())
        .scalar()
    )
    payments = (
        db.query(func.count(StudentPayment.id))
        .filter(StudentPayment.org_id == org_id, StudentPayment.kind == "payment", StudentPayment.voided_at.is_(None), StudentPayment.created_at >= since)
        .scalar()
    )
    return {"attendance_marks": marks or 0, "payments": payments or 0, "exams": None, "diagnostics": None}


@router.get("/{org_id}")
def get_org(org_id: str, p: Principal = Depends(require_platform("orgs.view")), db: Session = Depends(get_db)):
    org = _get(db, org_id)
    out = _load(db, org.id)
    members = (
        db.query(OrganizationMember, User)
        .join(User, User.id == OrganizationMember.user_id)
        .filter(OrganizationMember.org_id == org.id)
        .order_by(OrganizationMember.org_role, User.last_name)
        .all()
    )
    source = db.query(DemoRequest).filter(DemoRequest.org_id == org.id).order_by(DemoRequest.created_at.desc()).first()
    out.update(
        {
            "org_type": org.org_type,
            "request": None if source is None else {
                "id": str(source.id), "created_at": iso(source.created_at), "needs": list(source.needs or []), "current_tool": source.current_tool,
                "heard_from": source.heard_from, "branches_count": source.branches_count, "position": source.position,
                "preferred_contact_time": source.preferred_contact_time,
            },
            "phone": org.phone,
            "email": org.email,
            "address": org.address,
            "blocked_reason": org.blocked_reason,
            "members": [
                {
                    "user_id": str(u.id), "name": f"{u.first_name} {u.last_name}".strip(), "email": u.email, "org_role": m.org_role,
                    "is_active": bool(m.is_active and u.is_active), "last_login_at": iso(u.last_login_at),
                }
                for m, u in members
            ],
            "usage_30d": _usage_30d(db, org.id),
            "history": audit.history(db, "org", org.id),
        }
    )
    return out


@router.post("/{org_id}/block")
def block_org(
    org_id: str, request: Request, body: dict = Body(default=None),
    p: Principal = Depends(require_platform("orgs.block")), db: Session = Depends(get_db),
):
    form = Form(body)
    reason = form.text("reason", "Причина", min_len=3, max_len=500, multiline=True)
    form.done()
    org = _get(db, org_id, lock=True)
    if org.blocked_at is not None:
        raise PlatformError(409, "Центр уже заблокирован", "already_blocked")
    org.blocked_at = utcnow()
    org.blocked_reason = reason
    audit.record(db, actor_id=p.user.id, action="org.blocked", target_type="org", target_id=org.id, reason=reason, request=request)
    db.commit()
    return _load(db, org.id)


@router.post("/{org_id}/unblock")
def unblock_org(
    org_id: str, request: Request, body: dict = Body(default=None),
    p: Principal = Depends(require_platform("orgs.block")), db: Session = Depends(get_db),
):
    form = Form(body)
    reason = form.text("reason", "Причина", required=False, max_len=500, multiline=True)
    form.done()
    org = _get(db, org_id, lock=True)
    if org.blocked_at is None:
        raise PlatformError(409, "Центр не заблокирован", "not_blocked")
    org.blocked_at = None
    org.blocked_reason = None
    audit.record(db, actor_id=p.user.id, action="org.unblocked", target_type="org", target_id=org.id, reason=reason, request=request)
    db.commit()
    return _load(db, org.id)


@router.patch("/{org_id}/limits")
def set_limits(
    org_id: str, request: Request, body: dict = Body(default=None),
    p: Principal = Depends(require_platform("orgs.manage")), db: Session = Depends(get_db),
):
    form = Form(body)
    students = form.integer("max_students", "Лимит учеников", minimum=1, maximum=100000)
    teachers = form.integer("max_teachers", "Лимит учителей", minimum=1, maximum=10000)
    if students is None and teachers is None and not form.errors:
        form.fail("max_students", "Укажите хотя бы один лимит")
    form.done()
    org = _get(db, org_id, lock=True)
    before = {"max_students": org.max_students, "max_teachers": org.max_teachers}
    warnings = []
    if students is not None:
        org.max_students = students
        current = db.query(func.count(Student.id)).filter(Student.org_id == org.id, Student.status != "archived").scalar() or 0
        if students < current:
            warnings.append(f"Лимит учеников ({students}) ниже текущего числа учеников ({current})")
    if teachers is not None:
        org.max_teachers = teachers
        current = (
            db.query(func.count(OrganizationMember.id))
            .filter(OrganizationMember.org_id == org.id, OrganizationMember.org_role == "teacher", OrganizationMember.is_active.is_(True))
            .scalar() or 0
        )
        if teachers < current:
            warnings.append(f"Лимит учителей ({teachers}) ниже текущего числа учителей ({current})")
    audit.record(
        db, actor_id=p.user.id, action="org.limits_changed", target_type="org", target_id=org.id,
        details={"before": before, "after": {"max_students": org.max_students, "max_teachers": org.max_teachers}, "warnings": warnings},
        request=request,
    )
    db.commit()
    return {"org": _load(db, org.id), "warnings": warnings}
