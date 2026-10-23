"""
ALGO - the payments journal as an Excel file, in the house style (``xlsx_style``).  ``IncomeReports.journal_file`` loads the rows; this module only draws.

* Title block (centre; as-of, period and the filters in words; the totals in words), header on row 5, frozen under it and after the student, autofilter.
* ONE payment = ONE row with its final amount.  A payment that a newer one replaced is NEVER a row: it lives on in the «Подробности» of the corrected payment
  («исправлена 19.10: было 350 000 сум (№ 9), способ: Наличные · исправил … · причина: …», the oldest version first).  A cancelled payment is a grey struck-through
  row whose «Подробности» say «отменена 21.10 · кто · причина: …» and which is not in the totals.
* Real types: the receipt is a number, the date a date, the moment a datetime, the money a whole number with ``#,##0``.
* The blue «Итого» row (the active payments shown) and a small block «Итого по способам» under it.
"""
from __future__ import annotations

import io
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import payments_statement_texts as st_tx
from . import payments_texts as tx
from . import xlsx_style as xs
from .business_clock import BUSINESS_TIMEZONE
from .payments_income_schemas import JournalRow

SHEET_TITLE = "Журнал оплат"
DEFAULT_REASON = "Исправлено"             # what the ledger stores when the corrector typed no reason: not worth saying again
HEADER = ["№ квитанции", "Дата", "Ученик", "ID ученика", "Группа", "Преподаватель", "Оплачено", "Способ", "Кто внёс", "Внесена", "Статус",
          "Примечание", "Подробности"]
COL = {name: i for i, name in enumerate(
    ("receipt", "date", "student", "display_id", "group", "teacher", "amount", "method", "author", "entered", "status", "note", "details"), start=1)}
LAST_COL = len(HEADER)
FREEZE = "D6"                              # the receipt, the date and the student stay on the screen
WRAP_COLS = (COL["note"], COL["details"])
METHODS_TITLE = "Итого по способам"
TOTAL_LABEL = "Итого"
STATUS_TEXT = {"active": "Действует", "corrected": "Действует (исправлена)", "voided": "Отменена"}
STATUS_WORDS = {"active": "только действующие", "voided": "только отменённые", "cancelled": "только отменённые",
                "replaced": "только исправленные (заменённые версии в файл не выводятся — их история в «Подробности»)"}


def emitted(rows: Sequence[JournalRow]) -> List[JournalRow]:
    """The rows of the file: never a replaced one."""
    return [r for r in rows if r.status != "replaced"]


def status_text(r: JournalRow) -> str:
    if r.status == "voided":
        return STATUS_TEXT["voided"]
    return STATUS_TEXT["corrected" if (r.replaces_receipt_no or r.history) else "active"]


def _local(moment: Optional[str]) -> Optional[datetime]:
    """An ISO naive-UTC moment -> the clock of the centre (Tashkent), naive (what a cell shows)."""
    if not moment:
        return None
    parsed = datetime.fromisoformat(moment)
    if parsed.tzinfo is None:
        from datetime import timezone

        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(BUSINESS_TIMEZONE).replace(tzinfo=None)


def _dd_mm(moment: Optional[str]) -> str:
    local = _local(moment)
    return local.strftime("%d.%m") if local else ""


def details_text(r: JournalRow) -> str:
    """«Куда пошли деньги» + the version history of a corrected payment, or the cancellation line of a cancelled one."""
    if r.status == "voided":
        parts = [f"отменена {_dd_mm(r.voided_at)}".strip(), r.voided_by_name, f"причина: {r.void_reason}" if r.void_reason else None]
        return xs.words_join(parts)
    lines = [r.allocation_text] if r.allocation_text else []
    for h in r.history:
        head = f"исправлена {_dd_mm(h.replaced_at)}: было {tx.num(h.amount)} сум" + (f" (№ {h.receipt_no})" if h.receipt_no else "")
        lines.append(xs.words_join([f"{head}, способ: {h.method_label}", f"исправил {h.replaced_by_name}" if h.replaced_by_name else None,
                                    f"причина: {h.reason}" if h.reason and h.reason != DEFAULT_REASON else None]))
    return "\n".join(lines)


def table_values(r: JournalRow) -> List[Any]:
    """The cells of one row in the order of ``HEADER`` (real types)."""
    return [r.receipt_no, r.paid_on, r.student_name, r.display_id or None, r.group_name, r.teacher_name or None, int(r.amount), r.method_label,
            r.created_by_name or None, _local(r.created_at), status_text(r), r.note or None, details_text(r) or None]


def by_method(rows: Sequence[JournalRow]) -> List[Tuple[str, int, int]]:
    """``[(method label, payments, sum)]`` of the ACTIVE rows, the biggest sum first."""
    sums: Dict[str, List[int]] = {}
    for r in rows:
        if r.status == "active":
            item = sums.setdefault(r.method_label, [0, 0])
            item[0] += 1
            item[1] += int(r.amount)
    return sorted(((label, n, total) for label, (n, total) in sums.items()), key=lambda x: (-x[2], x[0]))


