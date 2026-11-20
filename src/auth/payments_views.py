"""
ALGO - PAY-2: ledger -> API models (pure conversions, no database, no money logic).

``billing_ledger`` answers; this module only renders the answer in the frozen contract (``payments_schemas``) and writes the
sentences (``payments_texts``).  Nothing here decides an amount.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import List, Optional, Tuple

from . import billing_ledger as bl
from . import pay_due
from . import payment_status as ps
from . import payments_texts as tx
from .payments_schemas import (
    Allocation,
    AllocationSummary,
    PaymentAllocationOut,
    PreviewDue,
    PreviewLessons,
    PreviewResult,
    PriceAhead,
    PriceOut,
    RecalcOut,
    Segment,
    SegmentLessons,
    SegmentPart,
)


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
        advance_after=after.advance, paid_until_before=before.status.paid_until, paid_until_after=after.status.paid_until,
    )


def shim_period(ledger: bl.Ledger, payment_id: str):
    """``valid_from`` / ``valid_to`` for the OLD front (one release): the first period the payment reached and "paid until"."""
    items = [a for a in ledger.allocations if a.credit_id == payment_id and a.segment_id != bl.ADVANCE_SEGMENT]
    first = min((a.period_start for a in items), default=None)
    return first, ledger.status.paid_until


BUCKET_DEBTORS, BUCKET_TO_PAY, BUCKET_EXPIRING, BUCKET_ACTIVE, BUCKET_PAUSED = "debtors", "to_pay", "expiring", "active", "paused"
BUCKET_DEBT_ALIAS = "debt"      # the old name of the debtors tab: an alias of ``debtors`` on input only (a link of the old front)
UNPAID_TABS = (BUCKET_DEBTORS, BUCKET_TO_PAY)        # the tabs of an unpaid sum: they are NOT exclusive (a student with both a debt and a sum within its term is in both) and read the closed participations too
STATE_DEBT, STATE_TO_PAY, STATE_EXPIRING, STATE_PAUSED, STATE_PAID, STATE_FREE, STATE_NONE = "debt", "to_pay", "expiring", "paused", "paid", "free", "none"
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
    if l.status.status in (ps.EXPIRING, ps.ENDS_TODAY, ps.EXPIRED):
        return BUCKET_EXPIRING
    return BUCKET_ACTIVE         # active, and «none» (nothing has fallen due yet)


def in_tab(e, tab: str) -> bool:
    """PAY-DUE 9.1: the tabs of the unpaid sum are the two names of the ONE function - ``debtors``: a debt is owed; ``to_pay``: a sum within its term is owed (a debtor with such a sum too is in both);
    the other tabs: ``calm_bucket``."""
    if tab == BUCKET_DEBTORS:
        return e.due.debt_amount > 0
    if tab == BUCKET_TO_PAY:
        return e.due.due_amount > 0
    return calm_bucket(e) == tab


def state_of(e) -> str:
    """The WORST state of a membership, for the single label (PAY-DUE 10.2): debt -> to pay -> payment ends soon -> pause -> paid / advance -> free -> no price.  The screen does not choose it."""
    d, l = e.due, e.ledger
    if d.debt_amount > 0:
        return STATE_DEBT
    if d.due_amount > 0:
        return STATE_TO_PAY
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


def unpaid_fields(e, today: date) -> dict:
    """The words and numbers of the unpaid sum of a row, ALL from ``pay_due`` and the sentences of ``payments_texts``: ``debt_amount`` / ``due_amount`` / ``due_until`` / ``overdue_days`` / ``due_lessons`` /
    ``debt_lessons`` and the texts of both names (which of the two is the main one is the choice of the tab: a view, not a rule), ``state`` / ``state_text`` of the worst state."""
    d, l = e.due, e.ledger
    packages = d.due_lessons is not None
    paid_until = l.status.paid_until if not packages else None
    out = {
        "debt_amount": d.debt_amount, "due_amount": d.due_amount, "due_until": d.due_until,
        "overdue_days": d.overdue_days if d.debt_amount > 0 else None,
        "due_lessons": d.due_lessons if d.due_amount > 0 or d.debt_amount > 0 else None, "debt_lessons": d.debt_lessons,
        "debt_text": tx.debt_text(d.debt_amount, d.debt_lessons if packages else None) if d.debt_amount > 0 else None,
        "debt_sub": tx.debt_sub(d.overdue_days, paid_until) if d.debt_amount > 0 else None,
        "due_text": tx.due_text(d.due_amount, d.due_lessons["used"] if packages and d.due_lessons else None) if d.due_amount > 0 else None,
        "due_sub": tx.due_sub(d.due_until, today) if d.due_amount > 0 else None,
        # the second line of a row that holds BOTH sums (the tab decides which is the main one): null when the row has no second sum
        "debt_also_text": tx.debt_also_text(d.debt_amount, d.debt_lessons if packages else None, d.overdue_days) if d.debt_amount > 0 and d.due_amount > 0 else None,
        "due_also_text": tx.due_also_text(d.due_amount, d.due_lessons["used"] if packages and d.due_lessons else None, d.due_until) if d.debt_amount > 0 and d.due_amount > 0 else None,
    }
    state = state_of(e)
    out["state"] = state
    out["state_text"] = state_text(e, state, out, today)
    return out


def state_text(e, state: str, fields: dict, today: date) -> Optional[str]:
    """The single label of the worst state."""
    l = e.ledger
    if state == STATE_DEBT:
        return fields["debt_text"]
    if state == STATE_TO_PAY:
        return fields["due_text"]
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
        return tx.paid_until_text(l.status.paid_until)
    if l.status.period_mode == bl.LESSONS and l.status.lessons_left is not None:      # a paid package: how many lessons are left (before the advance: the money of a package IS the lessons)
        return tx.lessons_left_text(l.status.lessons_left)
    if l.advance > 0 and not l.status.paid_until:
        return tx.advance_text(l.advance)
    return tx.paid_until_text(l.status.paid_until) if l.status.paid_until else (tx.advance_text(l.advance) if l.advance > 0 else None)


def parts_of_segment(due, seg_id: str) -> list:
    """``parts[]`` of a statement line of a charge: the unpaid part(s) of THAT segment by name (a package segment may hold both names: two parts, the debt first)."""
    return [{"name": p.name, "unpaid": p.unpaid, "due_until": p.due_until, "lessons": p.lessons} for p in due.parts_of(seg_id)]


def open_of_row(kind: str, ref_id: str, ledger: bl.Ledger, due, segments: dict, today: date) -> dict:
    """PAY-DUE 10.11: ``open_amount`` / ``open_name`` / ``open_text`` / ``open_parts`` of a statement row (see ``StatementRow``).  A charge whose unpaid part holds BOTH names (a package) has two ``open_parts``
    (the debt first), ``open_name`` is the worst of them and ``open_text`` says both.  ``open_sub`` is the term of the row in words: «срок был 05.11» for the debt part (only when the term has really passed: a student
    who left before the term has a debt whose term is still ahead - nothing is said), «до 24.11» / «до сегодня» for the part «к оплате», both joined by « · » (the debt first); ``None`` for a paid charge and for the opening balance."""
    none = {"open_amount": None, "open_name": None, "open_text": None, "open_sub": None, "open_parts": []}
    if kind in ("charge", "opening_debt"):
        seg = segments[ref_id]
        parts = due.parts_of(seg.id)
        packages = due.due_lessons is not None
        debt = sum(p.unpaid for p in parts if p.name == pay_due.DEBT)
        owed = sum(p.unpaid for p in parts if p.name == pay_due.DUE)
        out = []
        if debt > 0:
            out.append({"name": "debt", "amount": debt, "text": tx.debt_text(debt, sum(p.lessons or 0 for p in parts if p.name == pay_due.DEBT) if packages else None)})
        if owed > 0:
            out.append({"name": "due", "amount": owed, "text": tx.due_text(owed, sum(p.lessons or 0 for p in parts if p.name == pay_due.DUE) if packages else None)})
        if not out:
            out = [{"name": "paid", "amount": 0, "text": "Оплачено"}]
        text = tx.open_both_text(out[0]["text"], out[1]["text"]) if len(out) == 2 else out[0]["text"]
        was = [p.term for p in parts if p.name == pay_due.DEBT and p.term and p.term < today]
        until = [p.due_until for p in parts if p.name == pay_due.DUE and p.due_until]
        words = ([f"срок был {tx.ddmm(min(was))}"] if was else []) + ([tx.due_until_words(min(until), today)] if until else [])
        return {"open_amount": sum(p["amount"] for p in out), "open_name": out[0]["name"], "open_text": text, "open_sub": " · ".join(words) or None, "open_parts": out}
    if kind in (bl.PAYMENT, bl.TRANSFER_IN):      # the money that came (a payment, a transfer into the group) - the part not laid on a charge yet is «Аванс»; a transfer OUT, a refund, a forgiveness stay empty
        # the part of the money that is laid on NO begun charge: on the «advance» of no period yet, or on a period that has not begun (the bucket of an allocation is relative to the DAY of the payment: a period that has begun since is no advance)
        left = sum(a.amount for a in ledger.allocations if a.credit_id == ref_id and (a.segment_id == bl.ADVANCE_SEGMENT or (a.segment_id in segments and not segments[a.segment_id].begun)))
        if left > 0:
            text = tx.advance_text(left)
            return {"open_amount": left, "open_name": "advance", "open_text": text, "open_sub": None, "open_parts": [{"name": "advance", "amount": left, "text": text}]}
    return none


def due_block(before: bl.Ledger, due, today: date) -> PreviewDue:
    """PAY-DUE 10.2: what the student owes today (before the typed amount), named by ``pay_due``: the old ``past_debt`` / ``current_due`` are gone."""
    shown = [segment_out(s, due) for s in before.segments if s.begun and s.unpaid > 0]
    packages = due.due_lessons is not None
    return PreviewDue(
        unpaid=before.debt, debt_amount=due.debt_amount, due_amount=due.due_amount, due_until=due.due_until,
        overdue_days=due.overdue_days if due.debt_amount > 0 else None,
        debt_text=tx.debt_text(due.debt_amount, due.debt_lessons if packages else None) if due.debt_amount > 0 else None,
        debt_sub=tx.debt_sub(due.overdue_days, None if packages else before.status.paid_until) if due.debt_amount > 0 else None,
        due_text=tx.due_text(due.due_amount, due.due_lessons["used"] if packages and due.due_lessons else None) if due.due_amount > 0 else None,
        due_sub=tx.due_sub(due.due_until, today) if due.due_amount > 0 else None,
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


def preview_result(before: bl.Ledger, after: bl.Ledger, credit_id: str, price_today: Optional[int], due_before, due_after, today: date) -> PreviewResult:
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
        advance=sum(a.amount for a in raw if a.bucket == bl.ADVANCE), debt_after=after.debt, paid_until_after=after.status.paid_until,
        advance_periods=advance_periods,
        lessons=(after.lessons.covered, after.lessons.left) if lessons else None,
    )
    return PreviewResult(
        allocations=allocs, unpaid_after=after.debt, debt_amount_after=due_after.debt_amount, due_amount_after=due_after.due_amount,
        due_until_after=due_after.due_until, overdue_days_after=due_after.overdue_days if due_after.debt_amount > 0 else None,
        advance_after=after.advance, paid_until_before=before.status.paid_until, paid_until_after=after.status.paid_until,
        status_after=status_without_paid_until(after.status.status, after.debt),
        partial_credit_after=after.partial_credit, lessons=lessons, summary_text=text,
        after_text=tx.after_text(due_after.debt_amount, due_after.due_amount, after.status.paid_until, after.lessons.left if lessons else None, after.advance),
        price_ahead=price_ahead(after, credit_id, price_today),
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
