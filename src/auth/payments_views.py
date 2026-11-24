"""
ALGO - PAY-2: ledger -> API models (pure conversions, no database, no money logic).

``billing_ledger`` answers; this module only renders the answer in the frozen contract (``payments_schemas``) and writes the
sentences (``payments_texts``).  Nothing here decides an amount.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date, timedelta
from typing import Iterable, List, Optional, Tuple

from . import billing_ledger as bl
from . import pay_due
from . import pay_upcoming
from . import payment_status as ps
from . import payments_texts as tx
from .payments_schemas import (
    Allocation,
    AllocationSummary,
    MoneyRow,
    PauseChargeLater,
    PauseChargeRow,
    PaymentAllocationOut,
    PreviewDue,
    PreviewLessons,
    PreviewResult,
    PriceAhead,
    PriceOut,
    RecalcOut,
    RestRow,
    Segment,
    SegmentLessons,
    SegmentPart,
)

logger = logging.getLogger(__name__)


def price_out(info: bl.PriceInfo) -> PriceOut:
    return PriceOut(amount=info.amount, source=info.source, valid_from=info.valid_from,
                    group_amount=info.group_amount, reason=info.reason)


def segment_out(seg: bl.Segment, due=None) -> Segment:
    return Segment(
        parts=[SegmentPart(**p) for p in parts_of_segment(due, seg.id)] if due is not None else [],
        id=seg.id, kind=seg.kind, period_from=seg.period_start, period_to=seg.period_end, billable_days=len(seg.days),
        days_text=seg.days_text, price=price_out(seg.price), charge=seg.amount, basis=seg.basis,
        lessons=SegmentLessons(in_part=seg.lessons_in_part, in_period=seg.lessons_in_period)
        if seg.kind == "package" or seg.basis == "lessons" else None,
        paid=seg.money, written_off=seg.forgiven, recalculated=seg.recalculated, unpaid=seg.unpaid, remaining=seg.unpaid, covered_days=seg.covered_days,
        covered_through=seg.covered_through, partial_credit=seg.partial_credit, state=seg.state, bucket=seg.bucket,
    )


def recalc_out(line: bl.RecalcLine) -> RecalcOut:
    return RecalcOut(lesson_date=line.day, lesson_id=line.lesson_id, amount=line.amount, text=tx.recalc_text(line.amount, line.day))


def allocation_out(a: bl.Allocation) -> Allocation:
    return Allocation(
        segment_id=a.segment_id, period_from=a.period_start, period_to=a.period_end, amount=a.amount, days=a.days,
        bucket=a.bucket, closes=a.closes, label=a.label, price=a.price,
        segment_remaining_after=a.remaining_after,
    )


def allocations_of(ledger: bl.Ledger, credit_id: str) -> List[Allocation]:
    return [allocation_out(a) for a in ledger.allocations if a.credit_id == credit_id]


def payment_allocation(ledger: bl.Ledger, payment_id: str) -> PaymentAllocationOut:
    items = allocations_of(ledger, payment_id)
    refunded = ledger.refunded_from.get(payment_id, 0)
    moved = ledger.transferred_from.get(payment_id, 0)
    net = ledger.net_money.get(payment_id, 0)
    return PaymentAllocationOut(items=items, refunded=refunded, transferred=moved, net=net)


def allocation_summary(before: bl.Ledger, after: bl.Ledger, credit_id: str) -> AllocationSummary:
    return AllocationSummary(
        items=allocations_of(after, credit_id), unpaid_before=before.debt, unpaid_after=after.debt,
        advance_after=after.advance, paid_until_before=before.status.paid_until,
    )


def shim_period(ledger: bl.Ledger, payment_id: str):
    """``valid_from`` / ``valid_to`` for the OLD front (one release): the first period the payment reached and "paid until"."""
    items = [a for a in ledger.allocations if a.credit_id == payment_id and a.segment_id != bl.ADVANCE_SEGMENT]
    first = min((a.period_start for a in items), default=None)
    return first, ledger.status.paid_until


BUCKET_DEBTORS, BUCKET_TO_PAY, BUCKET_EXPIRING, BUCKET_ACTIVE, BUCKET_PAUSED = "debtors", "to_pay", "expiring", "active", "paused"
BUCKET_ADVANCE, BUCKET_LEFT_OPEN = "advance", "left_open"      # PAY-TAILS: the tab «Аванс» (every membership of the centre that holds an advance) and the closed memberships of a group that still have a settlement
BUCKET_DEBT_ALIAS = "debt"      # the old name of the debtors tab: an alias of ``debtors`` on input only (a link of the old front)
UNPAID_TABS = (BUCKET_DEBTORS, BUCKET_TO_PAY)        # the tabs of an unpaid sum: they are NOT exclusive (a student with both a debt and a sum within its term is in both) and read the closed participations too
CLOSED_TABS = (*UNPAID_TABS, BUCKET_ADVANCE, BUCKET_LEFT_OPEN)       # the tabs that read ALL the memberships of the centre, the closed ones too
STATE_DEBT, STATE_TO_PAY, STATE_EXPIRING, STATE_PAUSED, STATE_PAID, STATE_FREE, STATE_NONE = "debt", "to_pay", "expiring", "paused", "paid", "free", "none"
STATE_STARTS, STATE_SETTLED, STATE_NO_CHARGES = "starts", "settled", "no_charges"       # PAY-TAILS Б5.4: the charge has not begun / a closed membership with nothing left / one that never held anything
_MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь")


def is_tracked(e) -> bool:
    """The membership is on the status lists: active, in an active group, the student not archived."""
    return bool(e.membership.is_active and e.group.status == "active" and e.student.status != "archived")


def price_missing(e) -> bool:
    """С11: the student has NO price - neither a current one nor a planned one (a group row or an individual row that begins later).
    A planned price is a price the owner has set (``Цена с 25.11``): the warning «не задана цена» must not tell him otherwise.  Charges and debts are not touched."""
    if e.price.amount is not None:
        return False
    return e.inp.first_priced_day(e.inp.today) is None


def calm_bucket(e) -> Optional[str]:
    """The tab of a membership that owes NOTHING (PAY-DUE 10.2): ``paused`` (today is inside a pause), ``expiring`` («Заканчивается оплата»: the next period is due within ``expiring_days`` /
    the lessons are about to end), ``active`` («Без долга»: nothing unpaid and the payment does not end soon, an advance included).  A free student and a student without a price are on no tab
    (counted apart: ``free``, ``price_missing``); a membership that is not tracked is on none.  A membership with an unpaid sum is on ``debtors`` and / or ``to_pay`` (``in_tab``), never here."""
    l = e.ledger
    if e.due.unpaid > 0 or not is_tracked(e) or l.status.status == ps.FREE or e.price.amount is None:
        return None
    if l.status.status == ps.PAUSED:
        return BUCKET_PAUSED
    if l.status.status in (ps.EXPIRING, ps.ENDS_TODAY, ps.EXPIRED) and not nothing_covered(l):       # PAY-TAILS Б6.3: nothing is paid, so nothing «ends» (a future pause / start): «Без долга»
        return BUCKET_EXPIRING
    return BUCKET_ACTIVE         # active, and «none» (nothing has fallen due yet)


def in_tab(e, tab: str) -> bool:
    """PAY-DUE 9.1: the tabs of the unpaid sum are the two names of the ONE function - ``debtors``: a debt is owed; ``to_pay``: a sum within its term is owed (a debtor with such a sum too is in both);
    the other tabs: ``calm_bucket``."""
    if tab == BUCKET_DEBTORS:
        return e.due.debt_amount > 0
    if tab == BUCKET_TO_PAY:
        return e.due.due_amount > 0
    if tab == BUCKET_ADVANCE:                 # PAY-TAILS Б5.5: ONE function of the tab, shared by the list, the summary and the old ``balances``: the membership holds an advance (open or closed)
        return e.ledger.advance > 0
    if tab == BUCKET_LEFT_OPEN:               # a CLOSED membership with any settlement: a debt, a sum «к оплате» or an advance
        return is_closed(e) and (e.due.debt_amount > 0 or e.due.due_amount > 0 or e.ledger.advance > 0)
    return calm_bucket(e) == tab


def is_closed(e) -> bool:
    """PAY-TAILS Б5.4: the membership is CLOSED - it is not tracked (the student left, the group or the student is in the archive) or it is being closed by the action the window is about (``closing``).  The state of a closed
    membership does not depend on the hour of the day: it never says «оплачено до»."""
    return (not is_tracked(e)) or (e.loader is not None and e.membership.id in e.loader.closing)


def left_on(e) -> Optional[date]:
    """The day a closed membership ended (a membership that is being closed today ends today); ``None`` for an open one or when the archive, not a leaving, closed it."""
    if e.membership.is_active:
        return e.inp.today if (e.loader is not None and e.membership.id in e.loader.closing) else None
    return ps.business_date(e.membership.left_at) if e.membership.left_at else None


def nothing_happened(l: bl.Ledger) -> bool:
    """«Расчётов нет» (PAY-TAILS Б5.4): nothing was ever charged AND no money line was ever entered (a student who paid and got everything back has «Расчёт закрыт», not this)."""
    return not (l.charged or l.paid or l.transferred_in or l.written_off_granted or l.refunded or l.transferred_out or l.recalculated)


def nothing_covered(l: bl.Ledger) -> bool:
    """PAY-TAILS Б6.3: a calendar account that no money, forgiveness, recalculation or opening balance covers by a single day (a student who paid nothing and has a pause or a start in the future): there is no
    «оплачено до» to say, and nothing that ends soon.  ``status`` and ``paid_until`` are not touched - the words and the tab only."""
    return l.status.period_mode != bl.LESSONS and l.covered_by is None and l.covered_days_total == 0 and not (l.opening is not None and l.opening.paid_until is not None)


def cover_until(l: bl.Ledger, closed: bool = False) -> Optional[date]:
    """The day a text may name as «оплачено до»: the calendar coverage that stands on the BORDER of a period (a day inside a period is told in sums), and never for a closed membership or an account that
    nothing covers (Б6.3)."""
    if closed or l.status.period_mode == bl.LESSONS or l.cover_partial is not None or nothing_covered(l):
        return None
    return cover_day(l)


def cover_day(l: bl.Ledger) -> Optional[date]:
    """The day the words name: ``status.paid_until``, except a date inside a period that has not begun and is paid only in part (Б6.4) - then the border of the last period covered in full (``status`` itself is not touched)."""
    return l.cover_ahead[0] if l.cover_ahead is not None else l.status.paid_until


def partial_words(l: bl.Ledger, due, closed: bool = False) -> Optional[str]:
    """The words of a period that is paid only in part (``оплачено 100 000 из 420 000 сум · к оплате 320 000 сум до 23.11``); ``None`` when the coverage is whole or the membership is closed (a closed one says its state)."""
    if closed or l.cover_partial is None or l.mode == bl.LESSONS:
        return None
    paid, forgiven, charge = l.cover_partial
    return tx.partial_cover_text(paid, forgiven, charge, due.debt_amount, due.due_amount, due.due_until)


def cover_phrase(l: bl.Ledger, due, closed: bool = False) -> Optional[str]:
    """The line of the RECEIPT (PAY-TAILS Б1.5): ``оплачено до 30.11`` for a period closed by money (or the days of an opening balance); sums for a period paid in part; nothing for a closed membership, a period closed by
    the forgiveness or by a recalculation."""
    if closed:
        return None
    partial = partial_words(l, due)
    if partial:
        return partial
    until = cover_until(l)
    if until is None or l.covered_by not in ("money", "opening", None):
        return None
    return f"{tx.cover_words(l.covered_by).lower()} {tx.ddmm(until)}"


def forward_phrase(l: bl.Ledger, due, closed: bool = False) -> Optional[str]:
    """The quiet line of the money paid ahead of a period that has not begun (``вперёд оплачено 98 000 сум``), said next to the coverage of the RECEIPT; ``None`` when the coverage says no day (a part, a closed membership) or nothing
    is ahead.  Б11.24 п. 4: whether the state says the same sum as its advance is for ``receipt_state_text`` to decide - by the meaning, not by two sums that happen to be equal."""
    phrase = cover_phrase(l, due, closed)
    if phrase is None or partial_words(l, due) or l.cover_ahead is None or l.cover_ahead[1] <= 0:
        return None
    return tx.paid_forward_text(l.cover_ahead[1])


def starts_info(e) -> Tuple[Optional[pay_upcoming.Upcoming], Optional[dict]]:
    """PAY-TAILS Б5.3: ``(forecast, package wait)`` of an open membership whose charge has not begun; ``(None, None)`` for everything else (a closed one, one that owes something, a pause, a free one, a student with no price).
    Reads the ledger input of the row; builds no loader and writes nothing."""
    l = e.ledger
    if e.loader is None or is_closed(e) or e.due.unpaid > 0 or e.price.amount in (None, 0) or l.status.status in (ps.PAUSED, ps.FREE):
        return None, None
    settings = e.loader.settings
    return (pay_upcoming.forecast(e.inp, l, settings.pay_due_days, settings.pay_due_lessons),
            pay_upcoming.package_wait(e.inp, l, e.price.amount, settings.pay_due_lessons))


def upcoming_payload(up: pay_upcoming.Upcoming, today: date, left_names, money: bool = True) -> dict:
    """The forecast as the answer says it (the keys of ``UpcomingOut``); without the right to money only the date.  ``left_names``: the groups the student left TODAY with the day charged there - the reason a forecast that
    begins tomorrow was moved («сегодня ученик ещё числился в «Математике 7»»)."""
    names = sorted(left_names or ())
    note = tx.terms_note_text(names) if names and up.begin == today + timedelta(days=1) else None
    if not money:
        return {"from": up.begin, "amount": None, "lessons_text": None, "due_until": None, "text": None, "sub": None, "terms_note": None}
    return {"from": up.begin, "amount": up.amount, "lessons_text": up.lessons_text, "due_until": up.due_until, "text": up.text, "sub": up.sub or None, "terms_note": note}


def upcoming_dict(e, up: pay_upcoming.Upcoming, money: bool = True) -> dict:
    today = e.inp.today
    return upcoming_payload(up, today, e.loader.left_names.get(e.student.id, {}).get(today, set()) if e.loader is not None else (), money)


def state_of(e, info: Tuple[Optional[pay_upcoming.Upcoming], Optional[dict]] = (None, None)) -> str:
    """The WORST state of a membership, for the single label (PAY-DUE 10.2): debt -> to pay -> (closed: advance -> settled -> no charges) -> payment ends soon -> pause -> paid / advance -> free -> no price.  The screen does not choose it."""
    d, l = e.due, e.ledger
    if d.debt_amount > 0:
        return STATE_DEBT
    if d.due_amount > 0:
        return STATE_TO_PAY
    if is_closed(e):                       # PAY-TAILS Б5.4: right after the debt and the sum «к оплате», whatever the day: «Аванс N» -> «Расчёт закрыт» -> «Расчётов нет»
        return STATE_PAID if l.advance > 0 else STATE_NO_CHARGES if nothing_happened(l) else STATE_SETTLED
    if info[0] is not None or info[1] is not None:      # PAY-TAILS Б5.3: the charge has not begun («Начнётся 24.11» / «Уроков ещё не было»); the tab and the sums do not change (``status`` is untouched)
        return STATE_STARTS
    st = l.status.status
    if st in (ps.EXPIRING, ps.ENDS_TODAY, ps.EXPIRED):
        return STATE_EXPIRING
    if st == ps.PAUSED:
        return STATE_PAUSED
    if st == ps.FREE:
        return STATE_FREE
    if st == ps.NONE and l.advance <= 0:
        return STATE_NONE
    return STATE_PAID


def unpaid_status(e) -> str:
    """The ``status`` of a row (PAY-DUE 3.2).  The paid-until statuses (``expired`` / ``ends_today`` / ``expiring``) describe the PAID days, not the unpaid sum: they stay only for «Заканчивается оплата» at a zero
    balance; a row that owes something is ``active`` (``paused`` / ``free`` / ``none`` are kept).  The name of the sum is ``state``."""
    return status_without_paid_until(e.ledger.status.status, e.due.unpaid)


def status_without_paid_until(status: str, unpaid: int) -> str:
    return ps.ACTIVE if unpaid > 0 and status in (ps.EXPIRED, ps.ENDS_TODAY, ps.EXPIRING) else status


DEBT_AGE_BASKETS = ("to_30", "31_60", "61_90", "over_90")       # Money-2 T18: the baskets of the age of a debt in days: up to 30 | 31-60 | 61-90 | over 90


def debt_age_basket(days: int) -> str:
    """Money-2 T18: the basket of a debt that is ``days`` old (30 -> to_30, 31 -> 31_60, 60 -> 31_60, 61 -> 61_90, 90 -> 61_90, 91 -> over_90)."""
    return "to_30" if days <= 30 else "31_60" if days <= 60 else "61_90" if days <= 90 else "over_90"


def unpaid_fields(e, today: date, info: Optional[Tuple[Optional[pay_upcoming.Upcoming], Optional[dict]]] = None) -> dict:
    """The words and numbers of the unpaid sum of a row, ALL from ``pay_due`` and the sentences of ``payments_texts``: ``debt_amount`` / ``due_amount`` / ``due_until`` / ``overdue_days`` / ``due_lessons`` /
    ``debt_lessons`` and the texts of both names (which of the two is the main one is the choice of the tab: a view, not a rule), ``state`` / ``state_text`` of the worst state."""
    d, l = e.due, e.ledger
    debt_n, due_n = d.lessons_said(pay_due.DEBT), d.lessons_said(pay_due.DUE)
    closed, gone = is_closed(e), left_on(e)
    paid_until = cover_until(l, closed)           # «оплачено до» by the CURRENT mode of the participation (17.2); PAY-TAILS Б5.4: only on the border of a period, never for a closed membership
    out = {
        "debt_amount": d.debt_amount, "due_amount": d.due_amount, "due_until": d.due_until,
        "overdue_days": d.overdue_days if d.debt_amount > 0 else None,
        "due_lessons": d.due_lessons if d.due_amount > 0 or d.debt_amount > 0 else None, "debt_lessons": d.debt_lessons,
        "debt_text": tx.debt_text(d.debt_amount, debt_n) if d.debt_amount > 0 else None,
        "debt_sub": tx.debt_sub(d.overdue_days, paid_until, l.covered_by, gone) if d.debt_amount > 0 else None,
        "due_text": tx.due_text(d.due_amount, due_n) if d.due_amount > 0 else None,
        "due_sub": _with_left(gone, tx.package_term_words(d.due_until, today, due_n, (d.due_lessons or {}).get("limit"))) if d.due_amount > 0 else None,
        # the second line of a row that holds BOTH sums (the tab decides which is the main one): null when the row has no second sum
        "debt_also_text": tx.debt_also_text(d.debt_amount, debt_n, d.overdue_days) if d.debt_amount > 0 and d.due_amount > 0 else None,
        "due_also_text": tx.due_also_text(d.due_amount, due_n, d.due_until, tx.package_term_words(d.due_until, today, due_n, (d.due_lessons or {}).get("limit")) if due_n else None) if d.debt_amount > 0 and d.due_amount > 0 else None,
    }
    info = starts_info(e) if info is None else info       # PAY-TAILS Б6.8: a caller that needs the reading again (the statement) reads it once and passes it
    state = state_of(e, info)
    out["state"] = state
    out["state_text"] = state_text(e, state, out, today, info)
    out["state_sub"] = state_sub(e, state, gone, info)
    out["upcoming"] = upcoming_dict(e, info[0]) if info[0] is not None else None
    out["covered_by"] = l.covered_by
    out["left_on"] = gone
    return out


def closed_words(e, gone: Optional[date]) -> Optional[str]:
    """Why a closed membership is closed, in words without a verb that has a gender where it can be helped: ``ушёл 28.10`` (a leaving), ``группа в архиве``, ``ученик в архиве``."""
    if gone:
        return tx.left_text(gone)
    if e.group.status != "active":
        return "группа в архиве"
    if e.student.status == "archived":
        return "ученик в архиве"
    return None


def tab_words(e, tab: str) -> Tuple[Optional[str], Optional[str]]:
    """``(tab_text, tab_sub)`` of a row on the tab «Аванс» (PAY-TAILS Б5.5): the sum of the advance and WHY it stands there - a closed membership says it is closed, an open one how far it is paid.
    The other tabs have none (their words are ``state_text`` / ``state_sub``)."""
    if tab != BUCKET_ADVANCE:
        return None, None
    l = e.ledger
    if is_closed(e):
        return tx.advance_text(l.advance), closed_words(e, left_on(e))
    if l.status.period_mode == bl.LESSONS and l.status.lessons_left is not None:
        return tx.advance_text(l.advance), tx.lessons_left_text(l.status.lessons_left).lower()
    until = tx.cover_text(l.covered_by, cover_day(l))
    return tx.advance_text(l.advance), until[:1].lower() + until[1:] if until else None


def _with_left(gone: Optional[date], words: Optional[str]) -> Optional[str]:
    """``ушёл 20.11 · до 22.11``: a closed membership says it is closed before it says its term."""
    return " · ".join(x for x in (tx.left_text(gone) if gone else None, words) if x) or None


def state_sub(e, state: str, gone: Optional[date], info: Tuple[Optional[pay_upcoming.Upcoming], Optional[dict]] = (None, None)) -> Optional[str]:
    """The quiet line under the words of the state (PAY-TAILS Б5.4): a closed membership - ``ушёл 28.10``; an open one covered by money and a forgiveness - ``из них прощено 300 000 сум``."""
    l = e.ledger
    if is_closed(e):
        return closed_words(e, gone)
    if state == STATE_STARTS:
        if info[0] is not None:
            return tx.paid_ahead_text(l.advance) if l.advance > 0 else f"≈ {tx.money(info[0].amount)}"
        return pay_upcoming.package_text(info[1]) if info[1] else None
    if state in (STATE_PAID, STATE_EXPIRING) and l.status.period_mode != bl.LESSONS:
        words = [tx.forgiven_inside_text(l.forgiven_in_last)] if l.covered_by == "money" and l.forgiven_in_last > 0 else []
        if l.cover_ahead is not None and l.cover_ahead[1] > 0:           # Б6.4: the part of the next period that was paid ahead of its start
            words.append(tx.paid_forward_text(l.cover_ahead[1]))
        return " · ".join(words) or None
    return None


def state_text(e, state: str, fields: dict, today: date, info: Tuple[Optional[pay_upcoming.Upcoming], Optional[dict]] = (None, None)) -> Optional[str]:
    """The single label of the worst state."""
    l = e.ledger
    if state == STATE_DEBT:
        return fields["debt_text"]
    if state == STATE_TO_PAY:
        return fields["due_text"]
    if state == STATE_STARTS:
        return f"Начнётся {tx.ddmm(info[0].begin)}" if info[0] is not None else "Уроков ещё не было"
    if state == STATE_SETTLED:
        return tx.SETTLED_TEXT
    if state == STATE_NO_CHARGES:
        return tx.NO_CHARGES_TEXT
    if state == STATE_FREE:
        return "Бесплатно"
    if state == STATE_NONE:
        return "Нет цены" if price_missing(e) else None
    if state == STATE_PAUSED:
        until = l.status.paused_until
        return f"На паузе до {tx.ddmm(until)}" if until else "На паузе"
    if state == STATE_EXPIRING:
        if l.status.period_mode == bl.LESSONS and l.status.lessons_left is not None:
            return tx.lessons_left_text(l.status.lessons_left)
        return tx.cover_text(l.covered_by, cover_day(l))
    if state == STATE_PAID and is_closed(e) and l.advance > 0:           # a closed membership with money left: the sum, never a day
        return tx.advance_text(l.advance)
    if l.status.period_mode == bl.LESSONS and l.status.lessons_left is not None:      # a paid package: how many lessons are left (before the advance: the money of a package IS the lessons)
        return tx.lessons_left_text(l.status.lessons_left)
    day = cover_day(l)
    if l.advance > 0 and not day:
        return tx.advance_text(l.advance)
    return tx.cover_text(l.covered_by, day) if day else (tx.advance_text(l.advance) if l.advance > 0 else None)


def parts_of_segment(due, seg_id: str) -> list:
    """``parts[]`` of a statement line of a charge: the unpaid part(s) of THAT segment by name (a package segment may hold both names: two parts, the debt first)."""
    return [{"name": p.name, "unpaid": p.unpaid, "due_until": p.due_until, "lessons": p.lessons} for p in due.parts_of(seg_id)]


def _advance_left(ledger: bl.Ledger, segments: dict, credit_id: str) -> int:
    """The part of the money of one credit that is laid on NO begun charge: on the «advance» of no period yet, or on a period that has not begun."""
    return sum(a.amount for a in ledger.allocations if a.credit_id == credit_id and (a.segment_id == bl.ADVANCE_SEGMENT or (a.segment_id in segments and not segments[a.segment_id].begun)))


def recalc_advance(ledger: bl.Ledger, segments: dict) -> dict:
    """PAY-DUE 17.1: a recalculation (the deduction for a lesson the centre cancelled) frees money like a payment does: when the month was paid and the lesson is taken off, the freed sum is an advance and the
    row of the recalculation carries it.  The ledger gives a recalculation no allocation of its own, so the advance that the payments and transfers do not explain (``ledger.advance`` minus their parts) is laid on the
    recalculations, the NEWEST first (the money that came last is the money that overflows), each not above its own amount.  By construction the advances of the rows add up to the advance of the summary."""
    lines = sorted(ledger.recalcs, key=lambda line: (line.day, line.id))
    if not lines or ledger.advance <= 0:
        return {}
    placed = sum(_advance_left(ledger, segments, e.ref_id) for e in ledger.statement if e.kind in (bl.PAYMENT, bl.TRANSFER_IN))
    rest = max(0, int(ledger.advance) - placed)
    out: dict = {}
    for line in reversed(lines):
        take = min(rest, int(line.amount))
        if take > 0:
            out[line.id] = take
            rest -= take
    return out


def open_of_row(kind: str, ref_id: str, ledger: bl.Ledger, due, segments: dict, today: date, recalcs: Optional[dict] = None) -> dict:
    """PAY-DUE 10.11: ``open_amount`` / ``open_name`` / ``open_text`` / ``open_parts`` of a statement row (see ``StatementRow``).  A charge whose unpaid part holds BOTH names (a package) has two ``open_parts``
    (the debt first), ``open_name`` is the worst of them and ``open_text`` says both.  ``open_sub`` is the term of the row in words: «срок был 05.11» for the debt part (only when the term has really passed: a student
    who left before the term has a debt whose term is still ahead - nothing is said), «до 24.11» / «до сегодня» for the part «к оплате», both joined by « · » (the debt first); ``None`` for a paid charge and for the opening balance."""
    none = {"open_amount": None, "open_name": None, "open_text": None, "open_sub": None, "open_parts": []}
    if kind in ("charge", "opening_debt"):
        seg = segments[ref_id]
        parts = due.parts_of(seg.id)
        debt = sum(p.unpaid for p in parts if p.name == pay_due.DEBT)
        owed = sum(p.unpaid for p in parts if p.name == pay_due.DUE)
        out = []
        if debt > 0:
            out.append({"name": "debt", "amount": debt, "text": tx.debt_text(debt, sum(p.lessons or 0 for p in parts if p.name == pay_due.DEBT) if all(p.kind == "package" for p in parts if p.name == pay_due.DEBT) else None)})
        if owed > 0:
            out.append({"name": "due", "amount": owed, "text": tx.due_text(owed, sum(p.lessons or 0 for p in parts if p.name == pay_due.DUE) if all(p.kind == "package" for p in parts if p.name == pay_due.DUE) else None)})
        if not out:
            # 17.7: «paid» only for a charge closed by MONEY; a charge closed by a forgiveness alone is «forgiven»; PAY-TAILS Б6.12: closed by money AND a used forgiveness together is «closed» («Закрыто»: «Оплачено»
            # is about money only); closed to zero by a recalculation (or free) - nothing: the row of the recalculation says why
            if seg.money > 0 and seg.forgiven > 0:
                out = [{"name": "closed", "amount": 0, "text": tx.CLOSED_TEXT}]
            elif seg.money > 0:
                out = [{"name": "paid", "amount": 0, "text": "Оплачено"}]
            elif seg.forgiven > 0:
                out = [{"name": "forgiven", "amount": 0, "text": "Прощено"}]
            else:
                return none
        text = tx.open_both_text(out[0]["text"], out[1]["text"]) if len(out) == 2 else out[0]["text"]
        was = [p.term or p.lapsed for p in parts if p.name == pay_due.DEBT and (p.term or p.lapsed) and (p.term or p.lapsed) < today]
        until = [p.due_until for p in parts if p.name == pay_due.DUE and p.due_until]
        owed_parts = [p for p in parts if p.name == pay_due.DUE]
        said = sum(p.lessons or 0 for p in owed_parts) if owed_parts and all(p.kind == "package" for p in owed_parts) else None
        words = ([f"срок был {tx.ddmm(min(was))}"] if was else []) + ([tx.package_term_words(min(until), today, said, (due.due_lessons or {}).get("limit"))] if until else [])
        return {"open_amount": sum(p["amount"] for p in out), "open_name": out[0]["name"], "open_text": text, "open_sub": " · ".join(words) or None, "open_parts": out}
    if kind in (bl.PAYMENT, bl.TRANSFER_IN):      # the money that came (a payment, a transfer into the group) - the part not laid on a charge yet is «Аванс»; a transfer OUT, a refund, a forgiveness stay empty
        # the part of the money that is laid on NO begun charge: on the «advance» of no period yet, or on a period that has not begun (the bucket of an allocation is relative to the DAY of the payment: a period that has begun since is no advance)
        left = _advance_left(ledger, segments, ref_id)
        if left > 0:
            text = tx.advance_text(left)
            return {"open_amount": left, "open_name": "advance", "open_text": text, "open_sub": None, "open_parts": [{"name": "advance", "amount": left, "text": text}]}
    if kind == bl.RECALC and recalcs and recalcs.get(ref_id):
        text = tx.advance_text(recalcs[ref_id])
        return {"open_amount": recalcs[ref_id], "open_name": "advance", "open_text": text, "open_sub": None, "open_parts": [{"name": "advance", "amount": recalcs[ref_id], "text": text}]}
    return none


def due_block(before: bl.Ledger, due, today: date, closed: bool = False) -> PreviewDue:
    """PAY-DUE 10.2: what the student owes today (before the typed amount), named by ``pay_due``: the old ``past_debt`` / ``current_due`` are gone."""
    shown = [segment_out(s, due) for s in before.segments if s.begun and s.unpaid > 0]
    debt_n, due_n = due.lessons_said(pay_due.DEBT), due.lessons_said(pay_due.DUE)
    return PreviewDue(
        unpaid=before.debt, debt_amount=due.debt_amount, due_amount=due.due_amount, due_until=due.due_until,
        overdue_days=due.overdue_days if due.debt_amount > 0 else None,
        debt_text=tx.debt_text(due.debt_amount, debt_n) if due.debt_amount > 0 else None,
        debt_sub=tx.debt_sub(due.overdue_days, cover_until(before, closed), before.covered_by) if due.debt_amount > 0 else None,
        due_text=tx.due_text(due.due_amount, due_n) if due.due_amount > 0 else None,
        due_sub=tx.package_term_words(due.due_until, today, due_n, (due.due_lessons or {}).get("limit")) if due.due_amount > 0 else None,
        advance=before.advance, segments=shown, paid_until=before.status.paid_until,
    )


def next_period_start(before: bl.Ledger) -> Optional[date]:
    """Money-1 G-15: the first day of the period that comes after the last one the ledger has built - when the account owes nothing and every built period is paid (calendar); otherwise ``None``."""
    if before.debt > 0 or before.mode == bl.LESSONS or not before.segments:
        return None
    if any(seg.unpaid > 0 and not seg.unpriced for seg in before.segments):
        return None
    return before.segments[-1].period_end + timedelta(days=1)


def suggested_amount(before: bl.Ledger, price_today: Optional[int], price_next: Optional[int] = None) -> int:
    """«Всего к оплате»: the debt; with no debt the next unpaid period (calendar; ``price_next`` - its price when the current one is paid exactly and the next is not built yet) / a package without the
    partial credit."""
    if before.debt > 0:
        return before.debt
    if before.mode == bl.LESSONS:
        return max(0, (price_today or 0) - before.partial_credit)
    for seg in before.segments:
        if seg.unpaid > 0 and not seg.unpriced:
            return seg.unpaid
    return price_next if price_next is not None else (price_today or 0)


def price_ahead(after: bl.Ledger, credit_id: str, current: Optional[int]) -> List[PriceAhead]:
    """D8: periods that this payment reaches in advance whose price differs from today's."""
    out: List[PriceAhead] = []
    seen = set()
    by_id = {s.id: s for s in after.segments}
    for a in after.allocations:
        if a.credit_id != credit_id or a.bucket != bl.ADVANCE or a.segment_id == bl.ADVANCE_SEGMENT:
            continue
        seg = by_id.get(a.segment_id)
        if seg is None or seg.price.amount is None or seg.price.amount == current or seg.id in seen:
            continue
        seen.add(seg.id)
        out.append(PriceAhead(period_from=seg.period_start, period_to=seg.period_end, amount=seg.price.amount,
                              current_amount=current))
    return out


