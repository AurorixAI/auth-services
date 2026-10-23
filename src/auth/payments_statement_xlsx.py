"""
ALGO - the statement as an Excel file: it reads like the page (Выписка).

Title block (label / value), then the table «Дата | Операция | Стоимость | Оплачено | Баланс | Подробности» with a band per month,
the carry line of a single month and, below, the cancelled records.  The words come from ``payments_statement_texts`` (the mirror
of the page's rules); this module only draws them: real dates, whole-number money cells with a thousands format, the balance as
text («Долг 92 000»), widths from the content, frozen header.
"""
from __future__ import annotations

import io
import math
from datetime import date
from typing import Any, List, Optional, Sequence

from . import payments_statement_texts as st_tx

SHEET_TITLE = "Выписка"
HEADER = ["Дата", "Операция", "Стоимость", "Оплачено", "Баланс", "Подробности"]
LAST_COL = len(HEADER)
DATE_FORMAT = "DD.MM.YYYY"
MONEY_FORMAT = "#,##0"
MIN_WIDTH = 12
WRAP_CAP = 60                    # «Операция» and «Подробности» wrap from here
WRAP_COLS = (2, 6)               # B, F
CANCELLED_TITLE = "Отменённые записи"
CANCELLED_EMPTY = "Отменённых записей нет"


def _put(ws, row: int, col: int, value: Any, *, number_format: Optional[str] = None, **style: Any):
    """Write a cell.  A text stays text (``data_type`` "s"): a student's note starting with "=" is never a formula."""
    cell = ws.cell(row=row, column=col, value=value)
    if isinstance(value, str):
        cell.data_type = "s"
    if number_format:
        cell.number_format = number_format
    for key, item in style.items():
        setattr(cell, key, item)
    return cell


def _lines_needed(text: Any, width: float) -> int:
    if not isinstance(text, str) or not text:
        return 1
    usable = max(1, int(width) - 2)
    return sum(max(1, math.ceil(len(part) / usable)) for part in text.split("\n"))


def _shown_length(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, date):
        return len("00.00.0000")
    if isinstance(value, int):
        return len(f"{value:,}")
    return max((len(part) for part in str(value).split("\n")), default=0)


