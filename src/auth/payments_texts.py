"""
ALGO - PAY-2: the Russian texts of the payment API (pure functions, no database).

The server writes the sentences the front shows verbatim (``summary_text`` of the preview, the ``label`` of an
allocation, ``balance_text`` / ``title`` of a statement row, ``days_text`` of a segment), so that the screens, the
printed statement and the receipt can never disagree.  The golden fixtures (``tests/fixtures/pay2``) freeze them.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

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


def refunded_total_text(total: int, from_earlier: int) -> str:
    """R-В Д3: ``Всего возвращено за период: 140 000 сум (из них 50 000 — из денег прошлых периодов)``; the bracket only when the refunds took older money too."""
    text = f"Всего возвращено за период: {money(total)}"
    return text + f" (из них {num(from_earlier)} — из денег прошлых периодов)" if from_earlier > 0 else text


def stale_text(kind: str, was: int, now: int) -> str:
    """Money-1 V1: the sum the person SAW is not the sum now - ``Сумма изменилась — обновите: было 100 000 сум, сейчас 70 000 сум`` (kind: debt | refundable | advance)."""
    lead = {"debt": "Сумма изменилась — обновите", "refundable": "Сумма к возврату изменилась", "advance": "Аванс изменился"}[kind]
    return f"{lead}: было {money(was)}, сейчас {money(now)}"


def price_unusual_text(old: int, new: int) -> str:
    """Money-1 V3: a price in times (more than twice, less than a half, zero) is asked again - ``Вы уверены: 3 000 000 сум? Сейчас 300 000 сум``."""
    return f"Вы уверены: {money(new)}? Сейчас {money(old)}"


def opening_over_payments_text(total: int, covered_until: date) -> str:
    """Money-1 V4 (ДП-08): an opening balance on top of payments already entered - ``У ученика уже есть оплаты на 600 000 сум — покрытие станет до 20.01``."""
    return f"У ученика уже есть оплаты на {money(total)} — покрытие станет до {ddmm(covered_until)}"


def receipt_state_text(day: date, debt: int, advance: int, paid_until: Optional[date]) -> str:
    """Money-1 V5: ``На 21.10.2026: не оплачено 100 000 сум, оплачено до 14.11`` (the account of the student on the day the receipt is issued)."""
    parts = [f"не оплачено {money(debt)}" if debt else (f"аванс {money(advance)}" if advance else "всё оплачено")]
    if paid_until:
        parts.append(f"оплачено до {ddmm(paid_until)}")
    return f"На {day.strftime('%d.%m.%Y')}: " + ", ".join(parts)


def individual_price_ends_text(amount: int, group_name: str, new_amount: Optional[int]) -> str:
    """Money-1 V10 (ОП-13): the preview of a transfer - the individual price does not go with the student."""
    after = f"будет цена группы {money(new_amount)}" if new_amount else "будет цена новой группы"
    return f"У ученика индивидуальная цена {money(amount)} в группе «{group_name}»: в новой группе она не действует — {after}"


NOT_FISCAL = "Не является фискальным чеком"            # Money-1 V5/V6: the receipt of the centre is an internal paper, not a cheque of the tax service (owner 04.11)
REFUND_SIGNATURE = "Получил(а): ____________________  Подпись"


def ddmm(day: date) -> str:
    return day.strftime("%d.%m")


def period_text(start: date, end: date) -> str:
    """``15.09–14.10`` (an en dash); a period of one day is just ``30.09`` («за 30.09», m6)."""
    return ddmm(start) if start == end else f"{ddmm(start)}–{ddmm(end)}"


def decision_consequence(snapshot: Dict[str, Any], *, owner: bool) -> Optional[str]:
    """G3M С9: the line «Последствие» of the journal of decisions - the sentence the answer of the action gave (payroll, then the debts of the other groups, as ``with_sentence`` joins them);
    the payroll sentence with sums only for the owner (``money_effects.decision_texts``)."""
    payroll = (snapshot.get("payroll_text") if owner else None) or snapshot.get("payroll_text_public")
    debts = new_debts_text(snapshot.get("new_debts") or [], done=True, advance_drops=snapshot.get("advance_drops") or [])
    return "; ".join(x for x in (payroll, debts) if x) or None


def new_debts_text(debts: Sequence[Dict[str, Any]], *, done: bool, advance_drops: Sequence[Dict[str, Any]] = ()) -> str:
    """Money-1 Г-4 / Р-2: the debt that the correction of a payment of the OLD group leaves in the group the student was moved to (the money went with him).  ``amount`` is the growth,
    ``total`` the debt after it: «В группе «B» появится долг 900 000 сум» when the group did not owe, «долг вырастет на 900 000 сум — до 1 100 000 сум» when it did.
    G3M С3: ``advance_drops`` - the groups where the money taken away leaves NO debt but a smaller advance (``amount`` the fall, ``total`` the advance after it): the same sentence for the preview, the answer and the journal."""
    return "; ".join([*(_new_debt_text(d, done) for d in debts), *(_advance_drop_text(d, done) for d in advance_drops)])


def other_gains_text(gains: Sequence[Dict[str, Any]], *, done: bool) -> str:
    """Money-2 T7: what a restored payment does in the OTHER groups of the student (the money that came back with it): «В группе «B» аванс вырастет на 390 000 сум — до 390 000 сум», «долг уменьшится ... — до ...»."""
    out = []
    for g in gains:
        name = g["group_name"]
        if g["debt_fell"] > 0:
            out.append(f"В группе «{name}» неоплаченная сумма {'уменьшилась' if done else 'уменьшится'} на {money(g['debt_fell'])}" + (f" — до {money(g['debt'])}" if g["debt"] else f": {'долга не осталось' if done else 'долга не останется'}"))
        if g["advance_grew"] > 0:
            out.append(f"В группе «{name}» аванс {'вырос' if done else 'вырастет'} на {money(g['advance_grew'])} — до {money(g['advance'])}")
    return "; ".join(out)


def _advance_drop_text(drop: Dict[str, Any], done: bool) -> str:
    amount, total = int(drop["amount"]), int(drop["total"])
    if total == 0:
        return f"В группе «{drop['group_name']}» не {'осталось' if done else 'останется'} аванса: {money(amount)}"
    return f"В группе «{drop['group_name']}» аванс {'уменьшился' if done else 'уменьшится'} на {money(amount)} — до {money(total)}"


def _new_debt_text(debt: Dict[str, Any], done: bool) -> str:
    amount, total = int(debt["amount"]), int(debt["total"])
    if amount == total:
        return f"В группе «{debt['group_name']}» {'появилась' if done else 'появится'} неоплаченная сумма {money(amount)}"
    return f"В группе «{debt['group_name']}» неоплаченная сумма {'выросла' if done else 'вырастет'} на {money(amount)} — до {money(total)}"


def cash_parts_text(received: int, refunded: int, cancelled: int, corrected: int = 0) -> str:
    """Money-1 Г-17b / Р-11: `` (принято X, возвращено Y, отменено Z, исправлено ±W)`` under the till of a day - the zero parts are not shown, and nothing at all when money only came in.  Returned = money
    handed out; cancelled = a payment entered earlier and cancelled that day; corrected = a payment of an earlier day replaced that day: ONE part, the difference with its sign."""
    if not (refunded or cancelled or corrected):
        return ""
    parts = [f"{word} {money(amount)}" for amount, word in ((received, "принято"), (refunded, "возвращено"), (cancelled, "отменено")) if amount]
    if corrected:
        parts.append(f"исправлено {'+' if corrected > 0 else chr(0x2212)}{money(abs(corrected))}")
    return " (" + ", ".join(parts) + ")"


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
RECALC_HINT = ("Урок, отменённый центром, уменьшит неоплаченную сумму ученика сразу — на стоимость одного занятия периода; "
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


def _lessons_tail(lessons: int) -> str:
    return f" (за {lessons} {lessons_word(lessons)})" if lessons else ""


def immediate_lines(debt: int, due: int, due_until: Optional[date], *, debt_lessons: int = 0, due_lessons: int = 0, term_was: Optional[date] = None, total: bool = False) -> List[str]:
    """PAY-DUE 10.4: what the new stay owes on the day of adding, by the NAME the function of the term gives it (the words of the money dictionary):
    ``К оплате до 19.11: 300 000 сум (за 14 уроков)`` / ``Долг сразу: 300 000 сум · срок был 03.11``; a part of each - two lines ``Долг N сум`` and ``К оплате M сум до ДД.ММ``;
    under a bulk adding ``Всего ...``.  ``debt_lessons`` / ``due_lessons`` of 0: the lessons are not said (the part is paid in part - the server is silent on what it cannot state, LESSONS 151)."""
    was = f" · срок был {ddmm(term_was)}" if term_was else ""
    until = f" до {ddmm(due_until)}" if due_until else ""
    if debt > 0 and due > 0:
        first = "Всего долг" if total else "Долг"
        second = "Всего к оплате" if total else "К оплате"
        return [f"{first} {money(debt)}{was}", f"{second} {money(due)}{until}"]
    if debt > 0:
        return [f"{'Всего долг сразу' if total else 'Долг сразу'}: {money(debt)}{_lessons_tail(debt_lessons)}{was}"]
    if due > 0:
        return [f"{'Всего к оплате' if total else 'К оплате'}{until}: {money(due)}{_lessons_tail(due_lessons)}"]
    return []


def immediate_text(lines: List[str]) -> str:
    """The lines of ``immediate_lines`` as one text (one line per name; the screen shows ``lines`` when it wants two rows)."""
    return "\n".join(lines)


def pay_due_impact_texts(to_due: int, to_debt: int) -> List[str]:
    """PAY-DUE 10.6: the line(s) before the term is saved - who changes name by it (nothing at zero)."""
    out: List[str] = []
    if to_due:
        out.append(f"{to_due} {plural(to_due, 'ученик', 'ученика', 'учеников')} из должников {plural(to_due, 'станет', 'станут', 'станут')} «К оплате»")
    if to_debt:
        out.append(f"{to_debt} {plural(to_debt, 'ученик', 'ученика', 'учеников')} {plural(to_debt, 'станет', 'станут', 'станут')} должниками")
    return out


def pay_due_changed_text(before: Dict[str, int], after: Dict[str, int], to_due: Optional[int], to_debt: Optional[int]) -> str:
    """The journal line of a change of the term: ``Срок оплаты: 5 → 10 дней; к оплате станут 3, должниками 0``."""
    what = []
    if before["days"] != after["days"]:
        what.append(f"{before['days']} → {after['days']} {days_word(after['days'])}")
    if before["lessons"] != after["lessons"]:
        what.append(f"уроков без оплаты {before['lessons']} → {after['lessons']}")
    counts = f". Станут «К оплате»: {to_due}; станут должниками: {to_debt}" if to_due is not None and to_debt is not None else ""
    return "Срок оплаты изменён: " + "; ".join(what) + counts + ". Суммы не меняются"


def due_until_words(day: date, today: date) -> str:
    """PAY-DUE 9.2: the term in words - ``до сегодня`` / ``до завтра, 21.11`` / ``до 24.11``."""
    if day == today:
        return "до сегодня"
    if (day - today).days == 1:
        return f"до завтра, {ddmm(day)}"
    return f"до {ddmm(day)}"


def due_until_phrase(day: date, today: date) -> str:
    """PAY-DUE: the term for the phrase «ближайший срок ...» (no preposition): ``сегодня`` / ``завтра, 21.11`` / ``24.11``."""
    if day == today:
        return "сегодня"
    if (day - today).days == 1:
        return f"завтра, {ddmm(day)}"
    return ddmm(day)


_ORDINALS = {1: "Первый", 2: "Второй", 3: "Третий", 4: "Четвёртый", 5: "Пятый"}


def pay_due_example_period(start: date, days: int) -> str:
    """The example of the term of a period under the setting (PAY-DUE 10.3): ``Период с 01.12 — платить до 05.12 включительно``; 0 days - ``Неоплата — долг с первого дня``."""
    if days <= 0:
        return "Неоплата — долг с первого дня"
    return f"Период с {ddmm(start)} — платить до {ddmm(start + timedelta(days=days - 1))} включительно"


def pay_due_example_package(lessons: int, max_days: int) -> str:
    """The example of the term of a package (10.7): ``Третий неоплаченный урок или 15-й день после урока — уже долг`` (the lesson number is M + 1, the day is the limit + 1)."""
    return f"{_ORDINALS[lessons + 1]} неоплаченный урок или {max_days + 1}-й день после урока — уже долг"


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def debt_also_text(debt: int, lessons: Optional[int], overdue_days: Optional[int]) -> str:
    """The second line of a row that owes both: ``и долг 600 000 сум · просрочено 45 дней`` (a package: ``и долг 150 000 сум · 3 урока · просрочено ...``)."""
    head = "и " + _lower_first(debt_text(debt, lessons))
    return head + (f" · просрочено {overdue_days} {days_word(overdue_days)}" if overdue_days else "")


def due_also_text(amount: int, lessons: Optional[int], until: Optional[date]) -> str:
    """``и к оплате 231 000 сум до 24.11``; a package: ``и к оплате 100 000 сум · 2 урока · до 24.11``."""
    head = "и " + _lower_first(due_text(amount, lessons))
    if until is None:
        return head
    return head + (f" · до {ddmm(until)}" if lessons else f" до {ddmm(until)}")


def _with_lessons(head: str, lessons: Optional[int]) -> str:
    return f"{head} · {lessons} {lessons_word(lessons)}" if lessons else head


def debt_text(amount: int, lessons: Optional[int] = None) -> str:
    """PAY-DUE 9.2: the label of a debt - ``Долг 187 000 сум`` (a package: ``Долг 150 000 сум · 3 урока``)."""
    return _with_lessons(f"Долг {money(amount)}", lessons)


def debt_sub(overdue_days: Optional[int], paid_until: Optional[date]) -> Optional[str]:
    """PAY-DUE 10.9: ``просрочено 12 дней · оплачено до 14.11`` (each half only when it is there: a debt of a student who left before the term has no overdue words)."""
    parts = []
    if overdue_days:
        parts.append(f"просрочено {overdue_days} {days_word(overdue_days)}")
    if paid_until:
        parts.append(f"оплачено до {ddmm(paid_until)}")
    return " · ".join(parts) or None


def due_text(amount: int, lessons: Optional[int] = None) -> str:
    """PAY-DUE 9.2: the label of a sum within its term - ``К оплате 187 000 сум`` (a package: ``К оплате 100 000 сум · 2 урока``)."""
    return _with_lessons(f"К оплате {money(amount)}", lessons)


def due_sub(until: Optional[date], today: date) -> Optional[str]:
    return due_until_words(until, today) if until else None


def paid_until_text(day: Optional[date]) -> Optional[str]:
    return f"Оплачено до {ddmm(day)}" if day else None


def lessons_left_text(left: int) -> str:
    """PAY-DUE 10.7: a package that is about to end - ``Осталось 2 урока``."""
    return f"Осталось {left} {lessons_word(left)}"


def advance_text(amount: int) -> str:
    return f"Аванс {money(amount)}"


def after_text(debt: int, due: int, paid_until: Optional[date], lessons_left: Optional[int] = None, advance: int = 0) -> str:
    """PAY-DUE 15А.2.7: the state of the account AFTER a payment in the words of the server (the screen glues nothing): ``Останется: долг 100 000 сум · к оплате 50 000 сум`` while something
    is unpaid; otherwise ``Оплачено до 15.11`` / ``Осталось 3 урока`` / ``Аванс 21 000 сум`` / ``Всё оплачено``."""
    parts = [f"долг {money(debt)}" if debt > 0 else None, f"к оплате {money(due)}" if due > 0 else None]
    if any(parts):
        return "Останется: " + " · ".join(p for p in parts if p)
    if lessons_left:
        return lessons_left_text(lessons_left)
    if paid_until:
        return f"Оплачено до {ddmm(paid_until)}"
    return advance_text(advance) if advance > 0 else "Всё оплачено"


def open_both_text(debt: str, due: str) -> str:
    """PAY-DUE 10.11: a statement row that holds both names - ``Долг 50 000 сум · к оплате 100 000 сум``."""
    return f"{debt} · {due[:1].lower()}{due[1:]}"


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


def forgiven_title(reason: Optional[str], amount: Optional[int] = None) -> str:
    """Title of a write-off row of the statement (PAY-DUE 3.3, the neutral words of an action): ``Прощено 40 000 сум — причина …``."""
    reason = (reason or "").strip()
    head = f"Прощено {money(int(amount))}" if amount else "Прощено"
    return f"{head} — причина {reason}" if reason else head


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
            return ("Погашено за " if closes else "Частично погашено за ") + what
        if bucket == CURRENT:
            return "На текущий " + what
        return "Аванс: " + what
    if bucket == PAST_DEBT:
        return ("Погашено за " if closes else "Частично погашено за ") + span
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
    debt_paid: int,
    debt_before: int,
    due_paid: int,
    due_before: int,
    advance: int,
    debt_after: int,
    paid_until_after: Optional[date],
    advance_periods: Sequence[tuple[date, date]] = (),
    lessons: Optional[tuple[int, int]] = None,
) -> str:
    """The one-line answer to «что закроет» (calendar: ``paid_until_after``; lessons: ``(covered_after, left_after)``).  The names come from ``pay_due`` (PAY-DUE 10.10), the oldest sum is settled first:

    * ``Погасит долг 269 000 сум · к оплате 500 000 сум → оплачено до 15.10 · останется 469 000 сум``
    * only a sum within its term: ``Закроет «К оплате» 500 000 сум → ...``; a part: ``Погасит 100 000 сум из долга 269 000 сум``, ``к оплате 100 000 сум из 500 000 сум``
    * ``… · Переплата 21 000 сум → аванс на следующий период (15.11–14.12), оплачено до 15.11``
    """
    parts = []
    if debt_paid:
        parts.append(f"Погасит долг {money(debt_paid)}" if debt_paid >= debt_before else f"Погасит {money(debt_paid)} из долга {money(debt_before)}")
    if due_paid:
        word = f"к оплате {money(due_paid)}" if due_paid >= due_before else f"к оплате {money(due_paid)} из {money(due_before)}"
        parts.append(word if debt_paid else f"Закроет «К оплате»: {money(due_paid)}" if due_paid >= due_before else f"Закроет {money(due_paid)} из «К оплате» {money(due_before)}")
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
    KIND_REFUNDED: "возвращено из этих денег",
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
        parts.append(f"неоплачено останется {money(debt)}")
    if advance:
        parts.append(f"аванс останется {money(advance)}")
    text = "; ".join(parts)
    if charged_tomorrow:
        text += f". Со следующего дня начислят ещё {money(charged_tomorrow)}" + (f" (аванс останется {money(advance_tomorrow)})" if advance_tomorrow else "")
    return text
