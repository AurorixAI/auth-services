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
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from src.models.auth import Group, MembershipPriceOverride, StudentGroup

from . import leave_recalc
from . import payment_status as ps
from . import payments_texts as tx
from . import pricing


PROBE_DAYS = 93          # the common month that holds today is read on a later day (a stay that begins tomorrow has not begun today)


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
    named_debt: int = 0                                                       # PAY-DUE 10.5: the unpaid sum AFTER the action, by name (the function of the term; the student as he is after it): «долг» ...
    named_due: int = 0                                                        # ... and «к оплате» (the student stays in the centre and the term of the part goes on)
    due_until: Optional[date] = None                                          # the nearest last day of that «к оплате»
    turns: int = 0                                                            # the part that is «к оплате» now and becomes a debt BY the action (the last group, an archive, a package participation that closes)
    before_debt: int = 0                                                      # the part that is already a debt before the action
    centre: bool = True                                                       # the student stays in the centre after the action (he has another group / it is a transfer)
    recalc: List[leave_recalc.RecalcItem] = field(default_factory=list)     # PAY-TAILS Б1.3: what the closing does to the charges (before = the list of the centre, after = the account with the stay closed)
    others: List[str] = field(default_factory=list)                           # PAY-TAILS Б5.2: what the closing does to the OTHER groups of the student (the day is not paid twice); filled by ``others_of``


def snapshot(db: Session, org_id: Any, memberships: Sequence[StudentGroup], day: date, *, after=None, loader=None, in_centre: Optional[bool] = None) -> List[Snap]:
    """The facts of the closing of ``memberships`` on ``day`` (read before the stays are closed; nothing is written).  ``after`` / ``loader``: the result of ``LeaveService._after`` when the caller has it.
    ``in_centre``: ``True`` for a transfer (the student stays in the centre: the terms of the periods of the old group go on); ``None`` - read from the groups he has left after the action (a leaving, an archive)."""
    from .leave_service import LeaveService  # lazy: leave_service imports this module
    from .logica_service import PauseService

    if not memberships:
        return []
    if after is None or loader is None:
        after, loader = LeaveService(db)._after(str(org_id), [m.id for m in memberships], day)
    names = {g.id: g.name for g in db.query(Group).filter(Group.id.in_({m.group_id for m in memberships})).all()}
    pauses = PauseService(db)
    ordinary = ordinary_ledgers(db, org_id, [m.id for m in memberships], day)
    out: List[Snap] = []
    for m in memberships:
        snap = Snap(membership_id=m.id, group_id=m.group_id, group_name=names.get(m.group_id, ""))
        if m.id in after:
            ledger, refundable = after[m.id]
            snap.debt, snap.advance, snap.refundable = int(ledger.debt), int(ledger.advance), int(refundable)
            _name_the_unpaid(snap, loader, m, ledger, in_centre)
            snap.recalc = leave_recalc.items(ordinary.get(m.id), ledger)
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
    if in_centre is None and out:           # a leaving / an archive: the other groups of the student that began TODAY are moved by the day this closing keeps (Б5.2)
        out[0].others = second_groups(db, org_id, memberships, day, loader)
    return out


def ordinary_ledgers(db: Session, org_id: Any, ids: Sequence[UUID], day: date) -> Dict[UUID, Any]:
    """PAY-TAILS Б5.1: the accounts of the memberships as the LIST of the centre shows them on ``day`` - the ordinary loader, no ``closing`` (the «before» of «что было -> что станет»; the account of ``LeaveService._after``
    is already closed today and cannot be it).  Read-only; a fresh loader, nothing shared with the closing one."""
    from . import billing_ledger as bl
    from . import ledger_service as ls

    loader = ls.LedgerLoader(db, str(org_id), day)
    loader.load_facts(list(ids))
    out: Dict[UUID, Any] = {}
    for mid in ids:
        inp = loader.build_input(mid)
        if inp is not None:
            out[mid] = bl.run_ledger(inp)
    return out


