"""
ALGO - Money-2 / T3: what a transfer, a leaving and an archiving END, MOVE and LEAVE BEHIND - the structure and the words, ONE builder for the three of them.

``snapshot`` reads, BEFORE the stay is closed, everything the closing touches (the debt and the advance of the account, the individual price and the prices scheduled after the last day, the pauses,
a «Оплата с» that has not begun); ``build`` turns it into

    {"kind": "transfer" | "leave" | "archive", "moves": [...], "stays": [...], "ends": [...], "money_text": str | None, "texts": [str, ...]}

* ``moves``  - what goes on (transfer only): ``money`` (the sum; a package centre: lessons at the price of the new group and the rest), ``pause``, ``billing_from``;
* ``stays``  - what stays behind in the closed group: ``debt`` («останется за учеником … принять или простить можно в её выписке»), ``advance`` (leave / archive: it stays until it is returned);
* ``ends``   - what stops: ``individual_price``, ``scheduled_price``, and (leave / archive) ``pause``.

Every item has ``code``, ``group_id``, ``group_name`` and ``text``.  The sums (``amount`` ...) are money: only for a caller who may see it (``money=True``); without the right the same facts come as words
without a sum.  The preview and the real answer are built by this one function from the same facts, so the window says what the server writes (Money-2 И4).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from src.models.auth import Group, MembershipPriceOverride, StudentGroup

from . import payment_status as ps
from . import payments_texts as tx
from . import pricing


@dataclass
class Snap:
    """One closing membership as it stands before the closing."""

    membership_id: UUID
    group_id: UUID
    group_name: str
    debt: int = 0
    advance: int = 0
    refundable: int = 0
    price: Optional[Tuple[int, Optional[date], Optional[str]]] = None           # the individual price (amount, since, reason) in force on the last day
    scheduled: List[Tuple[date, Optional[int], Optional[str]]] = field(default_factory=list)    # the prices dated after the last day (not the system markers)
    pauses: List[Tuple[date, date]] = field(default_factory=list)             # the live pauses that last beyond the last day
    billing_from: Optional[date] = None                                       # a «Оплата с» that is still ahead


def snapshot(db: Session, org_id: Any, memberships: Sequence[StudentGroup], day: date, *, after=None, loader=None) -> List[Snap]:
    """The facts of the closing of ``memberships`` on ``day`` (read before the stays are closed; nothing is written).  ``after`` / ``loader``: the result of ``LeaveService._after`` when the caller has it."""
    from .leave_service import LeaveService  # lazy: leave_service imports this module
    from .logica_service import PauseService

    if not memberships:
        return []
    if after is None or loader is None:
        after, loader = LeaveService(db)._after(str(org_id), [m.id for m in memberships], day)
    names = {g.id: g.name for g in db.query(Group).filter(Group.id.in_({m.group_id for m in memberships})).all()}
    pauses = PauseService(db)
    out: List[Snap] = []
    for m in memberships:
        snap = Snap(membership_id=m.id, group_id=m.group_id, group_name=names.get(m.group_id, ""))
        if m.id in after:
            ledger, refundable = after[m.id]
            snap.debt, snap.advance, snap.refundable = int(ledger.debt), int(ledger.advance), int(refundable)
        price = loader.books.price(m.id, m.group_id, day)
        if price.source == pricing.SOURCE_OVERRIDE and price.amount is not None:
            snap.price = (int(price.amount), price.valid_from, price.reason)
        for row in (db.query(MembershipPriceOverride).filter(MembershipPriceOverride.membership_id == m.id, MembershipPriceOverride.valid_from > day)
                    .order_by(MembershipPriceOverride.valid_from).all()):
            if (row.reason or "") not in pricing.LEFT_MARKERS:
                snap.scheduled.append((row.valid_from, None if row.amount is None else int(row.amount), row.reason))
        snap.pauses = [(p.first_day, p.last_day) for p in pauses._live(m.id) if p.last_day > day]
        period = next((p for p in m.periods if p.left_at is None), None)
        if period is not None and period.billing_from is not None and period.billing_from > day:
            snap.billing_from = period.billing_from
        out.append(snap)
    return out


def carried_pay_from(candidates: Iterable[Optional[date]], today: date) -> Optional[date]:
    """The «Оплата с» that goes on to the NEW period: the first of ``candidates`` (the closed periods' own days) that is still ahead of today.  ONE rule for a transfer (T4) and for a cancelled
    leaving (R3b В1): a day that has already begun is no longer a start, the new period then follows the ordinary rule."""
    return next((d for d in candidates if d is not None and d > today), None)


def after_settle(snaps: Sequence[Snap], settled: Sequence[Any]) -> List[Snap]:
    """The facts after the choices of ``settle``: a forgiven debt no longer stays, a returned advance is not behind any more (the answer says what is REALLY left)."""
    by_id = {str(s.membership_id): s for s in snaps}
    for item in settled:
        snap = by_id.get(str(item.membership_id))
        if snap is None:
            continue
        if item.action == "writeoff":
            snap.debt = 0
        elif item.action == "refund":
            snap.advance = max(0, snap.advance - int(item.amount))
    return list(snaps)


def open_balances(db: Session, org_id: Any, membership_ids: Sequence[UUID]) -> List[Tuple[Any, str, int]]:
    """Money-2 T12 / T13: the ONE rule of «what is still on the account of these memberships (the left ones too) today»: ``(ledger entry, "debt" | "advance", amount)``, a debt and an advance each as its own line.
    R3b: an advance is the sum that CAN be returned (``refundable``); nothing to return = not listed - the archive of a group and the window of adding show the same number."""
    from . import ledger_service as ls  # lazy: ledger_service imports the pricing / status modules

    if not membership_ids:
        return []
    out: List[Tuple[Any, str, int]] = []
    for entry in ls.LedgerLoader(db, str(org_id), ps.business_today()).run(list(membership_ids)):
        for kind, amount in (("debt", entry.ledger.debt), ("advance", min(entry.ledger.advance, entry.ledger.refundable))):
            if amount > 0:
                out.append((entry, kind, int(amount)))
    return out


def past_balances(db: Session, org_id: Any, student_id: Any, group_id: Any, *, money: bool) -> List[Dict[str, Any]]:
    """Money-2 T13: the window of adding a student to ``group_id`` shows what is left on his closed stays in the OTHER groups of the centre: group, the day he left, a debt or an advance
    (sums and figures only with ``money``).  No action here: moving an advance to the new group is not in the package.
    The closed stay in THIS group is not here: ``returned_balance`` (read before the return)."""
    ids = [row[0] for row in db.query(StudentGroup.id).join(Group, Group.id == StudentGroup.group_id).filter(
        StudentGroup.student_id == student_id, StudentGroup.is_active.is_(False), StudentGroup.group_id != group_id, Group.org_id == org_id).all()]
    return _balance_rows(db, org_id, ids, money, same_group=False)


def returned_balance(db: Session, org_id: Any, membership_id: Any, *, money: bool) -> List[Dict[str, Any]]:
    """Money-2 stage 6 (М11Б): a student who COMES BACK to the group he left sees what the last time left on this account (a debt or an advance; no sum without ``money``): the same row as
    ``past_balances`` with the words «В этой группе с прошлого раза ...».  The caller reads it BEFORE the membership is reopened (afterwards the account is the account of the new stay)."""
    return _balance_rows(db, org_id, [membership_id], money, same_group=True)


def _balance_rows(db: Session, org_id: Any, ids: Sequence[Any], money: bool, *, same_group: bool) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for entry, kind, amount in open_balances(db, org_id, ids):
        left_on = ps.business_date(entry.membership.left_at) if entry.membership.left_at else None
        name = entry.group.name
        word = "долг" if kind == "debt" else "аванс"
        row: Dict[str, Any] = {"group_id": str(entry.group.id), "group_name": name, "membership_id": str(entry.membership.id), "left_on": left_on.isoformat() if left_on else None, "kind": kind}
        sum_text = f" {tx.money(amount)}" if money else ""
        if same_group:
            row["text"] = f"В этой группе с прошлого раза{f' (ушёл {_dmy(left_on)})' if left_on else ''} остался {word}{sum_text}"
        else:
            row["text"] = f"В группе «{name}»{f' (ушёл {_dmy(left_on)})' if left_on else ''} остался {word}{sum_text}: смотрите её выписку"
        if money:
            row["amount"] = amount
        out.append(row)
    out.sort(key=lambda r: (r["left_on"] or "", r["group_name"], r["kind"]))
    return out


def _ddmm(day: date) -> str:
    return day.strftime("%d.%m")


def _dmy(day: date) -> str:
    return day.strftime("%d.%m.%Y")


def _range(first: date, last: date) -> str:
    return _ddmm(first) if first == last else f"{_ddmm(first)}–{_ddmm(last)}"


def package_split(amount: int, price: Optional[int], size: int) -> Optional[Tuple[int, int]]:
    """``(lessons, rest)``: how many lessons of the new group a sum buys at its price (a package of ``size`` lessons costs ``price``; the lessons of a package cost its shares, ``package_share``) and what is left
    over.  ``None``: the group has no price, nothing to count by."""
    if not price or size < 1 or amount < 0:
        return None
    packages, rest = divmod(amount, price)
    lessons = 0
    while lessons < size and ps.package_share(price, lessons + 1, size) <= rest:
        lessons += 1
    return packages * size + lessons, rest - (ps.package_share(price, lessons, size) if lessons else 0)


def money_move_text(amount: int, *, group_name: str, lessons: Optional[Tuple[int, int]] = None) -> str:
    if lessons is not None:
        count, rest = lessons
        return f"Перейдёт {tx.money(amount)} в группу «{group_name}»: {count} {_lessons_word(count)} по цене новой группы и остаток {tx.money(rest)}"
    return f"Перейдёт {tx.money(amount)} в группу «{group_name}»"


def _lessons_word(n: int) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return "урок"
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return "урока"
    return "уроков"


def debt_text(snap: Snap, money: bool) -> str:
    if money:
        return f"Долг {tx.money(snap.debt)} останется за учеником в группе «{snap.group_name}»: принять или простить его можно в её выписке"
    return f"В группе «{snap.group_name}» останется долг: принять или простить его можно в её выписке"


def advance_text(snap: Snap, money: bool) -> str:
    if money:
        return f"Аванс {tx.money(snap.advance)} останется в группе «{snap.group_name}»: вернуть его можно сейчас или позже в её выписке"
    return f"В группе «{snap.group_name}» останется оплата вперёд: вернуть её может владелец или администратор с доступом к деньгам"


def build(
    snaps: Sequence[Snap], *, kind: str, money: bool, target_name: Optional[str] = None, carried: Optional[int] = None, package: Optional[Tuple[int, Optional[int]]] = None,
    carried_pauses: Sequence[Tuple[date, date]] = (), carry_text: Optional[str] = None, paid_until: Optional[date] = None,
) -> Dict[str, Any]:
    """The structure and the words (see the module docstring).  ``carried``: the sum a transfer moved (``None`` without the right to money); ``package``: ``(size, price of the new group)`` in a package centre;
    ``carried_pauses``: the pauses created in the new group; ``carry_text``: the sentence of the new account (``carry_after.text``) that a calendar centre shows; ``paid_until``: the «оплачено до» that travelled
    with the student (no money, but not «nothing» either)."""
    moves: List[Dict[str, Any]] = []
    stays: List[Dict[str, Any]] = []
    ends: List[Dict[str, Any]] = []
    transfer = kind == "transfer"
    for s in snaps:
        base = {"membership_id": str(s.membership_id), "group_id": str(s.group_id), "group_name": s.group_name}
        if transfer:
            if money and carried:
                split = package_split(carried, package[1], package[0]) if package is not None else None
                item = {**base, "code": "money", "amount": int(carried), "text": carry_text if carry_text and split is None else money_move_text(carried, group_name=target_name or "", lessons=split)}
                if split is not None:
                    item.update({"lessons": split[0], "rest": split[1]})
                moves.append(item)
            elif money:                  # R3 П5: no money goes (nothing to carry / it stays): a coded entry with the sum that stays, for the screen (the sentence is ``money_text``)
                stays.append({**base, "code": "money_stays", "amount": int(s.advance),
                              "text": (f"Деньги остаются в группе «{s.group_name}»: {tx.money(int(s.advance))}" if s.advance > 0 else f"Переносить нечего: в группе «{s.group_name}» нет аванса")})
            for first, last in carried_pauses:
                moves.append({**base, "code": "pause", "first_day": first.isoformat(), "last_day": last.isoformat(),
                              "text": f"Пауза {_range(first, last)} перейдёт в группу «{target_name or ''}»: дни паузы там не начисляются"})
            if s.billing_from is not None:
                moves.append({**base, "code": "billing_from", "date": s.billing_from.isoformat(),
                              "text": f"«Оплата с {_dmy(s.billing_from)}» перейдёт в группу «{target_name or ''}»: начисление там начнётся с этого дня"})
        if s.debt > 0:
            stays.append({**base, "code": "debt", **({"amount": s.debt} if money else {}), "text": debt_text(s, money)})
        if not transfer and s.advance > 0:
            stays.append({**base, "code": "advance", **({"amount": s.advance} if money else {}), "text": advance_text(s, money)})
        if s.price is not None:
            amount, since, reason = s.price
            where = f" в группе «{s.group_name}»"
            ends.append({**base, "code": "individual_price", **({"amount": amount} if money else {}), "since": since.isoformat() if since else None, "reason": reason if money else None,
                         "text": (f"Индивидуальная цена {tx.money(amount)}{where} закончится: " if money else f"Индивидуальная цена{where} закончится: ")
                                 + ("в новой группе будет цена этой группы" if transfer else "при возвращении ученик получит цену группы")})
        for valid_from, amount, _reason in s.scheduled:
            what = "возврат к цене группы" if amount is None else (f"цена {tx.money(amount)}" if money else "новая индивидуальная цена")
            ends.append({**base, "code": "scheduled_price", "valid_from": valid_from.isoformat(), **({"amount": amount} if money else {}),
                         "text": f"Запланированная с {_dmy(valid_from)} индивидуальная цена в группе «{s.group_name}» ({what}) будет отменена"})
        if not transfer:
            for first, last in s.pauses:
                ends.append({**base, "code": "pause", "first_day": first.isoformat(), "last_day": last.isoformat(),
                             "text": f"Пауза {_range(first, last)} в группе «{s.group_name}» закончится вместе с пребыванием ученика в ней"})
    money_text = None
    if money:
        if paid_until is not None:
            moves.append({"code": "paid_until", "date": paid_until.isoformat(), "text": f"Оплаченные дни — по {_dmy(paid_until)} — перейдут вместе с учеником"})
        if transfer and not moves and all(x["code"] == "money_stays" for x in stays) and all(s.advance == 0 and s.debt == 0 for s in snaps):
            money_text = "Деньги не изменятся: в прошлой группе нет аванса и долга"
        elif transfer and any(m["code"] == "money" for m in moves):
            money_text = carry_text or next(m["text"] for m in moves if m["code"] == "money")
    texts = [i["text"] for i in (*moves, *stays, *ends)]
    return {"kind": kind, "moves": moves, "stays": stays, "ends": ends, "money_text": money_text, "texts": texts}
