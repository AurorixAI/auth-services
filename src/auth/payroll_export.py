"""
ALGO - Payroll sheets as an Excel workbook (N13 P3), in the house style (``xlsx_style``).  ``openpyxl``; one sheet per teacher.

The layout follows the partner's table: group, lesson type, lesson time, student, price, percent, the lesson dates as columns (real dates; 1 = counted,
0 = not counted in red, "Ув.?" on yellow = excused, waiting for a decision, grey = counted but not paid), then counted / not paid / (included, only when the
owner used it) / earned and the blue total row.  Below the table: the blue block «Заработано / Выплачено / К выплате», the payouts (date and method) and the
legend of the codes.  Values are plain numbers, dates and text - no formulas, so any viewer shows the same figures.

A month export starts with a sheet «Итого»: one blue table teacher -> earned -> paid -> to pay with a total row; the teachers without a saved sheet are
listed there with a note; the sheet names are unique even for namesakes.

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

from . import payroll_engine as engine
from . import xlsx_style as xs
from .payments_statement_texts import method_label

XLSX_MIME = xs.SPREADSHEET_MIME

MONTHS = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)
_FORMULA_START = ("=", "+", "-", "@")
_BAD_SHEET_CHARS = re.compile(r"[\\/?*\[\]:]")

MONEY = xs.MONEY
PERCENT_WHOLE = xs.PERCENT
PERCENT_FRACTION = xs.PERCENT_FRACTION
SUMMARY_SHEET = "Итого"
UNSAVED_NOTE = "ведомость не сохранена — в расчёт не вошла"

HEADERS_LEFT = ["Группа", "Тип занятий", "Время урока", "Ученик", "Цена в месяц", "Процент"]
HEADER_COUNTED, HEADER_UNPAID, HEADER_INCLUDED, HEADER_EARNED = "Засчитано уроков", "Не оплачено уроков", "Включено в расчёт", "Заработано"
BLOCK_HEADERS = ("Заработано", "Выплачено", "К выплате")
LEGEND_LINE = ("Обозначения: 1 — урок засчитан · 0 — не засчитан (уважительная причина подтверждена) · Ув.? — уважительная причина, ждёт решения владельца · "
               "серая 1 — засчитан, но ученик не заплатил (в «Заработано» входит только с «да» в колонке «Включено в расчёт») · пусто — ученика в группе не было")
SUMMARY_NOTE = "Листы по учителям — дальше в этом файле. «К выплате» = заработано − выплачено."
FREEZE_COL = 5                   # the group, the type, the time and the student stay on the screen


_WHITESPACE = re.compile(r"[\t\r\n]")
# not allowed in a worksheet: openpyxl refuses \x00-\x08, \x0b, \x0c, \x0e-\x1f, and U+FFFE / U+FFFF are not XML characters
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


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
    if isinstance(value, str):
        value = safe_text(value)
    return xs.put(ws, row, column, value, **style)


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
    """``2026-10`` -> «октябрь 2026» (inside a sentence)."""
    year, number = month.split("-")
    return f"{MONTHS[int(number) - 1]} {year}"


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


def _balance(payout: Optional[Mapping[str, Any]], total: int) -> Tuple[int, int, int, str]:
    """``(earned, paid, to pay, header of the third cell)`` of a sheet: no negative number - an overpayment is told by the word «Переплачено»."""
    if not payout:
        return int(total), 0, int(total), BLOCK_HEADERS[2]
    balance = int(payout["balance"])
    return int(payout["target"]), int(payout["paid"]), abs(balance), "Переплачено" if balance < 0 else BLOCK_HEADERS[2]


def _earned_words(sheet: Mapping[str, Any]) -> str:
    earned, paid, to_pay, word = _balance(sheet.get("payout"), sheet["total_amount"])
    parts = [f"Заработано {earned:,}".replace(",", " ")]
    if sheet.get("payout"):
        parts += [f"Выплачено {paid:,}".replace(",", " "), f"{word} {to_pay:,}".replace(",", " ")]
    if sheet.get("written_off_total"):
        parts.append(f"из них прощено центром: {sheet['written_off_total']} уроков")
    return " · ".join(parts)


def write_sheet(ws, sheet: Mapping[str, Any]) -> None:
    """One teacher's sheet (``SheetOut.model_dump()``)."""
    columns, column_of = _columns(sheet)
    lines = sheet["lines"]
    show_included = any(line.get("include_unpaid") for line in lines)        # empty in the normal case: only when the owner used it
    first_date_col = len(HEADERS_LEFT) + 1
    last_date_col = first_date_col + len(columns) - 1
    right_col = last_date_col + 1
    included_col = right_col + 2 if show_included else None
    amount_col = right_col + (3 if show_included else 2)
    width = amount_col
    many_groups = len({line["group_id"] for line in lines}) > 1

    # ---- title block: who and when, the state of the sheet (+ the note), the totals in words
    status = (f"Редакция {sheet['revision']}" + (f" · сохранено {local_time(sheet['updated_at'])}" if sheet.get("updated_at") else "")
              if sheet.get("saved") else "Предварительный расчёт — не сохранён")
    if sheet.get("month_open"):
        status += " · месяц ещё не закончился"
    if sheet.get("note"):
        status += f" · Примечание: {sheet['note']}"
    header_row = xs.title_block(ws, [
        safe_text(f"Зарплата — {sheet.get('teacher_name') or 'преподаватель'} · {xs.month_title(sheet['month'])}"),
        safe_text(status), _earned_words(sheet)], width)

    # ---- the header: full words, the lesson days as real dates
    for i, text in enumerate(HEADERS_LEFT, start=1):
        xs.header_cell(ws, header_row, i, text)
    for i, (day, k) in enumerate(columns):
        xs.header_cell(ws, header_row, first_date_col + i, date.fromisoformat(day),
                       number_format=xs.DATE_SHORT if k == 0 else f'DD.MM" ({k + 1})"')
    xs.header_cell(ws, header_row, right_col, HEADER_COUNTED)
    xs.header_cell(ws, header_row, right_col + 1, HEADER_UNPAID)
    if included_col:
        xs.header_cell(ws, header_row, included_col, HEADER_INCLUDED)
    xs.header_cell(ws, header_row, amount_col, HEADER_EARNED)
    ws.row_dimensions[header_row].height = xs.HEADER_HEIGHT
    ws.freeze_panes = ws.cell(row=header_row + 1, column=FREEZE_COL)

    row = header_row + 1
    current_group: Optional[str] = None
    group_name = ""
    sums = {"counted": 0, "unpaid": 0, "amount": 0}
    unpaid_fill, pending_fill, excused_fill = xs.fill(xs.SERVICE_FILL), xs.fill(xs.PENDING_FILL), xs.fill(xs.WARN_FILL)
    red = xs.font(color="C00000", bold=True)
    grey = xs.font(color=xs.CANCELLED_COLOR)

    def subtotal() -> None:
        """«Итого: группа» in blue (only when the sheet has several groups): the sum of its LINES (a group that changed its type inside the month has two blocks)."""
        nonlocal row
        if current_group is None or not many_groups:
            return
        values = {right_col: sums["counted"], right_col + 1: sums["unpaid"], amount_col: sums["amount"]}
        xs.total_row(ws, row, f"Итого: {group_name}", values, width, formats={right_col: "0", right_col + 1: "0"})
        row += 1

    for line in lines:
        if line["group_id"] != current_group:
            subtotal()
            current_group, group_name = line["group_id"], line["group_name"]
            sums = {"counted": 0, "unpaid": 0, "amount": 0}
            xs.section_row(ws, row, f"{group_name} · {engine.LESSON_TYPE_LABELS.get(line['lesson_type'], line['lesson_type'])}", width)
            row += 1
        sums["counted"] += int(line["counted"])
        sums["unpaid"] += int(line["unpaid"])
        sums["amount"] += int(line["amount"])
        left, centre = xs.ALIGN_LEFT_TOP, xs.ALIGN_CENTER_TOP
        _put(ws, row, 1, line["group_name"], font=xs.FONT_BODY, border=xs.BORDER, alignment=left)
        _put(ws, row, 2, engine.LESSON_TYPE_LABELS.get(line["lesson_type"], line["lesson_type"]), font=xs.FONT_BODY, border=xs.BORDER, alignment=left)
        _put(ws, row, 3, line.get("lesson_time") or None, font=xs.FONT_BODY, border=xs.BORDER, alignment=centre)
        _put(ws, row, 4, line["student_name"], font=xs.FONT_BODY, border=xs.BORDER, alignment=left)
        fee_cell = _put(ws, row, 5, line.get("monthly_fee"), font=xs.FONT_BODY, border=xs.BORDER, number_format=MONEY, alignment=centre)
        note = _price_note(line)
        if note:
            fee_cell.comment = Comment(note, "ALGO", width=320, height=90)
            if line.get("price_changed") and not line.get("fee_overridden"):
                fee_cell.fill = xs.fill(xs.PENDING_FILL)
        percent = float(line["percent"]) if line.get("percent") is not None else None
        _put(ws, row, 6, percent, font=xs.FONT_BODY, border=xs.BORDER, alignment=centre,
             number_format=PERCENT_WHOLE if percent is None or percent == int(percent) else PERCENT_FRACTION)
        for mark in line["marks"]:
            col = first_date_col + column_of[(line["group_id"], mark["date"], mark["lesson_id"])]
            state = mark["state"]
            style = dict(border=xs.BORDER, alignment=xs.ALIGN_CENTER_TOP, font=xs.FONT_BODY)
            if state == engine.NOT_IN_GROUP:
                _put(ws, row, col, None, border=xs.BORDER)
            elif state == engine.EXCUSED_CONFIRMED:
                _put(ws, row, col, 0, **{**style, "font": red, "fill": excused_fill})
            elif state == engine.EXCUSED_PENDING:
                _put(ws, row, col, "Ув.?", **{**style, "fill": pending_fill})
            elif state == engine.UNPAID:
                _put(ws, row, col, 1, **{**style, "fill": unpaid_fill, "font": grey})
            else:
                _put(ws, row, col, 1, **style)
        for col in range(first_date_col, last_date_col + 1):                 # a day without a mark still has its borders
            ws.cell(row=row, column=col).border = xs.BORDER
        _put(ws, row, right_col, line["counted"], font=xs.FONT_BODY, border=xs.BORDER, alignment=xs.ALIGN_CENTER_TOP)
        _put(ws, row, right_col + 1, line["unpaid"], font=xs.FONT_BODY, border=xs.BORDER, alignment=xs.ALIGN_CENTER_TOP)
        if included_col:
            _put(ws, row, included_col, "да" if line.get("include_unpaid") else None, font=xs.FONT_BODY, border=xs.BORDER, alignment=xs.ALIGN_CENTER_TOP)
        _put(ws, row, amount_col, line["amount"], font=xs.FONT_BODY, border=xs.BORDER, number_format=MONEY, alignment=xs.ALIGN_CENTER_TOP)
        row += 1
    subtotal()

    # ---- the blue «Итого» row of the table
    totals = {right_col: sheet["counted_total"], right_col + 1: sheet["unpaid_total"], amount_col: sheet["total_amount"]}
    xs.total_row(ws, row, "Итого", totals, width, formats={right_col: "0", right_col + 1: "0"})

    # ---- «Заработано / Выплачено / К выплате» (blue) and the payouts
    payout = sheet.get("payout")
    earned, paid, to_pay, word = _balance(payout, sheet["total_amount"])
    row += 2
    for i, header in enumerate((BLOCK_HEADERS[0], BLOCK_HEADERS[1], word) if payout else (BLOCK_HEADERS[0],)):
        xs.header_cell(ws, row, 1 + i, header)
    row += 1
    xs.total_row(ws, row, "", dict(zip((1, 2, 3), (earned, paid, to_pay) if payout else (earned,))), 3 if payout else 1, label_col=1)
    if payout and payout.get("basis") == "fresh":
        row += 1
        _put(ws, row, 1, "Расчёт по актуальным данным: ведомость изменилась после сохранения", font=xs.FONT_NOTE)
    items = [item for item in (payout or {}).get("items") or ()]
    if items:
        row += 2
        xs.header_row(ws, row, ["Дата выплаты", "Способ", "Сумма", "Примечание"], left=(4,))
        for item in items:
            row += 1
            gone = bool(item.get("voided_at"))
            body, quiet = (xs.FONT_CANCELLED, xs.FONT_CANCELLED_NOTE) if gone else (xs.FONT_BODY, xs.FONT_BODY)
            paid_on = item["paid_on"]
            _put(ws, row, 1, paid_on if isinstance(paid_on, date) else date.fromisoformat(str(paid_on)), number_format=xs.DATE, font=body,
                 border=xs.BORDER, alignment=xs.ALIGN_CENTER_TOP)
            _put(ws, row, 2, method_label(item.get("method")), font=body, border=xs.BORDER, alignment=xs.ALIGN_CENTER_TOP)
            _put(ws, row, 3, int(item["amount"]), number_format=MONEY, font=body, border=xs.BORDER, alignment=xs.ALIGN_CENTER_TOP)
            text = (f"отменена: {item['void_reason']}" if gone and item.get("void_reason") else "отменена" if gone else item.get("note") or None)
            _put(ws, row, 4, text, font=quiet, border=xs.BORDER, alignment=xs.ALIGN_LEFT_TOP)

    # ---- the legend of the codes of the date columns: one quiet line
    row += 2
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=width)
    _put(ws, row, 1, LEGEND_LINE, font=xs.FONT_NOTE, alignment=xs.ALIGN_LEFT_TOP)

    # ---- widths: from the content; a lesson day is as narrow as its date
    xs.autosize(ws, header_row_number=header_row, last_col=width, min_width=10, wrap_cols=(1, 4),
                fixed={col: 8 for col in range(first_date_col, last_date_col + 1)})
    xs.print_setup(ws, header=header_row)


