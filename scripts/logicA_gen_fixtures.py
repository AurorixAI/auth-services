#!/usr/bin/env python3
"""LOGIC-A contract fixtures: tests/fixtures/logicA/*.json, generated from the chains of the spec
(``artifacts/partner_doc_check/logicA_spec.md`` §2-§3) by a deliberately naive, INDEPENDENT hand calculator
(``Hand``: day by day, money line over charges, floor days - it shares no code with ``billing_ledger``).  Every headline
number is asserted against the figure written in the spec.  The production ledger must reproduce them
(``tests/test_logicA_golden.py``); the front engineer builds mocks and contract tests on the same files.

    python scripts/logicA_gen_fixtures.py          # rewrite tests/fixtures/logicA
    python scripts/logicA_gen_fixtures.py --check  # fail when the files on disk differ (CI / test)
"""
from __future__ import annotations

import json
import os
import sys
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("SECRET_KEY", "ci-tests-only-secret-at-least-32-characters")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://x:x@localhost/x")  # never connected

from src.auth import payments_texts as tx  # noqa: E402
from src.auth.payments_schemas import (  # noqa: E402
    FreshBalance,
    MembershipPriceOut,
    PaymentsBuckets,
    PaymentsSummary,
    PriceOverrideChanged,
    Segment,
    StatementDetails,
    StatementMembership,
    StatementOut,
    StatementRow,
    StatementSummary,
    GroupLessonTypeChanged,
    GroupLessonTypes,
    GroupPriceChanged,
    ImpactMoney,
    LeaveMembership,
    LeaveMoney,
    LeavePreview,
    LeaveResult,
    LessonTypeImpact,
    LessonTypeRow,
    LessonTypeSheet,
    LogicAError,
    MembershipPaymentRow,
    OpeningBrief,
    OpeningOut,
    OpeningSaved,
    OpeningVoided,
    PausedRange,
    PauseLessonMark,
    PauseLessons,
    PauseOut,
    PausePayroll,
    PauseSaved,
    PauseVoided,
    PaymentSettingsChanged,
    PriceBlockedStudent,
    PriceImpact,
    PriceImpactItem,
    PriceImpactMoney,
    PriceOut,
    RegimeImpact,
    SettledOut,
)
from src.auth import payroll_engine as _engine  # noqa: E402
from src.auth.payroll_schemas import (  # noqa: E402
    GroupBlockOut,
    DivisorRow,
    LineOut,
    MarkOut,
    PayoutCreated,
    PayoutOut,
    PayoutState,
    PayoutVoided,
    PayrollSettings,
    PriceStepOut,
    SheetOut,
)

OUT = ROOT / "tests" / "fixtures" / "logicA"
D = date
TODAY = D(2026, 10, 21)