def build_statement_xlsx(
    statement, *, date_from: Optional[date] = None, date_to: Optional[date] = None, include_voided: bool = False,
) -> bytes:
    """``statement`` is the WHOLE ledger (``PaymentsReports.statement`` without a range): the carry line of a month needs what came
    before it; the range is applied here."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_TITLE
    m = statement.membership

    bold = Font(bold=True)
    head_fill = PatternFill("solid", start_color="D9E2F3", end_color="D9E2F3")
    band_fill = PatternFill("solid", start_color="EDF1F7", end_color="EDF1F7")
    grey_fill = PatternFill("solid", start_color="E7E6E6", end_color="E7E6E6")
    quiet = Font(italic=True, color="595959")
    cancelled_font = Font(color="808080", strike=True)
    cancelled_note_font = Font(color="808080")
    left_top = Alignment(horizontal="left", vertical="top", wrap_text=True)
    centre_top = Alignment(horizontal="center", vertical="top")
    left = Alignment(horizontal="left", vertical="center")

    month = st_tx.single_month(date_from, date_to)

    # ---- title block: label / value
    row = 1
    _put(ws, row, 1, SHEET_TITLE, font=Font(bold=True, size=14))
    title_rows: List[tuple] = [("Ученик", m.student_name, None)]
    if m.display_id:
        title_rows.append(("ID", m.display_id, None))
    title_rows.append(("Группа", m.group_name, None))
    if m.teacher_name:
        title_rows.append(("Преподаватель", m.teacher_name, None))
    title_rows += [
        ("По состоянию на", statement.as_of, DATE_FORMAT),
        ("Учёт ведётся с", statement.accounting_start, DATE_FORMAT),
        ("Период", st_tx.period_label(date_from, date_to), None),
    ]
    for label, value in st_tx.total_lines(statement.summary):
        title_rows.append((label, value, MONEY_FORMAT if isinstance(value, int) else None))
    label_width = 0
    for label, value, fmt in title_rows:
        row += 1
        _put(ws, row, 1, label, font=bold, alignment=left)
        _put(ws, row, 2, value, number_format=fmt, alignment=left)
        label_width = max(label_width, len(label))

    # ---- the table
    row += 2
    header_row = row
    for col, text in enumerate(HEADER, 1):
        _put(ws, row, col, text, font=bold, fill=head_fill,
             alignment=Alignment(horizontal="left" if col in WRAP_COLS else "center", vertical="center"))
    ws.freeze_panes = f"A{header_row + 1}"

    measured: List[List[Any]] = [[text] for text in HEADER]       # what each column has to hold (for the widths)
    measured[0].append("0" * label_width)                         # the title block's labels sit in column A
    wrapped: List[tuple] = []                                     # (row, [(col, text)]) for the row heights

    def money(col: int, value: Optional[int]):
        if value is None:
            return
        cell = _put(ws, row, col, int(value), number_format=MONEY_FORMAT, alignment=centre_top)
        measured[col - 1].append(int(value))
        return cell

    def data_row(entry_date: date, title: str, charged: Optional[int], paid: Optional[int], balance: str, details: str,
                 *, fonts: Optional[tuple] = None):
        nonlocal row
        row += 1
        a = _put(ws, row, 1, entry_date, number_format=DATE_FORMAT, alignment=centre_top)
        b = _put(ws, row, 2, title, alignment=left_top)
        c = money(3, charged)
        d = money(4, paid)
        e = _put(ws, row, 5, balance, alignment=centre_top) if balance else None
        f = _put(ws, row, 6, details, alignment=left_top) if details else None
        measured[0].append(entry_date)
        measured[1].append(title)
        if balance:
            measured[4].append(balance)
        if details:
            measured[5].append(details)
        wrapped.append((row, [(2, title), (6, details)]))
        if fonts:
            grey, note = fonts
            for cell in (a, b, c, d, e):
                if cell is not None:
                    cell.font = grey
            if f is not None:
                f.font = note

    def spanning(text: str, *, font=None, fill=None):
        nonlocal row
        row += 1
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=LAST_COL)
        style = {"font": font or bold, "alignment": Alignment(horizontal="left", vertical="center")}
        if fill is not None:
            style["fill"] = fill
        _put(ws, row, 1, text, **style)

    entries = [e for e in st_tx.build_entries(statement.rows, statement.upcoming) if st_tx.in_range(e.date, date_from, date_to)]
    items = st_tx.group_by_month(entries, statement.rows, month)
    if not items:
        spanning("В выписке пока нет записей" if not statement.rows else "Записей за период нет", font=quiet)
    for item in items:
        if isinstance(item, st_tx.Band):
            spanning(item.title, fill=band_fill)
        elif isinstance(item, (st_tx.Carry, st_tx.Quiet)):
            spanning(item.text, font=quiet)
        else:
            data_row(item.date, item.title, item.charged, item.paid, item.balance, item.details)

    # ---- the cancelled records (below, in grey)
    if include_voided:
        voided = [r for r in statement.voided if st_tx.in_range(r.date, date_from, date_to)]
        row += 1                                               # an empty line before the block
        spanning(CANCELLED_TITLE, fill=grey_fill)
        if not voided:
            spanning(CANCELLED_EMPTY, font=quiet)
        for r in voided:
            amount = int(r.credit) - int(r.outflow)
            data_row(r.date, r.title, None, amount if (r.credit or r.outflow) else None, "", st_tx.cancel_note(r),
                     fonts=(cancelled_font, cancelled_note_font))

    # ---- widths from the content, heights of the wrapped rows
    widths = []
    for col, values in enumerate(measured, 1):
        width = max(MIN_WIDTH, max(_shown_length(v) for v in values) + 2)
        if col in WRAP_COLS:
            width = min(width, WRAP_CAP)
        widths.append(width)
        ws.column_dimensions[chr(64 + col)].width = width
    for r, cells in wrapped:
        lines = max(_lines_needed(text, widths[col - 1]) for col, text in cells)
        if lines > 1:
            ws.row_dimensions[r].height = 15 * lines

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
