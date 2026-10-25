"""/api/v1/platform/{permissions,roles,staff}: the team of the console and what each role may do. Owner only (right ``team.manage``).

Rules: permissions are data (a role is a list of keys); the system role «Владелец платформы» cannot be edited or deleted; the platform owner
(``is_owner``) can do everything whatever his role says; the LAST active owner cannot be demoted or switched off; nobody changes
his own role or switches himself off; staff get a password from the owner and must change it at the first sign-in."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.core.security import hash_password
from src.models.auth import User
from src.models.platform import PlatformRole, PlatformStaff

from . import platform_audit as audit
from . import platform_auth
from . import platform_permissions as perms
from .platform_common import iso, not_found, utcnow
from .platform_errors import PlatformError, validation_error
from .platform_security import Principal, require_platform
from .platform_validation import Form, parse_uuid_or_404

logger = logging.getLogger(__name__)

router = APIRouter()

NAME_TAKEN = PlatformError(409, "Роль с таким названием уже есть", "name_taken")


# ── permissions ───────────────────────────────────────────────────────────────────────────────


@router.get("/permissions")
def list_permissions(p: Principal = Depends(require_platform())):
    return {"items": [{"key": x.key, "label": x.label, "group": x.group, "active": x.active} for x in perms.CATALOG]}


# ── roles ─────────────────────────────────────────────────────────────────────────────────────


def _role(db: Session, r: PlatformRole) -> dict:
    count = db.query(func.count(PlatformStaff.id)).filter(PlatformStaff.role_id == r.id).scalar() or 0
    return {
        "id": str(r.id), "name": r.name, "permissions": perms.normalise(r.permissions or []), "is_template": bool(r.is_template),
        "is_system": bool(r.is_system), "staff_count": count,
    }


def _get_role(db: Session, role_id: str, *, lock: bool = False) -> PlatformRole:
    uid = parse_uuid_or_404(role_id, "Роль")
    q = db.query(PlatformRole).filter(PlatformRole.id == uid)
    if lock:
        q = q.with_for_update()
    role = q.first()
    if role is None:
        raise not_found("Роль")
    return role


def _permission_list(form: Form) -> Optional[list]:
    keys = form.str_list("permissions", "Права")
    if keys is None:
        return None
    bad = perms.unknown_keys(keys)
    if bad:
        form.fail("permissions", f"Неизвестное право: {bad[0]}")
        return None
    if perms.TEAM_MANAGE in keys:
        raise PlatformError(
            422, "Право «Команда и роли» есть только у владельца платформы", "owner_only_permission",
            fields={"permissions": "Право «Команда и роли» есть только у владельца платформы"},
        )
    return perms.normalise(keys)


@router.get("/roles")
def list_roles(p: Principal = Depends(require_platform(perms.TEAM_MANAGE)), db: Session = Depends(get_db)):
    perms.ensure_template_roles(db)
    db.commit()
    roles = db.query(PlatformRole).order_by(PlatformRole.is_system.desc(), PlatformRole.is_template.desc(), PlatformRole.name).all()
    return {"items": [_role(db, r) for r in roles]}


@router.post("/roles", status_code=201)
def create_role(
    request: Request, body: dict = Body(default=None), p: Principal = Depends(require_platform(perms.TEAM_MANAGE)), db: Session = Depends(get_db)
):
    form = Form(body)
    name = form.text("name", "Название", max_len=80)
    keys = _permission_list(form)
    form.done()
    if db.query(PlatformRole.id).filter(func.lower(PlatformRole.name) == name.lower()).first():
        raise NAME_TAKEN
    role = PlatformRole(name=name, permissions=keys, is_template=False, is_system=False)
    db.add(role)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise NAME_TAKEN from None
    audit.record(db, actor_id=p.user.id, action="role.created", target_type="role", target_id=role.id, details={"name": name, "permissions": keys}, request=request)
    db.commit()
    return _role(db, role)


@router.patch("/roles/{role_id}")
def update_role(
    role_id: str, request: Request, body: dict = Body(default=None),
    p: Principal = Depends(require_platform(perms.TEAM_MANAGE)), db: Session = Depends(get_db),
):
    form = Form(body)
    name = form.text("name", "Название", required=False, max_len=80) if form.has("name") else None
    keys = _permission_list(form) if form.has("permissions") else None
    if name is None and keys is None and not form.errors:
        form.fail("name", "Нечего менять")
    form.done()
    role = _get_role(db, role_id, lock=True)
    if role.is_system:
        raise PlatformError(409, "Роль «Владелец платформы» изменить нельзя", "system_role")
    changes = {}
    if name is not None and name != role.name:
        if db.query(PlatformRole.id).filter(func.lower(PlatformRole.name) == name.lower(), PlatformRole.id != role.id).first():
            raise NAME_TAKEN
        changes["name"] = {"from": role.name, "to": name}
        role.name = name
    if keys is not None and keys != perms.normalise(role.permissions or []):
        changes["permissions"] = {"from": perms.normalise(role.permissions or []), "to": keys}
        role.permissions = keys
    if changes:
        role.updated_at = utcnow()
        audit.record(db, actor_id=p.user.id, action="role.updated", target_type="role", target_id=role.id, details={"changes": changes}, request=request)
        db.commit()
    return _role(db, role)


@router.delete("/roles/{role_id}")
def delete_role(
    role_id: str, request: Request, p: Principal = Depends(require_platform(perms.TEAM_MANAGE)), db: Session = Depends(get_db)
):
    role = _get_role(db, role_id, lock=True)
    if role.is_system:
        raise PlatformError(409, "Роль «Владелец платформы» удалить нельзя", "system_role")
    if db.query(PlatformStaff.id).filter(PlatformStaff.role_id == role.id).first():
        raise PlatformError(409, "Роль назначена сотрудникам — сначала смените им роль", "role_in_use")
    audit.record(db, actor_id=p.user.id, action="role.deleted", target_type="role", target_id=role.id, details={"name": role.name}, request=request)
    db.delete(role)
    db.commit()
    return {"ok": True}


# ── staff ─────────────────────────────────────────────────────────────────────────────────────


def _staff(s: PlatformStaff, u: User, r: Optional[PlatformRole]) -> dict:
    return {
        "user_id": str(u.id), "email": u.email, "first_name": u.first_name, "last_name": u.last_name,
        "name": f"{u.first_name} {u.last_name}".strip(), "role": {"id": str(r.id), "name": r.name} if r else None,
        "is_owner": bool(s.is_owner), "is_active": bool(s.is_active and u.is_active), "must_change_password": bool(s.must_change_password),
        "last_login_at": iso(s.last_login_at), "locked_until": iso(s.locked_until) if s.locked_until and s.locked_until > utcnow() else None,
        "created_at": iso(s.created_at),
    }


def _load_staff(db: Session, user_id, *, lock: bool = False):
    q = db.query(PlatformStaff, User, PlatformRole).join(User, User.id == PlatformStaff.user_id).join(PlatformRole, PlatformRole.id == PlatformStaff.role_id)
    q = q.filter(PlatformStaff.user_id == user_id)
    if lock:
        q = q.with_for_update(of=PlatformStaff)
    found = q.first()
    if found is None:
        raise not_found("Сотрудник")
    return found


def _owner_role(db: Session) -> PlatformRole:
    perms.ensure_template_roles(db)
    return db.query(PlatformRole).filter(PlatformRole.is_system.is_(True)).first()


def _other_active_owners(db: Session, except_user_id) -> int:
    return (
        db.query(func.count(PlatformStaff.id))
        .join(User, User.id == PlatformStaff.user_id)
        .filter(PlatformStaff.is_owner.is_(True), PlatformStaff.is_active.is_(True), User.is_active.is_(True), PlatformStaff.user_id != except_user_id)
        .scalar() or 0
    )


@router.get("/staff")
def list_staff(p: Principal = Depends(require_platform(perms.TEAM_MANAGE)), db: Session = Depends(get_db)):
    rows = (
        db.query(PlatformStaff, User, PlatformRole)
        .join(User, User.id == PlatformStaff.user_id).join(PlatformRole, PlatformRole.id == PlatformStaff.role_id)
        .order_by(PlatformStaff.is_owner.desc(), User.last_name, User.first_name)
        .all()
    )
    return {"items": [_staff(s, u, r) for s, u, r in rows]}


@router.post("/staff", status_code=201)
def create_staff(
    request: Request, body: dict = Body(default=None), p: Principal = Depends(require_platform(perms.TEAM_MANAGE)), db: Session = Depends(get_db)
):
    form = Form(body)
    email = form.email("email")
    first = form.text("first_name", "Имя", max_len=100)
    last = form.text("last_name", "Фамилия", max_len=100)
    role_id = form.uuid("role_id", "Роль")
    wants_owner = form.boolean("is_owner")
    password = form.data.get("password")
    reason = platform_auth.validate_new_password(password, email or "")
    if reason:
        form.fail("password", reason)
    form.done()
    role = db.get(PlatformRole, role_id)
    if role is None:
        raise validation_error({"role_id": "Роль не найдена"})
    if wants_owner and not role.is_system:
        role = _owner_role(db)
    is_owner = bool(role.is_system)
    if db.query(User.id).filter(func.lower(User.email) == email).first() is not None:
        raise PlatformError(409, "Эта почта уже используется в ALGO", "email_in_use")
    user = User(
        email=email, password_hash=hash_password(password), first_name=first, last_name=last, role="teacher", default_org_id=None,
        language="ru", is_active=True, is_verified=True, must_change_password=False,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise PlatformError(409, "Эта почта уже используется в ALGO", "email_in_use") from None
    staff = PlatformStaff(user_id=user.id, role_id=role.id, is_owner=is_owner, is_active=True, must_change_password=True)
    db.add(staff)
    db.flush()
    audit.record(
        db, actor_id=p.user.id, action="staff.created", target_type="staff", target_id=user.id,
        details={"email": email, "role_id": str(role.id), "role": role.name, "is_owner": is_owner}, request=request,
    )
    db.commit()
    return _staff(staff, user, role)


@router.patch("/staff/{user_id}")
def update_staff(
    user_id: str, request: Request, body: dict = Body(default=None),
    p: Principal = Depends(require_platform(perms.TEAM_MANAGE)), db: Session = Depends(get_db),
):
    uid = parse_uuid_or_404(user_id, "Сотрудник")
    form = Form(body)
    role_id = form.uuid("role_id", "Роль", required=False)
    first = form.text("first_name", "Имя", required=False, max_len=100) if form.has("first_name") else None
    last = form.text("last_name", "Фамилия", required=False, max_len=100) if form.has("last_name") else None
    active = form.boolean("is_active", default=None) if form.has("is_active") else None
    owner_flag = form.boolean("is_owner", default=None) if form.has("is_owner") else None
    if all(v is None for v in (role_id, first, last, active, owner_flag)) and not form.errors:
        form.fail("role_id", "Нечего менять")
    form.done()
    staff, user, role = _load_staff(db, uid, lock=True)
    is_self = uid == p.user.id

    new_role, new_owner = role, bool(staff.is_owner)
    if role_id is not None and role_id != role.id:
        new_role = db.get(PlatformRole, role_id)
        if new_role is None:
            raise validation_error({"role_id": "Роль не найдена"})
        new_owner = bool(new_role.is_system)
    if owner_flag is True and not new_owner:
        new_role, new_owner = _owner_role(db), True
    elif owner_flag is False and new_owner:
        if new_role.is_system:
            raise validation_error({"is_owner": "Владелец определяется ролью «Владелец платформы» — выберите другую роль"})
        new_owner = False
    changes = {}
    if new_role.id != role.id or new_owner != bool(staff.is_owner):
        if is_self:
            raise PlatformError(409, "Нельзя менять собственную роль или отключать себя", "self_change")
        if staff.is_owner and not new_owner and _other_active_owners(db, uid) == 0:
            raise PlatformError(409, "Нельзя лишить прав или отключить последнего владельца", "last_owner")
        changes["role"] = {"from": role.name, "to": new_role.name}
    if active is not None and active != bool(staff.is_active):
        if is_self and active is False:
            raise PlatformError(409, "Нельзя менять собственную роль или отключать себя", "self_change")
        if active is False and staff.is_owner and _other_active_owners(db, uid) == 0:
            raise PlatformError(409, "Нельзя лишить прав или отключить последнего владельца", "last_owner")
        changes["is_active"] = {"from": bool(staff.is_active), "to": active}
    if first is not None and first != user.first_name:
        changes["first_name"] = {"from": user.first_name, "to": first}
    if last is not None and last != user.last_name:
        changes["last_name"] = {"from": user.last_name, "to": last}
    if not changes:
        return _staff(staff, user, role)

    if "role" in changes:
        staff.role_id, staff.is_owner = new_role.id, new_owner
    if "is_active" in changes:
        staff.is_active = active
        if active:
            staff.failed_logins, staff.locked_until = 0, None
        else:
            platform_auth.revoke_sessions(db, uid, "staff_disabled")
    if "first_name" in changes:
        user.first_name = first
    if "last_name" in changes:
        user.last_name = last
    staff.updated_at = utcnow()
    audit.record(db, actor_id=p.user.id, action="staff.updated", target_type="staff", target_id=uid, details={"email": user.email, "changes": changes}, request=request)
    db.commit()
    return _staff(staff, user, new_role)


@router.post("/staff/{user_id}/reset-password")
def reset_staff_password(
    user_id: str, request: Request, body: dict = Body(default=None),
    p: Principal = Depends(require_platform(perms.TEAM_MANAGE)), db: Session = Depends(get_db),
):
    uid = parse_uuid_or_404(user_id, "Сотрудник")
    form = Form(body)
    password = form.data.get("password")
    reason = platform_auth.validate_new_password(password)
    if reason:
        form.fail("password", reason)
    form.done()
    staff, user, _ = _load_staff(db, uid, lock=True)
    if uid == p.user.id:
        raise PlatformError(409, "Свой пароль меняйте через «Сменить пароль»", "self_change")
    user.password_hash = hash_password(password)
    staff.must_change_password = True
    staff.failed_logins, staff.locked_until = 0, None
    platform_auth.revoke_sessions(db, uid, "password_reset")
    audit.record(db, actor_id=p.user.id, action="staff.password_reset", target_type="staff", target_id=uid, details={"email": user.email}, request=request)
    db.commit()
    return {"ok": True}
