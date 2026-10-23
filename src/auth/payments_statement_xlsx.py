"""
ALGO - the statement as an Excel file: it reads like the page (Выписка), in the house style (``xlsx_style``).

Title block (three lines: who, as-of / period, totals), then the table «Дата | Операция | Стоимость | Оплачено | [Прощено] | [Возвращено] | [Переведено] |
Баланс | Подробности» with a band per month, the carry line of a single month, a blue «Итого» row and, below, the periods to come and the cancelled
records.  The words come from ``payments_statement_texts`` (the mirror of the page's rules); this module only draws them.

The rules of the file (owner, 23.10.2026):

* no signed numbers: a payment is in «Оплачено», a forgiven debt in «Прощено», a refund in «Возвращено», a transfer to another group in «Переведено»
  (the last three columns exist only when the file has such an operation; money that came in from another group stays in «Оплачено», as on the page);
* ONE balance rule everywhere (the table, the «Итого» row, the title line): the balance is the words of the screen - «Долг 92 000» / «Аванс 21 000» / «0»;
* real dates, whole-number money cells with ``#,##0``, widths from the content, frozen header under the title block, print setup.
"""
from __future__ import annotations

import io
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from . import payments_statement_texts as st_tx
from . import xlsx_style as xs

SHEET_TITLE = "Выписка"
HEADER = ["Дата", "Операция", "Стоимость", "Оплачено", "Баланс", "Подробности"]       # the columns every statement has (the optional ones go after «Оплачено»)
OPTIONAL = (("forgiven", "Прощено"), ("refunded", "Возвращено"), ("transferred", "Переведено"))
DATE_FORMAT = xs.DATE
MONEY_FORMAT = xs.MONEY
CANCELLED_TITLE = "Отменённые записи"
CANCELLED_EMPTY = "Отменённых записей нет"
TOTAL_LABEL = "Итого"
# the type of a record -> the column of its money (anything else: «Оплачено»)
_COLUMN_OF = {"writeoff": "forgiven", "refund": "refunded", "transfer_out": "transferred"}


def money_column(entry_type: str) -> str:
    return _COLUMN_OF.get(entry_type, "paid")


def columns_of(kinds) -> List[Tuple[str, str]]:
    """``[(key, header)]`` of the table: the fixed columns and the optional money columns that the file really uses."""
    out = [("date", "Дата"), ("title", "Операция"), ("charged", "Стоимость"), ("paid", "Оплачено")]
    out += [(key, label) for key, label in OPTIONAL if key in kinds]
    return out + [("balance", "Баланс"), ("details", "Подробности")]


