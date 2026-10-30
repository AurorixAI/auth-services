"""The platform journal: one ``platform_audit`` row per change made in the console and per console sign-in attempt.

``record`` only ADDS the row to the caller's transaction (it commits with the change, so a change and its row are one unit).
A caller that is about to raise (a failed sign-in) commits the row first. Passwords, tokens and hashes never go in: any detail key that
looks like one is replaced before the row is written.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from fastapi import Request
from sqlalchemy.orm import Session

from src.models.auth import Organization, User
from src.models.platform import DemoRequest, PlatformAudit, PlatformRole

from .platform_common import client_meta, iso

ACTION_LABELS: Dict[str, str] = {
    "console.login.success": "Вход в консоль",
    "console.login.failed": "Неудачная попытка входа",
    "console.login.locked": "Вход заблокирован (слишком много попыток)",
    "console.login.forbidden": "Вход отклонён (нет доступа к консоли)",
    "console.logout": "Выход из консоли",
    "console.logout_all": "Выход со всех устройств",
    "console.password.changed": "Смена пароля консоли",
    "console.password.change_failed": "Неудачная смена пароля консоли (неверный текущий)",
    "user.password_set_by_link": "Пароль задан по ссылке",
    "demo_request.received": "Заявка получена",
    "demo_request.status_changed": "Статус заявки изменён",
    "demo_request.note_added": "Заметка к заявке",
    "demo_request.approved": "Заявка одобрена",
    "demo_request.declined": "Заявке отказано",
    "org.blocked": "Центр заблокирован",
    "org.unblocked": "Центр разблокирован",
    "org.limits_changed": "Лимиты центра изменены",
    "user.invite_resent": "Приглашение отправлено снова",
    "user.reset_sent": "Ссылка сброса пароля отправлена",
    "user.blocked": "Пользователь заблокирован",
    "user.unblocked": "Пользователь разблокирован",
    "staff.created": "Сотрудник консоли создан",
    "staff.updated": "Сотрудник консоли изменён",
    "staff.password_reset": "Пароль сотрудника сброшен",
    "role.created": "Роль создана",
    "role.updated": "Роль изменена",
    "role.deleted": "Роль удалена",
}

_SECRET_MARKERS = ("password", "token", "secret", "hash", "passwd")
TARGET_TYPES = ("demo_request", "org", "user", "staff", "role", "console")


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if any(marker in str(key).lower() for marker in _SECRET_MARKERS):
                out[str(key)] = "[скрыто]"
            else:
                out[str(key)] = _scrub(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub(v, depth + 1) for v in value][:100]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value if not isinstance(value, str) else value[:1000]
    return str(value)[:1000]


def record(
    db: Session,
    *,
    actor_id,
    action: str,
    target_type: Optional[str] = None,
    target_id=None,
    reason: Optional[str] = None,
    details: Optional[dict] = None,
    request: Optional[Request] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> PlatformAudit:
    if action not in ACTION_LABELS:
        raise ValueError(f"unknown audit action {action!r}")
    if request is not None:
        ip, user_agent = client_meta(request)
    row = PlatformAudit(
        actor_user_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        reason=reason[:2000] if reason else None,
        details=_scrub(details or {}),
        ip=ip,
        user_agent=user_agent,
    )
    db.add(row)
    db.flush()
    return row


def _actor_map(db: Session, ids: Iterable) -> Dict[str, dict]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = db.query(User.id, User.email, User.first_name, User.last_name).filter(User.id.in_(ids)).all()
    return {str(r.id): {"id": str(r.id), "name": f"{r.first_name} {r.last_name}".strip(), "email": r.email} for r in rows}


def _target_labels(db: Session, rows: List[PlatformAudit]) -> Dict[tuple, str]:
    from uuid import UUID

    def uuids(kind):
        out = []
        for r in rows:
            if r.target_type == kind and r.target_id:
                try:
                    out.append(UUID(r.target_id))
                except ValueError:
                    pass
        return out

    labels: Dict[tuple, str] = {}
    if ids := uuids("org"):
        for r in db.query(Organization.id, Organization.name).filter(Organization.id.in_(ids)):
            labels[("org", str(r.id))] = r.name
    if ids := uuids("demo_request"):
        for r in db.query(DemoRequest.id, DemoRequest.centre_name).filter(DemoRequest.id.in_(ids)):
            labels[("demo_request", str(r.id))] = r.centre_name
    if ids := uuids("role"):
        for r in db.query(PlatformRole.id, PlatformRole.name).filter(PlatformRole.id.in_(ids)):
            labels[("role", str(r.id))] = r.name
    user_ids = uuids("user") + uuids("staff")
    if user_ids:
        for r in db.query(User.id, User.email).filter(User.id.in_(user_ids)):
            labels[("user", str(r.id))] = r.email
            labels[("staff", str(r.id))] = r.email
    return labels


def serialize(db: Session, rows: List[PlatformAudit]) -> List[dict]:
    actors = _actor_map(db, (r.actor_user_id for r in rows))
    labels = _target_labels(db, rows)
    return [
        {
            "id": str(r.id),
            "created_at": iso(r.created_at),
            "actor": actors.get(str(r.actor_user_id)) if r.actor_user_id else None,
            "action": r.action,
            "label": ACTION_LABELS.get(r.action, r.action),
            "target_type": r.target_type,
            "target_id": r.target_id,
            "target_label": labels.get((r.target_type, r.target_id)),
            "reason": r.reason,
            "details": r.details or {},
            "ip": r.ip,
            "user_agent": r.user_agent,
        }
        for r in rows
    ]


def history(db: Session, target_type: str, target_id, limit: int = 100) -> List[dict]:
    rows = (
        db.query(PlatformAudit)
        .filter(PlatformAudit.target_type == target_type, PlatformAudit.target_id == str(target_id))
        .order_by(PlatformAudit.created_at.desc())
        .limit(limit)
        .all()
    )
    return serialize(db, rows)