def paid_by_name(due, raw_allocs) -> Tuple[int, int]:
    """``(to debt, to «к оплате»)``: how much of a payment settles the parts that were a debt / within the term BEFORE it (the oldest first inside a segment: the debt part is the older one)."""
    to_debt = to_due = 0
    for a in raw_allocs:
        left = a.amount
        for part in due.parts_of(a.segment_id):
            take = min(left, part.unpaid)
            if part.name == pay_due.DEBT:
                to_debt += take
            else:
                to_due += take
            left -= take
    return to_debt, to_due


def period_label(seg: bl.Segment, today: date, *, advance: bool = False) -> str:
    """The name of a period in the words of the server (Б11.33): ``Сентябрь`` (a whole calendar month; with the year when it is not this year: ``Декабрь 2025``), ``15.10–14.11`` (any other period), ``Пакет № 3``,
    ``Долг на начало учёта``; ``advance``: ``Аванс на январь`` / ``Аванс на 15.11–14.12`` / ``Аванс: уроки следующего пакета``."""
    if seg.kind == "opening":
        return "Долг на начало учёта"
    if seg.kind == "package":
        return "Аванс: уроки следующего пакета" if advance else f"Пакет № {seg.package_no}" if seg.package_no is not None else "Пакет"
    first, last = seg.period_start, seg.period_end
    if seg.by_cell and first.day == 1 and (last + timedelta(days=1)).day == 1 and (first.year, first.month) == (last.year, last.month):
        name = tx.MONTHS_NOMINATIVE[first.month - 1] + (f" {first.year}" if first.year != today.year else "")
        return f"Аванс на {name}" if advance else name.capitalize()
    name = tx.period_text(first, last)
    return f"Аванс на {name}" if advance else name