def build_statement_xlsx(
    statement, *, date_from: Optional[date] = None, date_to: Optional[date] = None, include_voided: bool = False,
) -> bytes:
    """``statement`` is the WHOLE ledger (``PaymentsReports.statement`` without a range): the carry line of a month needs what came
    before it; the range is applied here."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_TITLE
    m = statement.membership
    month = st_tx.single_month(date_from, date_to)

    entries = [e for e in st_tx.build_entries(statement.rows, statement.upcoming) if st_tx.in_range(e.date, date_from, date_to)]
    voided = [r for r in statement.voided if st_tx.in_range(r.date, date_from, date_to)] if include_voided else []
    used = {money_column(e.type) for e in entries if e.paid} | {money_column(r.type) for r in voided if (r.credit or r.outflow)}
    cols = columns_of(used)
    pos = {key: i for i, (key, _) in enumerate(cols, start=1)}
    last_col = len(cols)
    wrap_cols = (pos["title"], pos["details"])

    # ---- title block: three lines (the frozen area stays small), an empty row, then the header
    header_at = xs.title_block(ws, [
        st_tx.title_line(m),
        st_tx.asof_line(statement.as_of, statement.accounting_start, date_from, date_to),
        st_tx.totals_line(statement.summary),
    ], last_col)
    xs.header_row(ws, header_at, [h for _, h in cols], left=(pos["title"], pos["details"]))
    ws.freeze_panes = f"A{header_at + 1}"
    row = header_at

    def next_row() -> int:
        nonlocal row
        row += 1
        return row

    def data_row(entry_date: date, title: str, charged: Optional[int], money: Dict[str, int], balance: str, details: str, *, cancelled: bool = False):
        r = next_row()
        body = xs.FONT_CANCELLED if cancelled else xs.FONT_BODY
        note = xs.FONT_CANCELLED_NOTE if cancelled else xs.FONT_BODY
        xs.put(ws, r, pos["date"], entry_date, number_format=xs.DATE, font=body, border=xs.BORDER, alignment=xs.ALIGN_CENTER_TOP)
        xs.put(ws, r, pos["title"], title, font=body, border=xs.BORDER, alignment=xs.ALIGN_LEFT_TOP)
        for key in ("charged", "paid", *[k for k, _ in OPTIONAL]):
            if key not in pos:
                continue
            value = charged if key == "charged" else money.get(key)
            xs.put(ws, r, pos[key], int(value) if value is not None else None, number_format=xs.MONEY if value is not None else None, font=body,
                   border=xs.BORDER, alignment=xs.ALIGN_CENTER_TOP)
        xs.put(ws, r, pos["balance"], balance or None, font=body, border=xs.BORDER, alignment=xs.ALIGN_CENTER_TOP)
        xs.put(ws, r, pos["details"], details or None, font=note, border=xs.BORDER, alignment=xs.ALIGN_LEFT_TOP)

    def band(text: str, *, font=None, fill_rgb: str = xs.SECTION_FILL):
        xs.section_row(ws, next_row(), text, last_col, fill_rgb=fill_rgb, font_=font)

    shown = st_tx.group_by_month(entries, statement.rows, month)
    if not shown:
        band("В выписке пока нет записей" if not statement.rows else "Записей за период нет", font=xs.FONT_NOTE)
    for item in shown:
        if isinstance(item, st_tx.Band):
            band(item.title)
        elif isinstance(item, (st_tx.Carry, st_tx.Quiet)):
            band(item.text, font=xs.FONT_NOTE)
        else:
            amount = abs(int(item.paid)) if item.paid else None
            data_row(item.date, item.title, item.charged, {money_column(item.type): amount} if amount is not None else {}, item.balance, item.details)

    # ---- the blue total row: the sums of the columns of the rows above and the balance after the last of them
    if entries:
        sums: Dict[int, Any] = {pos["charged"]: sum(int(e.charged or 0) for e in entries)}
        for key in ("paid", *[k for k, _ in OPTIONAL]):
            if key in pos:
                sums[pos[key]] = sum(abs(int(e.paid)) for e in entries if e.paid and money_column(e.type) == key)
        closing = next((e.balance for e in reversed(entries) if e.balance), "")
        if closing:
            sums[pos["balance"]] = closing
        xs.total_row(ws, next_row(), TOTAL_LABEL, sums, last_col)

    # ---- the periods to come (as the page: only for the whole statement, never for a month or a range)
    if statement.upcoming and date_from is None and date_to is None:
        row += 1                                               # an empty line before the block
        band(st_tx.UPCOMING_TITLE, fill_rgb=xs.SERVICE_FILL)
        for segment in statement.upcoming:
            band(st_tx.upcoming_line(segment), font=xs.FONT_BODY, fill_rgb="FFFFFF")

    # ---- the cancelled records (below, in grey): never in the total
    if include_voided:
        row += 1
        band(CANCELLED_TITLE, fill_rgb=xs.SERVICE_FILL)
        if not voided:
            band(CANCELLED_EMPTY, font=xs.FONT_NOTE, fill_rgb="FFFFFF")
        for r in voided:
            amount = int(r.credit or 0) or int(r.outflow or 0)
            data_row(r.date, r.title, None, {money_column(r.type): amount} if amount else {}, "", st_tx.cancel_note(r), cancelled=True)

    xs.autosize(ws, header_row_number=header_at, last_col=last_col, min_width=12, wrap_cols=wrap_cols)
    xs.print_setup(ws, header=header_at)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def statement_file_name(statement, date_from: Optional[date] = None, date_to: Optional[date] = None) -> str:
    """«Выписка_Алиев_Анвар_октябрь-2026.xlsx» / «…_с-01.10.2026_по-15.11.2026» / «…_на-21.10.2026» (all months): no technical ids."""
    key = st_tx.single_month(date_from, date_to)
    if key:
        when = f"{xs.MONTH_NAMES[int(key[5:7]) - 1]}-{key[:4]}"
    elif date_from or date_to:
        when = "_".join(x for x in (f"с-{xs.date_ru(date_from)}" if date_from else "", f"по-{xs.date_ru(date_to)}" if date_to else "") if x)
    else:
        when = f"на-{xs.date_ru(statement.as_of)}"
    return xs.file_name("Выписка", statement.membership.student_name, when)
