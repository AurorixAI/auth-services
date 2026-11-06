"""
ALGO - PAY-2: the Russian texts of the payment API (pure functions, no database).

The server writes the sentences the front shows verbatim (``summary_text`` of the preview, the ``label`` of an
allocation, ``balance_text`` / ``title`` of a statement row, ``days_text`` of a segment), so that the screens, the
printed statement and the receipt can never disagree.  The golden fixtures (``tests/fixtures/pay2``) freeze them.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional, Sequence

PAST_DEBT = "past_debt"
CURRENT = "current"
ADVANCE = "advance"


# THE one way a sum is written in a text of the server (LESSONS 104 / 109): the groups of the thousands and the unit are joined by a NON-BREAKING space (U+00A0, the one the front writes: ``money-format.ts``),
# so a sum never wraps in the middle («360 / 000 сум» on a phone).  Excel and CSV files do not carry it: ``export_safety.plain`` turns it into a plain space there (a number in a file is a number).
NBSP = "\u00a0"


def num(amount: int) -> str:
    """``269 000`` (non-breaking spaces between the thousands)."""
    return f"{amount:,}".replace(",", NBSP)


def money(amount: int) -> str:
    """``269 000 сум`` (a non-breaking space before the unit too)."""
    return num(amount) + NBSP + "сум"


def stale_text(kind: str, was: int, now: int) -> str:
    """Money-1 V1: the sum the person SAW is not the sum now - ``Долг изменился: было 100 000 сум, сейчас 70 000 сум`` (kind: debt | refundable | advance)."""
    lead = {"debt": "Долг изменился", "refundable": "Сумма к возврату изменилась", "advance": "Аванс изменился"}[kind]
    return f"{lead}: было {money(was)}, сейчас {money(now)}"


def price_unusual_text(old: int, new: int) -> str:
    """Money-1 V3: a price in times (more than twice, less than a half, zero) is asked again - ``Вы уверены: 3 000 000 сум? Сейчас 300 000 сум``."""
    return f"Вы уверены: {money(new)}? Сейчас {money(old)}"


def ddmm(day: date) -> str:
    return day.strftime("%d.%m")


def period_text(start: date, end: date) -> str:
    """``15.09–14.10`` (an en dash); a period of one day is just ``30.09`` («за 30.09», m6)."""
    return ddmm(start) if start == end else f"{ddmm(start)}–{ddmm(end)}"


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


# ---- PAY-F: the names of the ways to count a payment - ONE dictionary for the settings, the hints, the statements and the letters.
# Keys are the stored values (``payment_date`` / ``fixed_day`` are the calendar anchors, ``lessons`` the package); the front mirrors this table in ``src/lib/payments.ts``.
MODE_NAMES = {
    "payment_date": ("Свой месяц у каждого ученика", "Месяц считается с даты «Оплата с» — по умолчанию это первое занятие ученика в группе: 05.10 → 04.11"),
    "fixed_day": ("Общий месяц для всех", "У всех один период, например с 1-го по последнее число; первый неполный месяц — по урокам"),
    "lessons": ("Пакет уроков", "Оплата закрывает заданное число проведённых уроков; отменённые уроки не списываются"),
}


def mode_title(key: str) -> str:
    return MODE_NAMES[key][0]


def mode_hint(key: str) -> str:
    return MODE_NAMES[key][1]


# ---- PAY-F: the recalculation for the lessons the centre cancelled and the forecast of a package
# Money-1 B8 (ДЫРА-08): the words of the setting - the deduction is NOT «из следующей оплаты»: it lowers the debt at once (and the «оплачено до»), and a period with every lesson cancelled gives back its whole price
RECALC_HINT = ("Урок, отменённый центром, уменьшит долг ученика сразу — на стоимость одного занятия периода; "
               "если отменены все уроки периода, перерасчёт вернёт всю цену. Прошлые отмены не пересчитываются")


def recalc_title(day: date) -> str:
    """The row of the statement: ``Перерасчёт: урок 21.10 отменён центром``."""
    return f"Перерасчёт: урок {ddmm(day)} отменён центром"


def recalc_text(amount: int, day: date) -> str:
    """The line of the receipt / of the payment dialog: ``−46 000 сум: урок 21.10 отменён центром`` (a real minus sign)."""
    return f"\u2212{money(amount)}: урок {ddmm(day)} отменён центром"


def repeated_payment_text(moment: datetime, now: datetime) -> str:
    """Money-1 B9 (ДЫРА-12): ``Такая же оплата уже внесена сегодня в 10:15 — внести ещё одну?`` (or ``вчера в 18:40``): the absolute time of the business
    timezone (Tashkent), not «N минут назад» - the screen may stay open.  Both moments are naive UTC (the convention of the audit columns); the window is 24 hours, so the day is today or yesterday."""
    from . import business_clock as bc

    local = moment.replace(tzinfo=timezone.utc).astimezone(bc.BUSINESS_TIMEZONE)
    today = now.replace(tzinfo=timezone.utc).astimezone(bc.BUSINESS_TIMEZONE).date()
    day = "сегодня" if local.date() >= today else "вчера"
    return f"Такая же оплата уже внесена {day} в {local.strftime('%H:%M')} — внести ещё одну?"


def immediate_text(amount: int, lessons: int, *, total: bool = False) -> str:
    """Money-1 B (R3 lead 11): ``Сразу начислится 300 000 сум (за 14 уроков)``; under a bulk adding ``Всего сразу начислится ...``."""
    word = f" (за {lessons} {lessons_word(lessons)})" if lessons else ""
    return f"{'Всего сразу' if total else 'Сразу'} начислится {money(amount)}{word}"


def waiting_text(until: date) -> str:
    """Money-1 B7: the reason of a student who owes only the period that has just begun (the debtor texts are «Ещё не платил» / «Просрочено на N дней»)."""
    return f"Ждём оплату до {ddmm(until)}"


def recalc_amount_text(amount: int) -> str:
    """The quiet line under the title of the statement row (the title already names the lesson): ``−46 000 сум``."""
    return f"\u2212{money(amount)}"


def forecast_text(day: Optional[date]) -> str:
    """``хватит до ~30.10 (по расписанию)``; no day = the package is over."""
    return f"хватит до ~{ddmm(day)} (по расписанию)" if day else "пакет закончился"


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
            return ("Погашен долг за " if closes else "Частично погашен долг за ") + what
        if bucket == CURRENT:
            return "На текущий " + what
        return "Аванс: " + what
    if bucket == PAST_DEBT:
        return ("Погашен долг за " if closes else "Частично погашен долг за ") + span
    if bucket == CURRENT:
        return f"На текущий период {span}"
    return f"Аванс на {span}"


def opening_label(day: date, closes: bool) -> str:
    """Where a payment went when it landed on the opening debt."""
    on = day.strftime("%d.%m.%Y")
    return f"Погашен долг на начало учёта на {on}" if closes else f"Частично погашен долг на начало учёта на {on}"


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
    opening: int = 0,
    opening_before: int = 0,
) -> str:
    """The one-line answer to «что закроет» (calendar: ``paid_until_after``; lessons: ``(covered_after, left_after)``).

    * ``Погасит долг 269 000 сум · на текущий период 31 000 сум → оплачено до 15.10 · останется 469 000 сум``
    * ``… · Переплата 21 000 сум → аванс на следующий период (15.11–14.12), оплачено до 15.11``
    * lessons: ``Погасит долг 600 000 сум → оплачено уроков 12, осталось 0 · аванс 30 000 сум``
    """
    parts = []
    if opening:  # LOGIC-A: the debt of the opening balance is paid before any period
        parts.append(f"Погасит долг на начало {money(opening)}" if opening >= opening_before
                     else f"Погасит {money(opening)} из долга на начало {money(opening_before)}")
    if past:
        parts.append(
            f"Погасит долг {money(past)}" if past >= past_debt_before else f"Погасит {money(past)} из долга {money(past_debt_before)}"
        )
    if current:
        parts.append(f"на текущий период {money(current)}")
    if lessons is None and advance:
        where = "следующий период" if len(advance_periods) <= 1 else "следующие периоды"
        if advance_periods:
            where += f" ({period_text(advance_periods[0][0], advance_periods[-1][1])})"
        parts.append(f"Переплата {money(advance)} → аванс на {where}")
    head = _first_upper(" · ".join(parts)) if parts else ""
    if lessons is not None:
        covered, left = lessons
        tail = f"оплачено уроков {covered}, осталось {left}"
        text = f"{head} → {tail}" if head else _first_upper(tail)
        if debt_after:
            text += f" · останется {money(debt_after)}"
        if advance:
            text += f" · аванс {money(advance)}"
        return text
    if advance and paid_until_after is not None:
        text = f"{head}, оплачено до {ddmm(paid_until_after)}"
        return text
    if paid_until_after is not None:
        text = f"{head} → оплачено до {ddmm(paid_until_after)}" if head else f"Оплачено до {ddmm(paid_until_after)}"
    else:
        text = head
    if debt_after:
        text += f" · останется {money(debt_after)}"
    return text


def price_change_warning(start: date, end: date, amount: int, current: int) -> str:
    """D8: the money reaches a period whose price differs from today's - the same sum buys other days."""
    more = "больше" if amount < current else "меньше"
    return (
        f"Цена периода {period_text(start, end)} — {money(amount)} (сейчас {money(current)}): "
        f"аванс на него купит {more} дней"
    )


