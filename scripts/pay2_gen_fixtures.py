#!/usr/bin/env python3
"""PAY-2 contract fixtures: tests/fixtures/pay2/*.json, generated from the worked examples E1-E8 of the design.

The numbers are computed here by a deliberately naive, INDEPENDENT hand calculator (``hand_ledger``: money line over
charges, floor days) and every headline number is asserted against the figure written in the design
(``artifacts/partner_doc_check/pay2_tech_design.md`` §1A).  The production ``billing_ledger.run_ledger`` must reproduce
these files (``tests/test_pay2_golden.py``); the front engineer builds mocks and contract tests on the same files.

    python scripts/pay2_gen_fixtures.py          # rewrite tests/fixtures/pay2
    python scripts/pay2_gen_fixtures.py --check  # fail when the files on disk differ (CI / test)
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.auth import payments_texts as tx  # noqa: E402
from src.auth.payments_schemas import (  # noqa: E402
    Allocation,
    AllocationSummary,
    BalancesPage,
    BalanceRow,
    MembershipPaymentRow,
    PaymentAllocationOut,
    PaymentCreated,
    PaymentOut,
    PaymentPreview,
    PaymentSettingsChanged,
    PaymentVoided,
    PriceAhead,
    PriceOut,
    PreviewDue,
    PreviewLessons,
    PreviewResult,
    ReceiptItem,
    ReceiptItems,
    ReceiptOut,
    ReceiptSnapshot,
    ReceiptsReport,
    RefundCreated,
    RefundOut,
    Segment,
    SegmentLessons,
    SnapshotPrice,
    StatementDetails,
    StatementMembership,
    StatementOut,
    StatementRow,
    StatementSummary,
    WriteoffCreated,
    WriteoffOut,
    PayError,
)

OUT = ROOT / "tests" / "fixtures" / "pay2"
D = date


def uid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def days(a: date, b: date) -> List[date]:
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


def round_1000(num: int, den: int = 1) -> int:
    return ((2 * num + 1000 * den) // (2000 * den)) * 1000


# ------------------------------------------------------------------ the hand calculator ------------------------


@dataclass
class HSeg:
    id: str
    a: date  # period_from
    b: date  # period_to
    dlist: List[date]  # billable days
    charge: int
    kind: str = "period"
    price: Optional[int] = None
    basis: str = "full"
    lessons: Optional[tuple] = None  # (in_part, in_period)
    text: Optional[str] = None
    pkg_no: Optional[int] = None
    since: date = D(2026, 1, 1)  # valid_from of the price row that gave ``price``
    # filled by hand_ledger
    money: int = 0
    wo: int = 0
    pieces: Dict[str, int] = field(default_factory=dict)  # credit id -> amount

    @property
    def n(self) -> int:
        return len(self.dlist)

    def covered(self, volume: int) -> int:
        return self.n if self.charge == 0 else min(self.n, volume * self.n // self.charge)


@dataclass
class HCredit:
    id: str
    kind: str  # payment | writeoff | transfer_in
    on: date
    amount: int


def hand_ledger(segs: List[HSeg], credits: List[HCredit], today: date, outflow: int = 0):
    """Money line.  Returns (segs filled, allocations by credit id, totals)."""
    # refunds: LIFO off the newest money credits
    net = {c.id: c.amount for c in credits}
    left = outflow
    for c in sorted((c for c in credits if c.kind != "writeoff"), key=lambda c: (c.on, c.id), reverse=True):
        take = min(left, net[c.id])
        net[c.id] -= take
        left -= take
    assert left == 0
    pos = 0
    spans = {}
    for c in credits:
        spans[c.id] = (pos, pos + net[c.id])
        pos += net[c.id]
    total_credit = pos
    cur = 0
    for s in segs:
        lo, hi = cur, cur + s.charge
        cur = hi
        s.pieces = {}
        for c in credits:
            a, b = spans[c.id]
            part = max(0, min(b, hi) - max(a, lo))
            if part:
                s.pieces[c.id] = part
        s.money = sum(v for k, v in s.pieces.items() if kind_of(credits, k) != "writeoff")
        s.wo = sum(v for k, v in s.pieces.items() if kind_of(credits, k) == "writeoff")
    return net, spans, total_credit


def kind_of(credits, cid):
    return next(c.kind for c in credits if c.id == cid)


def piece_days(seg: HSeg, credits: List[HCredit], cid: str) -> int:
    """days gained by credit ``cid`` in ``seg`` (telescoping over the credit order)."""
    run = 0
    before = after = 0
    for c in credits:
        part = seg.pieces.get(c.id, 0)
        if c.id == cid:
            before = seg.covered(run)
            after = seg.covered(run + part)
        run += part
    return after - before


def totals(segs: List[HSeg], total_credit: int, today: date, paid_total: int):
    begun = [s for s in segs if s.a <= today]
    charged = sum(s.charge for s in begun)
    debt = max(0, charged - total_credit)
    advance = max(0, total_credit - charged)
    past = sum(max(0, s.charge - s.money - s.wo) for s in begun if s.b < today)
    through = None
    for s in segs:
        vol = s.money + s.wo
        cd = s.covered(vol)
        if cd >= s.n:
            through = s.dlist[-1] if s.n else through
            continue
        through = s.dlist[cd - 1] if cd > 0 else (through if through is not None else s.dlist[0] - timedelta(days=1))
        break
    return dict(charged=charged, debt=debt, advance=advance, past=past, current=debt - past, through=through)


# ------------------------------------------------------------------ builders --------------------------------------

GROUP_PRICE = lambda amount, since=D(2026, 1, 1): PriceOut(  # noqa: E731
    amount=amount, source="group", valid_from=since, group_amount=amount, reason=None
)


def seg_model(s: HSeg, today: date) -> Segment:
    vol = s.money + s.wo
    cd = s.covered(vol)
    partial = vol - (cd * s.charge) // s.n if s.n and s.charge else 0
    if cd >= s.n:
        partial = 0
    unpaid = max(0, s.charge - s.money - s.wo)
    begun = s.a <= today
    if unpaid == 0:
        state = "closed"
    elif not begun:
        state = "future"
    elif vol > 0:
        state = "partial"
    else:
        state = "open"
    through = s.dlist[cd - 1] if cd > 0 else None
    ended = (s.lessons[0] >= s.lessons[1]) if s.kind == "package" else s.b < today
    bucket = "past_debt" if ended else ("current" if begun else "future")
    return Segment(
        id=s.id, kind=s.kind, period_from=s.a, period_to=s.b, billable_days=s.n, days_text=s.text,
        price=GROUP_PRICE(s.price, s.since) if s.price is not None else PriceOut(), charge=s.charge, basis=s.basis,
        lessons=SegmentLessons(in_part=s.lessons[0], in_period=s.lessons[1]) if s.lessons else None,
        paid=s.money, written_off=s.wo, unpaid=unpaid, remaining=unpaid, covered_days=cd, covered_through=through,
        partial_credit=partial, state=state, bucket=bucket,
    )


def bucket_for(s: HSeg, on: date) -> str:
    if s.kind == "package":
        return "past_debt" if s.lessons[0] >= s.lessons[1] else ("current" if s.a <= on else "advance")
    if s.b < on:
        return "past_debt"
    if s.a <= on <= s.b:
        return "current"
    return "advance"


def allocations_of(segs: List[HSeg], credits: List[HCredit], cid: str) -> List[Allocation]:
    out = []
    for s in segs:
        amount = s.pieces.get(cid, 0)
        if not amount:
            continue
        on = next(c.on for c in credits if c.id == cid)
        bucket = bucket_for(s, on)
        closes = s.money + s.wo >= s.charge and amount > 0 and _closes_with(s, credits, cid)
        out.append(
            Allocation(
                segment_id=s.id, period_from=s.a, period_to=s.b, amount=amount, days=piece_days(s, credits, cid),
                bucket=bucket, closes=closes, price=s.price,
                label=tx.allocation_label(bucket=bucket, closes=closes, start=s.a, end=s.b, package_no=s.pkg_no),
                segment_remaining_after=_remaining_after(s, credits, cid),
            )
        )
    return out


def _remaining_after(s: HSeg, credits: List[HCredit], cid: str) -> int:
    """What is still unpaid of the segment right after credit ``cid`` (credits in line order)."""
    run = 0
    for c in credits:
        run += s.pieces.get(c.id, 0)
        if c.id == cid:
            return max(0, s.charge - run)
    return s.charge


def _closes_with(s: HSeg, credits: List[HCredit], cid: str) -> bool:
    """The segment is paid in full right after credit ``cid`` (credits in line order)."""
    run = 0
    for c in credits:
        run += s.pieces.get(c.id, 0)
        if c.id == cid:
            return run >= s.charge
    return False


def adv_alloc(amount: int, on: date, label: Optional[str] = None) -> Allocation:
    return Allocation(
        segment_id="advance", period_from=on, period_to=on, amount=amount, days=0, bucket="advance", closes=False,
        label=label or tx.advance_label_lessons(), price=None,
    )


def pay_out(n: int, *, amount: int, paid_on: date, method: str = "cash", receipt_no: Optional[int], note=None,
            first: Optional[date] = None, last: Optional[date] = None, allocation=None, voided=None,
            replaces: Optional[int] = None, created_at: str = "2026-10-21T09:12:00") -> PaymentOut:
    return PaymentOut(
        id=uid(n), membership_id=uid(1), amount=amount, currency="UZS", paid_on=paid_on, valid_from=first, valid_to=last,
        lessons_count=None, legacy_period=False, method=method, kind="payment", allocation=allocation, note=note,
        replaces_payment_id=uid(replaces) if replaces else None, receipt_no=receipt_no, partial_period=False,
        files=[], created_by=uid(901), created_by_name="Рустам Каримов", created_at=created_at,
        voided_at=voided[0] if voided else None, voided_by=uid(901) if voided else None,
        voided_by_name="Рустам Каримов" if voided else None, void_reason=voided[1] if voided else None,
    )


def row_model(*, mid=1, student="Хасанов Азиз", display_id="N13-004", group="Математика 7А", group_id=11,
              teacher="Иванова Мария", price: PriceOut, status: str, mode="calendar", paid_until=None,
              overdue=None, left=None, last_payment=None, debt=0, past=0, current=0, advance=0, partial=0,
              refundable=0, writeoff_allowed=False, active=True, lessons=None, lessons_unpaid=0) -> MembershipPaymentRow:
    return MembershipPaymentRow(
        membership_id=uid(mid), student_id=uid(100 + mid), student_name=student, display_id=display_id, class_level=7,
        group_id=uid(group_id), group_name=group, teacher_id=uid(21), teacher_name=teacher, is_primary=True,
        is_active=active, price=price, monthly_fee=price.amount, status=status, period_mode=mode,
        paid_until=paid_until, days_overdue=overdue, days_left=left,
        lessons_total=lessons[0] if lessons else None, lessons_used=lessons[1] if lessons else None,
        lessons_left=lessons[2] if lessons else None, last_payment=last_payment,
        debt=debt, past_debt=past, current_due=current, advance=advance, partial_credit=partial,
        price_missing=False, lessons_unpaid=lessons_unpaid, refundable=refundable, writeoff_allowed=writeoff_allowed,
    )


def status_of(paid_until: Optional[date], today: date, expiring: int = 3):
    delta = (paid_until - today).days
    if delta < 0:
        return "expired", -delta, None
    if delta == 0:
        return "ends_today", None, 0
    if delta <= expiring:
        return "expiring", None, delta
    return "active", None, delta


def due_model(segs: List[HSeg], t: dict, today: date, paid_until, overdue) -> PreviewDue:
    shown = [seg_model(s, today) for s in segs if s.a <= today and max(0, s.charge - s.money - s.wo) > 0]
    return PreviewDue(
        debt=t["debt"], past_debt=t["past"], current_due=t["current"], advance=t["advance"], segments=shown,
        paid_until=paid_until, days_overdue=overdue,
    )


def write(name: str, model, store: Dict[str, str]) -> None:
    store[name] = json.dumps(model.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2) + "\n"


# ================================================================================================================
# E1 - the owner's example (payment_date, price 500 000, joined 15.09, today 21.10.2026, paid 231 000 on 20.09)
# ================================================================================================================

TODAY = D(2026, 10, 21)


def e1_segments() -> List[HSeg]:
    return [
        HSeg("seg:0:2026-09-15", D(2026, 9, 15), D(2026, 10, 14), days(D(2026, 9, 15), D(2026, 10, 14)), 500_000,
             price=500_000),
        HSeg("seg:0:2026-10-15", D(2026, 10, 15), D(2026, 11, 14), days(D(2026, 10, 15), D(2026, 11, 14)), 500_000,
             price=500_000),
        HSeg("seg:0:2026-11-15", D(2026, 11, 15), D(2026, 12, 14), days(D(2026, 11, 15), D(2026, 12, 14)), 500_000,
             price=500_000),
    ]


A_PAY = HCredit("p17", "payment", D(2026, 9, 20), 231_000)


def e1_state(extra: List[HCredit]):
    segs = e1_segments()
    credits = [A_PAY] + extra
    net, spans, tc = hand_ledger(segs, credits, TODAY)
    t = totals(segs, tc, TODAY, sum(c.amount for c in credits))
    return segs, credits, t


def e1_previews(store):
    # --- before: only the payment of 231 000
    segs0, cr0, t0 = e1_state([])
    assert (t0["debt"], t0["past"], t0["current"], t0["through"]) == (769_000, 269_000, 500_000, D(2026, 9, 27))
    overdue0 = (TODAY - t0["through"]).days
    assert overdue0 == 24
    due0 = due_model(segs0, t0, TODAY, t0["through"], overdue0)
    write("e1_preview_due.json", PaymentPreview(
        as_of=TODAY, mode="calendar", price=GROUP_PRICE(500_000), due=due0, suggested_amount=769_000, amount=None,
        result=None, warnings=[]), store)

    expected = {
        250_000: dict(debt=519_000, past=19_000, current=500_000, through=D(2026, 10, 12), days=[("seg0", 250_000, 15)],
                      partial=14_334),
        300_000: dict(debt=469_000, past=0, current=469_000, through=D(2026, 10, 15),
                      days=[("seg0", 269_000, 17), ("seg1", 31_000, 1)], partial=14_871),
        769_000: dict(debt=0, past=0, current=0, through=D(2026, 11, 14),
                      days=[("seg0", 269_000, 17), ("seg1", 500_000, 31)], partial=0),
        790_000: dict(debt=0, past=0, current=0, through=D(2026, 11, 15),
                      days=[("seg0", 269_000, 17), ("seg1", 500_000, 31), ("seg2", 21_000, 1)], partial=4_334),
    }
    for amount, exp in expected.items():
        new = HCredit("new", "payment", TODAY, amount)
        segs, credits, t = e1_state([new])
        assert (t["debt"], t["past"], t["current"], t["through"]) == (
            exp["debt"], exp["past"], exp["current"], exp["through"]), (amount, t)
        allocs = allocations_of(segs, credits, "new")
        assert [(a.amount, a.days) for a in allocs] == [(v, d) for _, v, d in exp["days"]], allocs
        status, overdue, left = status_of(t["through"], TODAY)
        adv_periods = [(a.period_from, a.period_to) for a in allocs if a.bucket == "advance"]
        result = PreviewResult(
            allocations=allocs, debt_after=t["debt"], past_debt_after=t["past"], current_due_after=t["current"],
            advance_after=t["advance"], paid_until_before=t0["through"], paid_until_after=t["through"],
            status_after=status, days_overdue_after=overdue, partial_credit_after=exp["partial"], lessons=None,
            summary_text=tx.summary_text(
                past=sum(a.amount for a in allocs if a.bucket == "past_debt"),
                current=sum(a.amount for a in allocs if a.bucket == "current"),
                advance=sum(a.amount for a in allocs if a.bucket == "advance"),
                past_debt_before=t0["past"], debt_after=t["debt"], paid_until_after=t["through"],
                advance_periods=adv_periods),
            price_ahead=[],
        )
        # the partial day is read off the segment where the first unpaid day lies
        sm = [seg_model(s, TODAY) for s in segs]
        first_open = next((m for m in sm if m.covered_days < m.billable_days), None)
        if first_open is not None:
            assert first_open.partial_credit == exp["partial"], (amount, first_open.partial_credit)
        write(f"e1_preview_{amount}.json", PaymentPreview(
            as_of=TODAY, mode="calendar", price=GROUP_PRICE(500_000), due=due0, suggested_amount=769_000,
            amount=amount, result=result, warnings=[]), store)
    # the headline sentences of the design
    assert json.loads(store["e1_preview_300000.json"])["result"]["summary_text"] == (
        "Закроет долг 269 000 · на текущий период 31 000 → оплачено до 15.10 · останется 469 000")
    assert json.loads(store["e1_preview_790000.json"])["result"]["summary_text"] == (
        "Закроет долг 269 000 · на текущий период 500 000 · Переплата 21 000 → аванс на следующий период "
        "(15.11–14.12), оплачено до 15.11")


def e1_create_replace_void(store):
    segs0, cr0, t0 = e1_state([])
    # --- create 300 000 (payment #18)
    new = HCredit("p18", "payment", TODAY, 300_000)
    segs, credits, t = e1_state([new])
    allocs = allocations_of(segs, credits, "p18")
    status, overdue, left = status_of(t["through"], TODAY)
    assert (status, overdue) == ("expired", 6)
    alloc_out = PaymentAllocationOut(items=allocs, refunded=0, net=300_000)
    p18 = pay_out(18, amount=300_000, paid_on=TODAY, receipt_no=18, note="за октябрь", first=D(2026, 9, 15),
                  last=t["through"], allocation=alloc_out)
    price = GROUP_PRICE(500_000)
    row = row_model(price=price, status=status, paid_until=t["through"], overdue=overdue, last_payment=p18,
                    debt=469_000, past=0, current=469_000, advance=0, partial=14_871, writeoff_allowed=True)
    write("e1_create_300000.json", PaymentCreated(
        payment=p18,
        allocation=AllocationSummary(items=allocs, debt_before=769_000, debt_after=469_000, advance_after=0,
                                     paid_until_before=t0["through"], paid_until_after=t["through"]),
        warnings=[], row=row), store)

    # --- receipt of that payment (the snapshot is "at the moment of the payment")
    snapshot = ReceiptSnapshot(
        computed_at="2026-10-21T09:12:00", business_date=TODAY, amount=300_000, method="cash", allocations=allocs,
        debt_before=769_000, debt_after=469_000, advance_after=0, paid_until_before=t0["through"],
        paid_until_after=t["through"], price=SnapshotPrice(amount=500_000, source="group"), mode="calendar")
    write("e1_receipt_snapshot.json", snapshot, store)
    write("e1_receipt_300000.json", ReceiptOut(
        receipt_no=18, org_name="ALGO Math Center", org_city="Ташкент", org_phone="+998901234567",
        student_name="Хасанов Азиз", display_id="N13-004", group_name="Математика 7А", amount=300_000,
        amount_words="Триста тысяч сум", currency="UZS", paid_on=TODAY, valid_from=D(2026, 9, 15),
        valid_to=t["through"], lessons_count=None, partial_period=False, method="cash", mode="calendar", allocations=allocs,
        debt_before=769_000, debt_after=469_000, advance_after=0, paid_until_before=t0["through"],
        paid_until_after=t["through"], price_snapshot=SnapshotPrice(amount=500_000, source="group"),
        note="за октябрь", accepted_by_name="Рустам Каримов", created_at="2026-10-21T09:12:00", voided_at=None,
        void_reason=None, replaced_by_receipt_no=19, replaces_receipt_no=None), store)

    # --- E8: the 300 000 is replaced by 250 000 (void + insert); the "before" excludes the replaced payment
    new2 = HCredit("p19", "payment", TODAY, 250_000)
    segs2, credits2, t2 = e1_state([new2])
    allocs2 = allocations_of(segs2, credits2, "p19")
    assert (t2["debt"], t2["through"]) == (519_000, D(2026, 10, 12))
    st2, ov2, _ = status_of(t2["through"], TODAY)
    p19 = pay_out(19, amount=250_000, paid_on=TODAY, receipt_no=19, note="за октябрь", first=D(2026, 9, 15),
                  last=t2["through"], allocation=PaymentAllocationOut(items=allocs2, refunded=0, net=250_000),
                  replaces=18, created_at="2026-10-21T09:20:00")
    row2 = row_model(price=price, status=st2, paid_until=t2["through"], overdue=ov2, last_payment=p19, debt=519_000,
                     past=19_000, current=500_000, advance=0, partial=14_334, writeoff_allowed=True)
    write("e8_replace_300000_by_250000.json", PaymentCreated(
        payment=p19,
        allocation=AllocationSummary(items=allocs2, debt_before=769_000, debt_after=519_000, advance_after=0,
                                     paid_until_before=t0["through"], paid_until_after=t2["through"]),
        warnings=[], row=row2), store)

    # --- void of the 300 000: the row returns to the state before it
    voided = pay_out(18, amount=300_000, paid_on=TODAY, receipt_no=18, note="за октябрь", first=D(2026, 9, 15),
                     last=t["through"], allocation=None, voided=("2026-10-21T09:30:00", "ошибка оператора"))
    seg_p17 = pay_out(17, amount=231_000, paid_on=D(2026, 9, 20), receipt_no=17, first=D(2026, 9, 15),
                      last=t0["through"], created_at="2026-09-20T10:00:00",
                      allocation=PaymentAllocationOut(items=allocations_of(segs0, cr0, "p17"), refunded=0, net=231_000))
    row0 = row_model(price=price, status="expired", paid_until=t0["through"], overdue=24, last_payment=seg_p17,
                     debt=769_000, past=269_000, current=500_000, advance=0, partial=14_334, writeoff_allowed=True)
    write("e1_void_300000.json", PaymentVoided(payment=voided, row=row0), store)


def e9_price_ahead(store):
    """D8: the group price rises from 01.11 (600 000), so the advance on the 3rd period buys fewer days."""
    segs = e1_segments()
    segs[2].price, segs[2].charge, segs[2].since = 600_000, 600_000, D(2026, 11, 1)
    segs0 = e1_segments()
    cr0 = [A_PAY]
    net, spans, tc = hand_ledger(segs0, cr0, TODAY)
    t0 = totals(segs0, tc, TODAY, 231_000)
    due0 = due_model(segs0, t0, TODAY, t0["through"], 24)
    new = HCredit("new", "payment", TODAY, 1_069_000)
    credits = [A_PAY, new]
    net, spans, tc = hand_ledger(segs, credits, TODAY)
    t = totals(segs, tc, TODAY, 0)
    assert (t["debt"], t["advance"], t["through"]) == (0, 300_000, D(2026, 11, 29))
    allocs = allocations_of(segs, credits, "new")
    assert [(a.amount, a.days) for a in allocs] == [(269_000, 17), (500_000, 31), (300_000, 15)]
    # at the OLD price the same 300 000 would have bought 18 days
    assert 300_000 * 30 // 500_000 == 18 and 300_000 * 30 // 600_000 == 15
    ahead = [PriceAhead(period_from=D(2026, 11, 15), period_to=D(2026, 12, 14), amount=600_000, current_amount=500_000)]
    res = PreviewResult(
        allocations=allocs, debt_after=0, past_debt_after=0, current_due_after=0, advance_after=300_000,
        paid_until_before=t0["through"], paid_until_after=t["through"], status_after="active", days_overdue_after=None,
        partial_credit_after=0, lessons=None,
        summary_text=tx.summary_text(past=269_000, current=500_000, advance=300_000, past_debt_before=269_000,
                                     debt_after=0, paid_until_after=t["through"],
                                     advance_periods=[(D(2026, 11, 15), D(2026, 12, 14))]),
        price_ahead=ahead)
    from src.auth.payments_schemas import PaymentWarningOut
    warn = PaymentWarningOut(code="price_change_ahead", message=tx.price_change_warning(
        D(2026, 11, 15), D(2026, 12, 14), 600_000, 500_000))
    write("e9_preview_price_ahead_1069000.json", PaymentPreview(
        as_of=TODAY, mode="calendar", price=GROUP_PRICE(500_000), due=due0, suggested_amount=769_000,
        amount=1_069_000, result=res, warnings=[warn]), store)


def e1_statement(store):
    new = HCredit("p18", "payment", TODAY, 790_000)
    segs, credits, t = e1_state([new])
    assert (t["debt"], t["advance"], t["through"]) == (0, 21_000, D(2026, 11, 15))
    sm = [seg_model(s, TODAY) for s in segs]
    a17 = allocations_of(segs, credits, "p17")
    a18 = allocations_of(segs, credits, "p18")
    assert [(a.amount, a.days, a.bucket) for a in a17] == [(231_000, 13, "current")]
    assert [(a.amount, a.days, a.bucket) for a in a18] == [
        (269_000, 17, "past_debt"), (500_000, 31, "current"), (21_000, 1, "advance")]
    bal = 0
    rows = []

    def add(date_, type_, title, debit=0, credit=0, outflow=0, details=None):
        nonlocal bal
        bal += credit - debit - outflow
        rows.append(StatementRow(seq=len(rows) + 1, date=date_, type=type_, title=title, debit=debit, credit=credit,
                                 outflow=outflow, balance_after=bal, balance_text=tx.balance_text(bal),
                                 details=details or StatementDetails()))

    add(D(2026, 9, 15), "charge", "Стоимость 15.09–14.10", debit=500_000, details=StatementDetails(segment=sm[0]))
    add(D(2026, 9, 20), "payment", "Оплата № 17", credit=231_000, details=StatementDetails(
        payment_id=uid(17), receipt_no=17, method="cash", created_by_name="Рустам Каримов", allocations=a17))
    add(D(2026, 10, 15), "charge", "Стоимость 15.10–14.11", debit=500_000, details=StatementDetails(segment=sm[1]))
    add(D(2026, 10, 21), "payment", "Оплата № 18", credit=790_000, details=StatementDetails(
        payment_id=uid(18), receipt_no=18, method="cash", created_by_name="Рустам Каримов", note="аванс", allocations=a18))
    assert bal == 21_000 and rows[-1].balance_text == "Аванс 21 000"
    summary = StatementSummary(charged=1_000_000, paid=1_021_000, transferred_in=0, written_off=0, refunded=0,
                               transferred_out=0, debt=0, advance=21_000, paid_until=t["through"], status="active",
                               reconciles=(1_000_000 - 1_021_000 == 0 - 21_000))
    write("e1_statement_790000.json", StatementOut(
        membership=StatementMembership(
            membership_id=uid(1), student_id=uid(101), student_name="Хасанов Азиз", display_id="N13-004",
            group_id=uid(11), group_name="Математика 7А", teacher_name="Иванова Мария", is_active=True),
        as_of=TODAY, mode="calendar", accounting_start=D(2026, 9, 1), summary=summary, rows=rows, voided=[],
        upcoming=[sm[2]]), store)
    # --- the row of the list after it
    p18 = pay_out(18, amount=790_000, paid_on=TODAY, receipt_no=18, note="аванс", first=D(2026, 9, 15),
                  last=t["through"], allocation=PaymentAllocationOut(items=a18, refunded=0, net=790_000))
    write("e1_row_after_790000.json", row_model(
        price=GROUP_PRICE(500_000), status="active", paid_until=t["through"], left=25, last_payment=p18,
        debt=0, advance=21_000, partial=4_334, refundable=21_000, writeoff_allowed=False), store)
    assert (t["through"] - TODAY).days == 25


# ================================================================================================================
# E2 - fixed_day=1, incomplete first period by lessons; E3 - a gap; both: NONE status before any money
# ================================================================================================================


def e2_e3(store):
    # ---------------- E2: price 600 000, joined Fri 16.10, today 21.10
    today = D(2026, 10, 21)
    oct_days = days(D(2026, 10, 16), D(2026, 10, 31))
    assert len(oct_days) == 16
    charge_oct = round_1000(600_000 * 7, 13)
    assert charge_oct == 323_000
    text = tx.days_text(basis="lessons", billable_days=16, period_days=31, first_day=D(2026, 10, 16),
                        lessons_in_part=7, lessons_in_period=13)
    assert text == "с 16.10 (7 уроков из 13)"

    def segs():
        return [
            HSeg("seg:0:2026-10-01", D(2026, 10, 1), D(2026, 10, 31), oct_days, charge_oct, price=600_000,
                 basis="lessons", lessons=(7, 13), text=text),
            HSeg("seg:0:2026-11-01", D(2026, 11, 1), D(2026, 11, 30), days(D(2026, 11, 1), D(2026, 11, 30)), 600_000,
                 price=600_000),
        ]

    s0 = segs()
    net, spans, tc = hand_ledger(s0, [], today)
    t0 = totals(s0, tc, today, 0)
    assert (t0["debt"], t0["past"], t0["current"]) == (323_000, 0, 323_000)
    price = GROUP_PRICE(600_000)
    due = due_model(s0, t0, today, None, None)
    write("e2_preview_due.json", PaymentPreview(as_of=today, mode="calendar", price=price, due=due,
                                                 suggested_amount=323_000, amount=None, result=None, warnings=[]), store)
    for amount, (debt, through, partial_exp, days_exp) in {
        200_000: (123_000, D(2026, 10, 24), 18_313, [9]),
        323_000: (0, D(2026, 10, 31), 0, [16]),
        900_000: (0, D(2026, 11, 28), 17_000, [16, 28]),
    }.items():
        new = HCredit("new", "payment", today, amount)
        ss = segs()
        net, spans, tc = hand_ledger(ss, [new], today)
        t = totals(ss, tc, today, amount)
        assert (t["debt"], t["through"]) == (debt, through), (amount, t)
        allocs = allocations_of(ss, [new], "new")
        assert [a.days for a in allocs] == days_exp, (amount, allocs)
        sm = [seg_model(s, today) for s in ss]
        fo = next((m for m in sm if m.covered_days < m.billable_days), None)
        if fo is not None:
            assert fo.partial_credit == partial_exp, (amount, fo.partial_credit)
        st, ov, left = status_of(t["through"], today)
        res = PreviewResult(
            allocations=allocs, debt_after=t["debt"], past_debt_after=0, current_due_after=t["current"],
            advance_after=t["advance"], paid_until_before=None, paid_until_after=t["through"], status_after=st,
            days_overdue_after=ov, partial_credit_after=partial_exp, lessons=None,
            summary_text=tx.summary_text(
                past=0, current=sum(a.amount for a in allocs if a.bucket == "current"),
                advance=sum(a.amount for a in allocs if a.bucket == "advance"), past_debt_before=0,
                debt_after=t["debt"], paid_until_after=t["through"],
                advance_periods=[(a.period_from, a.period_to) for a in allocs if a.bucket == "advance"]))
        write(f"e2_preview_{amount}.json", PaymentPreview(
            as_of=today, mode="calendar", price=price, due=due, suggested_amount=323_000, amount=amount, result=res,
            warnings=[]), store)

    # ---------------- E3: price 300 000, stays 01-10.10 and 20.10-, today 25.10
    today = D(2026, 10, 25)
    dl = days(D(2026, 10, 1), D(2026, 10, 10)) + days(D(2026, 10, 20), D(2026, 10, 31))
    assert len(dl) == 22
    charge = round_1000(300_000 * 22, 31)
    assert charge == 213_000
    txt = tx.days_text(basis="days", billable_days=22, period_days=31)
    assert txt == "22 дня из 31"

    def segs3():
        return [HSeg("seg:0:2026-10-01", D(2026, 10, 1), D(2026, 10, 31), dl, charge, price=300_000, basis="days",
                     text=txt)]

    s0 = segs3()
    net, spans, tc = hand_ledger(s0, [], today)
    t0 = totals(s0, tc, today, 0)
    price3 = GROUP_PRICE(300_000)
    due3 = due_model(s0, t0, today, None, None)
    write("e3_preview_due.json", PaymentPreview(as_of=today, mode="calendar", price=price3, due=due3,
                                                 suggested_amount=213_000, amount=None, result=None, warnings=[]), store)
    for amount, (debt, through, partial_exp, d_exp) in {
        100_000: (113_000, D(2026, 10, 10), 3_182, 10),
        213_000: (0, D(2026, 10, 31), 0, 22),
    }.items():
        new = HCredit("new", "payment", today, amount)
        ss = segs3()
        net, spans, tc = hand_ledger(ss, [new], today)
        t = totals(ss, tc, today, amount)
        assert (t["debt"], t["through"]) == (debt, through), (amount, t)
        allocs = allocations_of(ss, [new], "new")
        assert allocs[0].days == d_exp
        sm = seg_model(ss[0], today)
        if sm.covered_days < sm.billable_days:
            assert sm.partial_credit == partial_exp
        st, ov, left = status_of(t["through"], today)
        res = PreviewResult(
            allocations=allocs, debt_after=t["debt"], past_debt_after=0, current_due_after=t["current"],
            advance_after=0, paid_until_before=None, paid_until_after=t["through"], status_after=st,
            days_overdue_after=ov, partial_credit_after=partial_exp, lessons=None,
            summary_text=tx.summary_text(past=0, current=amount, advance=0, past_debt_before=0, debt_after=t["debt"],
                                         paid_until_after=t["through"]))
        write(f"e3_preview_{amount}.json", PaymentPreview(
            as_of=today, mode="calendar", price=price3, due=due3, suggested_amount=213_000, amount=amount, result=res,
            warnings=[]), store)


# ================================================================================================================
# E4 - a transfer A -> B (statement of B)
# ================================================================================================================


def e4(store):
    today = D(2026, 10, 25)
    T = D(2026, 10, 11)
    charge_a = round_1000(500_000 * 11, 31)
    assert charge_a == 177_000
    x = 500_000 - charge_a
    assert x == 323_000
    dl = days(D(2026, 10, 12), D(2026, 10, 31))
    assert len(dl) == 20
    charge_b = round_1000(400_000 * 20, 31)
    assert charge_b == 258_000
    txt = tx.days_text(basis="days", billable_days=20, period_days=31)
    segs = [
        HSeg("seg:0:2026-10-01", D(2026, 10, 1), D(2026, 10, 31), dl, charge_b, price=400_000, basis="days", text=txt),
        HSeg("seg:0:2026-11-01", D(2026, 11, 1), D(2026, 11, 30), days(D(2026, 11, 1), D(2026, 11, 30)), 400_000,
             price=400_000),
    ]
    credits = [HCredit("tin", "transfer_in", T, x)]
    net, spans, tc = hand_ledger(segs, credits, today)
    t = totals(segs, tc, today, 0)
    assert (t["debt"], t["advance"], t["through"]) == (0, 65_000, D(2026, 11, 4))
    sm = [seg_model(s, today) for s in segs]
    assert sm[1].covered_days == 4 and sm[1].partial_credit == 11_667
    al = allocations_of(segs, credits, "tin")
    assert [(a.amount, a.days, a.bucket) for a in al] == [(258_000, 20, "current"), (65_000, 4, "advance")]
    rows = [
        StatementRow(seq=1, date=T, type="transfer_in", title="Перенос из «Математика 7А»", debit=0, credit=x,
                     outflow=0, balance_after=x, balance_text="Аванс 323 000", details=StatementDetails(
                         from_group_name="Математика 7А", from_membership_id=uid(1), allocations=al)),
        StatementRow(seq=2, date=D(2026, 10, 12), type="charge", title="Стоимость 01.10–31.10",
                     debit=charge_b, credit=0, outflow=0, balance_after=x - charge_b, balance_text="Аванс 65 000",
                     details=StatementDetails(segment=sm[0])),
    ]
    status, _, left = status_of(t["through"], today)
    write("e4_statement_B.json", StatementOut(
        membership=StatementMembership(
            membership_id=uid(2), student_id=uid(101), student_name="Хасанов Азиз", display_id="N13-004",
            group_id=uid(12), group_name="Алгебра 7Б", teacher_name="Петров Олег", is_active=True),
        as_of=today, mode="calendar", accounting_start=D(2026, 9, 1),
        summary=StatementSummary(charged=charge_b, paid=0, transferred_in=x, written_off=0, refunded=0,
                                 transferred_out=0, debt=0, advance=65_000, paid_until=t["through"], status=status,
                                 reconciles=(charge_b - x == 0 - 65_000)),
        rows=rows, voided=[], upcoming=[sm[1]]), store)
    # statement of the group being left (A): paid 500 000 on 01.10, left on T
    seg_a = [HSeg("seg:0:2026-10-01", D(2026, 10, 1), D(2026, 10, 11), days(D(2026, 10, 1), D(2026, 10, 11)),
                  charge_a, price=500_000, basis="days", text=tx.days_text(basis="days", billable_days=11, period_days=31))]
    cr_a = [HCredit("p21", "payment", D(2026, 10, 1), 500_000)]
    net, spans, tc = hand_ledger(seg_a, cr_a, today, outflow=x)
    assert tc == charge_a and seg_a[0].money == charge_a
    sma = seg_model(seg_a[0], today)
    assert sma.covered_days == 11 and sma.state == "closed"
    al_a = [Allocation(segment_id=seg_a[0].id, period_from=D(2026, 10, 1), period_to=D(2026, 10, 11), amount=500_000,
                       days=11, bucket="current", closes=True, price=500_000, segment_remaining_after=0,
                       label=tx.allocation_label(bucket="current", closes=True, start=D(2026, 10, 1), end=D(2026, 10, 11)))]
    rows_a = [
        StatementRow(seq=1, date=D(2026, 10, 1), type="charge", title="Стоимость 01.10–11.10", debit=charge_a, credit=0,
                     outflow=0, balance_after=-charge_a, balance_text="Долг 177 000", details=StatementDetails(segment=sma)),
        StatementRow(seq=2, date=D(2026, 10, 1), type="payment", title="Оплата № 21", debit=0, credit=500_000, outflow=0,
                     balance_after=x, balance_text="Аванс 323 000", details=StatementDetails(
                         payment_id=uid(21), receipt_no=21, method="cash", created_by_name="Рустам Каримов",
                         allocations=al_a)),
        StatementRow(seq=3, date=T, type="transfer_out", title="Перенос в «Алгебра 7Б»", debit=0, credit=0, outflow=x,
                     balance_after=0, balance_text="0", details=StatementDetails(
                         to_group_name="Алгебра 7Б", to_membership_id=uid(2))),
    ]
    write("e4_statement_A.json", StatementOut(
        membership=StatementMembership(
            membership_id=uid(1), student_id=uid(101), student_name="Хасанов Азиз", display_id="N13-004",
            group_id=uid(11), group_name="Математика 7А", teacher_name="Иванова Мария", is_active=False),
        as_of=today, mode="calendar", accounting_start=D(2026, 9, 1),
        summary=StatementSummary(charged=charge_a, paid=500_000, transferred_in=0, written_off=0, refunded=0,
                                 transferred_out=x, debt=0, advance=0, paid_until=D(2026, 10, 11), status="none",
                                 reconciles=(charge_a - 500_000 + x == 0)),
        rows=rows_a, voided=[], upcoming=[]), store)


# ================================================================================================================
# E5 - lessons packages (600 000 per 12 lessons = 50 000 a lesson)
# ================================================================================================================


def e5(store):
    N, P = 12, 600_000
    price = GROUP_PRICE(P)

    def cost(i):  # cost of the i-th (1-based) lesson of a package
        return P * i // N - P * (i - 1) // N

    assert all(cost(i) == 50_000 for i in range(1, 13))
    lesson_dates = [D(2026, 9, 25), D(2026, 9, 28), D(2026, 9, 30), D(2026, 10, 2), D(2026, 10, 5), D(2026, 10, 7),
                    D(2026, 10, 9), D(2026, 10, 12), D(2026, 10, 14), D(2026, 10, 16), D(2026, 10, 19),
                    D(2026, 10, 21), D(2026, 10, 23), D(2026, 10, 26)]

    def world(held: int, today: date):
        dl = lesson_dates[:held]
        pk1 = HSeg("seg:0:pkg1", dl[0], dl[min(held, 12) - 1], dl[:12], 600_000 if held >= 12 else cost(1) * held,
                   kind="package", price=P, basis="lessons", lessons=(min(held, 12), 12), pkg_no=1)
        segs = [pk1]
        if held > 12:
            segs.append(HSeg("seg:0:pkg2", dl[12], dl[held - 1], dl[12:], 50_000 * (held - 12), kind="package",
                             price=P, basis="lessons", lessons=(held - 12, 12), pkg_no=2))
        return segs

    def scenario(held: int, today: date, amount: int, name: str, covered_exp: int, debt_after_exp: int):
        segs = world(held, today)
        charged = sum(s.charge for s in segs)
        due_debt = charged
        past = sum(s.charge for s in segs if s.lessons[0] >= s.lessons[1])
        due_segments = []
        # before: no money
        for s in segs:
            s.money = s.wo = 0
        due_models = [seg_model(s, today) for s in segs]
        due = PreviewDue(debt=due_debt, past_debt=past, current_due=due_debt - past, advance=0, segments=due_models,
                         paid_until=None, days_overdue=None)
        # after: the money
        new = HCredit("new", "payment", today, amount)
        net, spans, tc = hand_ledger(segs, [new], today)
        leftover = max(0, tc - charged)
        covered_lessons = 0
        run = 0
        c = 0
        for i in range(1, 200):  # lessons held and virtual ones (price of today)
            run_cost = 50_000
            if c + run_cost <= tc:
                c += run_cost
                covered_lessons += 1
            else:
                break
        assert covered_lessons == covered_exp, (name, covered_lessons)
        debt_after = max(0, charged - tc)
        assert debt_after == debt_after_exp
        allocs = allocations_of(segs, [new], "new")
        if leftover:
            allocs.append(adv_alloc(leftover, today))
        used = held
        left = max(0, covered_lessons - used)
        unpaid = max(0, used - covered_lessons)
        partial = tc - covered_lessons * 50_000 if tc >= charged else 0
        st = "expired" if left == 0 else ("expiring" if left <= 3 else "active")
        res = PreviewResult(
            allocations=allocs, debt_after=debt_after, past_debt_after=sum(
                max(0, s.charge - s.money - s.wo) for s in segs if s.lessons[0] >= s.lessons[1]),
            current_due_after=0, advance_after=leftover, paid_until_before=None, paid_until_after=None,
            status_after=st, days_overdue_after=None, partial_credit_after=partial,
            lessons=PreviewLessons(covered_before=0, covered_after=covered_lessons, left_after=left, unpaid_after=unpaid),
            summary_text=tx.summary_text(
                past=sum(a.amount for a in allocs if a.bucket == "past_debt"),
                current=sum(a.amount for a in allocs if a.bucket == "current"),
                advance=leftover, past_debt_before=past, debt_after=debt_after, paid_until_after=None,
                lessons=(covered_lessons, left)))
        res.current_due_after = debt_after - res.past_debt_after
        write(name, PaymentPreview(
            as_of=today, mode="lessons", price=price, due=due, suggested_amount=due_debt, amount=amount, result=res,
            warnings=[]), store)

    scenario(12, D(2026, 10, 21), 630_000, "e5_preview_630000_held12.json", 12, 0)
    scenario(14, D(2026, 10, 27), 700_000, "e5_preview_700000_held14.json", 14, 0)
    scenario(14, D(2026, 10, 27), 650_000, "e5_preview_650000_held14.json", 13, 50_000)
    assert json.loads(store["e5_preview_630000_held12.json"])["result"]["summary_text"] == (
        "Закроет долг 600 000 → оплачено уроков 12, осталось 0 · аванс 30 000")


# ================================================================================================================
# E6 write-off, E7 refund
# ================================================================================================================


def e6_e7(store):
    price = GROUP_PRICE(500_000)
    today = D(2026, 10, 21)
    # E6: joined 15.10, paid 400 000 on 15.10, debt 100 000; written off 40 000 -> debt 60 000
    seg = [HSeg("seg:0:2026-10-15", D(2026, 10, 15), D(2026, 11, 14), days(D(2026, 10, 15), D(2026, 11, 14)), 500_000,
                price=500_000)]
    cr = [HCredit("p31", "payment", D(2026, 10, 15), 400_000), HCredit("w1", "writeoff", today, 40_000)]
    net, spans, tc = hand_ledger(seg, cr, today)
    t = totals(seg, tc, today, 400_000)
    assert (t["debt"], t["past"], t["current"]) == (60_000, 0, 60_000)
    sm = seg_model(seg[0], today)
    assert sm.covered_days == 27 and sm.covered_through == D(2026, 11, 10) and sm.partial_credit == 4_517
    st, ov, left = status_of(t["through"], today)
    a31 = allocations_of(seg, cr, "p31")
    p31 = pay_out(31, amount=400_000, paid_on=D(2026, 10, 15), receipt_no=31, first=D(2026, 10, 15), last=t["through"],
                  allocation=PaymentAllocationOut(items=a31, refunded=0, net=400_000), created_at="2026-10-15T10:00:00")
    row = row_model(mid=3, student="Каримова Алия", display_id="N13-007", price=price, status=st,
                    paid_until=t["through"], left=left, last_payment=p31, debt=60_000, past=0, current=60_000,
                    advance=0, partial=4_517, writeoff_allowed=True)
    wo = WriteoffOut(id=uid(41), membership_id=uid(3), amount=40_000, reason="болезнь, договорённость с родителем",
                     effective_on=today, created_by=uid(901), created_by_name="Рустам Каримов",
                     created_at="2026-10-21T11:00:00")
    write("e6_writeoff_40000.json", WriteoffCreated(writeoff=wo, debt_before=100_000, debt_after=60_000, row=row), store)

    # E7: the E1 student (790 000 paid) leaves on 25.10: P2 is cut to 15.10-25.10 (11 days of 31 = 177 000)
    today = D(2026, 10, 25)
    cut_charge = round_1000(500_000 * 11, 31)
    assert cut_charge == 177_000
    segs = [
        HSeg("seg:0:2026-09-15", D(2026, 9, 15), D(2026, 10, 14), days(D(2026, 9, 15), D(2026, 10, 14)), 500_000,
             price=500_000),
        HSeg("seg:0:2026-10-15", D(2026, 10, 15), D(2026, 10, 25), days(D(2026, 10, 15), D(2026, 10, 25)), cut_charge,
             price=500_000, basis="days", text=tx.days_text(basis="days", billable_days=11, period_days=31)),
    ]
    credits = [HCredit("p17", "payment", D(2026, 9, 20), 231_000), HCredit("p18", "payment", D(2026, 10, 21), 790_000)]
    net, spans, tc = hand_ledger(segs, credits, today)
    t = totals(segs, tc, today, 1_021_000)
    assert (t["charged"], t["debt"], t["advance"]) == (677_000, 0, 344_000)
    # the refund of the whole advance
    net, spans, tc = hand_ledger(segs, credits, today, outflow=344_000)
    t = totals(segs, tc, today, 1_021_000)
    assert (t["debt"], t["advance"], t["through"]) == (0, 0, D(2026, 10, 25))
    assert 677_000 - 1_021_000 + 344_000 == 0
    a18 = allocations_of(segs, credits, "p18")
    p18 = pay_out(18, amount=790_000, paid_on=D(2026, 10, 21), receipt_no=18, note="аванс", first=D(2026, 9, 15),
                  last=t["through"], allocation=PaymentAllocationOut(items=a18, refunded=344_000, net=446_000))
    row = row_model(price=price, status="none", paid_until=t["through"], last_payment=p18, debt=0, advance=0,
                    active=False)
    rf = RefundOut(id=uid(51), membership_id=uid(1), amount=344_000, method="cash", reason="ученик ушёл", note=None,
                   effective_on=today, created_by=uid(901), created_by_name="Рустам Каримов",
                   created_at="2026-10-25T12:00:00")
    write("e7_refund_344000.json", RefundCreated(refund=rf, advance_before=344_000, advance_after=0, row=row), store)


# ================================================================================================================
# the report of the centre, balances, settings, errors
# ================================================================================================================


def report(store):
    today = D(2026, 10, 21)
    # B (E1 student): 790 000 cash 21.10;  E (student 2): 200 000 card 12.10;  F (student 3): 550 000 cash 01.10, refunded
    # 324 000 on 15.10 (he left on 14.10: charge 14 days of 31 = 226 000);  write-off 30 000 of student 2 on 20.10.
    assert round_1000(500_000 * 14, 31) == 226_000
    assert 550_000 - 226_000 == 324_000
    totals_ = dict(received=1_540_000, refunded=324_000, net_received=1_216_000, refunded_from_received=324_000,
                   transferred_from_received=0, to_past_debt=269_000, to_current=926_000, to_advance=21_000, written_off=30_000, payments_count=3)
    assert totals_["to_past_debt"] + totals_["to_current"] + totals_["to_advance"] + totals_["refunded_from_received"] \
        == totals_["received"]
    charged = 1_000_000 + 600_000 + 226_000
    paid = 1_021_000 + 500_000 + 550_000
    left = charged - paid - 30_000 + 324_000
    assert left == 49_000 == 70_000 - 21_000
    rep = {
        "as_of": today, "from": D(2026, 10, 1), "to": D(2026, 10, 31), "group_by": "day",
        "totals": totals_,
        "by_method": [{"method": "cash", "amount": 1_340_000, "count": 2}, {"method": "card", "amount": 200_000, "count": 1}],
        "by_group": [
            {"group_id": uid(11), "group_name": "Математика 7А", "teacher_id": uid(21), "teacher_name": "Иванова Мария",
             "received": 1_340_000, "to_past_debt": 269_000, "to_current": 726_000, "to_advance": 21_000,
             "written_off": 0, "refunded": 324_000, "count": 2},
            {"group_id": uid(13), "group_name": "Алгебра 9Б", "teacher_id": uid(22), "teacher_name": "Петров Олег",
             "received": 200_000, "to_past_debt": 0, "to_current": 200_000, "to_advance": 0, "written_off": 30_000,
             "refunded": 0, "count": 1},
        ],
        "by_teacher": [
            {"teacher_id": uid(21), "teacher_name": "Иванова Мария", "received": 1_340_000, "to_past_debt": 269_000,
             "to_current": 726_000, "to_advance": 21_000, "written_off": 0, "refunded": 324_000, "count": 2},
            {"teacher_id": uid(22), "teacher_name": "Петров Олег", "received": 200_000, "to_past_debt": 0,
             "to_current": 200_000, "to_advance": 0, "written_off": 30_000, "refunded": 0, "count": 1},
        ],
        "series": [
            {"bucket": "2026-10-01", "received": 550_000, "by_method": {"cash": 550_000}, "to_past_debt": 0,
             "to_current": 226_000, "to_advance": 0, "written_off": 0, "refunded": 0, "count": 1},
            {"bucket": "2026-10-12", "received": 200_000, "by_method": {"card": 200_000}, "to_past_debt": 0,
             "to_current": 200_000, "to_advance": 0, "written_off": 0, "refunded": 0, "count": 1},
            {"bucket": "2026-10-15", "received": 0, "by_method": {}, "to_past_debt": 0, "to_current": 0, "to_advance": 0,
             "written_off": 0, "refunded": 324_000, "count": 0},
            {"bucket": "2026-10-20", "received": 0, "by_method": {}, "to_past_debt": 0, "to_current": 0, "to_advance": 0,
             "written_off": 30_000, "refunded": 0, "count": 0},
            {"bucket": "2026-10-21", "received": 790_000, "by_method": {"cash": 790_000}, "to_past_debt": 269_000,
             "to_current": 500_000, "to_advance": 21_000, "written_off": 0, "refunded": 0, "count": 1},
        ],
        "position": {"debt_total": 70_000, "past_debt_total": 0, "advance_total": 21_000, "memberships_with_debt": 1,
                     "students_with_debt": 1, "memberships_with_advance": 1},
        "reconciliation": {"ok": True, "checks": [
            {"code": "received_eq_allocated_plus_refunded", "left": 1_540_000, "right": 1_216_000 + 324_000, "diff": 0},
            {"code": "charged_minus_paid_eq_debt_minus_advance", "left": left, "right": 49_000, "diff": 0},
            {"code": "transfers_net_zero", "left": 0, "right": 0, "diff": 0}]},
    }
    assert sum(r["received"] for r in rep["series"]) == totals_["received"]
    assert sum(r["to_current"] for r in rep["series"]) == totals_["to_current"]
    assert sum(r["refunded"] for r in rep["series"]) == totals_["refunded"]
    write("receipts_report_2026-10.json", ReceiptsReport.model_validate(rep), store)

    # drill-down: «на прошлые долги» -> the payment of the E1 student
    segs, credits, t = e1_state([HCredit("p18", "payment", TODAY, 790_000)])
    a18 = allocations_of(segs, credits, "p18")
    item = ReceiptItem(
        type="payment", id=uid(18), date=TODAY, amount=790_000, column_amount=269_000, student_id=uid(101),
        student_name="Хасанов Азиз", membership_id=uid(1), group_name="Математика 7А", teacher_name="Иванова Мария",
        method="cash", receipt_no=18, allocations=a18)
    write("receipts_items_to_past_debt.json", ReceiptItems(items=[item], total=1, limit=50, offset=0), store)

    row = BalanceRow(
        membership_id=uid(5), student_id=uid(105), student_name="Каримова Алия", display_id="N13-007",
        group_id=uid(13), group_name="Алгебра 9Б", teacher_id=uid(22), teacher_name="Петров Олег", is_active=True,
        student_archived=False, status="active", paid_until=D(2026, 11, 1), debt=70_000, past_debt=0,
        current_due=70_000, advance=0, refundable=0)
    write("balances_debt.json", BalancesPage(items=[row], total=1, limit=50, offset=0), store)

    st = PaymentSettingsChanged(
        period_mode="calendar", billing_anchor="payment_date", billing_day=1, lessons_per_payment=12,
        expiring_days=3, expiring_lessons=3, excuse_deadline_days=5, excuse_deadline_lessons=2,
        admin_can_manage=True, accounting_start_date=D(2026, 9, 1), updated_by=uid(901),
        updated_at="2026-10-01T08:00:00", dry_run=True,
        impact={"memberships_with_new_debt": 12, "debt_delta": 5_400_000, "memberships_with_changed_advance": 3,
                "advance_delta": -1_200_000, "refundable_delta": -900_000})
    write("settings_dry_run.json", st, store)

    # error bodies the front maps to texts
    errors = {
        "WRITEOFF_EXCEEDS_DEBT": (422, PayError(detail=dict(code="WRITEOFF_EXCEEDS_DEBT",
                                                           message="Простить можно не больше долга: 60 000 сум", debt=60_000))),
        "REFUND_EXCEEDS_ADVANCE": (422, PayError(detail=dict(code="REFUND_EXCEEDS_ADVANCE",
                                                             message="Вернуть можно не больше 344 000 сум",
                                                             refundable=344_000))),
        "DUPLICATE_PAYMENT": (409, PayError(detail=dict(code="DUPLICATE_PAYMENT", message="Оплата уже внесена",
                                                         payment_id=uid(18)))),
        "VOID_BREAKS_REFUND": (409, PayError(detail=dict(
            code="VOID_BREAKS_REFUND", message="Сначала отмените возврат: без этой оплаты вернуть нечего"))),
        "AMOUNT_RANGE": (422, PayError(detail=dict(code="AMOUNT_RANGE", message="Сумма оплаты: от 1 до 10 000 000 000"))),
    }
    store["errors.json"] = json.dumps(
        {k: {"status": v[0], "body": v[1].model_dump(mode="json")} for k, v in errors.items()},
        ensure_ascii=False, indent=2) + "\n"


MODELS = {
    # fixture -> response model name of the contract (checked by tests/test_pay2_golden.py)
    "e1_preview_*": "PaymentPreview", "e9_preview_*": "PaymentPreview", "e2_preview_*": "PaymentPreview", "e3_preview_*": "PaymentPreview",
    "e5_preview_*": "PaymentPreview", "e1_create_300000": "PaymentCreated", "e8_replace_*": "PaymentCreated",
    "e1_void_300000": "PaymentVoided", "e1_receipt_snapshot": "ReceiptSnapshot", "e1_receipt_300000": "ReceiptOut",
    "e1_statement_790000": "StatementOut", "e4_statement_*": "StatementOut", "e1_row_after_790000": "MembershipPaymentRow",
    "e6_writeoff_40000": "WriteoffCreated", "e7_refund_344000": "RefundCreated",
    "receipts_report_2026-10": "ReceiptsReport", "receipts_items_to_past_debt": "ReceiptItems",
    "balances_debt": "BalancesPage", "settings_dry_run": "PaymentSettingsChanged",
}


def build() -> Dict[str, str]:
    store: Dict[str, str] = {}
    e1_previews(store)
    e1_create_replace_void(store)
    e9_price_ahead(store)
    e1_statement(store)
    e2_e3(store)
    e4(store)
    e5(store)
    e6_e7(store)
    report(store)
    store["index.json"] = json.dumps(MODELS, ensure_ascii=False, indent=2) + "\n"
    return store


def main() -> int:
    store = build()
    if "--check" in sys.argv:
        bad = [n for n, text in store.items() if not (OUT / n).exists() or (OUT / n).read_text(encoding="utf-8") != text]
        if bad:
            print("fixtures differ:", bad)
            return 1
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in store.items():
        (OUT / name).write_text(text, encoding="utf-8")
    print(f"{len(store)} fixtures -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
