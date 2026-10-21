"""Builders for the pure-ledger tests: the worlds of the design examples E1-E9 (same as ``scripts/pay2_gen_fixtures.py``)."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

from src.auth import billing_ledger as bl
from src.auth import payment_status as ps

D = date
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pay2"
T0 = datetime(2026, 1, 1)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def flat(amount: Optional[int], since: date = D(2026, 1, 1), changes: Optional[dict] = None):
    """``price_at``: one price from ``since``; ``changes`` = {from_day: amount} later rows."""
    rows = sorted([(since, amount)] + list((changes or {}).items()))

    def price_at(day: date) -> bl.PriceInfo:
        current = None
        for start, value in rows:
            if start <= day:
                current = (start, value)
        if current is None:
            return bl.NO_PRICE
        start, value = current
        if value is None:
            return bl.NO_PRICE
        return bl.PriceInfo(value, "group", start, value, None)

    return price_at


def credit(cid: str, on: date, amount: int, kind: str = bl.PAYMENT, minutes: int = 0) -> bl.Credit:
    return bl.Credit(cid, kind, on, T0 + timedelta(minutes=minutes), amount)


def outflow(oid: str, on: date, amount: int, kind: str = bl.REFUND, minutes: int = 0) -> bl.Outflow:
    return bl.Outflow(oid, kind, on, T0 + timedelta(minutes=minutes), amount)


def make(*, today: date, stays, price_at, credits=(), outflows=(), regimes=None, start: date = D(2026, 9, 1),
         floor: Optional[date] = None, lesson_days=None, counted=(), expiring_days: int = 3, expiring_lessons: int = 3):
    return bl.LedgerInput(
        today=today, accounting_start=start, stays=tuple(stays), regimes=tuple(regimes or [bl.Regime()]),
        price_at=price_at, credits=tuple(credits), outflows=tuple(outflows), first_billable_floor=floor,
        lesson_days=lesson_days or (lambda a, b: ()), counted_lessons=tuple(counted),
        expiring_days=expiring_days, expiring_lessons=expiring_lessons,
    )


def weekly(weekdays, first: date = D(2026, 1, 1)):
    """``lesson_days`` for a group that meets on the given weekdays (Mon=0)."""

    def days(a: date, b: date):
        out, d = [], a
        while d <= b:
            if d.weekday() in weekdays:
                out.append(d)
            d += timedelta(days=1)
        return out

    return days


# ---- the worlds of the examples ---------------------------------------------------------------------------

TODAY = D(2026, 10, 21)
A_PAY = credit("p17", D(2026, 9, 20), 231_000)


def e1(extra=(), outflows=(), today: date = TODAY, price_at=None, stays=None):
    return make(today=today, stays=stays or [bl.Stay(D(2026, 9, 15))], price_at=price_at or flat(500_000),
                credits=[A_PAY, *extra], outflows=outflows)


def e2(extra=()):
    reg = bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)
    return make(today=D(2026, 10, 21), stays=[bl.Stay(D(2026, 10, 16))], price_at=flat(600_000), regimes=[reg],
                credits=extra, lesson_days=weekly({0, 2, 4}))


def e3(extra=()):
    reg = bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)
    return make(today=D(2026, 10, 25), stays=[bl.Stay(D(2026, 10, 1), D(2026, 10, 10)), bl.Stay(D(2026, 10, 20))],
                price_at=flat(300_000), regimes=[reg], credits=extra)


LESSON_DATES = [D(2026, 9, 25), D(2026, 9, 28), D(2026, 9, 30), D(2026, 10, 2), D(2026, 10, 5), D(2026, 10, 7),
                D(2026, 10, 9), D(2026, 10, 12), D(2026, 10, 14), D(2026, 10, 16), D(2026, 10, 19), D(2026, 10, 21),
                D(2026, 10, 23), D(2026, 10, 26)]


def e5(held: int, today: date, extra=()):
    reg = bl.Regime(mode=bl.LESSONS, package_size=12)
    return make(today=today, stays=[bl.Stay(D(2026, 9, 25))], price_at=flat(600_000), regimes=[reg], credits=extra,
                counted=[bl.CountedLesson(d, f"l{i}") for i, d in enumerate(LESSON_DATES[:held])])


def seg_dict(s: bl.Segment) -> dict:
    return {
        "id": s.id, "kind": s.kind, "period_from": s.period_start.isoformat(), "period_to": s.period_end.isoformat(),
        "billable_days": len(s.days), "days_text": s.days_text, "charge": s.amount, "basis": s.basis,
        "paid": s.money, "written_off": s.forgiven, "unpaid": s.unpaid, "covered_days": s.covered_days,
        "covered_through": s.covered_through.isoformat() if s.covered_through else None,
        "partial_credit": s.partial_credit, "state": s.state, "bucket": s.bucket, "price_amount": s.price.amount,
    }


def fixture_seg(d: dict) -> dict:
    out = {k: d[k] for k in ("id", "kind", "period_from", "period_to", "billable_days", "days_text", "charge", "basis",
                             "paid", "written_off", "unpaid", "covered_days", "covered_through", "partial_credit",
                             "state", "bucket")}
    out["price_amount"] = d["price"]["amount"]
    return out


def alloc_tuple(a: bl.Allocation):
    return (a.segment_id, a.amount, a.days, a.bucket, a.closes)


def fixture_alloc_tuple(a: dict):
    return (a["segment_id"], a["amount"], a["days"], a["bucket"], a["closes"])