def second_groups(db: Session, org_id: Any, closing: Sequence[StudentGroup], day: date, loader: Any) -> List[str]:
    """PAY-TAILS Б5.2 (the rule «a day is not paid twice»): the closing of A keeps ``day`` for A, so another open group B of the same student that BEGAN today starts a day later.  ONE sentence per such group, with the
    money: before = the ordinary loader (what the list says), after = the loader with ``closing`` (the very account of the write).  Three modes: the common month - the sum falls; the personal period - the
    period begins the next day (the sum is the same); a package - today's lesson is not paid.  Nothing when B does not move (it began earlier, or the day is not a day of B)."""
    from . import billing_ledger as bl

    closing_ids = {m.id for m in closing}
    students = {m.student_id for m in closing}
    rows = (db.query(StudentGroup, Group).join(Group, Group.id == StudentGroup.group_id)
            .filter(StudentGroup.student_id.in_(students), StudentGroup.is_active.is_(True), Group.org_id == org_id, Group.status == "active", StudentGroup.id.notin_(closing_ids)).all())
    started = []
    for b, g in rows:
        period = next((p for p in b.periods if p.left_at is None), None)
        if period is not None and period.joined_at is not None and ps.business_date(period.joined_at) == day:
            started.append((b, g))
    if not started:
        return []
    from dataclasses import replace as _replace

    from . import ledger_service as ls

    ids = [b.id for b, _ in started]
    plain = ls.LedgerLoader(db, str(org_id), day)
    plain.load_facts(ids)
    loader.load_facts(ids)
    holders = sorted({loader.members[mid][2].name for mid in closing_ids if mid in loader.members and day in loader.left_days.get(loader.members[mid][1].id, {}).get(mid, set())})
    where = ", ".join(f"«{n}»" for n in holders) or "другой группе"
    out: List[str] = []

    def period_of_today(ledger: Any) -> Optional[Any]:
        return next((x for x in ledger.segments if x.kind == "period" and x.period_start <= day <= x.period_end), None)

    for b, g in sorted(started, key=lambda x: x[1].name):
        was_inp, now_inp = plain.build_input(b.id), loader.build_input(b.id)
        if was_inp is None or now_inp is None:
            continue
        was, now = bl.run_ledger(was_inp), bl.run_ledger(now_inp)
        if now.mode == bl.LESSONS:
            if any(c.day == day for c in was.counted) and not any(c.day == day for c in now.counted):
                out.append(f"Сегодняшний урок в группе «{g.name}» не оплачивается: день оплачен в группе {where}")
            continue
        if any(s.by_cell for s in now.segments):          # the common month: the sum of the month that holds today (read when it has begun: the shifted stay may begin tomorrow)
            later = day + timedelta(days=PROBE_DAYS)
            was_seg, now_seg = period_of_today(bl.run_ledger(_replace(was_inp, today=later))), period_of_today(bl.run_ledger(_replace(now_inp, today=later)))
            if was_seg is not None and now_seg is not None and was_seg.amount != now_seg.amount:
                out.append(f"В группе «{g.name}»: {tx.num(was_seg.amount)} → {tx.money(now_seg.amount)} — сегодняшний урок оплачен в группе {where}")
        elif now.first_billable is not None and was.first_billable is not None and now.first_billable != was.first_billable:
            out.append(f"В группе «{g.name}» период начнётся с {_ddmm(now.first_billable)}: сегодняшний день оплачен в группе {where}")
    return out


def recalc_of(snaps: Sequence[Snap], *, done: bool = False, named: bool = False) -> Optional[Any]:
    """The block ``recalc`` of an answer about ``snaps`` (see ``leave_recalc.lines_of``): ONE membership - its own block; several (an archive of a student) - the lines of all, each with the name of its group."""
    if len(snaps) == 1 and not named:
        return leave_recalc.lines_of(snaps[0].recalc, done)          # ``{lines, more}`` or ``None``
    lines: List[Dict[str, Any]] = []
    more = 0
    for snap in snaps:
        block = leave_recalc.lines_of(snap.recalc, done, snap.group_name)
        if block:
            lines.extend(block["lines"])
            more += block["more"]
    return {"lines": lines, "more": more} if lines else None


def _name_the_unpaid(snap: Snap, loader: Any, membership: StudentGroup, ledger: Any, in_centre: Optional[bool]) -> None:
    """PAY-DUE 10.5: the name of the unpaid sum by the state AFTER the action (``closed`` stay, the student in the centre or not) and the part the action turns from «к оплате» into a debt (against the name BEFORE it).
    One function of the term for both (``pay_due.classify``); the ledger is the account after the closing (``LeaveService._after``)."""
    from . import pay_due  # lazy: pay_due imports the ledger

    if ledger.debt <= 0:
        return
    days, lessons = loader.settings.pay_due_days, loader.settings.pay_due_lessons
    snap.centre = bool(loader.in_centre(membership.student_id)) if in_centre is None else in_centre
    named = pay_due.classify(ledger, loader.today, days, lessons, snap.centre, closed=True)
    was = pay_due.classify(ledger, loader.today, days, lessons, True, closed=False)
    snap.named_debt, snap.named_due, snap.due_until = named.debt_amount, named.due_amount, named.due_until
    snap.before_debt = was.debt_amount
    snap.turns = max(0, named.debt_amount - was.debt_amount)


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
            snap.debt = snap.named_debt = snap.named_due = snap.turns = snap.before_debt = 0
            snap.due_until = None
        elif item.action == "refund":
            snap.advance = max(0, snap.advance - int(item.amount))
    return list(snaps)