def _folding_kind(seg: bl.Segment) -> str:
    """Б11.36 п. 6: the KIND of a period for the folding of closed lines: a package, a calendar month (named by its month), a personal period (named by dates), the opening debt (never folded)."""
    if seg.kind == "package":
        return "package"
    if seg.kind == "opening":
        return "opening"
    return "month" if seg.by_cell and seg.period_start.day == 1 and (seg.period_end + timedelta(days=1)).day == 1 and (seg.period_start.year, seg.period_start.month) == (seg.period_end.year, seg.period_end.month) else "personal"


def _next_period(prev: bl.Segment, seg: bl.Segment) -> bool:
    """``seg`` comes right after ``prev`` and is of the same kind: a package whose number is the next one, a period that begins the day after ``prev`` ends (a month after a month, a personal period after a personal one)."""
    kind = _folding_kind(prev)
    if kind != _folding_kind(seg) or kind == "opening":
        return False
    if kind == "package":
        return prev.package_no is not None and seg.package_no == prev.package_no + 1
    return seg.period_start == prev.period_end + timedelta(days=1)


def money_rows(ledger: bl.Ledger, credit_id: str, today: date) -> List[MoneyRow]:
    """THE layout of one payment by the periods (Б11.33 п. 2, lesson 174): the window «Внести оплату» (the probe) and the receipt (the written payment) read the same pieces of the ledger.  From the oldest period; a period
    that was already paid in part before this payment is named «…, остаток»; the money that reached no begun period is «Аванс»; closed periods in a row IN THE CALENDAR (no «остаток»; Б11.35 М2: the next begins the day after the previous ends, of one kind; packages by their numbers) are folded into one line."""
    by_id = {s.id: s for s in ledger.segments}
    pieces: List[Tuple[Optional[bl.Segment], bl.Allocation]] = []
    for a in ledger.allocations:
        if a.credit_id != credit_id or a.amount <= 0:
            continue
        pieces.append((by_id.get(a.segment_id), a))
    merged: List[List] = []                                     # one line per segment (a payment can touch it by several pieces)
    for seg, a in pieces:
        for m in merged:
            if m[0] is seg and m[0] is not None:
                m[1] += a.amount
                m[2] = a.remaining_after
                break
        else:
            merged.append([seg, a.amount, a.remaining_after, a.segment_id])
    rows: List[Tuple] = []
    advance = 0
    advance_lines: List[Tuple] = []
    for seg, amount, remaining, segment_id in merged:
        if segment_id == bl.ADVANCE_SEGMENT or seg is None or not seg.begun:
            if seg is None:
                if segment_id != bl.ADVANCE_SEGMENT:        # Б11.35 М3: a piece that points at no period of the account is a defect of the ledger, not the advance: said in the log, shown as «Аванс» (the sum of the lines stays the payment)
                    logger.error("money_rows: the piece of the payment %s (%s) points at the period %s that is not in the account", credit_id, amount, segment_id)
                advance += amount
            else:
                advance_lines.append((seg.period_start, MoneyRow(label=period_label(seg, today, advance=True), amount=amount, state="advance")))
            continue
        left = remaining if remaining is not None else seg.unpaid
        of = left + amount                                      # what was unpaid of the period before this payment
        rest = seg.amount - of > 0                              # it was paid in part before: «, остаток»
        label = period_label(seg, today) + (", остаток" if rest else "")
        closed = left <= 0
        rows.append((seg.period_start, MoneyRow(label=label, amount=amount, of=None if closed else of, state="closed" if closed else "part"), rest, seg))
    rows.sort(key=lambda r: r[0])
    out: List[MoneyRow] = []
    run: List[Tuple] = []

    def flush() -> None:
        if len(run) == 1:
            out.append(run[0][1])
        elif run:
            first, last = run[0][3], run[-1][3]
            if first.kind == "package":
                label = f"Пакеты № {first.package_no}–{last.package_no}"
            elif _folding_kind(first) == "month":
                end = period_label(last, today)
                label = f"{period_label(first, today)} – {end[0].lower()}{end[1:]}"
            else:
                label = f"{tx.ddmm(first.period_start)}–{tx.ddmm(last.period_end)}"
            out.append(MoneyRow(label=label, amount=sum(r[1].amount for r in run), state="closed", periods=len(run)))
        run.clear()

    for r in rows:
        if r[1].state == "closed" and not r[2]:
            if run and not _next_period(run[-1][3], r[3]):         # Б11.35 М2: only periods one after another in the CALENDAR (or packages by their numbers) are one line
                flush()
            run.append(r)
        else:
            flush()
            out.append(r[1])
    flush()
    advance_lines.sort(key=lambda r: r[0])
    out.extend(r[1] for r in advance_lines)
    if advance:
        out.append(MoneyRow(label="Аванс: уроки следующего пакета" if ledger.mode == bl.LESSONS else "Аванс", amount=advance, state="advance"))
    return out