def _lessons_held(sheet: Mapping[str, Any]) -> int:
    return sum(len(block["dates"]) for block in sheet["groups"])


def write_month_summary(ws, month: str, sheets: Sequence[Mapping[str, Any]], titles: Sequence[str], unsaved: Sequence[Mapping[str, Any]],
                        month_open: Optional[bool], org_name: Optional[str] = None, as_of: Optional[date] = None) -> None:
    """The first sheet of a month: one blue table «Учитель | Групп | Уроков проведено | Заработано | Выплачено | К выплате | Примечание», the teachers without a
    sheet (yellow, with a note) and the total row."""
    headers = ["Учитель", "Групп", "Уроков проведено", "Заработано", "Выплачено", "К выплате", "Примечание"]
    width = len(headers)
    rows: List[Tuple[str, Optional[Tuple[int, int, int, int, int]], str]] = []
    totals = [0, 0, 0, 0, 0]
    for sheet in sheets:
        earned, paid, to_pay, word = _balance(sheet.get("payout"), sheet["total_amount"])
        note = f"переплачено на {to_pay:,}".replace(",", " ") if word == "Переплачено" else ""
        shown_to_pay = 0 if word == "Переплачено" else to_pay
        values = (len({line["group_id"] for line in sheet["lines"]}), _lessons_held(sheet), earned, paid, shown_to_pay)
        rows.append((sheet.get("teacher_name") or "Преподаватель", values, note))
        totals = [a + b for a, b in zip(totals, values)]
    for item in unsaved:
        rows.append((item.get("teacher_name") or "Преподаватель", None, item.get("note") or UNSAVED_NOTE))
    status = xs.words_join([f"По состоянию на {xs.date_ru(as_of)}" if as_of else None, f"Учителей: {len(rows)}",
                            f"Не сохранено ведомостей: {len(unsaved)}" if unsaved else None, "месяц ещё не закончился" if month_open else None])
    title = f"Зарплата — {xs.month_title(month)}" + (f" · {org_name}" if org_name else "")
    header_row = xs.title_block(ws, [safe_text(title), status,
                                     f"Заработано {totals[2]:,} · Выплачено {totals[3]:,} · К выплате {totals[4]:,}".replace(",", " ")], width)
    xs.header_row(ws, header_row, headers, left=(1, 7))
    ws.freeze_panes = f"A{header_row + 1}"
    row = header_row
    for name, values, note in rows:
        row += 1
        _put(ws, row, 1, name, font=xs.FONT_BODY, border=xs.BORDER, alignment=xs.ALIGN_LEFT_TOP)
        for col, value in enumerate(values or (None,) * 5, start=2):
            _put(ws, row, col, value, number_format=MONEY if col >= 4 else None, font=xs.FONT_BODY, border=xs.BORDER, alignment=xs.ALIGN_CENTER_TOP)
        _put(ws, row, 7, note or None, font=xs.FONT_NOTE if values is None else xs.FONT_BODY, border=xs.BORDER, alignment=xs.ALIGN_LEFT_TOP)
        if values is None:
            for col in range(1, width + 1):
                ws.cell(row=row, column=col).fill = xs.fill(xs.PENDING_FILL)
    row += 1
    xs.total_row(ws, row, "Итого", dict(zip((2, 3, 4, 5, 6), totals)), width, formats={2: "0", 3: "0"})
    row += 2
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=width)
    _put(ws, row, 1, SUMMARY_NOTE, font=xs.FONT_NOTE, alignment=xs.ALIGN_LEFT)
    xs.autosize(ws, header_row_number=header_row, last_col=width, min_width=10, wrap_cols=(1, 7))
    xs.print_setup(ws, header=header_row)