def uid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def round_1000(num: int, den: int = 1) -> int:
    return ((2 * num + 1000 * den) // (2000 * den)) * 1000


def span(a: date, b: date) -> List[date]:
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


# ------------------------------------------------------------------ the hand calculator -----------------------------
# calendar mode, fixed billing day 1 (calendar months), no schedule: every incomplete period is charged by days.


@dataclass
class HSeg:
    start: date
    end: date
    days: List[date]
    amount: int
    kind: str = "period"  # period | opening
    money: int = 0

    @property
    def covered(self) -> int:
        if self.kind == "opening":
            return 0
        return len(self.days) if self.amount == 0 else min(len(self.days), self.money * len(self.days) // self.amount)


@dataclass
class Hand:
    today: date
    accounting_start: date
    stays: List[tuple]  # (first, last|None)
    price: object  # callable day -> Optional[int]
    payments: List[tuple] = field(default_factory=list)  # (on, amount)
    writeoffs: int = 0
    refunds: int = 0
    transfer_out: int = 0
    transfer_in: int = 0
    pauses: List[tuple] = field(default_factory=list)
    paid_until: Optional[date] = None
    debt: int = 0
    debt_on: Optional[date] = None
    floor_extra: Optional[date] = None

    def run(self) -> dict:
        floor = max(self.accounting_start, self.floor_extra or date.min, (self.paid_until + timedelta(days=1)) if self.paid_until else date.min)
        credit = sum(a for _, a in self.payments) + self.transfer_in + self.writeoffs - self.refunds - self.transfer_out
        money_net = sum(a for _, a in self.payments) + self.transfer_in - self.refunds - self.transfer_out
        segs: List[HSeg] = []
        if self.debt:
            segs.append(HSeg(self.debt_on, self.debt_on, [], self.debt, "opening"))
        horizon = D(self.today.year + 3, 1, 1)

        def bill_days(first, last):
            lo = max(first, floor)
            hi = last if last is not None else horizon
            return [d for d in span(lo, hi) if not any(a <= d <= b for a, b in self.pauses)] if lo <= hi else []

        by_month: Dict[tuple, List[date]] = {}
        for first, last in self.stays:
            # an open stay is generated month by month while it is needed
            for d in bill_days(first, last if last else min(horizon, D(2099, 1, 1)) if False else last):
                pass
        # closed stays: all their days; open stay: months up to today's, then while the money is not absorbed
        for first, last in self.stays:
            if last is not None:
                for d in bill_days(first, last):
                    by_month.setdefault((d.year, d.month), []).append(d)
        open_first = next((f for f, l in self.stays if l is None), None)
        cum = sum(s.amount for s in segs)
        months = sorted(by_month)
        def seg_of(year, month, days_in):
            start, end = D(year, month, 1), D(year, month, monthrange(year, month)[1])
            first = days_in[0]
            p = self.price(first)
            if p is None:
                return None
            n, nominal = len(days_in), (end - start).days + 1
            amount = p if n >= nominal else round_1000(p * n, nominal)
            return HSeg(start, end, days_in, amount)
        if open_first is not None:
            y, m = max(open_first, floor).year, max(open_first, floor).month
            for _ in range(40):
                start = D(y, m, 1)
                end = D(y, m, monthrange(y, m)[1])
                if start > self.today and cum >= credit:
                    break
                days_in = sorted(set(by_month.get((y, m), []) + [d for d in bill_days(open_first, None) if start <= d <= end]))
                if days_in:
                    s = seg_of(y, m, days_in)
                    if s:
                        segs.append(s)
                        cum += s.amount
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
            by_month = {k: v for k, v in by_month.items() if False}
        for (y, m) in months:
            if open_first is not None and (y, m) >= (max(open_first, floor).year, max(open_first, floor).month):
                continue
            s = seg_of(y, m, sorted(by_month[(y, m)]))
            if s:
                segs.append(s)
        segs.sort(key=lambda s: (s.days[0] if s.days else s.start, s.start))
        pos = 0
        for s in segs:
            s.money = max(0, min(credit, pos + s.amount) - pos)
            pos += s.amount
        begun = [s for s in segs if (s.days[0] if s.days else s.start) <= self.today]
        charged = sum(s.amount for s in begun)
        debt = max(0, charged - credit)
        advance = max(0, credit - charged)
        past = min(sum(s.amount - s.money for s in begun if s.end < self.today), debt)
        through = self.paid_until
        blocked = False
        for s in segs:
            if s.kind == "opening":
                if s.amount - s.money > 0:
                    blocked = True
                    break
                continue
            if s.covered >= len(s.days):
                through = s.days[-1]
                continue
            through = s.days[s.covered - 1] if s.covered else (through if through else s.days[0] - timedelta(days=1))
            break
        return dict(charged=charged, debt=debt, advance=advance, past=past, refundable=min(advance, money_net), through=through,
                    segs=segs, credit=credit)


def price_flat(amount, changes=None):
    changes = sorted((changes or {}).items())

    def at(day):
        cur = amount
        for d, a in changes:
            if day >= d:
                cur = a
        return cur
    return at


# ------------------------------------------------------------------ builders --------------------------------------


def write(name: str, model, store: Dict[str, str]) -> None:
    store[name] = json.dumps(model.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2) + "\n"


def group_price(amount, since=D(2026, 1, 1)):
    return PriceOut(amount=amount, source="group", valid_from=since, group_amount=amount, reason=None)


def row(n: int, name: str, group: str, price: PriceOut, status: str, *, res: dict, today: date, paused=None, opening=None,
        left_override=False, active=True, overdue=None) -> MembershipPaymentRow:
    through = res["through"]
    delta = (through - today).days if through else None
    return MembershipPaymentRow(
        membership_id=uid(100 + n), student_id=uid(200 + n), student_name=name, display_id=f"S-{1000 + n}", class_level=7,
        group_id=uid(300 + n), group_name=group, teacher_id=uid(400), teacher_name="Каримова", is_primary=True, is_active=active,
        price=price, monthly_fee=price.amount, status=status, period_mode="calendar",
        paid_until=through if status != "none" else None,
        days_overdue=(overdue if overdue is not None else (-delta if status == "expired" and delta is not None else None)),
        days_left=(delta if status in ("active", "expiring", "ends_today") else None),
        debt=res["debt"], past_debt=res["past"], current_due=res["debt"] - res["past"], advance=res["advance"],
        partial_credit=0, price_missing=False, lessons_unpaid=0, refundable=res["refundable"], writeoff_allowed=res["debt"] > 0,
        paused=PausedRange(**{"from": paused[0], "to": paused[1]}) if paused else None, opening=opening, price_ends_on_leave=left_override,
    )


def impact(before: dict, after: dict) -> ImpactMoney:
    return ImpactMoney(
        debt_before=before["debt"], debt_after=after["debt"], advance_before=before["advance"], advance_after=after["advance"],
        paid_until_before=before["through"], paid_until_after=after["through"], charged_delta=after["charged"] - before["charged"],
        refundable_before=before["refundable"], refundable_after=after["refundable"])


A_PRICE = 620_000
B_PRICE_OCT, B_PRICE_NOV = 465_000, 540_000
AS = D(2026, 10, 1)


def opening_chain(store):
    """G-OPEN: centre fixed_day=1, price 600 000, accounting start 01.10, today 21.10, enrolled 21.10."""
    price = price_flat(600_000)
    base = dict(today=TODAY, accounting_start=AS, stays=[(TODAY, None)], price=price)
    none = Hand(**base).run()
    assert (none["debt"], none["charged"]) == (213_000, 213_000)
    stamp = "2026-10-21T09:30:00"

    def opening_out(n, **kw) -> OpeningOut:
        return OpeningOut(id=uid(n), membership_id=uid(101), note="из таблицы Excel", source="manual", created_by_name="Арслан",
                          created_at=stamp, **kw)

    # A: paid until 31.10
    a = Hand(**base, paid_until=D(2026, 10, 31)).run()
    assert (a["debt"], a["advance"], a["through"], a["charged"]) == (0, 0, D(2026, 10, 31), 0)
    o = opening_out(1, paid_until=D(2026, 10, 31))
    write("A01_opening_paid_until_31_10.json", OpeningSaved(
        opening=o, impact=impact(none, a), dry_run=False,
        row=row(1, "Каримов Алишер", "A-1", group_price(600_000, D(2026, 1, 1)), "active", res=a, today=TODAY,
                opening=OpeningBrief(id=uid(1), paid_until=D(2026, 10, 31)))), store)
    # A at 02.11 without money: November begun, covered through 31.10, debt 600 000
    a2 = Hand(**{**base, "today": D(2026, 11, 2)}, paid_until=D(2026, 10, 31)).run()
    assert (a2["debt"], a2["through"]) == (600_000, D(2026, 10, 31)) and (D(2026, 11, 2) - a2["through"]).days == 2
    # B: debt 250 000 on 30.09
    b = Hand(**base, debt=250_000, debt_on=D(2026, 9, 30)).run()
    assert (b["debt"], b["past"], b["charged"]) == (463_000, 250_000, 463_000)
    write("A02_opening_debt_250000.json", OpeningSaved(
        opening=opening_out(2, debt_amount=250_000, debt_on=D(2026, 9, 30)), impact=impact(none, b), dry_run=False,
        row=row(1, "Каримов Алишер", "A-1", group_price(600_000), "expired", res=b, today=TODAY, overdue=21,
                opening=OpeningBrief(id=uid(2), debt_amount=250_000, debt_on=D(2026, 9, 30)))), store)
    pay = Hand(**base, debt=250_000, debt_on=D(2026, 9, 30), payments=[(TODAY, 300_000)]).run()
    assert pay["debt"] == 163_000 and pay["through"] == D(2026, 10, 22)
    # C: paid until 25.10 + debt 100 000 on 30.09
    c = Hand(**base, paid_until=D(2026, 10, 25), debt=100_000, debt_on=D(2026, 9, 30)).run()
    assert (c["debt"], c["through"]) == (100_000, D(2026, 10, 25))
    assert [s.amount for s in c["segs"]] == [100_000, 116_000]
    write("A03_opening_both_dry_run.json", OpeningSaved(
        opening=opening_out(3, paid_until=D(2026, 10, 25), debt_amount=100_000, debt_on=D(2026, 9, 30)), impact=impact(none, c),
        dry_run=True,
        row=row(1, "Каримов Алишер", "A-1", group_price(600_000), "active", res=c, today=TODAY,
                opening=OpeningBrief(id=uid(3), paid_until=D(2026, 10, 25), debt_amount=100_000, debt_on=D(2026, 9, 30)))), store)
    write("A04_opening_get.json", opening_out(2, debt_amount=250_000, debt_on=D(2026, 9, 30)), store)
    voided = opening_out(2, debt_amount=250_000, debt_on=D(2026, 9, 30))
    voided.voided_at = "2026-10-21T10:00:00"
    voided.void_reason = "ошибка в таблице"
    write("A05_opening_void.json", OpeningVoided(
        opening=voided, impact=impact(b, none),
        row=row(1, "Каримов Алишер", "A-1", group_price(600_000), "expired", res=none, today=TODAY)), store)
    return pay


def chain1(store):
    """Chain 1 (§3): A 620 000 / B 465 000 (540 000 from 01.11), accounting start 01.10, fixed_day=1."""
    pa = price_flat(A_PRICE)
    # 21.10: opening paid until 15.10 + payment 300 000 on 16.10 + pause 20-24.10 entered on 21.10
    base_a = dict(accounting_start=AS, stays=[(D(2026, 10, 11), None)], price=pa, paid_until=D(2026, 10, 15))
    s11 = Hand(today=D(2026, 10, 11), **base_a).run()
    assert (s11["debt"], s11["advance"], s11["through"]) == (0, 0, D(2026, 10, 15))
    s16 = Hand(today=D(2026, 10, 16), payments=[(D(2026, 10, 16), 300_000)], **base_a).run()
    assert (s16["debt"], s16["through"]) == (20_000, D(2026, 10, 30)) and s16["segs"][0].amount == 320_000
    pause = [(D(2026, 10, 20), D(2026, 10, 24))]
    before = Hand(today=TODAY, payments=[(D(2026, 10, 16), 300_000)], **base_a).run()
    after = Hand(today=TODAY, payments=[(D(2026, 10, 16), 300_000)], pauses=pause, **base_a).run()
    assert (after["charged"], after["advance"], after["through"], after["refundable"]) == (220_000, 80_000, D(2026, 11, 3), 80_000)
    pause_out = PauseOut(id=uid(11), membership_id=uid(101), first_day=D(2026, 10, 20), last_day=D(2026, 10, 24), days=5,
                         reason="уехал к родственникам", created_by_name="Арслан", created_at="2026-10-21T09:45:00")
    write("A06_pause_create.json", PauseSaved(
        pause=pause_out, impact=impact(before, after),
        lessons=PauseLessons(excluded=[PauseLessonMark(date=D(2026, 10, 20), marks="absent"),
                                       PauseLessonMark(date=D(2026, 10, 22), marks=None)], attended=[]),
        payroll=PausePayroll(months=["2026-10"], saved_sheets_changed=1), dry_run=False,
        row=row(1, "Каримов Алишер", "A-1", group_price(A_PRICE), "paused", res=after, today=TODAY,
                paused=(D(2026, 10, 20), D(2026, 10, 24)), opening=OpeningBrief(id=uid(1), paid_until=D(2026, 10, 15)))), store)
    voided = pause_out.model_copy(update={"voided_at": "2026-10-21T11:00:00", "void_reason": "ошибся датой"})
    write("A07_pause_void.json", PauseVoided(
        pause=voided, impact=impact(after, before),
        row=row(1, "Каримов Алишер", "A-1", group_price(A_PRICE), "active", res=before, today=TODAY,
                opening=OpeningBrief(id=uid(1), paid_until=D(2026, 10, 15)))), store)
    # 26.10 transfer to B: X = refundable of A on 26.10
    a26 = Hand(today=D(2026, 10, 26), payments=[(D(2026, 10, 16), 300_000)], pauses=pause,
               **{**base_a, "stays": [(D(2026, 10, 11), D(2026, 10, 26))]}).run()
    assert (a26["charged"], a26["refundable"]) == (120_000, 180_000)
    pb = price_flat(B_PRICE_OCT, {D(2026, 11, 1): B_PRICE_NOV})
    b27 = Hand(today=D(2026, 10, 28), accounting_start=AS, stays=[(D(2026, 10, 26), None)], price=pb, transfer_in=180_000,
               floor_extra=D(2026, 10, 27)).run()
    assert (b27["charged"], b27["advance"], b27["through"]) == (75_000, 105_000, D(2026, 11, 5))
    # 29.10 leave with refund
    b29 = Hand(today=D(2026, 10, 29), accounting_start=AS, stays=[(D(2026, 10, 26), D(2026, 10, 29))], price=pb, transfer_in=180_000,
               floor_extra=D(2026, 10, 27)).run()
    assert (b29["charged"], b29["advance"], b29["refundable"]) == (45_000, 135_000, 135_000)
    assert 120_000 + 45_000 + 135_000 == 300_000
    write("A09_leave_preview_refund_135000.json", LeavePreview(as_of=D(2026, 10, 29), memberships=[LeaveMembership(
        membership_id=uid(102), group_id=uid(302), group_name="B-1", has_open_balance=True, allowed=["keep", "refund"],
        money=LeaveMoney(debt=0, advance=135_000, refundable=135_000, paid_until=D(2026, 10, 29), charged_after_leave=45_000,
                         individual_price_ends=False))]), store)
    write("A10_leave_result_refund.json", LeaveResult(message="Ученик убран из группы", settled=[SettledOut(
        membership_id=uid(102), action="refund", amount=135_000, refund_id=uid(61))]), store)
    return b27


def chain4_6(store):
    price = price_flat(600_000)
    base = dict(accounting_start=AS, stays=[(TODAY, None)], price=price, debt=250_000, debt_on=D(2026, 9, 30))
    arch = Hand(today=D(2026, 10, 25), **{**base, "stays": [(TODAY, D(2026, 10, 25))]}).run()
    assert arch["debt"] == 347_000 and arch["charged"] == 347_000
    write("A11_archive_preview_debt_347000.json", LeavePreview(as_of=D(2026, 10, 25), memberships=[LeaveMembership(
        membership_id=uid(101), group_id=uid(301), group_name="A-1", has_open_balance=True, allowed=["keep", "writeoff"],
        money=LeaveMoney(debt=347_000, advance=0, refundable=0, paid_until=None, charged_after_leave=347_000,
                         individual_price_ends=False))]), store)
    # chain 6: individual 400 000 since 01.09, left 10.10 -> debt 129 000 for 01-10.10, Sep paid
    p6 = price_flat(400_000)
    left = Hand(today=D(2026, 10, 10), accounting_start=D(2026, 9, 1), stays=[(D(2026, 9, 1), D(2026, 10, 10))], price=p6,
                payments=[(D(2026, 9, 1), 400_000)]).run()
    assert (left["debt"], left["charged"]) == (129_000, 529_000)
    write("A12_leave_preview_individual_price_ends.json", LeavePreview(as_of=D(2026, 10, 10), memberships=[LeaveMembership(
        membership_id=uid(103), group_id=uid(303), group_name="C-1", has_open_balance=True, allowed=["keep", "writeoff"],
        money=LeaveMoney(debt=129_000, advance=0, refundable=0, paid_until=D(2026, 9, 30), charged_after_leave=529_000,
                         individual_price_ends=True))]), store)
    back = Hand(today=D(2026, 11, 30), accounting_start=D(2026, 9, 1), stays=[(D(2026, 11, 20), None)], price=price_flat(600_000)).run()
    assert back["segs"][0].amount == 220_000  # 11 of 30 days at the group price 600 000


def price_chain(store):
    # chain 7: group 500 000; A paid 500 000 on 01.10; B2 paid 700 000 on 20.10 (advance 200 000)
    old = price_flat(500_000)
    new = price_flat(500_000, {D(2026, 11, 1): 600_000})
    base = dict(today=TODAY, accounting_start=AS, stays=[(D(2026, 10, 1), None)], payments=[(D(2026, 10, 20), 700_000)])
    b_before, b_after = Hand(price=old, **base).run(), Hand(price=new, **base).run()
    assert (b_before["advance"], b_before["through"], b_after["through"]) == (200_000, D(2026, 11, 12), D(2026, 11, 10))
    write("A14_price_future_impact_money.json", GroupPriceChanged(
        dry_run=True,
        impact=PriceImpact(students_following=12, students_with_override=1, stale_payroll_sheets=0, stale_sheets=[],
                           money=PriceImpactMoney(
                               memberships_with_new_debt=0, debt_delta=0, memberships_with_less_advance=1, advance_delta=0,
                               days_lost_total=2, backdated=False,
                               items=[PriceImpactItem(membership_id=uid(102), student_name="Ахмедов Б.", debt_before=0, debt_after=0,
                                                      advance_before=200_000, advance_after=200_000,
                                                      paid_until_before=b_before["through"], paid_until_after=b_after["through"],
                                                      has_money=True)]))), store)
    # backdated, nobody has money: debt appears
    write("A15_price_backdated_impact_no_payments.json", GroupPriceChanged(
        dry_run=True,
        impact=PriceImpact(students_following=2, students_with_override=0, stale_payroll_sheets=1, stale_sheets=[],
                           money=PriceImpactMoney(
                               memberships_with_new_debt=2, debt_delta=200_000, memberships_with_less_advance=0, advance_delta=0,
                               days_lost_total=0, backdated=True,
                               items=[PriceImpactItem(membership_id=uid(110 + i), student_name=f"Ученик {i}", debt_before=0,
                                                      debt_after=100_000, advance_before=0, advance_after=0, paid_until_before=None,
                                                      paid_until_after=None, has_money=False) for i in (1, 2)]))), store)


def settings_chain(store):
    # chain 5: 38 students, first switch 01.11, last 14.11
    write("A16_settings_regime_impact.json", PaymentSettingsChanged(
        period_mode="calendar", billing_anchor="payment_date", billing_day=1, lessons_per_payment=12, expiring_days=3,
        expiring_lessons=3, excuse_deadline_days=5, excuse_deadline_lessons=2, admin_can_manage=False,
        accounting_start_date=AS, updated_by="Арслан", updated_at="2026-10-01T08:00:00", dry_run=True, impact=None,
        regime_impact=RegimeImpact(students_affected=38, first_switch=D(2026, 11, 1), latest_switch=D(2026, 11, 14), defer=True)), store)
    # chain 5 numbers: payment_date, joined 15.10, price 600 000 paid; the running period (15.10-14.11) is untouched
    h = Hand(today=TODAY, accounting_start=AS, stays=[(D(2026, 10, 15), None)], price=price_flat(600_000), payments=[(D(2026, 10, 15), 600_000)])
    # (the hand calculator has calendar months; the payment_date chain is checked by tests/test_logicA_ledger.py)
    write("A17_lesson_types.json", GroupLessonTypes(current="pair", history=[
        LessonTypeRow(valid_from=D(2020, 1, 1), lesson_type="group", created_by_name=None),
        LessonTypeRow(valid_from=D(2026, 10, 15), lesson_type="pair", created_by_name="Арслан")]), store)
    write("A18_lesson_type_put_impact.json", GroupLessonTypeChanged(
        current="pair", dry_run=False,
        history=[LessonTypeRow(valid_from=D(2020, 1, 1), lesson_type="group", created_by_name=None),
                 LessonTypeRow(valid_from=D(2026, 10, 15), lesson_type="pair", created_by_name="Арслан")],
        impact=LessonTypeImpact(stale_payroll_sheets=1, stale_sheets=[LessonTypeSheet(sheet_id=uid(500), month="2026-10",
                                                                                      teacher_name="Каримова")])), store)
    write("A19_payroll_settings_history.json", PayrollSettings(
        divisor=13, valid_from="2026-10", updated_by="Арслан", updated_at="2026-10-02T08:00:00",
        history=[DivisorRow(valid_from="2020-01", divisor=12), DivisorRow(valid_from="2026-10", divisor=13)]), store)


# ---- payroll: payouts (G-PAY) and sheets (F1, F14) --------------------------------------------------------------

LESSONS_OCT = [1, 3, 6, 8, 10, 13, 15, 17, 20, 22, 24, 27]


def payout(n: int, amount: int, paid_on: date, **kw) -> PayoutOut:
    return PayoutOut(id=uid(600 + n), sheet_id=uid(500), teacher_id=uid(400), month="2026-10", amount=amount, paid_on=paid_on,
                     method=kw.pop("method", "cash"), note=None, sheet_revision=kw.pop("rev", 1), sheet_total=kw.pop("total", 475_000),
                     created_by_name="Арслан", created_at=f"{paid_on.isoformat()}T10:00:00", **kw)


def state(saved: int, fresh: Optional[int], paid: int, items: List[PayoutOut]) -> PayoutState:
    stale = fresh is not None and fresh != saved
    target = fresh if stale else saved
    balance = target - paid
    st = "settled" if balance == 0 else ("overpaid" if balance < 0 else ("unpaid" if paid == 0 else "due"))
    return PayoutState(saved_total=saved, fresh_total=fresh, target=target, paid=paid, balance=balance, state=st,
                       basis="fresh" if stale else "saved",
                       text=tx.payout_text(state=st, saved_total=saved, fresh_total=fresh, target=target, paid=paid, balance=balance),
                       items=items)


def payroll_chain(store):
    # G-PAY: 50 %, divisor 12, price 600 000; S1 paid 600 000; S2 paid 300 000 -> floor(300000*31/600000) = 15 days (to 15.10)
    base = 600_000 * 50 // 100 // 12
    assert base == 25_000
    s1 = sum(1 for _ in LESSONS_OCT) * base
    covered = 300_000 * 31 // 600_000
    assert covered == 15
    s2 = sum(1 for d in LESSONS_OCT if d <= 15) * base
    assert (s1, s2, s1 + s2) == (300_000, 175_000, 475_000)  # lessons 1..15: 1,3,6,8,10,13,15 = 7
    p1 = payout(1, 475_000, D(2026, 11, 5))
    write("A20_payout_created_475000.json", PayoutCreated(payout=p1, state=state(475_000, 475_000, 475_000, [p1])), store)
    # S2 pays 300 000 on 10.11: October fully covered: fresh total 600 000
    write("A21_payout_state_due_after_late_payment.json", state(475_000, 600_000, 475_000, [p1]), store)
    st = state(475_000, 600_000, 475_000, [p1])
    assert (st.balance, st.state) == (125_000, "due") and st.text == "Выплачено 475 000 · после изменений данных доплатить 125 000"
    # recalculated (version 2: 600 000): the payout of 12.11
    write("A22_payout_state_due_recalculated.json", state(600_000, 600_000, 475_000, [p1]), store)
    p2 = payout(2, 125_000, D(2026, 11, 12), rev=2, total=600_000)
    write("A23_payout_created_second_settled.json", PayoutCreated(payout=p2, state=state(600_000, 600_000, 600_000, [p1, p2])), store)
    # the payment of S2 is cancelled on 20.11: fresh 475 000, paid 600 000 -> overpaid 125 000
    over = state(600_000, 475_000, 600_000, [p1, p2])
    assert (over.balance, over.state, over.text) == (-125_000, "overpaid", "Переплачено 125 000")
    write("A24_payout_state_overpaid.json", over, store)
    p2v = p2.model_copy(update={"voided_at": "2026-11-21T09:00:00", "void_reason": "внесли дважды"})
    write("A25_payout_void.json", PayoutVoided(payout=p2v, state=state(600_000, 475_000, 475_000, [p1, p2v])), store)


def lines(kind: str):
    """A sheet line: chain 3 (F1) and chain 8 (F14)."""
    def marks(days, price, cover="money", source="group"):
        return [MarkOut(date=f"2026-10-{d:02d}", lesson_id=uid(700 + d), mark=1, state="counted", divisor=12, price=price,
                        price_source=source, cover=cover) for d in days]
    if kind == "f1":
        # price 600 000 -> 700 000 from 15.10: the period of October is priced on its first day (600 000): 12 x 25 000
        m = marks(LESSONS_OCT, 600_000)
        return [LineOut(line_key=uid(101), membership_id=uid(101), student_id=uid(201), student_name="Каримов Алишер", display_id="S-1001",
                        group_id=uid(301), group_name="A-1", lesson_type="group", monthly_fee=600_000, source_fee=600_000,
                        price_source="group", price_changed=False, price_steps=[], percent="50", divisor=12, marks=m, counted=12,
                        amount=300_000)]
    first = [d for d in LESSONS_OCT if d < 15]
    second = [d for d in LESSONS_OCT if d >= 15]
    assert (len(first), len(second)) == (6, 6)
    return [
        LineOut(line_key=f"{uid(101)}:group", membership_id=uid(101), student_id=uid(201), student_name="Каримов Алишер", display_id="S-1001",
                group_id=uid(301), group_name="A-1", lesson_type="group", monthly_fee=600_000, source_fee=600_000, price_source="group",
                percent="50", divisor=12, marks=marks(first, 600_000), counted=6, amount=6 * 25_000),
        LineOut(line_key=f"{uid(101)}:pair", membership_id=uid(101), student_id=uid(201), student_name="Каримов Алишер", display_id="S-1001",
                group_id=uid(301), group_name="A-1", lesson_type="pair", monthly_fee=600_000, source_fee=600_000, price_source="group",
                percent="60", divisor=12, marks=marks(second, 600_000), counted=6, amount=6 * 30_000),
    ]


def sheets(store):
    for name, kind, total in (("A26_sheet_f1_one_price_per_period.json", "f1", 300_000), ("A27_sheet_f14_split_by_type.json", "f14", 330_000)):
        ls = lines(kind)
        assert sum(l.amount for l in ls) == total
        write(name, SheetOut(
            id=uid(500), saved=True, month="2026-10", period_start="2026-10-01", period_end="2026-10-31", month_open=False,
            teacher_id=uid(400), teacher_name="Каримова", revision=1, percents={"group": "50", "pair": "60"} if kind == "f14" else {"group": "50"},
            divisor_default=12, lines=ls, groups=[GroupBlockOut(**b) for b in _engine.group_blocks([l.model_dump() for l in ls])], total_amount=total, counted_total=sum(l.counted for l in ls),
            source_fingerprint="0" * 64, stale=False,
            payout=state(total, total, 0, [])), store)
    # unpaid state: nothing paid yet
    write("A28_payout_state_unpaid.json", state(475_000, 475_000, 0, []), store)


def tabs_and_statement(store):
    """The payment tabs (a debtor with the reason, the summary) and the statement rows of an opening balance and a pause."""
    res = Hand(today=TODAY, accounting_start=AS, stays=[(TODAY, None)], price=price_flat(600_000), debt=250_000, debt_on=D(2026, 9, 30)).run()
    debtor = row(1, "Каримов Алишер", "A-1", group_price(600_000), "expired", res=res, today=TODAY, overdue=21,
                 opening=OpeningBrief(id=uid(2), debt_amount=250_000, debt_on=D(2026, 9, 30)))
    debtor = debtor.model_copy(update={"bucket": "debt", "debt_reason": "never_paid", "reason": "Ещё не платил"})
    write("A29_status_row_debtor.json", debtor, store)
    write("A30_payments_summary_tabs.json", PaymentsSummary(
        as_of="2026-10-21", due=2, ends_today=0, expired=2, expiring=1, none=0, active=5, free=1, tracked_total=9, period_mode="calendar",
        lessons_per_payment=12, students_due=2, students_expired=2, debt_total=1_063_000, advance_total=200_000, memberships_with_debt=3,
        students_with_debt=3, accounting_start_date=AS, paused=1, buckets=PaymentsBuckets(debt=3, expiring=1, active=4, paused=1),
        price_missing=1, students_with_advance=1, debtors=3, students_paused=1), store)
    seg_open = Segment(id="seg:opening", kind="opening", period_from=D(2026, 9, 30), period_to=D(2026, 9, 30), billable_days=0, days_text=None,
                       price=PriceOut(amount=None, source="none"), charge=250_000, basis="opening", paid=0, written_off=0, unpaid=250_000,
                       remaining=250_000, covered_days=0, covered_through=None, partial_credit=0, state="open", bucket="past_debt")
    seg_oct = Segment(id="seg:0:2026-10-01", kind="period", period_from=D(2026, 10, 1), period_to=D(2026, 10, 31), billable_days=11,
                      days_text="11 дней из 31", price=group_price(600_000), charge=213_000, basis="days", paid=0, written_off=0, unpaid=213_000,
                      remaining=213_000, covered_days=0, covered_through=None, partial_credit=0, state="open", bucket="current")
    summary = StatementSummary(charged=463_000, paid=0, debt=463_000, advance=0, paid_until=None, status="expired", reconciles=True)
    member = StatementMembership(membership_id=uid(101), student_id=uid(201), student_name="Каримов Алишер", display_id="S-1001",
                                 group_id=uid(301), group_name="A-1", teacher_name="Каримова", is_active=True)
    rows = [
        StatementRow(seq=1, date=D(2026, 9, 30), type="opening_debt", title="Долг на начало учёта на 30.09.2026", debit=250_000,
                     balance_after=-250_000, balance_text="Долг 250 000", details=StatementDetails(segment=seg_open)),
        StatementRow(seq=2, date=D(2026, 10, 21), type="charge", title="Стоимость 01.10–31.10", debit=213_000, balance_after=-463_000,
                     balance_text="Долг 463 000", details=StatementDetails(segment=seg_oct)),
    ]
    write("A31_statement_opening_debt.json", StatementOut(membership=member, as_of=TODAY, mode="calendar", accounting_start=AS, summary=summary,
                                                          rows=rows), store)
    info = [
        StatementRow(seq=1, date=D(2026, 10, 11), type="opening_paid_until", title="Остаток на начало: оплачено до 15.11", balance_after=0,
                     balance_text="0"),
        StatementRow(seq=2, date=D(2026, 11, 5), type="pause", title="Пауза 05.11–20.11 — стоимость не считается", balance_after=0, balance_text="0"),
    ]
    write("A32_statement_info_rows.json", StatementOut(
        membership=member, as_of=TODAY, mode="calendar", accounting_start=AS,
        summary=StatementSummary(charged=0, paid=0, debt=0, advance=0, paid_until=D(2026, 11, 15), status="active", reconciles=True), rows=info), store)
    base = Hand(today=D(2026, 10, 21), accounting_start=AS, stays=[(D(2026, 10, 1), None)], price=price_flat(500_000),
                payments=[(D(2026, 10, 20), 700_000)]).run()
    row_ = row(2, "Ахмедов Б.", "A-1", group_price(500_000), "active", res=base, today=D(2026, 10, 21))
    write("A33_price_override_dry_run.json", PriceOverrideChanged(
        membership_id=uid(102), group_id=uid(301), as_of="2026-10-21", period_mode="calendar", price_label="Цена в месяц",
        price=group_price(500_000), history=[], cancelled=[], row=row_, dry_run=True,
        impact=PriceImpact(students_following=0, students_with_override=1, stale_payroll_sheets=0, stale_sheets=[],
                           money=PriceImpactMoney(memberships_with_new_debt=0, debt_delta=0, memberships_with_less_advance=1, advance_delta=0,
                                                  days_lost_total=2, backdated=False, items=[PriceImpactItem(
                               membership_id=uid(102), student_name="Ахмедов Б.", debt_before=0, debt_after=0, advance_before=200_000,
                               advance_after=200_000, paid_until_before=D(2026, 11, 12), paid_until_after=D(2026, 11, 10), has_money=True)]))), store)


def errors(store):
    def err(code, message, **kw):
        return LogicAError(detail=dict(code=code, message=message, **kw))

    e = {
        "PAYMENT_EXCEEDS_DEBT": (422, err("PAYMENT_EXCEEDS_DEBT", "Ученик ушёл: принять можно не больше долга 347 000 сум", debt=347_000)),
        "NOTHING_TO_PAY": (422, err("NOTHING_TO_PAY", "Ученик ушёл, долга нет: принимать нечего")),
        "BALANCE_CHANGED": (409, err("BALANCE_CHANGED", "Данные оплат изменились — проверьте суммы",
                                     fresh=[FreshBalance(membership_id=uid(102), debt=0, advance=135_000, refundable=135_000)])),
        "PRICE_BACKDATE_WITH_PAYMENTS": (409, err(
            "PRICE_BACKDATE_WITH_PAYMENTS",
            "Нельзя изменить цену задним числом: у 1 ученика уже есть оплаты за период, который это затронет. "
            "Разницу за прошлое закройте прощением долга или возвратом.",
            students=[PriceBlockedStudent(membership_id=uid(101), student_name="Каримов А.")])),
        "PAUSE_OVERLAPS_ATTENDED": (409, err("PAUSE_OVERLAPS_ATTENDED", "Ученик был отмечен на уроках 12.10, 14.10 — на эти даты паузу поставить нельзя",
                                             dates=[D(2026, 10, 12), D(2026, 10, 14)])),
        "PAUSE_OVERLAP": (409, err("PAUSE_OVERLAP", "Пауза пересекается с другой паузой этого ученика")),
        "PAUSE_RANGE": (422, err("PAUSE_RANGE", "Пауза начинается не раньше чем 30 дней назад")),
        "OPENING_RANGE": (422, err("OPENING_RANGE", "Оплачено до не раньше даты зачисления")),
        "OPENING_NOT_ALLOWED": (409, err("OPENING_NOT_ALLOWED", "Остаток нельзя внести: ученик пришёл переводом")),
        "LESSON_TYPE_RANGE": (422, err("LESSON_TYPE_RANGE", "Тип занятий можно менять с начала прошлого месяца")),
        "PRICE_MARKER": (409, err("PRICE_MARKER", "Эту запись создала система при уходе из группы; её нельзя отменить")),
        "FINGERPRINT_REQUIRED": (422, err("FINGERPRINT_REQUIRED", "Откройте расчёт заново: сохранение без проверки данных невозможно")),
        "DUPLICATE_PAYOUT": (409, err("DUPLICATE_PAYOUT", "Выплата уже внесена", payment_id=None)),
        "SHEET_NOT_SAVED": (409, err("SHEET_NOT_SAVED", "Сначала сохраните ведомость")),
        "DIVISOR_RANGE": (422, err("DIVISOR_RANGE", "Делитель: от 1 до 31")),
    }
    # DUPLICATE_PAYOUT carries ``payout_id`` (PayoutError); LogicAError has no such field: use the payroll model for it
    from src.auth.payroll_schemas import PayoutError

    store["errors.json"] = json.dumps(
        {k: {"status": v[0], "body": ({"detail": PayoutError(code="DUPLICATE_PAYOUT", message="Выплата уже внесена", payout_id=uid(601)).model_dump(mode="json")}
                                      if k == "DUPLICATE_PAYOUT" else v[1].model_dump(mode="json"))} for k, v in e.items()},
        ensure_ascii=False, indent=2) + "\n"


MODELS = {
    "A01_*": "OpeningSaved", "A02_*": "OpeningSaved", "A03_*": "OpeningSaved", "A04_*": "OpeningOut", "A05_*": "OpeningVoided",
    "A06_*": "PauseSaved", "A07_*": "PauseVoided", "A09_*": "LeavePreview", "A10_*": "LeaveResult", "A11_*": "LeavePreview",
    "A12_*": "LeavePreview", "A14_*": "GroupPriceChanged", "A15_*": "GroupPriceChanged", "A16_*": "PaymentSettingsChanged",
    "A17_*": "GroupLessonTypes", "A18_*": "GroupLessonTypeChanged", "A19_*": "PayrollSettings", "A20_*": "PayoutCreated",
    "A21_*": "PayoutState", "A22_*": "PayoutState", "A23_*": "PayoutCreated", "A24_*": "PayoutState", "A25_*": "PayoutVoided",
    "A26_*": "SheetOut", "A27_*": "SheetOut", "A28_*": "PayoutState", "A29_*": "MembershipPaymentRow", "A30_*": "PaymentsSummary",
    "A31_*": "StatementOut", "A32_*": "StatementOut", "A33_*": "PriceOverrideChanged",
}
# the models live in two modules
PAYROLL_MODELS = {"PayrollSettings", "PayoutCreated", "PayoutState", "PayoutVoided", "SheetOut"}


def build() -> Dict[str, str]:
    store: Dict[str, str] = {}
    opening_chain(store)
    chain1(store)
    chain4_6(store)
    price_chain(store)
    settings_chain(store)
    payroll_chain(store)
    sheets(store)
    tabs_and_statement(store)
    errors(store)
    store["index.json"] = json.dumps({"models": MODELS, "payroll_models": sorted(PAYROLL_MODELS)}, ensure_ascii=False, indent=2) + "\n"
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
