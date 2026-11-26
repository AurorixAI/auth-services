"""LESSON-RULES С0: the rules of a centre - ONE mechanism of the settings a centre chooses for itself (the owner's decision 26.11: «гибкая система, которая поддержит разные решения»).

Every rule is a typed key (``KEYS``): the values it may take, its default, its words.  A key is stored in the settings of the centre (``organizations.settings["policy"]``)
as VERSIONS ``[{"from": "min" | "YYYY-MM-DD" | "YYYY-MM", "value": ..., "at": utc}]`` - the history and the «acts from a month» are one way, not two (the same shape as
``recalc_history`` / ``billing_history``).  A key may have NO default (``default=None``: «не выбрано»): the code reads ``None`` and decides what that means.  Every rule is read in the code
in ONE place - through ``CentrePolicy`` - never as a constant beside the code.  A write takes the row of the organisation ``FOR UPDATE`` and merges the ``policy`` block only (a parallel
write of the payment settings and of the rules of the lessons loses neither), and every change goes to the journal of the centre (``org_money_audit``, ``details.section = "lessons"``:
who, when, «было → стало»); the page of the payment settings does not show these lines, the page of the lessons does.

Not here: the dates of the money (``centre_cancel_recalc`` lives with the payments), a screen or a route for a key without a value / with a month (the payroll pack)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from . import business_clock as bc
from src.models.auth import Organization, OrgMoneyAudit, User
from .money_journal_actions import POLICY_CHANGED
from .ops_rules import OpsError, conflict, not_found, unprocessable

LESSON_WINDOW_DAYS = "teacher_lesson_window_days"
PAST_CANCEL_NEEDS_CONFIRMATION = "past_cancel_needs_confirmation"
LESSONS_SECTION = "lessons"

TEACHER_ACCESS_DAYS = 7           # the days a substitute / a former teacher keeps the access to a group and to his lesson in the journal (NOT the window of the actions: ``access_days``)


@dataclass(frozen=True)
class PolicyKey:
    name: str
    section: str
    default: Any                     # ``None``: the key has no default («не выбрано»)
    allowed: Tuple[Any, ...]
    title: str
    hint: str

    def words(self, value: Any) -> str:
        if value is None:
            return "не выбрано"
        if isinstance(value, bool):
            return "да" if value else "нет"
        return str(value)


KEYS: Dict[str, PolicyKey] = {k.name: k for k in (
    PolicyKey(LESSON_WINDOW_DAYS, LESSONS_SECTION, 7, (1, 3, 7, 14, 30),
              "Преподаватель сам оформляет прошедший урок в течение",
              "Позже этого срока урок оформляет администратор или владелец. Оформленное задним числом видно в «Замены → История»."),
    PolicyKey(PAST_CANCEL_NEEDS_CONFIRMATION, LESSONS_SECTION, True, (True, False),
              "Отмену прошедшего урока преподавателем или администратором без доступа к деньгам подтверждает владелец или администратор с доступом к деньгам",
              "Пока отмена не подтверждена, перерасчёта ученикам нет."),
)}


def window_days_text(days: int) -> str:
    """«7 дней» / «1 день» / «3 дня»: the number of the window in the words of a refusal."""
    if days % 10 == 1 and days % 100 != 11:
        return f"{days} день"
    if days % 10 in (2, 3, 4) and days % 100 not in (12, 13, 14):
        return f"{days} дня"
    return f"{days} дней"


def _parse_from(value: Any) -> Tuple[int, int, int]:
    """The order key of a version start: ``min`` first, a month = its first day."""
    if value in (None, "min"):
        return (0, 0, 0)
    text = str(value)
    if len(text) == 7:
        text += "-01"
    parsed = date.fromisoformat(text)
    return (parsed.year, parsed.month, parsed.day)


def value_of(versions: Any, key: PolicyKey, on: Optional[date] = None) -> Any:
    """The value of a key from its versions: the last one that started on or before ``on`` (``None`` = the last of all); the default when nothing is stored.
    A stored value the key does not allow (a hand edit) is the default, never an error of a page."""
    chosen = key.default
    if isinstance(versions, list):
        ordered = sorted((v for v in versions if isinstance(v, dict) and "value" in v), key=lambda v: _parse_from(v.get("from")))
        for version in ordered:
            if on is not None and _parse_from(version.get("from")) > (on.year, on.month, on.day):
                break
            chosen = version["value"] if version["value"] in key.allowed or version["value"] is None and key.default is None else chosen
    return chosen


class CentrePolicy:
    """The rules of ONE centre, read once (one query) and asked many times: ``CentrePolicy.of(db, org_id)``."""

    def __init__(self, block: Dict[str, Any]) -> None:
        self.block = block

    @classmethod
    def of(cls, db: Session, org_id: Any) -> "CentrePolicy":
        raw = db.query(Organization.settings).filter(Organization.id == org_id).scalar()
        block = raw.get("policy") if isinstance(raw, dict) else None
        return cls(dict(block) if isinstance(block, dict) else {})

    def value(self, name: str, on: Optional[date] = None) -> Any:
        key = KEYS[name]
        return value_of(self.block.get(name), key, on)

    # the two rules of this pack, in the words the code asks them
    @property
    def lesson_window_days(self) -> int:
        return int(self.value(LESSON_WINDOW_DAYS))

    @property
    def past_cancel_needs_confirmation(self) -> bool:
        return bool(self.value(PAST_CANCEL_NEEDS_CONFIRMATION))

    @property
    def access_days(self) -> int:
        """The days of the ACCESS to a group / a lesson of a substitute and of a former teacher: the window of the actions when it is longer than the old 7, else 7 (a window of 1 or 3 days
        limits the action, never the access: the lesson stays visible)."""
        return max(TEACHER_ACCESS_DAYS, self.lesson_window_days)

    def view(self) -> Dict[str, Any]:
        """The page «Уроки» of the settings: every key with its value, default, the allowed values and its words."""
        return {"items": [{"key": k.name, "value": self.value(k.name), "default": k.default, "allowed": list(k.allowed), "title": k.title, "hint": k.hint, "words": k.words(self.value(k.name))}
                          for k in KEYS.values() if k.section == LESSONS_SECTION],
                "teacher_lesson_window_days": self.lesson_window_days, "past_cancel_needs_confirmation": self.past_cancel_needs_confirmation}


def lesson_window_days(db: Session, org_id: Any) -> int:
    return CentrePolicy.of(db, org_id).lesson_window_days


def update(db: Session, org_id: Any, actor_id: Any, changes: Dict[str, Any]) -> Dict[str, Any]:
    """Write the rules the owner changed (an absent key stays).  The row of the organisation is locked; only the ``policy`` block is replaced; one line of the journal per changed key.
    A value the key does not allow is a 422 ``POLICY_VALUE_INVALID``; a key that is not a rule is a 422 ``POLICY_KEY_UNKNOWN``."""
    for name, value in changes.items():
        key = KEYS.get(name)
        if key is None:
            raise unprocessable("POLICY_KEY_UNKNOWN", "Такого правила нет")
        if value not in key.allowed or isinstance(value, bool) != isinstance(key.allowed[0], bool):
            raise unprocessable("POLICY_VALUE_INVALID", f"Значение правила «{key.title}» не из допустимых: {', '.join(key.words(a) for a in key.allowed)}", key=name)
    org = db.query(Organization).filter(Organization.id == org_id).with_for_update().populate_existing().first()
    if org is None:
        raise not_found("ORG_NOT_FOUND", "Организация не найдена")
    block = dict((org.settings or {}).get("policy") or {})
    now = bc.utc_now()
    today = bc.business_today()
    audit: List[OrgMoneyAudit] = []
    for name, value in changes.items():
        key = KEYS[name]
        before = value_of(block.get(name), key)
        if before == value and isinstance(before, bool) == isinstance(value, bool):
            continue
        versions = [dict(v) for v in (block.get(name) or []) if isinstance(v, dict)]
        entry = {"from": today.isoformat(), "value": value, "at": now.isoformat()}
        if versions and versions[-1].get("from") == today.isoformat():
            versions[-1] = entry
        else:
            versions.append(entry)
        block[name] = versions
        audit.append(OrgMoneyAudit(org_id=UUID(str(org_id)), actor_id=UUID(str(actor_id)), action=POLICY_CHANGED, occurred_at=now, details={
            "section": key.section, "key": name, "before": before, "after": value,
            "text": f"{key.title}: {key.words(before)} → {key.words(value)}"}))
    if audit:
        block["updated_by"], block["updated_at"] = str(actor_id), now.isoformat()
        org.settings = {**(org.settings or {}), "policy": block}
        flag_modified(org, "settings")
        for line in audit:
            db.add(line)
    db.commit()
    return CentrePolicy.of(db, org_id).view()


def history(db: Session, org_id: Any, *, limit: int = 50) -> Dict[str, Any]:
    """The journal of the rules of the lessons, the newest first: who, when, «было → стало»."""
    rows = [r for r in (db.query(OrgMoneyAudit).filter(OrgMoneyAudit.org_id == org_id, OrgMoneyAudit.action == POLICY_CHANGED)
                        .order_by(OrgMoneyAudit.occurred_at.desc(), OrgMoneyAudit.id.desc()).limit(max(1, min(limit, 200))).all())
            if (r.details or {}).get("section") == LESSONS_SECTION]
    names = {u.id: " ".join(p for p in (u.last_name, u.first_name) if p) for u in db.query(User).filter(User.id.in_({r.actor_id for r in rows if r.actor_id}))} if rows else {}
    return {"items": [{"id": str(r.id), "at": r.occurred_at.isoformat() if r.occurred_at else None, "actor_id": str(r.actor_id) if r.actor_id else None, "actor_name": names.get(r.actor_id),
                       "key": (r.details or {}).get("key"), "text": (r.details or {}).get("text") or "", "details": r.details or {}} for r in rows]}
