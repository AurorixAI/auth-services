"""
ALGO - «Цены групп»: one table of the prices of every group of the centre (ROADMAP #105, owner request 21.10).

``GET /org/group-prices`` answers the dedicated page «Оплаты -> Цены групп»: for every group that is NOT archived (an
archived group takes no new students or payments, so its price is not managed here) - the current price, the next
scheduled price and how many active students carry an individual price today.  The group price is no longer edited in
the group card.

Five queries however many groups there are (no query per group): the groups with their teacher, every price row of
those groups, the individual-price rows of their active students (in force today; and the count of those that start later), the billing mode.  The business day is Tashkent
(``payment_status.business_today``) like all the pricing; the price on a day is the ONE rule of ``pricing.GroupBook`` /
``OverrideBook`` (the row with the greatest ``valid_from <= day``).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import distinct, func
from sqlalchemy.orm import Session

from src.models.auth import Group, GroupPrice, MembershipPriceOverride, Student, StudentGroup, User

from . import payment_status as ps
from .payments_files import staff_name
from .pricing import GroupBook, OverrideBook


def list_group_prices(db: Session, org_id: str, today: Optional[date] = None) -> Dict[str, Any]:
    """The table of the page (see the module docstring); money roles only - the endpoint is under ``require_payments_access``."""
    from .payments_service import PaymentService  # lazy: pulls in the status maths

    today = today or ps.business_today()
    groups = (
        db.query(Group.id, Group.name, Group.lesson_type, Group.teacher_id, User.first_name, User.last_name)
        .outerjoin(User, User.id == Group.teacher_id)
        .filter(Group.org_id == org_id, Group.status != "archived")
        .all()
    )
    mode = PaymentService(db).get_settings(org_id).period_mode
    out: Dict[str, Any] = {
        "as_of": today.isoformat(),
        "period_mode": mode,
        "price_label": "Цена в месяц" if mode == ps.CALENDAR else "Цена пакета",
        "items": [],
    }
    if not groups:
        return out
    group_ids = [g.id for g in groups]

    steps: Dict[UUID, List[Any]] = defaultdict(list)
    for row in db.query(GroupPrice.id, GroupPrice.group_id, GroupPrice.valid_from, GroupPrice.amount).filter(
        GroupPrice.group_id.in_(group_ids)
    ):
        steps[row.group_id].append(row)

    # the individual price rows in force today (valid_from <= today) of the ACTIVE students of these groups
    overrides: Dict[UUID, List[Any]] = defaultdict(list)
    group_of: Dict[UUID, UUID] = {}
    for row in (
        db.query(
            StudentGroup.id, StudentGroup.group_id, MembershipPriceOverride.valid_from, MembershipPriceOverride.amount
        )
        .join(Student, Student.id == StudentGroup.student_id)
        .join(MembershipPriceOverride, MembershipPriceOverride.membership_id == StudentGroup.id)
        .filter(
            StudentGroup.group_id.in_(group_ids),
            StudentGroup.is_active.is_(True),
            Student.org_id == org_id,
            Student.status != "archived",
            MembershipPriceOverride.valid_from <= today,
        )
    ):
        overrides[row.id].append((row.valid_from, row.amount, None))
        group_of[row.id] = row.group_id
    with_override: Dict[UUID, int] = defaultdict(int)
    for membership_id, rows in overrides.items():
        latest = OverrideBook(rows).at(today)
        if latest is not None and latest[1] is not None:  # an ended exception ("the group price again") is not counted
            with_override[group_of[membership_id]] += 1

    # active students who have an individual price that starts AFTER today (#126: «и N с запланированной»); one grouped query
    scheduled_overrides: Dict[UUID, int] = {
        row.group_id: row.n
        for row in db.query(StudentGroup.group_id, func.count(distinct(StudentGroup.id)).label("n"))
        .join(Student, Student.id == StudentGroup.student_id)
        .join(MembershipPriceOverride, MembershipPriceOverride.membership_id == StudentGroup.id)
        .filter(
            StudentGroup.group_id.in_(group_ids),
            StudentGroup.is_active.is_(True),
            Student.org_id == org_id,
            Student.status != "archived",
            MembershipPriceOverride.valid_from > today,
        )
        .group_by(StudentGroup.group_id)
    }

    items = []
    for g in groups:
        rows = steps.get(g.id, [])
        current = GroupBook((r.valid_from, r.amount) for r in rows).at(today)
        future = sorted((r for r in rows if r.valid_from > today), key=lambda r: r.valid_from)
        items.append(
            {
                "group_id": str(g.id),
                "name": g.name,
                "teacher_id": str(g.teacher_id) if g.teacher_id else None,
                "teacher_name": staff_name(g.first_name, g.last_name),
                "kind": g.lesson_type,
                "current": {"amount": current[1], "valid_from": current[0]} if current else None,
                "scheduled": (
                    {"row_id": str(future[0].id), "amount": future[0].amount, "valid_from": future[0].valid_from}
                    if future
                    else None
                ),
                "scheduled_count": len(future),
                "students_with_override": with_override.get(g.id, 0),
                "scheduled_override_count": scheduled_overrides.get(g.id, 0),
            }
        )
    items.sort(key=lambda i: ((i["name"] or "").casefold(), i["group_id"]))
    out["items"] = items
    return out
