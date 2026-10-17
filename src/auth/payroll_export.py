"""
ALGO - Payroll sheets as an Excel workbook (N13 P3).  ``openpyxl``; one sheet per teacher.

The layout follows the partner's table: group, student, fee, percent, lesson time, the lesson
dates as columns (1 = counted, 0 = not counted in red, "Ув.?" on yellow = excused, waiting for
a decision, grey = counted but not paid), then counted / not paid / included / amount and a total row.
Values are plain numbers and text - no formulas, so any viewer shows the same figures.

Text that comes from the database (names, groups, notes) is data, never a formula: a cell whose text
starts with ``= + - @`` (or a tab / CR) gets a leading apostrophe and is stored as a string.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties

from . import payroll_engine as engine

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

MONTHS = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)
_FORMULA_START = ("=", "+", "-", "@")
_BAD_SHEET_CHARS = re.compile(r"[\\/?*\[\]:]")

RED = Font(color="C00000", bold=True)
BOLD = Font(bold=True)
GREY_FILL = PatternFill("solid", start_color="E5E7EB", end_color="E5E7EB")
YELLOW_FILL = PatternFill("solid", start_color="FFF2B3", end_color="FFF2B3")
RED_FILL = PatternFill("solid", start_color="FDE2E1", end_color="FDE2E1")
HEAD_FILL = PatternFill("solid", start_color="DCE6F1", end_color="DCE6F1")
GROUP_FILL = PatternFill("solid", start_color="F3F4F6", end_color="F3F4F6")
THIN = Side(style="thin", color="B0B7C3")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
CENTER = Alignment(horizontal="center", vertical="center")
MONEY = "#,##0"
PERCENT_WHOLE = '0"%"'
PERCENT_FRACTION = '0.0#"%"'

HEADERS_LEFT = ["Группа", "Тип занятий", "Время", "Ученик", "Оплата в месяц", "%"]
HEADERS_RIGHT = ["Засчитано", "Не оплачено", "Включено", "Начислено"]


_WHITESPACE = re.compile(r"[\t\r\n]")
# not allowed in a worksheet: openpyxl refuses \x00-\x08, \x0b, \x0c, \x0e-\x1f, and U+FFFE / U+FFFF are not XML characters
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def clean_text(value: Any) -> str:
    """Text for a worksheet: tab / CR / LF become a space, characters XML cannot hold are dropped."""
    return _CONTROL.sub("", _WHITESPACE.sub(" ", "" if value is None else str(value)))


def safe_text(value: Any) -> str:
    """A string that no spreadsheet program will read as a formula and that a worksheet accepts."""
    text = clean_text(value)
    return "'" + text if text.lstrip().startswith(_FORMULA_START) else text


def local_time(moment: Optional[str]) -> str:
    """``2026-09-30T20:30:00`` (naive UTC, as stored) -> ``01.10.2026 01:30`` (Tashkent)."""
    if not moment:
        return ""
    from .service import BUSINESS_TIMEZONE  # lazy: service is heavy

    parsed = datetime.fromisoformat(moment)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(BUSINESS_TIMEZONE).strftime("%d.%m.%Y %H:%M")


def _put(ws, row: int, column: int, value: Any, **style: Any):
    """Write a cell; text goes through ``safe_text`` and is stored as a string."""
    cell = ws.cell(row=row, column=column)
    if isinstance(value, str):
        cell.value = safe_text(value)
        cell.data_type = "s"
    else:
        cell.value = value
    for key, item in style.items():
        setattr(cell, key, item)
    return cell


def _price_note(line: Mapping[str, Any]) -> Optional[str]:
    """The comment of the price cell: a price that changed inside the month (with the steps), or an individual one."""
    if line.get("fee_overridden"):
        return None  # the owner wrote this sum on the sheet; nothing to explain
    parts: List[str] = []
    if line.get("price_changed"):
        steps = []
        for step in line.get("price_steps") or ():
            day = date.fromisoformat(step["date"])
            amount = step.get("amount")
            steps.append(f"с {day:%d.%m}: " + ("не задана" if amount is None else f"{amount:,}".replace(",", " ")))
        parts.append("Цена менялась в этом месяце — " + "; ".join(steps))
    if line.get("price_source") == "override":
        parts.append("Индивидуальная цена ученика")
    return clean_text(". ".join(parts)) or None


def sheet_title(name: Optional[str], taken: Sequence[str]) -> str:
    """A worksheet name: at most 31 characters, none of ``\\ / ? * [ ] :``, not starting or ending with ``'``, unique."""
    base = _BAD_SHEET_CHARS.sub(" ", clean_text(name) or "Преподаватель").strip().strip("'").strip() or "Преподаватель"
    base = base[:31].strip().strip("'") or "Преподаватель"
    candidate, n = base, 1
    while candidate.casefold() in {t.casefold() for t in taken}:
        n += 1
        suffix = f" ({n})"
        candidate = base[: 31 - len(suffix)] + suffix
    return candidate