def title_lines(rows: Sequence[JournalRow], *, org_name: Optional[str], as_of: date, first: date, last: date, filters: Sequence[str],
                kind_label: Optional[str] = None, kind_sum: Optional[int] = None) -> List[str]:
    active = [r for r in rows if r.status == "active"]
    cancelled = len(rows) - len(active)
    total = sum(int(r.amount) for r in active)
    line1 = f"Журнал оплат — {org_name}" if org_name else "Журнал оплат"
    line2 = xs.words_join([f"По состоянию на {xs.date_ru(as_of)}", f"Период: {st_tx.period_label(first, last)}", *filters])
    parts = []
    if active or not cancelled:
        parts += [f"Оплат {len(active)}", f"Оплачено {tx.num(total)}"]
    if kind_label and kind_sum is not None:
        parts.append(f"{kind_label}: {tx.num(kind_sum)}")
    if cancelled:
        parts.append(f"Отменённых {cancelled}")
    return [line1, line2, xs.words_join(parts)]


def build_journal_xlsx(rows: Sequence[JournalRow], *, org_name: Optional[str], as_of: date, first: date, last: date, filters: Sequence[str] = (),
                       kind_label: Optional[str] = None, kind_sum: Optional[int] = None) -> bytes:
    from openpyxl import Workbook

    rows = emitted(rows)
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_TITLE
    header_at = xs.title_block(ws, title_lines(rows, org_name=org_name, as_of=as_of, first=first, last=last, filters=filters,
                                               kind_label=kind_label, kind_sum=kind_sum), LAST_COL)
    xs.header_row(ws, header_at, HEADER, left=(COL["student"], COL["note"], COL["details"]))
    ws.freeze_panes = FREEZE
    row = header_at
    for r in rows:
        row += 1
        cancelled = r.status == "voided"
        body = xs.FONT_CANCELLED if cancelled else xs.FONT_BODY
        note = xs.FONT_CANCELLED_NOTE if cancelled else xs.FONT_BODY
        values = table_values(r)
        for col, value in enumerate(values, start=1):
            if col == COL["date"]:
                shown, fmt = value, xs.DATE
            elif col == COL["entered"]:
                shown, fmt = value, xs.DATETIME
            elif col == COL["amount"]:
                shown, fmt = value, xs.MONEY
            else:
                shown, fmt = value, None
            text_col = col in (COL["student"], COL["group"], COL["teacher"], COL["author"], COL["note"], COL["details"])
            xs.put(ws, row, col, shown, number_format=fmt, font=note if col in (COL["note"], COL["details"]) else body, border=xs.BORDER,
                   alignment=xs.ALIGN_LEFT_TOP if text_col else xs.ALIGN_CENTER_TOP)
    last_data = row
    if not rows:
        row += 1
        xs.section_row(ws, row, "Оплат за период нет", LAST_COL, font_=xs.FONT_NOTE)
        last_data = header_at
    else:
        active = [r for r in rows if r.status == "active"]
        row += 1
        xs.total_row(ws, row, TOTAL_LABEL, {COL["student"]: f"Оплат: {len(active)}", COL["amount"]: sum(int(r.amount) for r in active)}, LAST_COL)
    methods = by_method(rows)
    if methods:
        row += 2
        xs.section_row(ws, row, METHODS_TITLE, LAST_COL)
        row += 1
        xs.header_row(ws, row, ["Способ", "Оплат", "Оплачено"], start_col=COL["student"])
        for label, n, total in methods:
            row += 1
            xs.put(ws, row, COL["student"], label, font=xs.FONT_BODY, border=xs.BORDER, alignment=xs.ALIGN_LEFT)
            xs.put(ws, row, COL["student"] + 1, n, font=xs.FONT_BODY, border=xs.BORDER, alignment=xs.ALIGN_CENTER)
            xs.put(ws, row, COL["student"] + 2, total, number_format=xs.MONEY, font=xs.FONT_BODY, border=xs.BORDER, alignment=xs.ALIGN_CENTER)
    xs.autosize(ws, header_row_number=header_at, last_col=LAST_COL, min_width=12, wrap_cols=WRAP_COLS)
    ws.auto_filter.ref = f"A{header_at}:{xs.get_column_letter(LAST_COL)}{max(last_data, header_at + 1)}"
    xs.print_setup(ws, header=header_at)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def journal_file_name(first: date, last: date) -> str:
    """«Журнал_оплат_октябрь-2026.xlsx» for a calendar month, else «Журнал_оплат_01.10-15.11.2026.xlsx» (the dates without a year when it is one)."""
    key = st_tx.single_month(first, last)
    if key:
        return xs.file_name("Журнал оплат", f"{xs.MONTH_NAMES[int(key[5:7]) - 1]}-{key[:4]}")
    if first == last:
        return xs.file_name("Журнал оплат", xs.date_ru(first))
    span = f"{first.strftime('%d.%m')}-{last.strftime('%d.%m.%Y')}" if first.year == last.year else f"{xs.date_ru(first)}-{xs.date_ru(last)}"
    return xs.file_name("Журнал оплат", span)