def open_balances(db: Session, org_id: Any, membership_ids: Sequence[UUID], *, in_centre: Optional[bool] = None) -> List[Tuple[Any, str, int, Any]]:
    """Money-2 T12 / T13 + PAY-DUE 10.4: the ONE rule of «what is still on the account of these memberships (the left ones too) today»: ``(ledger entry, "debt" | "due" | "advance", amount, the named sum)``,
    each as its own line - the unpaid sum is named by the function of the term (``pay_due.classify``: «долг» after the term, «к оплате» within it), a part of each is two lines.
    ``in_centre``: ``None`` - the student as he is now (the window of an archive); ``True`` - as a student who IS in the centre (the window of adding: he is about to be in this group, the term of an old part does not move).
    R3b: an advance is the sum that CAN be returned (``refundable``); nothing to return = not listed - the archive of a group and the window of adding show the same number."""
    from . import ledger_service as ls  # lazy: ledger_service imports the pricing / status modules
    from . import pay_due

    if not membership_ids:
        return []
    today = ps.business_today()
    loader = ls.LedgerLoader(db, str(org_id), today)
    out: List[Tuple[Any, str, int, Any]] = []
    for entry in loader.run(list(membership_ids)):
        due = entry.due if in_centre is None else pay_due.classify(entry.ledger, today, loader.settings.pay_due_days, loader.settings.pay_due_lessons, in_centre)
        for kind, amount in (("debt", due.debt_amount), ("due", due.due_amount), ("advance", entry.ledger.advance)):
            if amount > 0:
                out.append((entry, kind, int(amount), due))
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
    for entry, kind, amount, due in open_balances(db, org_id, ids, in_centre=True):      # the student is about to be in the centre again: the names are the ones of the account after the adding
        left_on = ps.business_date(entry.membership.left_at) if entry.membership.left_at else None
        name = entry.group.name
        row: Dict[str, Any] = {"group_id": str(entry.group.id), "group_name": name, "membership_id": str(entry.membership.id), "left_on": left_on.isoformat() if left_on else None, "kind": kind}
        until = f" до {_ddmm(due.due_until)}" if kind == "due" and money and due.due_until else ""
        sum_text = f" {tx.money(amount)}" if money else ""
        left = f", ушёл {_dmy(left_on)}" if left_on else ""
        if kind == "due":
            what = f"осталось к оплате{sum_text}{until}" if money else "осталась сумма к оплате"
        else:
            what = f"остался {'долг' if kind == 'debt' else 'аванс'}{sum_text}"
        if same_group:
            row["same_group"] = True      # stage 7 М4: the row of THIS group (the window may say «в этой группе»); the rows of other groups have no such field
            row["text"] = f"Уже был в этой группе{left} — {what}"
        else:
            row["text"] = f"В группе «{name}»{f' (ушёл {_dmy(left_on)})' if left_on else ''} {what}: смотрите её выписку"
        if money:
            row["amount"] = amount
            if kind == "due":
                row["due_until"] = due.due_until.isoformat() if due.due_until else None
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


