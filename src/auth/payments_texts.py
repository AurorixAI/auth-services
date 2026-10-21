"""
ALGO - PAY-2: the Russian texts of the payment API (pure functions, no database).

The server writes the sentences the front shows verbatim (``summary_text`` of the preview, the ``label`` of an
allocation, ``balance_text`` / ``title`` of a statement row, ``days_text`` of a segment), so that the screens, the
printed statement and the receipt can never disagree.  The golden fixtures (``tests/fixtures/pay2``) freeze them.
"""
from __future__ import annotations

from datetime import date
from typing import Optional, Sequence

PAST_DEBT = "past_debt"
CURRENT = "current"
ADVANCE = "advance"


def num(amount: int) -> str:
    """``269 000`` (a plain space between the thousands)."""
    return f"{amount:,}".replace(",", " ")


def money(amount: int) -> str:
    """``269 000 сум``."""
    return num(amount) + " сум"


def ddmm(day: date) -> str:
    return day.strftime("%d.%m")


def period_text(start: date, end: date) -> str:
    """``15.09–14.10`` (an en dash)."""
    return f"{ddmm(start)}–{ddmm(end)}"


def plural(count: int, one: str, few: str, many: str) -> str:
    last, last2 = count % 10, count % 100
    if last == 1 and last2 != 11:
        return one
    if 2 <= last <= 4 and not 12 <= last2 <= 14:
        return few
    return many


def days_word(count: int) -> str:
    return plural(count, "день", "дня", "дней")


def lessons_word(count: int) -> str:
    return plural(count, "урок", "урока", "уроков")


def balance_text(balance: int) -> str:
    """«Баланс» of a statement (the personal account): negative = ``Долг N``, positive = ``Аванс N``, zero = ``0``."""
    if balance < 0:
        return f"Долг {num(-balance)}"
    if balance > 0:
        return f"Аванс {num(balance)}"
    return "0"


def forgiven_title(reason: Optional[str]) -> str:
    """Title of a write-off row of the statement: ``Долг прощён — причина …`` (the owner's vocabulary: «Простить долг»)."""
    reason = (reason or "").strip()
    return f"Долг прощён — причина {reason}" if reason else "Долг прощён"


def days_text(*, basis: str, billable_days: int, period_days: int, first_day: Optional[date] = None,
              lessons_in_part: Optional[int] = None, lessons_in_period: Optional[int] = None) -> Optional[str]:
    """What an incomplete period was charged by; ``None`` for a complete one."""
    if basis == "lessons" and lessons_in_part is not None and lessons_in_period is not None:
        since = f"с {ddmm(first_day)} " if first_day is not None else ""
        return f"{since}({lessons_in_part} {lessons_word(lessons_in_part)} из {lessons_in_period})"
    if basis == "days":
        return f"{billable_days} {days_word(billable_days)} из {period_days}"
    return None


def allocation_label(*, bucket: str, closes: bool, start: date, end: date, package_no: Optional[int] = None) -> str:
    """«куда пошла часть оплаты»."""
    span = period_text(start, end)
    if package_no is not None:
        what = f"пакет №{package_no} ({span})"
        if bucket == PAST_DEBT:
            return ("Закрыла долг: " if closes else "Частично погасила долг: ") + what
        if bucket == CURRENT:
            return "На текущий " + what
        return "Аванс: " + what
    if bucket == PAST_DEBT:
        return ("Закрыла долг " if closes else "Частично погасила долг ") + span
    if bucket == CURRENT:
        return f"На текущий период {span}"
    return f"Аванс на {span}"


def advance_label_lessons() -> str:
    return "Аванс на следующие уроки"


def _first_upper(text: str) -> str:
    return text[:1].upper() + text[1:]


def summary_text(
    *,
    past: int,
    current: int,
    advance: int,
    past_debt_before: int,
    debt_after: int,
    paid_until_after: Optional[date],
    advance_periods: Sequence[tuple[date, date]] = (),
    lessons: Optional[tuple[int, int]] = None,
) -> str:
    """The one-line answer to «что закроет» (calendar: ``paid_until_after``; lessons: ``(covered_after, left_after)``).

    * ``Закроет долг 269 000 · на текущий период 31 000 → оплачено до 15.10 · останется 469 000``
    * ``… · Переплата 21 000 → аванс на следующий период (15.11–14.12), оплачено до 15.11``
    * lessons: ``Закроет долг 600 000 → оплачено уроков 12, осталось 0 · аванс 30 000``
    """
    parts = []
    if past:
        parts.append(
            f"Закроет долг {num(past)}" if past >= past_debt_before else f"Погасит {num(past)} из долга {num(past_debt_before)}"
        )
    if current:
        parts.append(f"на текущий период {num(current)}")
    if lessons is None and advance:
        where = "следующий период" if len(advance_periods) <= 1 else "следующие периоды"
        if advance_periods:
            where += f" ({period_text(advance_periods[0][0], advance_periods[-1][1])})"
        parts.append(f"Переплата {num(advance)} → аванс на {where}")
    head = _first_upper(" · ".join(parts)) if parts else ""
    if lessons is not None:
        covered, left = lessons
        tail = f"оплачено уроков {covered}, осталось {left}"
        text = f"{head} → {tail}" if head else _first_upper(tail)
        if debt_after:
            text += f" · останется {num(debt_after)}"
        if advance:
            text += f" · аванс {num(advance)}"
        return text
    if advance and paid_until_after is not None:
        text = f"{head}, оплачено до {ddmm(paid_until_after)}"
        return text
    if paid_until_after is not None:
        text = f"{head} → оплачено до {ddmm(paid_until_after)}" if head else f"Оплачено до {ddmm(paid_until_after)}"
    else:
        text = head
    if debt_after:
        text += f" · останется {num(debt_after)}"
    return text


def price_change_warning(start: date, end: date, amount: int, current: int) -> str:
    """D8: the money reaches a period whose price differs from today's - the same sum buys other days."""
    more = "больше" if amount < current else "меньше"
    return (
        f"Цена периода {period_text(start, end)} — {money(amount)} (сейчас {money(current)}): "
        f"аванс на него купит {more} дней"
    )


# ---- LOGIC-A: payroll payouts (decision 6) ------------------------------------------------------------------


def payout_text(*, state: str, saved_total: int, fresh_total: Optional[int], target: int, paid: int, balance: int) -> str:
    """The sentence of ``PayoutState.text`` (spec §5.8).  A sheet is "stale" when the fresh total differs from the saved one."""
    if state == "unpaid":
        return "Не выплачено"
    if state == "settled":
        return "Выплачено полностью"
    if state == "overpaid":
        return f"Переплачено {num(-balance)}"
    if fresh_total is not None and fresh_total != saved_total:
        return f"Выплачено {num(paid)} · после изменений данных доплатить {num(balance)}"
    return f"Выплачено {num(paid)} из {num(target)} · осталось {num(balance)}"