# ---- LOGIC-A: payroll payouts (decision 6) ------------------------------------------------------------------


def payout_text(*, state: str, saved_total: int, fresh_total: Optional[int], target: int, paid: int, balance: int, carry_used: int = 0) -> str:
    """The sentence of ``PayoutState.text`` (spec §5.8).  A sheet is "stale" when the fresh total differs from the saved one.
    ``carry_used`` (R3 3): a month that the extra payout of the earlier months covers whole is «Закрыто выплатой прошлого месяца»; otherwise the sentence is the old one (that payout has its own line).
    Words of the dictionary (LESSONS 109): a payout that is more than the saved pay is «Выплачено больше на N сум — вычтем из следующей выплаты»; every sum has its «сум»."""
    if state == "settled" and paid == 0 and carry_used:
        return f"Закрыто выплатой прошлого месяца: {money(carry_used)}"
    if state == "unpaid":
        return "Не выплачено"
    if state == "settled":
        return "Выплачено полностью"
    if state == "overpaid":
        return f"Выплачено больше на {money(-balance)} — вычтем из следующей выплаты"
    return f"Выплачено {money(paid)} из {money(target)} · осталось {money(balance)}"


def open_month_payout_text(*, state: str, accrued: int, paid: int, balance: int, carry_used: int = 0) -> str:
    """Money-1 R3 #20: the sentence of a payout state of an OPEN month - what the data says now, not the (stage) saved sheet; never «Выплачено полностью» while the accrued is more than the paid.
    An open month never says «переплачено»: what is paid over the accrued so far is «Выплачено вперёд» and covered by what the month still accrues."""
    head = f"Месяц открыт: начислено сейчас {money(accrued)}"
    if state == "settled" and paid == 0 and carry_used:
        return f"{head}, закрыто выплатой прошлого месяца: {money(carry_used)}"
    if state == "unpaid":
        return f"{head}, выплат нет, к выплате {money(balance)}"
    if state == "settled":
        return f"{head}, выплачено {money(paid)}: на сегодня выплачено всё"
    if state == "overpaid":
        return f"{head}, выплачено {money(paid)}. Выплачено вперёд {money(-balance)}: покроется начислениями до конца месяца; если останется — уменьшит следующую выплату"
    return f"{head}, выплачено {money(paid)}, к выплате {money(balance)}"


