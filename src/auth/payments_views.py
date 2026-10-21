"""
ALGO - PAY-2: ledger -> API models (pure conversions, no database, no money logic).

``billing_ledger`` answers; this module only renders the answer in the frozen contract (``payments_schemas``) and writes the
sentences (``payments_texts``).  Nothing here decides an amount.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, Iterable, List, Optional, Sequence

from . import billing_ledger as bl
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
    Segment,
    SegmentLessons,
)


def price_out(info: bl.PriceInfo) -> PriceOut:
    return PriceOut(amount=info.amount, source=info.source, valid_from=info.valid_from,
                    group_amount=info.group_amount, reason=info.reason)


def segment_out(seg: bl.Segment) -> Segment:
    return Segment(
        id=seg.id, kind=seg.kind, period_from=seg.period_start, period_to=seg.period_end, billable_days=len(seg.days),
        days_text=seg.days_text, price=price_out(seg.price), charge=seg.amount, basis=seg.basis,
        lessons=SegmentLessons(in_part=seg.lessons_in_part, in_period=seg.lessons_in_period)
        if seg.kind == "package" or seg.basis == "lessons" else None,
        paid=seg.money, written_off=seg.forgiven, unpaid=seg.unpaid, covered_days=seg.covered_days,
        covered_through=seg.covered_through, partial_credit=seg.partial_credit, state=seg.state, bucket=seg.bucket,
    )


def allocation_out(a: bl.Allocation) -> Allocation:
    return Allocation(
        segment_id=a.segment_id, period_from=a.period_start, period_to=a.period_end, amount=a.amount, days=a.days,
        bucket=a.bucket, closes=a.closes, label=a.label, price=a.price,
    )


def allocations_of(ledger: bl.Ledger, credit_id: str) -> List[Allocation]:
    return [allocation_out(a) for a in ledger.allocations if a.credit_id == credit_id]


def payment_allocation(ledger: bl.Ledger, payment_id: str) -> PaymentAllocationOut:
    items = allocations_of(ledger, payment_id)
    refunded = ledger.refunded_from.get(payment_id, 0)
    net = ledger.net_money.get(payment_id, 0)
    return PaymentAllocationOut(items=items, refunded=refunded, net=net)


def allocation_summary(before: bl.Ledger, after: bl.Ledger, credit_id: str) -> AllocationSummary:
    return AllocationSummary(
        items=allocations_of(after, credit_id), debt_before=before.debt, debt_after=after.debt,
        advance_after=after.advance, paid_until_before=before.status.paid_until, paid_until_after=after.status.paid_until,
    )


def shim_period(ledger: bl.Ledger, payment_id: str):
    """``valid_from`` / ``valid_to`` for the OLD front (one release): the first period the payment reached and "paid until"."""
    items = [a for a in ledger.allocations if a.credit_id == payment_id and a.segment_id != bl.ADVANCE_SEGMENT]
    first = min((a.period_start for a in items), default=None)
    return first, ledger.status.paid_until


def due_block(before: bl.Ledger) -> PreviewDue:
    shown = [segment_out(s) for s in before.segments if s.begun and s.unpaid > 0]
    return PreviewDue(
        debt=before.debt, past_debt=before.past_debt, current_due=before.current_due, advance=before.advance,
        segments=shown, paid_until=before.status.paid_until, days_overdue=before.status.days_overdue,
    )


def suggested_amount(before: bl.Ledger, price_today: Optional[int]) -> int:
    """«Всего к оплате»: the debt; with no debt the next unpaid period (calendar) / a package without the partial credit."""
    if before.debt > 0:
        return before.debt
    if before.mode == bl.LESSONS:
        return max(0, (price_today or 0) - before.partial_credit)
    for seg in before.segments:
        if seg.unpaid > 0 and not seg.unpriced:
            return seg.unpaid
    return price_today or 0


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


def preview_result(before: bl.Ledger, after: bl.Ledger, credit_id: str, price_today: Optional[int]) -> PreviewResult:
    allocs = allocations_of(after, credit_id)
    raw = [a for a in after.allocations if a.credit_id == credit_id]
    lessons = None
    if after.mode == bl.LESSONS and after.lessons is not None:
        lessons = PreviewLessons(
            covered_before=before.lessons.covered if before.lessons else 0, covered_after=after.lessons.covered,
            left_after=after.lessons.left, unpaid_after=after.lessons.unpaid)
    advance_periods = [(a.period_start, a.period_end) for a in raw if a.bucket == bl.ADVANCE and a.segment_id != bl.ADVANCE_SEGMENT]
    text = tx.summary_text(
        past=sum(a.amount for a in raw if a.bucket == bl.PAST_DEBT),
        current=sum(a.amount for a in raw if a.bucket == bl.CURRENT),
        advance=sum(a.amount for a in raw if a.bucket == bl.ADVANCE),
        past_debt_before=before.past_debt, debt_after=after.debt, paid_until_after=after.status.paid_until,
        advance_periods=advance_periods,
        lessons=(after.lessons.covered, after.lessons.left) if lessons else None,
    )
    return PreviewResult(
        allocations=allocs, debt_after=after.debt, past_debt_after=after.past_debt, current_due_after=after.current_due,
        advance_after=after.advance, paid_until_before=before.status.paid_until, paid_until_after=after.status.paid_until,
        status_after=after.status.status, days_overdue_after=after.status.days_overdue,
        partial_credit_after=after.partial_credit, lessons=lessons, summary_text=text,
        price_ahead=price_ahead(after, credit_id, price_today),
    )