def debt_text(snap: Snap, money: bool, kind: str = "leave", done: bool = False) -> str:
    """The words of the debt the closing leaves (PAY-DUE 10.5, by the state AFTER the action): a part that is a debt already stays a debt in the group; the part that is «к оплате» now and becomes a debt by the
    action says so BEFORE it is done (the last group, an archive of the student, a package participation that closes)."""
    g = snap.group_name
    if snap.turns > 0:
        if snap.before_debt > 0 and snap.named_due > 0:      # 17.9: only a PART becomes a debt (a package closes, the periods stay): the two names are said apart
            return (f"Долг {tx.money(snap.before_debt)} и к оплате {tx.money(snap.turns + snap.named_due)}. После ухода {tx.money(snap.turns)} станет долгом, к оплате останется {tx.money(snap.named_due)}" if money
                    else f"В группе «{g}» есть долг и сумма к оплате. После ухода часть суммы станет долгом, часть останется к оплате")
        if snap.before_debt > 0:
            return (f"Долг {tx.money(snap.before_debt)} и к оплате {tx.money(snap.turns)}. После ухода вся сумма {tx.money(snap.named_debt)} станет долгом" if money
                    else f"В группе «{g}» есть долг и сумма к оплате. После ухода вся неоплаченная сумма станет долгом")
        if kind == "archive":
            return (f"Ученик уходит из центра: вся неоплаченная сумма {tx.money(snap.named_debt)} в группе «{g}» станет долгом" if money
                    else f"Ученик уходит из центра: вся неоплаченная сумма в группе «{g}» станет долгом")
        if snap.centre:      # he stays in the centre: a package participation (or a transfer) closes, its unpaid lessons are a debt at once
            return (f"К оплате {tx.money(snap.turns)}. Участие в группе «{g}» закрывается: сумма станет долгом" if money
                    else f"В группе «{g}» есть сумма к оплате. Участие закрывается: сумма станет долгом")
        if money:           # PAY-TAILS Б1.3: the total in ONE short sentence (the sum is a debt now: «Останется долг …»)
            return (f"Долг {tx.money(snap.named_debt)} остался за учеником: это единственная группа ученика" if done
                    else f"Останется долг {tx.money(snap.named_debt)}: это единственная группа ученика")
        return f"В группе «{g}» есть сумма к оплате. Это единственная группа ученика: после ухода сумма станет долгом"
    amount = snap.named_debt or snap.debt
    if money:           # PAY-TAILS Б1.3: the total of the window in one short sentence of the server (what to do with it is the choice under it, not this line)
        return f"Долг {tx.money(amount)} остался за учеником" if done else f"Останется долг {tx.money(amount)}"
    return f"В группе «{g}» останется долг: принять или простить его можно в её выписке"


def due_text(snap: Snap, money: bool, done: bool = False) -> str:
    """The sum that stays «к оплате» after the action (the student is still in the centre; the term of the part goes on)."""
    g = snap.group_name
    if money:
        until = f" до {_ddmm(snap.due_until)}" if snap.due_until else ""
        return f"К оплате {tx.money(snap.named_due)}{until} осталось за учеником" if done else f"Останется к оплате {tx.money(snap.named_due)}{until}"
    return f"В группе «{g}» останется сумма к оплате, срок не меняется: оплатить или простить её можно в выписке группы"


def unpaid_stays(base: Dict[str, Any], s: Snap, money: bool, kind: str, done: bool = False) -> List[Dict[str, Any]]:
    """What the closing leaves unpaid in the group, one item per NAME (the debt, the sum «к оплате»); a snapshot without a name (the account has no named part) is told as a debt."""
    out: List[Dict[str, Any]] = []
    debt = s.named_debt if (s.named_debt or s.named_due) else s.debt
    if debt > 0:
        out.append({**base, "code": "debt", **({"amount": debt, "turns": s.turns} if money else {}), "text": debt_text(s, money, kind, done)})
    if s.named_due > 0:
        out.append({**base, "code": "due", **({"amount": s.named_due, "due_until": s.due_until.isoformat() if s.due_until else None} if money else {}), "text": due_text(s, money, done)})
    return out


def advance_text(snap: Snap, money: bool, done: bool = False) -> str:
    if money:           # PAY-TAILS Б1.3: the total in one short sentence; «ушедший с авансом» is on the tab «Аванс» (the choice under the line says what can be done)
        return f"Аванс {tx.money(snap.advance)} остался на счёте" if done else f"Останется аванс {tx.money(snap.advance)}"
    return f"В группе «{snap.group_name}» останется оплата вперёд: вернуть её может владелец или администратор с доступом к деньгам"


def build(
    snaps: Sequence[Snap], *, kind: str, money: bool, target_name: Optional[str] = None, carried: Optional[int] = None, package: Optional[Tuple[int, Optional[int]]] = None,
    carried_pauses: Sequence[Tuple[date, date]] = (), carry_text: Optional[str] = None, paid_until: Optional[date] = None, done: bool = False,
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
        stays.extend(unpaid_stays(base, s, money, kind, done))
        if not transfer and s.advance > 0:
            stays.append({**base, "code": "advance", **({"amount": s.advance} if money else {}), "text": advance_text(s, money, done)})
        if not transfer and not s.debt and not s.advance and not s.named_due:          # PAY-TAILS Б1.3: nothing is left to settle - say so (the window has no empty line)
            stays.append({**base, "code": "none", "text": "Расчётов нет — можно убирать" if not done else "Расчётов нет"})
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
