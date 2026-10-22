"""
ALGO - OPENING-BULK: the Excel round trip of «Остатки на начало».

``build_export``: the table as .xlsx (sheet «Остатки»: columns A-I as the import, then the ids, «Без остатка», «Осталось уроков», the reference columns),
prefilled with the current openings.  ``import_preview``: the file back -> the SAME preview as ``batch/preview`` (+ what happened to every row, the items
ready for ``POST /org/opening-balances/batch``).  The file is never executed (``data_only``), never stored, never uploaded twice: the apply is the
atomic batch.  Formats of the cells: ``import_service._parse_date`` / ``_parse_money`` (the same as the import of students).
"""
from __future__ import annotations

import io
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from . import import_service as imp
from . import payment_status as ps
from .openings_schemas import (
    OpeningBatchItem,
    OpeningBatchPreview,
    OpeningsImportFile,
    OpeningsImportPreview,
    OpeningsImportRow,
)
from .openings_service import MAX_IMPORT_BYTES, OpeningsService, TableRow
from .payments_income import FORMULA_STARTS
from .payments_schemas import MAX_MONEY
from .payments_service import PaymentInvalid

logger = logging.getLogger(__name__)

SHEET = "Остатки"
SHEET_INSTRUCTIONS = "Инструкция"
MAX_EXPORT_ROWS = 20_000
MAX_FILE_ROWS = 5_000
MAX_FILE_SHEETS = 5
MAX_CHANGES = 2_000
YES = {"да", "yes", "y", "x", "1", "+"}

HEADERS = [
    "Фамилия*", "Имя*", "Класс (1-11)*", "Телефон", "Группа*", "Комментарий", "Оплачено до (дата)", "Долг, сум", "Долг на дату",
    "ID ученика (не менять)", "ID членства (не менять)", "Без остатка (да)", "Осталось уроков (пакет)", "Цена", "Режим", "Статус",
]
KEYS = {
    "last": "фамилия", "first": "имя", "class": "класс (1-11)", "group": "группа", "paid": "оплачено до (дата)", "debt": "долг, сум",
    "debt_on": "долг на дату", "student": "id ученика (не менять)", "membership": "id членства (не менять)", "none": "без остатка (да)",
    "lessons": "осталось уроков (пакет)",
}


class FileError(PaymentInvalid):
    pass


def _err(code: str, message: str) -> FileError:
    e = FileError(message)
    e.code = code
    return e


def _norm(h: Any) -> str:
    return re.sub(r"\s+", " ", imp._norm_header(h))


# ============================================================================================================
# Export
# ============================================================================================================


def _put_text(ws, row: int, col: int, value: Any):
    """A text cell stays text: a surname that starts with = + - @ is never a formula (OWASP)."""
    cell = ws.cell(row=row, column=col, value=value)
    if isinstance(value, str):
        cell.data_type = "s"
    return cell


