"""Money-1 B3: «было → станет» of ONE student in sums and lessons - the same words for the window of a price, of the billing mode and of the day of the shared month.

A window does not decide anything: it takes two ledgers of the SAME student on the SAME day (what the account says now and what it would say after the change) - from the one function of the account
(``billing_ledger.run_ledger``, LESSONS 104) - and says what differs: the debt, the advance, «оплачено до» (calendar modes), the paid lessons of a package.  The sentence is written here, once, so
that the screen shows it verbatim (LESSONS 109: the dictionary of the money words)."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from . import billing_ledger as bl
from . import payments_texts as tx


def _ddmm(day: Optional[date]) -> str:
    return tx.ddmm(day) if day else "—"             # R3 money R-5: a student who never paid has no «оплачено до»: «— → 24.10», not «нет → 24.10»


def lessons_of(ledger: bl.Ledger) -> Optional[Dict[str, int]]:
    """The package view of an account: how many lessons are paid, how many are used, how many are left (``None`` for the calendar modes)."""
    view = ledger.lessons
    if view is None:
        return None
    return {"covered": view.covered, "used": view.used, "left": view.left, "package_size": view.package_size}


def snapshot(ledger: bl.Ledger) -> Dict[str, Any]:
    return {"charged": ledger.charged, "debt": ledger.debt, "advance": ledger.advance, "paid_until": ledger.covered_through, "lessons": lessons_of(ledger)}


def changed(before: Dict[str, Any], after: Dict[str, Any]) -> bool:
    return any(before[key] != after[key] for key in ("debt", "advance", "paid_until", "lessons"))


def parts_of(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    """The pieces of the sentence: only what differs, in the order the owner reads money (debt, advance, paid until, lessons)."""
    parts: List[str] = []
    if before["debt"] != after["debt"]:
        parts.append(f"долг {tx.money(before['debt'])} → {tx.money(after['debt'])}")
    if before["advance"] != after["advance"]:
        parts.append(f"аванс {tx.money(before['advance'])} → {tx.money(after['advance'])}")
    if before["paid_until"] != after["paid_until"]:
        parts.append(f"оплачено до {_ddmm(before['paid_until'])} → {_ddmm(after['paid_until'])}")
    lb, la = before["lessons"], after["lessons"]
    if lb is not None and la is not None:
        if lb["covered"] != la["covered"]:
            parts.append(f"оплачено уроков {lb['covered']} → {la['covered']}")
        if lb["left"] != la["left"]:
            parts.append(f"осталось уроков {lb['left']} → {la['left']}")
    return parts


NEXT_PERIOD_PROBE_DAYS = 93         # the next period of a student begins within a month; three months is enough even with pauses


def next_period(inp_old: bl.LedgerInput, inp_new: bl.LedgerInput, reach: date) -> Optional[Dict[str, Any]]:
    """Money-1 R3 №4: the FIRST period (cell) of the student that begins on or after ``reach`` and is priced differently, when it begins AFTER ``reach`` (one that begins on it is already in the account of that day) - the first one the change prices differently - as ``{period_from, period_to, amount_before,
    amount_after, difference}``, or ``None`` (no such period, a package, the same sum).  The ledgers are read at a later day so that the period has BEGUN and is generated even for a student
    who has paid exactly up to ``reach`` (a period that is not yet begun and not paid for is not built)."""
    probe = reach + timedelta(days=NEXT_PERIOD_PROBE_DAYS)
    led_old, led_new = bl.run_ledger(replace(inp_old, today=probe)), bl.run_ledger(replace(inp_new, today=probe))
    if led_new.mode == bl.LESSONS:
        return None

    def starts(led: bl.Ledger) -> Dict[date, bl.Segment]:
        return {s.period_start: s for s in led.segments if s.kind == "period" and (s.days[0] if s.days else s.period_start) >= reach}

    new_by, old_by = starts(led_new), starts(led_old)
    for start in sorted(new_by):
        seg_new, seg_old = new_by[start], old_by.get(start)
        if seg_old is None or seg_old.unpriced or seg_new.unpriced:
            continue
        if seg_old.amount == seg_new.amount:
            return None                   # the FIRST period he will be charged for costs the same: nothing to say
        if (seg_new.days[0] if seg_new.days else seg_new.period_start) == reach:
            return None                   # it begins ON ``reach``: it is already in the debt / the advance of that day (the next ones are the same story, not a second «first affected period»)
        return {"period_from": seg_new.period_start, "period_to": seg_new.period_end, "amount_before": seg_old.amount, "amount_after": seg_new.amount,
                "difference": seg_new.amount - seg_old.amount}
    return None


def next_period_part(next_period: Optional[Dict[str, Any]]) -> Optional[str]:
    """Money-1 R3 №4: the first period of the student that BEGINS after the day the change reaches him - «следующий период с 05.12: 300 000 → 400 000 сум (+100 000)».  A student who pays
    in time shows no difference in his account on the day of the price, but the next period costs another sum: the window must say it."""
    if not next_period:
        return None
    sign = "+" if next_period["difference"] > 0 else "−"
    return (f"следующий период с {tx.ddmm(next_period['period_from'])}: {tx.num(next_period['amount_before'])} → {tx.money(next_period['amount_after'])} "
            f"({sign}{tx.num(abs(next_period['difference']))})")


def sentence(name: str, before: Dict[str, Any], after: Dict[str, Any], *, on: Optional[date] = None, next_period: Optional[Dict[str, Any]] = None) -> str:
    """``Имя (на 01.12): долг … → …``: the day the accounts are read on is told (R3 money R-5: «долг 400 000 → 500 000» without a day reads as today's debt)."""
    parts = parts_of(before, after)
    extra = next_period_part(next_period)
    if extra:
        parts.append(extra)
    head = f"{name} (на {tx.ddmm(on)})" if on else name
    return f"{head}: " + "; ".join(parts) if parts else f"{head}: деньги не изменятся"


def item(membership_id: Any, name: str, before: Dict[str, Any], after: Dict[str, Any], *, has_money: bool, on: Optional[date] = None,
         next_period: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One student of a window: the numbers before / after and the sentence."""
    return {
        "membership_id": str(membership_id), "student_name": name,
        "debt_before": before["debt"], "debt_after": after["debt"], "advance_before": before["advance"], "advance_after": after["advance"],
        "paid_until_before": before["paid_until"], "paid_until_after": after["paid_until"], "has_money": has_money,
        "charged_before": before["charged"], "charged_after": after["charged"],
        "lessons_before": before["lessons"], "lessons_after": after["lessons"],
        "next_period": next_period,
        "text": sentence(name, before, after, on=on, next_period=next_period),
    }


def summary_text(count: int, debt_delta: int, advance_delta: int, days_lost: int, lessons_lost: int = 0, next_count: int = 0, next_delta: int = 0) -> str:
    """The line over the list of a window: how many students move and by how much (the worst figures, not a sum of different things)."""
    if not count:
        return "Деньги учеников не изменятся"
    word = tx.plural(count, "ученика", "учеников", "учеников")
    bits = []
    if debt_delta:
        bits.append(f"долг вырастет на {tx.money(debt_delta)}")
    if advance_delta:
        bits.append(f"аванс уменьшится на {tx.money(-advance_delta)}")
    if days_lost:
        bits.append(f"оплаченных дней станет меньше на {days_lost}")
    if lessons_lost:
        bits.append(f"оплаченных уроков станет меньше на {lessons_lost}")
    if next_count:
        who = tx.plural(next_count, "ученика", "учеников", "учеников")
        bits.append(f"следующий период {'дороже' if next_delta > 0 else 'дешевле'} на {tx.money(abs(next_delta))} у {next_count} {who}")
    tail = ": " + ", ".join(bits) if bits else ""
    return f"Изменится у {count} {word}{tail}"