def build_workbook(sheets: Sequence[Mapping[str, Any]], *, summary: Optional[Mapping[str, Any]] = None) -> bytes:
    """A workbook with a worksheet per sheet (``SheetOut.model_dump()``), in the order given.  ``summary`` (a month export) =
    ``{"month": "2026-10", "month_open": bool, "unsaved": [{"teacher_name", "note"}]}`` adds the first sheet «Итого»."""
    workbook = Workbook()
    workbook.remove(workbook.active)
    taken: List[str] = [SUMMARY_SHEET] if summary is not None else []
    titles: List[str] = []
    for sheet in sheets:
        title = sheet_title(sheet.get("teacher_name"), taken)
        taken.append(title)
        titles.append(title)
    if summary is not None:
        write_month_summary(workbook.create_sheet(title=SUMMARY_SHEET), summary["month"], sheets, titles, summary.get("unsaved") or (),
                            summary.get("month_open"), summary.get("org_name"), summary.get("as_of"))
    for sheet, title in zip(sheets, titles):
        write_sheet(workbook.create_sheet(title=title), sheet)
    if not sheets and summary is None:
        workbook.create_sheet(title="Пусто")
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def export_filename(month: str, teacher_name: Optional[str] = None) -> str:
    """``Зарплата_октябрь-2026.xlsx`` / ``Зарплата_Иван_Фамилия_октябрь-2026.xlsx`` (``month`` = ``2026-10``): Russian, no path characters, no control characters."""
    slug = xs.month_slug(date(int(month[:4]), int(month[5:7]), 1)) if re.fullmatch(r"\d{4}-\d{2}", month or "") else month
    return xs.file_name("Зарплата", clean_text(teacher_name) if teacher_name else None, slug)