def build_export(svc: OpeningsService, org_id: str, *, group_id=None, missing_only: bool = False, q: Optional[str] = None,
                 include_left: bool = False) -> Tuple[bytes, str]:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    settings, rows, totals, state, today = svc.rows(
        org_id, group_id=group_id, missing_only=missing_only, q=q, include_left=include_left, limit=MAX_EXPORT_ROWS,
        too_many="Слишком много учеников для файла — выберите группу")
    start = settings.accounting_start
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET
    head_font, head_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="2F6FED")
    for i, title in enumerate(HEADERS, start=1):
        c = ws.cell(row=1, column=i, value=title)
        c.font, c.fill, c.alignment = head_font, head_fill, Alignment(horizontal="center", vertical="center", wrap_text=True)
    for i, width in enumerate((18, 16, 10, 16, 20, 20, 16, 14, 14, 38, 38, 12, 14, 14, 10, 22), start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    mode = settings.current.mode
    for n, r in enumerate(rows, start=2):
        o = r.opening
        _put_text(ws, n, 1, r.s.last_name)
        _put_text(ws, n, 2, r.s.first_name)
        ws.cell(row=n, column=3, value=r.s.class_level)
        _put_text(ws, n, 4, r.s.student_phone)
        _put_text(ws, n, 5, r.g.name)
        _put_text(ws, n, 6, r.s.comment)
        if o is not None and o.paid_until:
            ws.cell(row=n, column=7, value=o.paid_until).number_format = "DD.MM.YYYY"
        if o is not None and o.debt_amount:
            ws.cell(row=n, column=8, value=int(o.debt_amount)).number_format = "# ##0"
            ws.cell(row=n, column=9, value=o.debt_on).number_format = "DD.MM.YYYY"
        _put_text(ws, n, 10, str(r.s.id))
        _put_text(ws, n, 11, str(r.m.id))
        if r.price.amount is not None:
            ws.cell(row=n, column=14, value=int(r.price.amount)).number_format = "# ##0"
        _put_text(ws, n, 15, "уроки" if mode == ps.LESSONS else "месяц")
        note = "ушёл: только долг" if r.left else ("перевод: остаток нельзя" if r.transfer else ("бесплатно" if r.free else (
            "пауза" if r.paused else ("есть оплаты" if r.money else ""))))
        _put_text(ws, n, 16, note)
    last = max(len(rows) + 1, 2)
    only_date = DataValidation(type="date", operator="greaterThanOrEqual", formula1="DATE(2020,1,1)", allow_blank=True,
                               showErrorMessage=True, errorTitle="Дата", error="Нужна дата не раньше 01.01.2020")
    whole = DataValidation(type="whole", operator="greaterThanOrEqual", formula1="1", allow_blank=True, showErrorMessage=True,
                           errorTitle="Сумма", error="Целое число сум, не меньше 1")
    lessons = DataValidation(type="whole", operator="between", formula1="0", formula2="400", allow_blank=True, showErrorMessage=True,
                             errorTitle="Уроки", error="Целое число от 0 до 400")
    for dv, cols in ((only_date, ("G", "I")), (whole, ("H",)), (lessons, ("M",))):
        ws.add_data_validation(dv)
        for col in cols:
            dv.add(f"{col}2:{col}{last}")
    info = wb.create_sheet(SHEET_INSTRUCTIONS)
    info.column_dimensions["A"].width = 110
    lines = [
        ("Остатки на начало — как заполнять", True),
        (f"Учёт оплат в ALGO идёт с {start.strftime('%d.%m.%Y')}. Здесь — что было у ученика ДО этой даты.", False),
        ("«Оплачено до (дата)» — ученик заплатил до этого дня (не раньше " + (start - _day()).strftime("%d.%m.%Y") +
         "). «Оплачено до» = день перед началом учёта означает «на начало долга нет»: строка считается заполненной.", False),
        ("«Долг, сум» + «Долг на дату» — сколько он был должен и на какую дату (дата не позже дня перед началом учёта; пусто = этот день).", False),
        ("«Осталось уроков (пакет)» — только для центра, где платят пакетами: вместо «Оплачено до» (дата считается по расписанию группы).", False),
        ("«Без остатка (да)» — проверено, на начало учёта долга нет (записывается «оплачено до» " + (start - _day()).strftime("%d.%m.%Y") + ").", False),
        ("Пустые поля у ученика, у которого остаток уже внесён, — убирают этот остаток.", False),
        ("Колонки J и K (ID) не меняйте и не сортируйте отдельно от строки: по ним ALGO находит ученика. Остальные колонки справочные.", False),
        ("После загрузки вы увидите сравнение «было → станет»; ничего не записывается, пока вы не нажмёте «Сохранить».", False),
    ]
    for i, (text, bold) in enumerate(lines, start=1):
        c = info.cell(row=i, column=1, value=text)
        c.alignment = Alignment(wrap_text=True)
        if bold:
            c.font = Font(bold=True, size=14)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue(), f"ostatki_na_nachalo_{today.isoformat()}.xlsx"


def _day():
    from datetime import timedelta

    return timedelta(days=1)


# ============================================================================================================
# Import
# ============================================================================================================


class _Cell:
    __slots__ = ("value", "formula")

    def __init__(self, value: Any, formula: bool = False) -> None:
        self.value, self.formula = value, formula


def _read(data: bytes) -> Tuple[Dict[str, int], List[Tuple[int, Dict[str, _Cell]]]]:
    """``(columns by key, [(excel row, {key: cell})])``.  Two passes over the sheet: the values (``data_only``: what the cell shows, nothing is
    executed) and the formulas (a formula cell that has no value is an error of the row, not an empty cell)."""
    from openpyxl import load_workbook

    if len(data) > MAX_IMPORT_BYTES:
        raise _err("FILE_TOO_LARGE", "Файл слишком большой (макс. 5 МБ)")
    try:
        values = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001 - not an xlsx, a corrupt zip, a bomb the library refuses ...
        raise _err("FILE_UNREADABLE", "Не удалось прочитать файл. Загрузите .xlsx, скачанный из ALGO") from None
    try:
        if len(values.sheetnames) > MAX_FILE_SHEETS:
            raise _err("FILE_UNREADABLE", "В файле слишком много листов. Загрузите файл, скачанный из ALGO")
        if SHEET not in values.sheetnames:
            raise _err("SHEET_MISSING", "В файле нет листа «Остатки». Скачайте актуальный файл")
        grid = []
        for n, cells in enumerate(values[SHEET].iter_rows(values_only=True), start=1):
            grid.append((n, list(cells)))
            if n > MAX_FILE_ROWS + 1 + 200:      # blank tails are fine, but never read a sheet of a million rows
                break
    finally:
        values.close()
    formulas: set = set()
    try:
        raw = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
        try:
            for n, cells in enumerate(raw[SHEET].iter_rows(values_only=False), start=1):
                for i, cell in enumerate(cells):
                    if getattr(cell, "data_type", None) == "f":
                        formulas.add((n, i))
                if n >= len(grid):
                    break
        finally:
            raw.close()
    except Exception:  # noqa: BLE001 - the second pass only adds a hint
        formulas = set()
    if not grid:
        raise _err("SHEET_MISSING", "Лист «Остатки» пуст")
    columns: Dict[str, int] = {}
    wanted = {v: k for k, v in KEYS.items()}
    for i, title in enumerate(grid[0][1]):
        key = _norm(title)
        for known, name in wanted.items():
            if key == known or (name in ("student", "membership", "none", "lessons") and key.startswith(known.split(" (")[0])):
                columns.setdefault(name, i)
    if "paid" not in columns and "debt" not in columns and "none" not in columns and "lessons" not in columns:
        raise _err("FILE_UNREADABLE", "В файле нет колонок «Оплачено до (дата)» и «Долг, сум». Скачайте актуальный файл")
    if "membership" not in columns and "student" not in columns and not ("last" in columns and "first" in columns and "group" in columns):
        raise _err("FILE_UNREADABLE", "В файле нет колонок для поиска ученика (ID членства или фамилия, имя, группа)")
    rows: List[Tuple[int, Dict[str, _Cell]]] = []
    for n, cells in grid[1:]:
        record = {}
        for key, i in columns.items():
            record[key] = _Cell(cells[i] if i < len(cells) else None, (n, i) in formulas)
        if all((c.value is None or str(c.value).strip() == "") and not c.formula for c in record.values()):
            continue
        rows.append((n, record))
    if len(rows) > MAX_FILE_ROWS:
        raise _err("TOO_MANY_ROWS", f"В файле больше {MAX_FILE_ROWS} строк")
    return columns, rows


def _text(cell: Optional[_Cell]) -> str:
    return "" if cell is None else imp._cell(cell.value)


def _empty(cell: Optional[_Cell]) -> bool:
    """No value in the cell (a formula without a cached value is "empty" here; the readers below turn it into the error FORMULA)."""
    return cell is None or cell.value is None or _text(cell) == ""


def import_preview(svc: OpeningsService, org_id: str, data: bytes) -> OpeningsImportPreview:
    columns, file_rows = _read(data)
    settings, table, totals, state, today = svc.rows(org_id, include_left=True, limit=10 ** 9)
    by_id: Dict[UUID, TableRow] = {r.m.id: r for r in table}
    by_student: Dict[Tuple[UUID, str], List[TableRow]] = {}
    by_name: Dict[Tuple[str, str, str], List[TableRow]] = {}
    for r in table:
        by_student.setdefault((r.s.id, (r.g.name or "").casefold()), []).append(r)
        by_name.setdefault(((r.s.last_name or "").casefold(), (r.s.first_name or "").casefold(), (r.g.name or "").casefold()), []).append(r)

    rows: List[OpeningsImportRow] = []
    candidates: List[Tuple[int, TableRow, str, Optional[OpeningBatchItem]]] = []   # (excel row, table row, matched_by, item | None)
    for n, rec in file_rows:
        out = OpeningsImportRow(row=n, status="error")
        rows.append(out)
        # ---- 1. which membership
        match: Optional[TableRow] = None
        matched_by = None
        last, first, grp = _text(rec.get("last")).strip(), _text(rec.get("first")).strip(), _text(rec.get("group")).strip()
        raw_k, raw_j = _text(rec.get("membership")).strip(), _text(rec.get("student")).strip()
        try:
            if raw_k:
                try:
                    mid = UUID(raw_k)
                except ValueError:
                    raise _row_error("UNKNOWN", "ID членства не распознан: не меняйте колонки ID")
                match, matched_by = by_id.get(mid), "membership_id"
                if match is None:
                    raise _row_error("UNKNOWN", "Такой записи нет в списке остатков (ученик ушёл, в архиве или это файл другого центра)")
                if last and (match.s.last_name or "").casefold() != last.casefold():
                    raise _row_error("ID_MISMATCH", "ID членства принадлежит другому ученику: проверьте, не сдвинулись ли колонки")
                if raw_j and raw_j.lower() != str(match.s.id).lower():
                    raise _row_error("ID_MISMATCH", "ID ученика не совпадает с ID членства: проверьте, не сдвинулись ли колонки")
            elif raw_j:
                try:
                    sid = UUID(raw_j)
                except ValueError:
                    raise _row_error("UNKNOWN", "ID ученика не распознан: не меняйте колонки ID")
                found = by_student.get((sid, grp.casefold()), [])
                match, matched_by = _single(found, "Ученика с таким ID нет в этой группе", "В группе несколько записей ученика"), "student_group"
            else:
                if not (last and first and grp):
                    raise _row_error("UNKNOWN", "Не хватает данных: нужны ID членства или фамилия, имя и группа")
                found = by_name.get((last.casefold(), first.casefold(), grp.casefold()), [])
                cls = _text(rec.get("class")).strip()
                if cls:
                    found = [r for r in found if str(r.s.class_level) == cls]
                match, matched_by = _single(found, "Ученика нет в центре: добавьте его через импорт учеников", "Нашлось несколько учеников с таким именем: используйте ID"), "name"
        except _RowError as bad:
            out.code, out.message = bad.code, bad.message
            if match is not None:
                out.membership_id, out.matched_by = str(match.m.id), matched_by
                out.student_name, out.group_name = match.student_name, match.g.name
            continue
        out.membership_id, out.matched_by, out.student_name, out.group_name = str(match.m.id), matched_by, match.student_name, match.g.name
        # ---- 2. what the row says
        try:
            item = _item_of(rec, match, settings.accounting_start)
        except _RowError as bad:
            out.code, out.message = bad.code, bad.message
            continue
        candidates.append((n, match, matched_by, item))
        out.status = "noop"                                  # until the plan says otherwise

    # ---- 3. one membership in two rows: neither is applied
    seen: Dict[UUID, List[int]] = {}
    for n, r, _, _ in candidates:
        seen.setdefault(r.m.id, []).append(n)
    by_row = {row.row: row for row in rows}
    live: List[Tuple[int, TableRow, Optional[OpeningBatchItem]]] = []
    for n, r, _, item in candidates:
        if len(seen[r.m.id]) > 1:
            dup = by_row[n]
            dup.status, dup.code = "error", "DUPLICATE"
            dup.message = "Ученик встречается в файле дважды (строки " + " и ".join(str(x) for x in seen[r.m.id]) + "): ни одна не применена"
        elif item is not None:
            live.append((n, r, item))
    # ---- 4. the plan of all the rows that say something: which of them change, which are errors
    items = [it for _, _, it in live]
    classified: Dict[str, Any] = {}
    if items:
        first_pass = svc._preview_with(org_id, items, max_items=MAX_FILE_ROWS)
        classified = {p.membership_id: p for p in first_pass.items}
    final: List[OpeningBatchItem] = []
    for n, r, it in live:
        res = classified[str(r.m.id)]
        row = by_row[n]
        row.kind = res.kind
        if res.status == "error":
            row.status, row.code, row.message = "error", res.code, res.message
        elif res.status == "noop":
            row.status = "noop"
        else:
            row.status = "ok"
            final.append(it)
    if len(final) > MAX_CHANGES:
        raise _err("TOO_MANY_CHANGES", f"В файле больше {MAX_CHANGES} изменений — загрузите файл по группам")
    # ---- 5. the preview of what will really be sent (the digest belongs to THIS list)
    if final:
        preview = svc.plan_preview(org_id, final)
    else:
        preview = _empty_preview(settings.accounting_start, len(items))
    mentioned = {r.membership_id for r in rows if r.membership_id}
    errors = sum(1 for r in rows if r.status == "error")
    file = OpeningsImportFile(
        rows_read=len(rows), matched=sum(1 for r in rows if r.membership_id), changed=len(final),
        unchanged=sum(1 for r in rows if r.status == "noop"), errors=errors,
        unknown=sum(1 for r in rows if r.code in ("UNKNOWN", "AMBIGUOUS")),
        missing_in_file=sum(1 for r in table if not r.left and str(r.m.id) not in mentioned))
    return OpeningsImportPreview(**preview.model_dump(), file=file, rows=rows, batch_items=final)


def _empty_preview(start: date, items: int) -> OpeningBatchPreview:
    import hashlib

    from .openings_schemas import OpeningBatchImpact, OpeningBatchSummary

    return OpeningBatchPreview(ok=True, digest=hashlib.sha256(b"").hexdigest(), accounting_start=start,
                               summary=OpeningBatchSummary(items=0, unchanged=0), impact=OpeningBatchImpact(), needs_reason=[], items=[])


class _RowError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code, self.message = code, message


def _row_error(code: str, message: str) -> _RowError:
    return _RowError(code, message)


def _single(found: List[TableRow], none: str, many: str) -> TableRow:
    if not found:
        raise _RowError("UNKNOWN", none)
    if len(found) > 1:
        raise _RowError("AMBIGUOUS", many)
    return found[0]


def _date_of(cell: Optional[_Cell], title: str) -> Optional[date]:
    if _empty(cell):
        if cell is not None and cell.formula:
            raise _RowError("FORMULA", f"{title}: в ячейке формула без значения — сохраните файл как значения")
        return None
    value = cell.value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return imp._parse_date(imp._cell(value))
    except ValueError:
        raise _RowError("BAD_DATE", f"{title}: «{imp._cell(value)[:30]}» — не дата") from None


def _int_of(cell: Optional[_Cell], title: str) -> Optional[int]:
    if _empty(cell):
        if cell is not None and cell.formula:
            raise _RowError("FORMULA", f"{title}: в ячейке формула без значения — сохраните файл как значения")
        return None
    if isinstance(cell.value, bool):
        raise _RowError("BAD_AMOUNT", f"{title}: «{cell.value}» — не число")
    try:
        value = imp._parse_money(imp._cell(cell.value))
    except ValueError:
        raise _RowError("BAD_AMOUNT", f"{title}: «{imp._cell(cell.value)[:30]}» — не целое число") from None
    if value is not None and abs(value) > MAX_MONEY * 10:
        raise _RowError("BAD_AMOUNT", f"{title}: слишком большое число")
    return value


def _item_of(rec: Dict[str, _Cell], match: TableRow, start: date) -> Optional[OpeningBatchItem]:
    """The batch item of a row, ``None`` when the row says nothing and the membership has no opening (no change).  «Без остатка (да)» = reviewed,
    nothing owed: «оплачено до» the day before the accounting start (a row that cannot have an opening - left, transfer, free - just clears)."""
    paid = _date_of(rec.get("paid"), "Оплачено до")
    amount = _int_of(rec.get("debt"), "Долг")
    debt_on = _date_of(rec.get("debt_on"), "Долг на дату")
    lessons = _int_of(rec.get("lessons"), "Осталось уроков")
    flag = _text(rec.get("none")).strip().casefold()
    if flag and flag not in YES:
        raise _RowError("ROW_CONFLICT", "«Без остатка»: напишите «да» или оставьте пусто")
    has_values = any(x is not None for x in (paid, amount, debt_on, lessons))
    if flag and has_values:
        raise _RowError("ROW_CONFLICT", "«Без остатка» вместе с «Оплачено до» / долгом: оставьте что-то одно")
    base = {"membership_id": str(match.m.id), "version": match.version}
    if flag:
        if match.left or not match.editable:
            return OpeningBatchItem(**base, clear=True)
        return OpeningBatchItem(**base, paid_until=start - timedelta(days=1))
    if not has_values and match.opening is not None:
        return OpeningBatchItem(**base, clear=True)
    if not has_values:
        return None
    return OpeningBatchItem(**base, paid_until=paid, debt_amount=amount, debt_on=debt_on, lessons_left=lessons)
