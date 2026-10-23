"""
ALGO - the words of the statement FILE (xlsx): a Python mirror of the page's rules, so the file reads exactly like the screen.

The page builds its rows in ``algo-front-int/src/lib/payments-ledger.ts`` (``chargeWords``, ``periodName``, ``closedWords``,
``balanceCell``, ``carryLine``, ``openingBalance``, ``buildStatementItems``) and draws them in
``src/components/payments/statement-view.tsx``.  Every function here keeps the name and the order of the original (snake_case):
when the page changes a sentence, change it here and in ``tests/test_payments_statement_texts.py``.  Pure: no database, no xlsx.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import payments_texts as tx

# ---------------------------------------------------------------------------------------------------------- methods

METHOD_LABEL = {
    "cash": "Наличные", "card": "Карта", "bank_transfer": "Перевод", "click": "Click", "payme": "Payme", "other": "Другое",
}
UNKNOWN_METHOD = "Способ не указан"


def method_label(method: Optional[str]) -> str:
    """«Наличные»; a record without a method (or with ``unknown``) reads «Способ не указан»."""
    return METHOD_LABEL.get(method or "", UNKNOWN_METHOD)


def lower_method(method: Optional[str]) -> str:
    return method_label(method).lower()


# ----------------------------------------------------------------------------------------------------------- months

MONTH_NAMES = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
MONTH_GENITIVE = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def month_key(day: date) -> str:
    """``2026-10``."""
    return f"{day.year:04d}-{day.month:02d}"


def month_title(key: str) -> str:
    """«Сентябрь 2026»."""
    name = MONTH_NAMES[int(key[5:7]) - 1]
    return f"{name[:1].upper()}{name[1:]} {key[:4]}"


def month_word(key: str) -> str:
    """«сентябрь» (a month inside a sentence)."""
    return MONTH_NAMES[int(key[5:7]) - 1]


def shift_month_key(key: str, delta: int) -> str:
    total = int(key[:4]) * 12 + (int(key[5:7]) - 1) + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def last_day_of(key: str) -> date:
    nxt = shift_month_key(key, 1)
    return date.fromordinal(date(int(nxt[:4]), int(nxt[5:7]), 1).toordinal() - 1)


def is_calendar_month_period(start: date, end: date) -> bool:
    """A period inside one calendar month that starts on its 1st or ends on its last day: it is called by the month."""
    return month_key(start) == month_key(end) and (start.day == 1 or end == last_day_of(month_key(start)))


def month_from_words(key: str, of: str) -> str:
    """«сентября» / «декабря 2025» (the year only when it is not the year of ``of``): the month a balance is carried from."""
    name = MONTH_GENITIVE[int(key[5:7]) - 1]
    return name if key[:4] == of[:4] else f"{name} {key[:4]}"


def date_ru(day: date) -> str:
    return day.strftime("%d.%m.%Y")


def single_month(date_from: Optional[date], date_to: Optional[date]) -> Optional[str]:
    """The month key when ``from`` / ``to`` cover exactly one calendar month (the page's month switcher), else ``None``."""
    if date_from is None or date_to is None:
        return None
    key = month_key(date_from)
    if date_from.day == 1 and date_to == last_day_of(key):
        return key
    return None


def period_label(date_from: Optional[date], date_to: Optional[date]) -> str:
    """The «Период» of the title block: «Октябрь 2026» / «01.10.2026–15.11.2026» / «Все месяцы» (a one-sided range: «с …» / «по …»)."""
    key = single_month(date_from, date_to)
    if key:
        return month_title(key)
    if date_from and date_to:
        return f"{date_ru(date_from)}–{date_ru(date_to)}"
    if date_from:
        return f"с {date_ru(date_from)}"
    if date_to:
        return f"по {date_ru(date_to)}"
    return "Все месяцы"


# ----------------------------------------------------------------------------------------------------------- balance

def balance_cell(balance: Optional[int]) -> Tuple[str, str]:
    """``(kind, text)``: «Долг 68 000» / «Аванс 21 000» / «0» (no unit: the header says сум once); a missing balance is empty."""
    if balance is None:
        return "none", ""
    if balance < 0:
        return "debt", f"Долг {tx.num(-balance)}"
    if balance > 0:
        return "advance", f"Аванс {tx.num(balance)}"
    return "none", "0"


def carry_line(key: str, balance: int) -> Optional[str]:
    """«С сентября перенесён аванс 508 000» / «С сентября перенесён долг 92 000»; ``None`` for a zero balance."""
    kind, text = balance_cell(balance)
    if kind == "none":
        return None
    lead = f"С {month_from_words(shift_month_key(key, -1), key)} перенесён"
    amount = f"долг {text.replace('Долг ', '')}" if kind == "debt" else f"аванс {text.replace('Аванс ', '')}"
    return f"{lead} {amount}"


def opening_balance(rows: Sequence, key: str) -> int:
    """The balance before the first day of month ``key``: after the last earlier row, 0 before the first row.  ``rows`` is the
    WHOLE ledger (never a filtered one: a month cut by ``from`` still knows what came before it)."""
    balance = 0
    for row in rows:
        if month_key(row.date) >= key:
            break
        if row.balance_after is not None:
            balance = int(row.balance_after)
    return balance


# ---------------------------------------------------------------------------------------------------------- charges

def price_source_text(source: Optional[str]) -> str:
    if source == "override":
        return "индивидуальная цена"
    if source == "group":
        return "цена группы"
    return "цена не задана"


def part_words(days_text: str) -> str:
    """«с 16.10 (7 уроков из 13)» -> «с 16.10, 7 уроков из 13»; «11 дней из 31» stays."""
    match = re.match(r"^(.*?)\s*\((.*)\)$", days_text.strip(), re.S)
    return f"{match.group(1)}, {match.group(2)}" if match else days_text.strip()


def charge_words(segment) -> Tuple[str, str]:
    """``(title, sub)``: «Август 2026 — неполный месяц (с 28.08, 2 урока из 13)» / «Сентябрь 2026» / «Период 15.09–14.10» /
    «Пакет уроков 25.09–21.10 (12 уроков из 12)»; sub = «цена 500 000 сум в месяц · индивидуальная цена»."""
    price = segment.price.amount if segment.price else None
    unit = "за пакет" if segment.kind == "package" else "в месяц"
    if price is None:
        sub = "цена не задана"
    elif price == 0:
        sub = "бесплатно"
    else:
        sub = f"цена {tx.num(price)} сум {unit} · {price_source_text(segment.price.source)}"
    if segment.days_text:
        part = part_words(segment.days_text)
    elif segment.kind == "package" and segment.lessons:
        n = segment.lessons.in_period
        part = f"{segment.lessons.in_part} из {n} {'урок' if n % 10 == 1 and n % 100 != 11 else 'уроков'}"
    else:
        part = ""
    span = f"{tx.ddmm(segment.period_from)}–{tx.ddmm(segment.period_to)}"
    if segment.kind == "package":
        return f"Пакет уроков {span}{f' ({part})' if part else ''}", sub
    if is_calendar_month_period(segment.period_from, segment.period_to):
        key = month_key(segment.period_from)
        partial = bool(segment.days_text) or segment.basis in ("lessons", "days")
        words = part or f"с {tx.ddmm(segment.period_from)}"
        return (f"{month_title(key)} — неполный месяц ({words})" if partial else month_title(key)), sub
    return f"Период {span}{f' ({part})' if part else ''}", sub


def period_name(start: date, end: date, kind: Optional[str] = None) -> str:
    """«август» / «15.09–14.10» / «пакет 25.09–21.10»: how a period is named inside «за …»."""
    if kind == "package":
        return f"пакет {tx.ddmm(start)}–{tx.ddmm(end)}"
    return month_word(month_key(start)) if is_calendar_month_period(start, end) else f"{tx.ddmm(start)}–{tx.ddmm(end)}"


def closed_words(name: str, kind: Optional[str], line) -> str:
    """«август закрыт» for a month, «период закрыт» / «пакет закрыт» otherwise."""
    if kind == "package":
        return "пакет закрыт"
    return f"{name} закрыт" if is_calendar_month_period(line.period_from, line.period_to) else "период закрыт"


# ------------------------------------------------------------------------------------------------------------ items

@dataclass
class Entry:
    """One row of the table, the page's ``StatementEntry``."""
    seq: int
    type: str
    date: date
    title: str
    sub: Optional[str]
    charged: Optional[int]
    paid: Optional[int]           # money in the «Оплачено» column; negative for a refund or a transfer out
    balance_kind: str
    balance: str
    allocations: List[str] = field(default_factory=list)
    replaced: Optional[str] = None

    @property
    def details(self) -> str:
        """«Подробности»: the quiet line, the correction note, then one line per allocation (the order of the page)."""
        return "\n".join(x for x in [self.sub, self.replaced, *self.allocations] if x)


@dataclass
class Band:
    key: str
    title: str                    # «Октябрь 2026»


@dataclass
class Carry:
    text: str


@dataclass
class Quiet:
    """A quiet line: a single month without a record."""
    text: str


def build_entries(rows: Sequence, upcoming: Iterable = ()) -> List[Entry]:
    """The rows of the WHOLE statement in the words of the page (``buildStatementItems``, the row part).  The balance after every
    row is the server's; only «осталось» of a period after a payment is recomputed for a statement without the server's remainder."""
    segments: Dict[str, object] = {}
    for row in rows:
        if row.details.segment is not None:
            segments[row.details.segment.id] = row.details.segment
    for segment in upcoming:
        segments[segment.id] = segment
    allocated: Dict[str, int] = {}
    writeoff_seen = False
    out: List[Entry] = []
    for row in rows:
        d = row.details
        kind, balance = balance_cell(row.balance_after)
        base = dict(seq=row.seq, type=row.type, date=row.date, balance_kind=kind, balance=balance)
        if row.type == "charge":
            title, sub = charge_words(d.segment) if d.segment else (row.title, "")
            out.append(Entry(title=title, sub=sub or None, charged=int(row.debit), paid=None, **base))
        elif row.type == "payment":
            head = " · ".join(x for x in (f"Оплата{f' № {d.receipt_no}' if d.receipt_no else ''}", lower_method(d.method),
                                          f"внёс {d.created_by_name}" if d.created_by_name else "") if x)
            lines: List[str] = []
            for line in d.allocations:
                if line.amount <= 0:
                    continue
                segment = segments.get(line.segment_id)
                seg_kind = segment.kind if segment else None
                if line.bucket == "advance" or line.segment_id == "advance":
                    if line.segment_id == "advance":
                        lines.append(f"аванс на следующие уроки — {tx.num(line.amount)}")
                    else:
                        lines.append(f"аванс на {period_name(line.period_from, line.period_to, seg_kind)} — {tx.num(line.amount)}")
                    continue
                name = period_name(line.period_from, line.period_to, seg_kind)
                total = allocated.get(line.segment_id, 0)
                allocated[line.segment_id] = total + int(line.amount)
                outcome = ""
                exact = line.segment_remaining_after
                if exact is not None:
                    # the server's exact remainder of this period right after this piece of money
                    outcome = (f" ({closed_words(name, seg_kind, line)})" if line.closes or exact == 0
                               else f" (осталось {tx.num(exact)})")
                elif segment is not None:
                    off = int(segment.written_off) if writeoff_seen else 0
                    remaining = max(0, int(segment.charge) - total - int(line.amount) - off)
                    outcome = (f" ({closed_words(name, segment.kind, line)})" if line.closes or remaining == 0
                               else f" (осталось {tx.num(remaining)})")
                elif line.closes:
                    outcome = f" ({closed_words(name, None, line)})"
                lines.append(f"за {name} — {tx.num(line.amount)}{outcome}")
            replaced = (f"исправлена: было {tx.num(d.replaces_amount)}" if d.replaces_amount
                        else f"исправление оплаты № {d.replaces_receipt_no}" if d.replaces_receipt_no else None)
            out.append(Entry(title=head, sub=f"комментарий: {d.note}" if d.note else None, charged=None,
                             paid=int(row.credit), allocations=lines, replaced=replaced, **base))
        elif row.type == "writeoff":
            writeoff_seen = True
            title = f"Долг прощён{f' — причина: {d.reason}' if d.reason else ''}{f' · {d.created_by_name}' if d.created_by_name else ''}"
            out.append(Entry(title=title, sub=None, charged=None, paid=int(row.credit), **base))
        elif row.type == "refund":
            title = f"Возврат аванса — {lower_method(d.method)}{f' · {d.created_by_name}' if d.created_by_name else ''}"
            out.append(Entry(title=title, sub=f"причина: {d.reason}" if d.reason else None, charged=None,
                             paid=-int(row.outflow), **base))
        elif row.type == "transfer_in":
            out.append(Entry(title=row.title, sub=None, charged=None, paid=int(row.credit), **base))
        elif row.type == "transfer_out":
            out.append(Entry(title=row.title, sub=None, charged=None, paid=-int(row.outflow), **base))
        elif row.type == "opening_debt":
            out.append(Entry(title=row.title or "Долг на начало учёта", sub="до запуска в ALGO", charged=int(row.debit), paid=None, **base))
        else:
            # «Остаток на начало: оплачено до 31.10» and a pause: facts without money
            out.append(Entry(title=row.title or STATEMENT_TYPE_LABEL.get(row.type, row.type), sub=None, charged=None, paid=None, **base))
    return out


STATEMENT_TYPE_LABEL = {
    "charge": "Стоимость", "payment": "Оплата", "writeoff": "Долг прощён", "refund": "Возврат", "transfer_in": "Перенос из группы",
    "transfer_out": "Перенос в группу", "opening_debt": "Долг на начало учёта", "opening_paid_until": "Оплачено до начала учёта",
    "pause": "Пауза",
}


def group_by_month(entries: Sequence[Entry], full_rows: Sequence, month: Optional[str]) -> List[object]:
    """The table as the page draws it: a band per month (in order of appearance), the rows of the month and, for ONE month, the
    carry line when the balance before it is not zero.  All months at once never repeat the carry (``month`` is ``None``)."""
    items: List[object] = []
    keys = [month] if month else list(dict.fromkeys(month_key(e.date) for e in entries))
    for key in keys:
        inside = [e for e in entries if month_key(e.date) == key]
        items.append(Band(key=key, title=month_title(key)))
        if month:
            text = carry_line(key, opening_balance(full_rows, key))
            if text:
                items.append(Carry(text=text))
            if not inside:
                items.append(Quiet(text="Записей нет"))
        items.extend(inside)
    return items


def cancel_note(row) -> str:
    """«Отменено: причина · кто» (+ « · заменена оплатой № 18»)."""
    d = row.details
    return (f"Отменено: {row.void_reason or 'причина не указана'}{f' · {row.voided_by_name}' if row.voided_by_name else ''}"
            f"{f' · заменена оплатой № {d.replaced_by_receipt_no}' if d.replaced_by_receipt_no else ''}")


def in_range(day: date, date_from: Optional[date], date_to: Optional[date]) -> bool:
    return (date_from is None or day >= date_from) and (date_to is None or day <= date_to)


def total_lines(summary) -> List[Tuple[str, object]]:
    """The tiles above the statement (``statementTotals``): Стоимость обучения · Оплачено · (Перенесено из групп · Прощено ·
    Возвращено · Перенесено в группы, only when there is something) · Баланс.  The balance is text («Долг 92 000»)."""
    out: List[Tuple[str, object]] = [("Стоимость обучения", int(summary.charged)), ("Оплачено", int(summary.paid))]
    for label, value in (("Перенесено из групп", summary.transferred_in), ("Прощено", summary.written_off),
                         ("Возвращено", summary.refunded), ("Перенесено в группы", summary.transferred_out)):
        if int(value) > 0:
            out.append((label, int(value)))
    out.append(("Баланс", balance_cell(int(summary.advance) - int(summary.debt))[1]))
    return out
