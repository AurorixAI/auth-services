"""
ALGO - PAY-TAILS Б1.4 / Б5.3: the charge that has NOT BEGUN yet (a future «Оплата с», a first lesson ahead, a shift by the rule «a day is not paid twice», the accounting of the centre that starts later).

One pure reader of a ledger input; no database, no new loader.  The forecast is the SAME account read on a later day (``replace(inp, today=...)``, the trick of ``money_whatif.next_period``): the first period with a price
and a sum whose first day is after today is what the ledger will charge on that day, to the sum - so «the forecast of D-3 = the charge of D» holds by construction.  The term and «долг с первого дня» are read from
``pay_due.classify`` of that account on the first day of the period (the term counts from the first PAYABLE day and skips the pauses).  A forecast is never a part of a sum: not of «Долг», «К оплате», «Аванс», not of
the income, the panel or the reconciliation - it is a separate field of a row, of a statement and of an answer of adding.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any, Dict, Optional

from . import billing_ledger as bl
from . import pay_due
from . import payment_status as ps
from . import payments_texts as tx

PROBE_DAYS = 93          # the next period of a student begins within a month; three months is enough even with pauses (as ``money_whatif.NEXT_PERIOD_PROBE_DAYS``)
_IN_MONTH = ("январе", "феврале", "марте", "апреле", "мае", "июне", "июле", "августе", "сентябре", "октябре", "ноябре", "декабре")


@dataclass(frozen=True)
class Upcoming:
    """The forecast of the first charge that begins later: ``begin`` - its first day; ``amount`` - what the ledger will charge for the period; ``lessons`` - ``(in_part, in_period)`` of a part of a month counted by lessons."""

    begin: date
    amount: int
    lessons: Optional[tuple]
    month: Optional[int]
    due_until: Optional[date]
    debt_from_first_day: bool
    period_start: Optional[date] = None
    period_end: Optional[date] = None

    @property
    def lessons_text(self) -> Optional[str]:
        """``2 урока из 8 в ноябре`` (only a part of a month counted by lessons)."""
        if not self.lessons or self.month is None:
            return None
        part, total = self.lessons
        return f"{part} {tx.lessons_word(part)} из {total} в {_IN_MONTH[self.month - 1]}"

    @property
    def row_title(self) -> str:
        """The title of the grey forecast row of a statement: ``Ноябрь 2026 — неполный месяц (с 24.11, 2 урока из 8)`` / ``Период 24.11–23.12 — начнётся 24.11``."""
        if self.lessons and self.month is not None and self.period_start is not None:
            part, total = self.lessons
            return f"{tx.MONTHS_NOMINATIVE[self.month - 1].capitalize()} {self.period_start.year} — неполный месяц (с {tx.ddmm(self.begin)}, {part} {tx.lessons_word(part)} из {total})"
        if self.period_start is not None and self.period_end is not None:
            return f"Период {tx.period_text(self.period_start, self.period_end)} — начнётся {tx.ddmm(self.begin)}"
        return f"Начнётся {tx.ddmm(self.begin)}"

    @property
    def row_sub(self) -> str:
        return f"прогноз: начислится {tx.money(self.amount)}"

    @property
    def sub(self) -> str:
        return "долг с первого дня" if self.debt_from_first_day else (f"платить до {tx.ddmm(self.due_until)}" if self.due_until else "")

    @property
    def text(self) -> str:
        return f"Начисление с {tx.ddmm(self.begin)}: {tx.money(self.amount)}"


def _open_stay(inp: bl.LedgerInput) -> Optional[bl.Stay]:
    live = [s for s in inp.stays if s.last is None or s.last >= inp.today]
    return max(live, key=lambda s: s.first) if live else None


def starts_later(inp: bl.LedgerInput, ledger: bl.Ledger) -> bool:
    """A candidate: the open stay begins to be charged after today, or no begun period of this stay exists yet (NOT «no begun segment at all»: a student who came back has old ones)."""
    stay = _open_stay(inp)
    if stay is None:
        return False
    if stay.first > inp.today:
        return True
    return not any(s.begun and s.kind in ("period", "package") and s.days and s.days[-1] >= stay.first for s in ledger.segments)


def forecast(inp: bl.LedgerInput, ledger: bl.Ledger, pay_due_days: int, pay_due_lessons: int) -> Optional[Upcoming]:
    """The forecast of a calendar membership whose charge has not begun, or ``None`` (a package, nothing to forecast, no price, nothing is charged in the horizon)."""
    if ledger.mode == bl.LESSONS or not starts_later(inp, ledger):
        return None
    stay = _open_stay(inp)
    probe = replace(inp, today=max(inp.today, stay.first) + timedelta(days=PROBE_DAYS))
    later = bl.run_ledger(probe)
    seg = next((s for s in later.segments if s.kind == "period" and not s.unpriced and s.amount > 0 and s.days and s.days[0] > inp.today), None)
    if seg is None:
        return None
    due = pay_due.classify(later, seg.days[0], pay_due_days, pay_due_lessons, True)
    part = next((p for p in due.parts if p.segment_id == seg.id), None)
    lessons = (seg.lessons_in_part, seg.lessons_in_period) if seg.basis == "lessons" and seg.lessons_in_part is not None and seg.lessons_in_period else None
    return Upcoming(seg.days[0], int(seg.amount), lessons, seg.days[0].month if lessons else None,
                    part.due_until if part is not None and part.name == pay_due.DUE else None, part is not None and part.name == pay_due.DEBT, seg.period_start, seg.period_end)


def package_wait(inp: bl.LedgerInput, ledger: bl.Ledger, price: Optional[int], pay_due_lessons: int) -> Optional[Dict[str, Any]]:
    """A package membership with no lesson held yet: ``{"price": per lesson, "lessons": how many may go unpaid}`` («Уроков ещё не было», «50 000 сум за урок · без оплаты 2 урока»)."""
    if ledger.mode != bl.LESSONS or ledger.lessons is None or not price or price <= 0:
        return None
    if ledger.lessons.used or ledger.charged or ledger.paid or ledger.transferred_in or _open_stay(inp) is None:
        return None
    size = ledger.lessons.package_size or 0
    if size < 1:
        return None
    return {"price": int(ps.package_share(price, 1, size)), "lessons": pay_due_lessons}


def package_text(wait: Dict[str, Any]) -> str:
    n = wait["lessons"]
    return f"{tx.money(wait['price'])} за урок · без оплаты {n} {tx.lessons_word(n)}"