def _cell_periods(ledger: bl.Ledger) -> dict:
    """The periods of a calendar month of the common month (``Segment.slot`` = ``("c", start)``) by their slot: the key two accounts of one membership are matched by."""
    return {s.slot: s for s in ledger.segments if s.kind == "period" and s.slot is not None and s.slot[0] == "c"}


def pause_charge_rows(before: bl.Ledger, after: bl.Ledger, today: date, changed: Iterable[date], *, money: bool, membership_id: object = None) -> Tuple[List[PauseChargeRow], Optional[PauseChargeLater]]:
    """Б11.40 С1'-С5': the lines «период · уроков → сумма» of a change of a pause (the window «Пауза ученика»).  Decides no money: reads the two accounts the route already counted.

    A line is made only where the pause takes lessons out of the price of a month: a period of the common month counted by lessons that BEGAN in at least one of the two accounts and whose charge
    changed; ``lessons`` / ``lesson_dates`` come from the same set of dates the sum comes from (``Segment.lesson_dates``).  ``changed`` = the days whose state (paused or not) the action changed: the
    days of them that lie after every begun period are ``charge_later`` (one record, no sum).  A personal period, a package, a period with no lesson in the changed days: no lines (the screen shows the old
    one line).  Insurance (С4'): when the sum of the lines is not ``charged_delta`` - no lines, no ``charge_later``, an error in the log."""
    old, new = _cell_periods(before), _cell_periods(after)
    if not old and not new:
        return [], None
    year = today.year
    rows: List[PauseChargeRow] = []
    begun_end: Optional[date] = None
    for slot in sorted(set(old) | set(new)):
        b, a = old.get(slot), new.get(slot)
        if not ((b is not None and b.begun) or (a is not None and a.begun)):
            continue
        end = (b or a).period_end
        begun_end = end if begun_end is None or end > begun_end else begun_end
        amount = (a.amount if a is not None and a.begun else 0) - (b.amount if b is not None and b.begun else 0)
        if amount == 0 or any(x is not None and x.lesson_dates is None for x in (a, b)):
            continue
        days = set(a.days if a is not None else ()) ^ set(b.days if b is not None else ())
        was, now = Counter(b.lesson_dates if b is not None else ()), Counter(a.lesson_dates if a is not None else ())
        gone, back = was - now, now - was
        if not days:
            continue
        first, last = min(days), max(days)
        rows.append(PauseChargeRow(
            label=tx.range_text(first, last, year), first_day=first, last_day=last, days=len(days), lessons=sum(gone.values()) + sum(back.values()),
            lessons_passed=sum(n for d, n in back.items() if d < today) if amount > 0 else None, amount=amount,
            period_begins=min(a.days) if a is not None and a.days and bool(b is not None and b.begun) != bool(a.begun) else None))     # Б11.43: the period is carried across the working date
    total = sum(r.amount for r in rows)
    if rows and total != after.charged - before.charged:
        logger.error("pause_charge_rows: the lines of the pause sum to %s, the change of the charge is %s (membership %s): no lines", total, after.charged - before.charged, membership_id)
        return [], None
    if not money:                                                            # С5': the lines stay, the sums do not
        rows = [r.model_copy(update={"amount": None}) for r in rows]
    later = sorted(d for d in set(changed) if begun_end is None or d > begun_end)
    if not later:
        return rows, None
    return rows, PauseChargeLater(label=tx.range_text(later[0], later[-1], year), first_day=later[0], last_day=later[-1])