def payout_change_text(delta: int) -> str:
    """Money-1 A1b: the line under the payout of a sheet whose data moved after it was saved (the payout stays by the saved total)."""
    return f"После сохранения зарплата изменилась на {'+' if delta > 0 else '−'}{money(abs(delta))}. Сохраните новую сумму, чтобы учесть изменение"


# ---- INCOME-2 («Поступления» v2): the owner's vocabulary ------------------------------------------------------

MONTHS_NOMINATIVE = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь",
                     "декабрь")
MONTHS_GENITIVE = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября",
                   "декабря")
MONTHS_DATIVE = ("январю", "февралю", "марту", "апрелю", "маю", "июню", "июлю", "августу", "сентябрю", "октябрю", "ноябрю", "декабрю")
MONTHS_SHORT = ("янв.", "февр.", "мар.", "апр.", "мая", "июн.", "июл.", "авг.", "сент.", "окт.", "нояб.", "дек.")
METHOD_LABELS = {
    "cash": "Наличные", "card": "Карта", "bank_transfer": "Перевод", "click": "Click", "payme": "Payme", "other": "Другое",
    "unknown": "Способ не указан",
}


def method_label(method: Optional[str]) -> str:
    return METHOD_LABELS.get(method or "unknown", METHOD_LABELS["other"])


def day_genitive(day: date) -> str:
    """``21 октября``."""
    return f"{day.day} {MONTHS_GENITIVE[day.month - 1]}"


def span_text(first: date, last: date) -> str:
    """``12–18 октября`` / ``30 сентября – 6 октября`` / ``21 октября`` (one day)."""
    if first == last:
        return day_genitive(first)
    if (first.year, first.month) == (last.year, last.month):
        return f"{first.day}–{last.day} {MONTHS_GENITIVE[last.month - 1]}"
    return f"{day_genitive(first)} – {day_genitive(last)}"


# «Куда ушли деньги» of the period and of one payment (S1, lead's decision 22.10): every payment is split by the CURRENT ledger allocation (FIFO, as of today - the same as the
# drawer / the statement), each piece classified relative to the payment's OWN date.
KIND_OLD_DEBT, KIND_CURRENT, KIND_FORWARD, KIND_ADVANCE, KIND_REFUNDED, KIND_TRANSFERRED = (
    "old_debt", "current", "forward", "advance", "refunded", "transferred")
