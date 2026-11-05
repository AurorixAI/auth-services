"""Money-1 B3: «было → станет» of ONE student in sums and lessons - the same words for the window of a price, of the billing mode and of the day of the shared month.

A window does not decide anything: it takes two ledgers of the SAME student on the SAME day (what the account says now and what it would say after the change) - from the one function of the account
(``billing_ledger.run_ledger``, LESSONS 104) - and says what differs: the debt, the advance, «оплачено до» (calendar modes), the paid lessons of a package.  The sentence is written here, once, so
that the screen shows it verbatim (LESSONS 109: the dictionary of the money words)."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from . import billing_ledger as bl
from . import payments_texts as tx


def _ddmm(day: Optional[date]) -> str:
    return tx.ddmm(day) if day else "нет"


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


def sentence(name: str, before: Dict[str, Any], after: Dict[str, Any]) -> str:
    parts = parts_of(before, after)
    return f"{name}: " + "; ".join(parts) if parts else f"{name}: деньги не изменятся"


def item(membership_id: Any, name: str, before: Dict[str, Any], after: Dict[str, Any], *, has_money: bool) -> Dict[str, Any]:
    """One student of a window: the numbers before / after and the sentence."""
    return {
        "membership_id": str(membership_id), "student_name": name,
        "debt_before": before["debt"], "debt_after": after["debt"], "advance_before": before["advance"], "advance_after": after["advance"],
        "paid_until_before": before["paid_until"], "paid_until_after": after["paid_until"], "has_money": has_money,
        "charged_before": before["charged"], "charged_after": after["charged"],
        "lessons_before": before["lessons"], "lessons_after": after["lessons"],
        "text": sentence(name, before, after),
    }


def summary_text(count: int, debt_delta: int, advance_delta: int, days_lost: int) -> str:
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
    tail = ": " + ", ".join(bits) if bits else ""
    return f"Изменится у {count} {word}{tail}"