def rest_rows(ledger: bl.Ledger, due, name: str, today: date) -> List[RestRow]:
    """What stays unpaid after a payment under ONE name (``debt`` / ``due``), by the periods from the oldest: the same parts the sums ``debt_amount`` / ``due_amount`` are made of."""
    by_id = {s.id: s for s in ledger.segments}
    parts = sorted((p for p in due.parts if p.name == name and p.unpaid > 0), key=lambda p: by_id[p.segment_id].period_start if p.segment_id in by_id else date.min)
    return [RestRow(label=period_label(by_id[p.segment_id], today) if p.segment_id in by_id else "Долг", amount=p.unpaid) for p in parts]


def preview_result(before: bl.Ledger, after: bl.Ledger, credit_id: str, price_today: Optional[int], due_before, due_after, today: date, closed: bool = False) -> PreviewResult:
    allocs = allocations_of(after, credit_id)
    raw = [a for a in after.allocations if a.credit_id == credit_id]
    lessons = None
    if after.mode == bl.LESSONS and after.lessons is not None:
        lessons = PreviewLessons(
            covered_before=before.lessons.covered if before.lessons else 0, covered_after=after.lessons.covered,
            left_after=after.lessons.left, unpaid_after=after.lessons.unpaid)
    advance_periods = [(a.period_start, a.period_end) for a in raw if a.bucket == bl.ADVANCE and a.segment_id != bl.ADVANCE_SEGMENT]
    to_debt, to_due = paid_by_name(due_before, [a for a in raw if a.segment_id != bl.ADVANCE_SEGMENT])
    text = tx.summary_text(
        debt_paid=to_debt, debt_before=due_before.debt_amount, due_paid=to_due, due_before=due_before.due_amount,
        advance=after.advance if lessons else sum(a.amount for a in raw if a.bucket == bl.ADVANCE), debt_after=after.debt, paid_until_after=None if closed else after.status.paid_until,
        advance_periods=advance_periods,
        lessons=(after.lessons.covered, after.lessons.left) if lessons else None,
        kind=after.covered_by, partial=partial_words(after, due_after, closed),
    )
    return PreviewResult(
        allocations=allocs, unpaid_after=after.debt, debt_amount_after=due_after.debt_amount, due_amount_after=due_after.due_amount,
        due_until_after=due_after.due_until, overdue_days_after=due_after.overdue_days if due_after.debt_amount > 0 else None,
        advance_after=after.advance, paid_until_before=before.status.paid_until,
        status_after=status_without_paid_until(after.status.status, after.debt),
        partial_credit_after=after.partial_credit, lessons=lessons, summary_text=text,
        after_text=tx.after_text(due_after.debt_amount, due_after.due_amount, cover_until(after, closed), after.lessons.left if lessons else None, after.advance, after.covered_by),
        price_ahead=price_ahead(after, credit_id, price_today),
        money_rows=money_rows(after, credit_id, today), debt_rest=rest_rows(after, due_after, pay_due.DEBT, today),
        due_after=due_after.due_amount, due_rest=rest_rows(after, due_after, pay_due.DUE, today),
    )