INCOME_KIND_LABELS = {
    KIND_OLD_DEBT: "на старые долги",                       # the period ended before the payment's date
    KIND_CURRENT: "за текущий период",                      # the period contains the payment's date
    KIND_FORWARD: "вперёд (за будущие периоды)",            # the period starts after the payment's date (and is already consumed today)
    KIND_ADVANCE: "пока не потрачено (аванс)",              # no period has absorbed it yet
    KIND_REFUNDED: "возвращено",
    KIND_TRANSFERRED: "деньги перешли в другую группу",
}


def allocation_summary(parts: Sequence[tuple]) -> str:
    """The journal line «куда пошли деньги»: ``за сентябрь — 200 000 сум · аванс — 50 000 сум``; ``parts`` = ``(label, amount)``;
    the pieces with the same label are added up, the order of the first appearance stays."""
    merged: dict = {}
    for label, amount in parts:
        if amount:
            merged[label] = merged.get(label, 0) + amount
    return " · ".join(f"{label} — {money(amount)}" for label, amount in merged.items())


def percent_change(current: int, previous: int) -> Optional[int]:
    """``+12`` for 112 against 100; ``None`` when there is nothing to compare with.  Rounded half away from zero."""
    if previous <= 0:
        return None
    raw = (current - previous) * 100
    sign = -1 if raw < 0 else 1
    return sign * ((abs(raw) * 2 + previous) // (2 * previous))


def income_summary_text(*, lead: str, received: int, previous_received: int, previous_ref: str,
                        collected_pct: Optional[int] = None) -> str:
    """The one sentence under the cards: ``За октябрь пришло 7 885 000 сум — на 12% больше, чем за сентябрь. Собрано 63% стоимости месяца.``

    ``lead`` = «За октябрь» / «Сегодня»; ``previous_ref`` = «за сентябрь» / «вчера» (what it is compared with).
    """
    if received <= 0:
        text = f"{lead} оплат не было"
        text += f"; {previous_ref} пришло {money(previous_received)}." if previous_received > 0 else "."
    elif previous_received <= 0:
        text = f"{lead} пришло {money(received)}."          # nothing to compare with: the card says «за … оплат не было», the sentence does not repeat it
    else:
        pct = percent_change(received, previous_received)
        if received == previous_received:
            diff = "столько же"
        elif pct == 0:
            diff = "почти столько же"
        else:
            diff = f"на {abs(pct)}% {'больше' if pct > 0 else 'меньше'}"
        diff = diff + (f", чем {previous_ref}" if "%" in diff else f", сколько {previous_ref}")
        text = f"{lead} пришло {money(received)} — {diff}."
    if collected_pct is not None:
        text += f" Собрано {collected_pct}% стоимости месяца."
    return text


def collection_text(paid: int, charged: int, pct: Optional[int]) -> str:
    """The line of the card «Собрано за месяц»."""
    if charged <= 0:
        return "За этот месяц стоимость не считалась"
    return f"Оплачено {num(paid)} из {money(charged)} ({pct}%)"


def delta_text(pct: int, previous_ref: str) -> str:
    """``12% к сентябрю`` (the arrow and the colour are the front's)."""
    return f"{abs(pct)}% {previous_ref}"


def short_span_text(first: date, last: date) -> str:
    """``12–18 окт.`` / ``30 сент. – 6 окт.`` / ``20 окт.`` (the label of a comparison period)."""
    mon = lambda d: MONTHS_SHORT[d.month - 1]  # noqa: E731
    if first == last:
        return f"{first.day} {mon(first)}"
    if (first.year, first.month) == (last.year, last.month):
        return f"{first.day}–{last.day} {mon(last)}"
    return f"{first.day} {mon(first)} – {last.day} {mon(last)}"


def transfer_balance_text(*, carried: int, charged_now: int, advance: int, debt: int, charged_tomorrow: int, advance_tomorrow: int, group_name: Optional[str] = None) -> str:
    """The window of a transfer (stage walk D-6): ``Деньги ученика — 640 000 сум — переходят в группу «Геометрия 8»: сразу 280 000 сум пойдут на остаток месяца; аванс останется 360 000 сум. Со следующего дня начислят ещё 350 000 сум (аванс останется 290 000 сум)``.
    The same words before the transfer (the window) and after it (the toast).  A group the student comes back to charges the rest of the month at once (``сразу … пойдут на остаток месяца``); a new one - from the next day."""
    where = f"в группу «{group_name}»" if group_name else "в эту группу"
    head = f"Деньги ученика — {money(carried)} — переходят {where}"
    if charged_now:
        head += f": сразу {money(charged_now)} пойдут на остаток месяца"
    parts = [head]
    if debt:
        parts.append(f"долг останется {money(debt)}")
    if advance:
        parts.append(f"аванс останется {money(advance)}")
    text = "; ".join(parts)
    if charged_tomorrow:
        text += f". Со следующего дня начислят ещё {money(charged_tomorrow)}" + (f" (аванс останется {money(advance_tomorrow)})" if advance_tomorrow else "")
    return text