def month_title(month: str) -> str:
    year, number = month.split("-")
    return f"{MONTHS[int(number) - 1]} {year}"


def _short(day: str, ordinal: int) -> str:
    parsed = date.fromisoformat(day)
    return f"{parsed:%d.%m}" + (f" ({ordinal + 1})" if ordinal else "")


def _columns(sheet: Mapping[str, Any]) -> Tuple[List[Tuple[str, int]], Dict[Tuple[str, str, str], int]]:
    """Date columns of the whole sheet: ``(day, k)`` where k numbers the lessons of one group on one day.

    Returns the sorted column keys and ``{(group_id, day, lesson_id): index}``.
    """
    keys = set()
    where: Dict[Tuple[str, str, str], Tuple[str, int]] = {}
    for block in sheet["groups"]:
        per_day: Dict[str, int] = {}
        for item in block["dates"]:
            k = per_day.get(item["date"], 0)
            per_day[item["date"]] = k + 1
            keys.add((item["date"], k))
            where[(block["group_id"], item["date"], item["lesson_id"])] = (item["date"], k)
    ordered = sorted(keys)
    index = {key: i for i, key in enumerate(ordered)}
    return ordered, {k: index[v] for k, v in where.items()}


def write_sheet(ws, sheet: Mapping[str, Any]) -> None:
    """One teacher's sheet (``SheetOut.model_dump()``)."""
    columns, column_of = _columns(sheet)
    first_date_col = len(HEADERS_LEFT) + 1
    last_date_col = first_date_col + len(columns) - 1
    right_col = last_date_col + 1
    width = right_col + len(HEADERS_RIGHT) - 1

    title = f"Ведомость — {sheet.get('teacher_name') or 'преподаватель'} — {month_title(sheet['month'])}"
    _put(ws, 1, 1, title, font=Font(bold=True, size=14))
    if sheet.get("saved"):
        note = f"Редакция {sheet['revision']}"
        if sheet.get("updated_at"):
            note += f" · сохранено {local_time(sheet['updated_at'])}"
    else:
        note = "Предварительный расчёт — не сохранён"
    if sheet.get("month_open"):
        note += " · месяц ещё не закончился"
    _put(ws, 2, 1, note, font=Font(italic=True, color="555555"))
    if sheet.get("note"):
        _put(ws, 3, 1, f"Примечание: {sheet['note']}", font=Font(italic=True, color="555555"))

    header_row = 5
    for i, text in enumerate(HEADERS_LEFT, start=1):
        _put(ws, header_row, i, text, font=BOLD, fill=HEAD_FILL, border=BORDER, alignment=CENTER)
    for i, (day, k) in enumerate(columns):
        _put(ws, header_row, first_date_col + i, _short(day, k), font=BOLD, fill=HEAD_FILL, border=BORDER,
             alignment=CENTER)
    for i, text in enumerate(HEADERS_RIGHT):
        _put(ws, header_row, right_col + i, text, font=BOLD, fill=HEAD_FILL, border=BORDER, alignment=CENTER)

    row = header_row + 1
    blocks = {block["group_id"]: block for block in sheet["groups"]}
    current_group: Optional[str] = None
    block_rows: List[int] = []

    def subtotal() -> None:
        nonlocal row
        if current_group is None:
            return
        block = blocks[current_group]
        _put(ws, row, 1, f"Итого: {block['group_name']}", font=BOLD, fill=GROUP_FILL, border=BORDER)
        for col in range(2, width + 1):
            _put(ws, row, col, None, fill=GROUP_FILL, border=BORDER)
        _put(ws, row, right_col, block["counted"], font=BOLD, fill=GROUP_FILL, border=BORDER, alignment=CENTER)
        _put(ws, row, right_col + 1, block["unpaid"], font=BOLD, fill=GROUP_FILL, border=BORDER, alignment=CENTER)
        _put(ws, row, right_col + 3, block["total_amount"], font=BOLD, fill=GROUP_FILL, border=BORDER,
             number_format=MONEY)
        row += 1

    for line in sheet["lines"]:
        if line["group_id"] != current_group:
            subtotal()
            current_group = line["group_id"]
        _put(ws, row, 1, line["group_name"], border=BORDER)
        _put(ws, row, 2, engine.LESSON_TYPE_LABELS.get(line["lesson_type"], line["lesson_type"]), border=BORDER)
        _put(ws, row, 3, line.get("lesson_time") or "", border=BORDER)
        _put(ws, row, 4, line["student_name"], border=BORDER)
        fee_cell = _put(ws, row, 5, line.get("monthly_fee"), border=BORDER, number_format=MONEY)
        note = _price_note(line)
        if note:
            fee_cell.comment = Comment(note, "ALGO", width=320, height=90)
            if line.get("price_changed") and not line.get("fee_overridden"):
                fee_cell.fill = YELLOW_FILL
        percent = float(line["percent"]) if line.get("percent") is not None else None
        _put(ws, row, 6, percent, border=BORDER,
             number_format=PERCENT_WHOLE if percent is None or percent == int(percent) else PERCENT_FRACTION)
        for mark in line["marks"]:
            col = first_date_col + column_of[(line["group_id"], mark["date"], mark["lesson_id"])]
            state = mark["state"]
            if state == engine.NOT_IN_GROUP:
                _put(ws, row, col, None, border=BORDER)
            elif state == engine.EXCUSED_CONFIRMED:
                _put(ws, row, col, 0, font=RED, fill=RED_FILL, border=BORDER, alignment=CENTER)
            elif state == engine.EXCUSED_PENDING:
                _put(ws, row, col, "Ув.?", fill=YELLOW_FILL, border=BORDER, alignment=CENTER)
            elif state == engine.UNPAID:
                _put(ws, row, col, 1, fill=GREY_FILL, font=Font(color="6B7280"), border=BORDER, alignment=CENTER)
            else:
                _put(ws, row, col, 1, border=BORDER, alignment=CENTER)
        _put(ws, row, right_col, line["counted"], border=BORDER, alignment=CENTER)
        _put(ws, row, right_col + 1, line["unpaid"], border=BORDER, alignment=CENTER)
        _put(ws, row, right_col + 2, "да" if line.get("include_unpaid") else "", border=BORDER, alignment=CENTER)
        _put(ws, row, right_col + 3, line["amount"], border=BORDER, number_format=MONEY)
        row += 1
    subtotal()

    # ИТОГО
    for col in range(1, width + 1):
        _put(ws, row, col, None, fill=HEAD_FILL, border=BORDER)
    _put(ws, row, 1, "ИТОГО", font=Font(bold=True, size=12), fill=HEAD_FILL, border=BORDER)
    _put(ws, row, right_col, sheet["counted_total"], font=BOLD, fill=HEAD_FILL, border=BORDER, alignment=CENTER)
    _put(ws, row, right_col + 1, sheet["unpaid_total"], font=BOLD, fill=HEAD_FILL, border=BORDER, alignment=CENTER)
    _put(ws, row, right_col + 3, sheet["total_amount"], font=Font(bold=True, size=12), fill=HEAD_FILL,
         border=BORDER, number_format=MONEY)

    for col, size in zip(range(1, 7), (22, 14, 14, 26, 15, 7)):
        ws.column_dimensions[get_column_letter(col)].width = size
    for col in range(first_date_col, last_date_col + 1):
        ws.column_dimensions[get_column_letter(col)].width = 7
    for offset, size in enumerate((11, 12, 10, 15)):
        ws.column_dimensions[get_column_letter(right_col + offset)].width = size
    ws.freeze_panes = ws.cell(row=header_row + 1, column=first_date_col)
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)


def build_workbook(sheets: Sequence[Mapping[str, Any]]) -> bytes:
    """A workbook with a worksheet per sheet (``SheetOut.model_dump()``), in the order given."""
    workbook = Workbook()
    workbook.remove(workbook.active)
    taken: List[str] = []
    for sheet in sheets:
        title = sheet_title(sheet.get("teacher_name"), taken)
        taken.append(title)
        write_sheet(workbook.create_sheet(title=title), sheet)
    if not sheets:
        workbook.create_sheet(title="Пусто")
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def export_filename(month: str, teacher_name: Optional[str] = None) -> str:
    """``Зарплата 2026-09 Имя Фамилия.xlsx`` - no path characters, no control characters."""
    parts = ["Зарплата", month] + ([teacher_name] if teacher_name else [])
    name = clean_text(" ".join(parts))
    name = re.sub(r'[\x00-\x1f\x7f"\\/:*?<>|;]', " ", name)
    name = re.sub(r"\s+", " ", name).strip()[:120]
    return f"{name or 'Зарплата'}.xlsx"