# ---- INCOME-2: the one-line «куда пошли» of the payments journal -------------------------------------------


def _segment_word(seg: Optional[bl.Segment]) -> str:
    """``за сентябрь`` (a whole calendar month) / ``за 15.09–14.10`` / ``пакет №2`` / ``долг на начало учёта``."""
    if seg is None:
        return "аванс"
    if seg.kind == "opening":
        return "долг на начало учёта"
    if seg.kind == "package" and seg.package_no is not None:
        return f"пакет №{seg.package_no}"
    first, last = seg.period_start, seg.period_end
    if first.day == 1 and (last + timedelta(days=1)).day == 1 and (first.year, first.month) == (last.year, last.month):
        return f"за {tx.MONTHS_NOMINATIVE[first.month - 1]}"
    return f"за {tx.period_text(first, last)}"


def allocation_text(ledger: bl.Ledger, credit_id: str) -> str:
    """``за сентябрь — 200 000 сум · аванс — 50 000 сум`` (+ ``возвращено — N сум`` / ``перенесено в другую группу — N сум``).  «Аванс» is money that is NOT consumed today: it
    sits in a period that has not begun yet, or in no period at all.  Money that reached a period which started after the payment but has begun since (a back-dated
    payment, a period that began meanwhile) is shown with that period: ``за 22.10–21.11 — 450 000 сум`` (S1: the same words as the drawer and the statement)."""
    by_id = {s.id: s for s in ledger.segments}
    parts: List[Tuple[str, int]] = []
    for a in ledger.allocations:
        if a.credit_id != credit_id:
            continue
        seg = by_id.get(a.segment_id)
        if a.segment_id == bl.ADVANCE_SEGMENT or seg is None or not seg.begun:
            parts.append(("аванс", a.amount))
        else:
            parts.append((_segment_word(by_id.get(a.segment_id)), a.amount))
    parts.append(("возвращено", ledger.refunded_from.get(credit_id, 0)))
    parts.append(("деньги перешли в другую группу", ledger.transferred_from.get(credit_id, 0)))
    # the advance goes last, the cut-offs after it
    parts.sort(key=lambda p: {"аванс": 1, "возвращено": 2, "деньги перешли в другую группу": 3}.get(p[0], 0))
    return tx.allocation_summary(parts)
