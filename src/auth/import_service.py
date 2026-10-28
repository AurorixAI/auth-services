"""
ALGO — Bulk import (xlsx).

A learning center that already has students/groups on paper can fill
one .xlsx workbook (2 sheets: Группы / Ученики) and load everything
at once instead of typing records one by one. Teachers are managed separately
via the UI — groups may reference an existing teacher by email.

Flow (all under require_org_admin — owner/admin):
    GET  /org/import/template  → this module generates the .xlsx skeleton.
    POST /org/import/preview   → parse + validate, return a dry-run report
                                 (nothing is written).
    POST /org/import/commit    → parse + validate again, then write EVERYTHING
                                 in a single transaction (all-or-nothing).

Key data-model facts this module respects (see src/models/auth.py):
  * Students are login-less rows — created directly.
  * Teachers are NOT imported here — add them via the org UI first. A group
    may optionally reference an existing org member by email.
  * There is no direct student↔teacher link — it flows through the group
    (student → group → group.teacher_id). So the workbook ties them via the
    group's name.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import zipfile
from io import BytesIO
from xml.etree import ElementTree
from typing import Optional

from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.auth import (
    Group,
    OrganizationMember,
    Student,
    StudentGroup,
    User,
)

from . import import_limits as limits
from .membership_periods import open_period
from .student_id_service import DisplayIdAllocator

logger = logging.getLogger(__name__)

# ── Sheet + column names (the template we dictate) ──────────────────────────
SHEET_GROUPS = "Группы"
SHEET_STUDENTS = "Ученики"
SHEET_INSTRUCTIONS = "Инструкция"

# Column headers per sheet. "*" marks required — stripped when matching.
COLS_GROUPS = ["Название группы*", "Класс (1-11)", "Почта учителя", "Макс. учеников"]
COLS_STUDENTS = ["Фамилия*", "Имя*", "Класс (1-11)*", "Телефон", "Группа*", "Комментарий",
                 "Оплачено до (дата)", "Долг, сум", "Долг на дату"]   # LOGIC-A (decision 1): the opening balance of the student in the group


# ════════════════════════════════════════════════════════════════════════════
# Response models (feature-local — kept out of the shared schemas.py)
# ════════════════════════════════════════════════════════════════════════════
class ImportIssue(BaseModel):
    sheet: str
    row: int          # 1-based Excel row (header = 1)
    field: Optional[str] = None
    level: str        # "error" | "warning"
    message: str


class ImportCounts(BaseModel):
    teachers_new: int = 0
    teachers_existing: int = 0
    groups_new: int = 0
    groups_existing: int = 0
    students_new: int = 0
    students_existing: int = 0
    openings: int = 0  # LOGIC-A: opening balances that the commit writes (the rows with «оплачено до» / «долг»)


class ImportPreviewResponse(BaseModel):
    ok: bool                       # True → no blocking errors, safe to commit
    counts: ImportCounts
    issues: list[ImportIssue]
    # Small samples for the preview UI (not the full data)
    teachers_preview: list[dict] = []
    groups_preview: list[dict] = []
    students_preview: list[dict] = []


class TeacherCredential(BaseModel):
    full_name: str
    email: str
    role: str
    temp_password: str


class ImportCommitResponse(BaseModel):
    ok: bool
    counts: ImportCounts
    issues: list[ImportIssue]
    # Shown ONCE — temp passwords for freshly created teacher accounts.
    credentials: list[TeacherCredential] = []


# ════════════════════════════════════════════════════════════════════════════
# Parsing
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class ParsedRow:
    row: int
    data: dict


@dataclass
class ParsedWorkbook:
    groups: list[ParsedRow] = field(default_factory=list)
    students: list[ParsedRow] = field(default_factory=list)
    fatal: Optional[str] = None  # set if the file itself is unreadable/wrong


def _norm_header(h) -> str:
    """Normalize a header cell for matching: strip, drop trailing '*', lower."""
    if h is None:
        return ""
    return str(h).replace("*", "").strip().lower()


def _cell(v) -> str:
    """Normalize a data cell to a trimmed string ('' for blank)."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


# Older templates called the column «Email учителя»: both headers are accepted (normalised header -> the normalised header of the current template)
HEADER_ALIASES = {"email учителя": "почта учителя", "e-mail учителя": "почта учителя"}
EXAMPLE_MARK = "пример — удалите"      # the grey example row of the template carries this text in its last column; the importer ignores that row
NOTE_HEADER = "Пометка"                  # the header of that last column (it holds nothing else and is never read as data)


def _is_example(cells) -> bool:
    """A row that carries the mark «пример — удалите» (any dash, any case) in any cell is the example of the template, never data."""
    for cell in cells:
        value = getattr(cell, "value", cell)
        if isinstance(value, str) and EXAMPLE_MARK in re.sub(r"\s*[-–—]\s*", " — ", value.casefold()):
            return True
    return False


def _read_sheet(ws, expected_cols: list[str]) -> list[ParsedRow]:
    """Map columns by header name (order-independent, tolerant of extras); the example row of the template is skipped.

    The sheet is streamed (read-only workbook): at most ``MAX_IMPORT_ROWS`` data rows are kept; a longer sheet is refused (422).
    ``ws.iter_rows`` pads skipped rows, so the position in the iteration is the Excel row (the header is row 1).
    """
    if hasattr(ws, "reset_dimensions"):
        ws.reset_dimensions()  # a read-only sheet trusts the <dimension> tag of the file; a wrong one silently drops rows and columns
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header_cells = next(rows_iter)
    except StopIteration:
        return []

    # header index → normalized expected key
    wanted = {_norm_header(c): c for c in expected_cols}
    col_map: dict[int, str] = {}
    for idx, cell in enumerate(header_cells):
        key = _norm_header(cell)
        key = HEADER_ALIASES.get(key, key)
        if key in wanted:
            col_map[idx] = key

    out: list[ParsedRow] = []
    for excel_row, cells in enumerate(rows_iter, start=2):
        if excel_row > limits.MAX_SCANNED_ROWS:
            raise limits.ImportFileRejected(limits.TOO_MANY_ROWS.format(sheet=ws.title))
        if _is_example(cells):
            continue  # the grey example row of the template
        values = {}
        any_value = False
        for idx, key in col_map.items():
            val = _cell(cells[idx] if idx < len(cells) else None)
            if val:
                any_value = True
            values[key] = val
        if not any_value:
            continue  # skip fully-blank rows
        if len(out) >= limits.MAX_IMPORT_ROWS:
            raise limits.ImportFileRejected(limits.TOO_MANY_ROWS.format(sheet=ws.title))
        out.append(ParsedRow(row=excel_row, data=values))
    return out


def parse_workbook(data: bytes) -> ParsedWorkbook:
    """Read the two sheets of the template. A file that cannot be an import at all (not an xlsx, a zip bomb, too many sheets / rows)
    raises ``ImportFileRejected`` (the route answers 422); a readable file without the sheets comes back with ``fatal`` set."""
    from openpyxl import load_workbook

    limits.check_zip(data)
    result = ParsedWorkbook()
    try:
        wb = load_workbook(BytesIO(data), data_only=True, read_only=True)
    except Exception:
        raise limits.ImportFileRejected(limits.UNREADABLE) from None
    try:
        if len(wb.sheetnames) > limits.MAX_IMPORT_SHEETS:
            raise limits.ImportFileRejected(limits.TOO_MANY_SHEETS)
        names = set(wb.sheetnames)
        missing = [s for s in (SHEET_GROUPS, SHEET_STUDENTS) if s not in names]
        if missing:
            result.fatal = (
                "В файле нет обязательных вкладок: "
                + ", ".join(missing)
                + ". Скачайте актуальный шаблон."
            )
            return result

        result.groups = _read_sheet(wb[SHEET_GROUPS], COLS_GROUPS)
        result.students = _read_sheet(wb[SHEET_STUDENTS], COLS_STUDENTS)
        return result
    except limits.ImportFileRejected:
        raise
    except (zipfile.BadZipFile, KeyError, ValueError, OSError, EOFError, ElementTree.ParseError):
        raise limits.ImportFileRejected(limits.UNREADABLE) from None
    finally:
        wb.close()


# ════════════════════════════════════════════════════════════════════════════
# Validation → plan
# ════════════════════════════════════════════════════════════════════════════
# Normalized-header keys (must match _norm_header of the COLS_* entries)
K_LAST = "фамилия"
K_FIRST = "имя"
K_GROUP_NAME = "название группы"
K_CLASS = "класс (1-11)"
K_TEACHER_EMAIL = "почта учителя"
K_MAX_STUDENTS = "макс. учеников"
K_STUDENT_CLASS = "класс (1-11)"
K_PHONE = "телефон"
K_GROUP = "группа"
K_COMMENT = "комментарий"
K_PAID_UNTIL = "оплачено до (дата)"
K_DEBT = "долг, сум"
K_DEBT_ON = "долг на дату"


@dataclass
class GroupPlan:
    row: int
    name: str
    class_level: Optional[int]
    teacher_email: str
    max_students: int
    action: str            # create | existing
    existing_group_id: Optional[str] = None


@dataclass
class StudentPlan:
    row: int
    last_name: str
    first_name: str
    class_level: int
    phone: Optional[str]
    group_name: str
    comment: str
    action: str            # create | existing
    existing_student_id: Optional[str] = None
    # LOGIC-A: the opening balance asked for in the row (already checked; ``None`` fields = not asked / skipped with a warning)
    paid_until: Optional[date] = None
    debt_amount: Optional[int] = None
    debt_on: Optional[date] = None


@dataclass
class ImportPlan:
    groups: list[GroupPlan] = field(default_factory=list)
    students: list[StudentPlan] = field(default_factory=list)
    issues: list[ImportIssue] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(i.level == "error" for i in self.issues)

    def counts(self) -> ImportCounts:
        c = ImportCounts()
        for g in self.groups:
            if g.action == "create":
                c.groups_new += 1
            else:
                c.groups_existing += 1
        for s in self.students:
            if s.action == "create":
                c.students_new += 1
                if s.paid_until or s.debt_amount:
                    c.openings += 1
            else:
                c.students_existing += 1
        return c


def _parse_class_level(raw: str) -> Optional[int]:
    if not raw:
        return None
    try:
        return int(float(raw))
    except (ValueError, OverflowError):          # m7: «inf» / «1e999» are not a class
        return None


_DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d.%m.%y", "%d-%m-%Y")


def _parse_date(raw: str) -> Optional[date]:
    """An Excel date cell (read as text by ``_cell``): ``2026-10-31`` / ``2026-10-31 00:00:00`` / ``31.10.2026`` / ``31/10/2026``."""
    raw = (raw or "").strip()
    if not raw:
        return None
    raw = raw.split(" ")[0].split("T")[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(raw)


def _parse_money(raw: str) -> Optional[int]:
    text = re.sub(r"[\s\u00a0\u202f]", "", (raw or "")).replace(",", ".")
    if not text:
        return None
    try:
        value = float(text)
        if not math.isfinite(value) or value != int(value):       # m7: inf / nan / 1e999 are a row error (never an OverflowError -> 500)
            raise ValueError(raw)
        return int(value)
    except (ValueError, OverflowError):
        raise ValueError(raw) from None


def parse_opening(d: dict, row: int, *, money: bool, accounting_start: date, enrol_day: date, err, warn):
    """LOGIC-A: the opening-balance columns G-I of a student row -> ``(ok, paid_until, debt_amount, debt_on)``.  ``ok=False`` = a blocking error
    (already reported through ``err``).  Shared by the legacy plan and by LOGIC-B's ``import_ops.build_plan``."""
    paid_until = debt_amount = debt_on = None
    if not any(d.get(k) for k in (K_PAID_UNTIL, K_DEBT, K_DEBT_ON)):
        return True, None, None, None
    try:
        raw_paid, raw_debt, raw_on = _parse_date(d.get(K_PAID_UNTIL, "")), _parse_money(d.get(K_DEBT, "")), _parse_date(d.get(K_DEBT_ON, ""))
    except ValueError as bad:
        err(SHEET_STUDENTS, row, "Остаток", f"Не удалось прочитать дату или сумму остатка: '{bad}'")
        return False, None, None, None
    if raw_debt is not None and not 1 <= raw_debt <= 10_000_000_000:
        err(SHEET_STUDENTS, row, "Долг, сум", "Долг: от 1 до 10 000 000 000 сум")
        return False, None, None, None
    if raw_on is not None and raw_debt is None:
        warn(SHEET_STUDENTS, row, "Долг на дату", "Указана дата долга без суммы — дата пропущена.")
    if not money:
        warn(SHEET_STUDENTS, row, "Остаток", "Нет прав вносить остатки — ученик импортирован без остатка.")
        return True, None, None, None
    floor = max(enrol_day, accounting_start - timedelta(days=1))   # S0: the same L as ``OpeningService.floor_of``
    if raw_paid is not None and raw_paid < floor:
        warn(SHEET_STUDENTS, row, "Оплачено до (дата)", "«Оплачено до» раньше даты зачисления или начала учёта — остаток пропущен.")
        raw_paid = None
    if raw_paid is not None and raw_paid > enrol_day + timedelta(days=366):
        warn(SHEET_STUDENTS, row, "Оплачено до (дата)", "«Оплачено до» дальше чем через год — остаток пропущен.")
        raw_paid = None
    if raw_debt is not None:
        day = raw_on or (max(enrol_day, accounting_start) - timedelta(days=1))   # default: the day before the enrolment / the accounting start
        if day > floor or day < date(2020, 1, 1):
            warn(SHEET_STUDENTS, row, "Долг на дату", "Дата долга должна быть не позже дня зачисления (или дня перед началом учёта) и не раньше 2020 года — долг пропущен.")
        else:
            debt_amount, debt_on = raw_debt, day
    return True, raw_paid, debt_amount, debt_on


def build_plan(org_id: str, parsed: ParsedWorkbook, db: Session, *, money: bool = True) -> ImportPlan:
    """``money``: the caller may enter money (owner / an admin with the switch): without it the opening-balance columns are ignored with a
    warning and the rows are imported without them."""
    plan = ImportPlan()
    from .ledger_service import accounting_start_of
    from .payments_service import PaymentService

    accounting_start = PaymentService(db)._loader(org_id).settings.accounting_start
    enrol_day = _today()

    def err(sheet, row, field_, msg):
        plan.issues.append(ImportIssue(sheet=sheet, row=row, field=field_, level="error", message=msg))

    def warn(sheet, row, field_, msg):
        plan.issues.append(ImportIssue(sheet=sheet, row=row, field=field_, level="warning", message=msg))

    # Existing org-member emails (groups may reference a teacher already in the system).
    org_member_emails: set[str] = set()
    for u, in (
        db.query(User.email)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .filter(OrganizationMember.org_id == org_id)
        .all()
    ):
        if u:
            org_member_emails.add(u.lower())

    # ── Groups ────────────────────────────────────────────────────────────────
    file_group_names: set[str] = set()
    seen_group_names: set[str] = set()
    for r in parsed.groups:
        d = r.data
        name = d.get(K_GROUP_NAME, "")
        if not name:
            err(SHEET_GROUPS, r.row, "Название", "Название группы обязательно.")
            continue

        name_key = name.lower()
        if name_key in seen_group_names:
            warn(SHEET_GROUPS, r.row, "Название", f"Группа '{name}' повторяется — строка пропущена.")
            continue
        seen_group_names.add(name_key)
        file_group_names.add(name_key)

        class_level = _parse_class_level(d.get(K_CLASS, ""))
        if d.get(K_CLASS) and class_level is None:
            err(SHEET_GROUPS, r.row, "Класс", f"Класс должен быть числом 1–11: '{d.get(K_CLASS)}'")
            continue
        if class_level is not None and not (1 <= class_level <= 11):
            err(SHEET_GROUPS, r.row, "Класс", f"Класс вне диапазона 1–11: {class_level}")
            continue

        teacher_email = d.get(K_TEACHER_EMAIL, "").lower()
        if teacher_email:
            if teacher_email not in org_member_emails:
                err(SHEET_GROUPS, r.row, "Почта учителя",
                    f"Учитель {teacher_email} не найден в системе. Добавьте его через интерфейс.")
                continue

        max_students = _parse_class_level(d.get(K_MAX_STUDENTS, "")) or 30

        existing = (
            db.query(Group)
            .filter(Group.org_id == org_id, func.lower(Group.name) == name_key)
            .first()
        )
        if existing:
            plan.groups.append(GroupPlan(
                row=r.row, name=name, class_level=class_level,
                teacher_email=teacher_email, max_students=max_students,
                action="existing", existing_group_id=str(existing.id),
            ))
        else:
            plan.groups.append(GroupPlan(
                row=r.row, name=name, class_level=class_level,
                teacher_email=teacher_email, max_students=max_students,
                action="create",
            ))

    # Names of groups that will exist after import (new + existing referenced)
    known_group_names = set(file_group_names)

    # ── Students ──────────────────────────────────────────────────────────────
    seen_students: set[tuple] = set()
    for r in parsed.students:
        d = r.data
        last = d.get(K_LAST, "")
        first = d.get(K_FIRST, "")
        group_name = ""
        phone = None
        if not last or not first:
            err(SHEET_STUDENTS, r.row, "Фамилия/Имя", "Фамилия и имя обязательны.")
            continue
        class_level = _parse_class_level(d.get(K_STUDENT_CLASS, ""))
        if class_level is None:
            err(SHEET_STUDENTS, r.row, "Класс", "Класс обязателен и должен быть числом 1–11.")
            continue
        if not (1 <= class_level <= 11):
            err(SHEET_STUDENTS, r.row, "Класс", f"Класс вне диапазона 1–11: {class_level}")
            continue

        phone = d.get(K_PHONE, "") or None
        group_name = d.get(K_GROUP, "")
        if not group_name:
            err(SHEET_STUDENTS, r.row, "Группа", "Группа обязательна")
            continue
        if group_name:
            gk = group_name.lower()
            group_exists_db = (
                db.query(Group)
                .filter(Group.org_id == org_id, func.lower(Group.name) == gk)
                .first()
                is not None
            )
            if gk not in known_group_names and not group_exists_db:
                err(SHEET_STUDENTS, r.row, "Группа",
                    f"Группа '{group_name}' не найдена во вкладке «Группы» и не существует.")
                continue

        ok, paid_until, debt_amount, debt_on = parse_opening(
            d, r.row, money=money, accounting_start=accounting_start, enrol_day=enrol_day, err=err, warn=warn)
        if not ok:
            continue

        dedup_key = (last.lower(), first.lower(), (phone or "").strip())
        if dedup_key in seen_students:
            warn(SHEET_STUDENTS, r.row, "ФИО", f"Ученик {last} {first} повторяется — строка пропущена.")
            continue
        seen_students.add(dedup_key)

        existing_q = db.query(Student).filter(
            Student.org_id == org_id,
            func.lower(Student.last_name) == last.lower(),
            func.lower(Student.first_name) == first.lower(),
        )
        if phone:
            existing_q = existing_q.filter(Student.student_phone == phone)
        else:
            existing_q = existing_q.filter(Student.student_phone.is_(None))
        existing = existing_q.first()

        if existing:
            if paid_until or debt_amount:
                warn(SHEET_STUDENTS, r.row, "Остаток", "Ученик уже есть в системе — остаток не внесён.")
            plan.students.append(StudentPlan(
                row=r.row, last_name=last, first_name=first, class_level=class_level,
                phone=phone, group_name=group_name, comment=d.get(K_COMMENT, ""),
                action="existing", existing_student_id=str(existing.id),
            ))
        else:
            plan.students.append(StudentPlan(
                row=r.row, last_name=last, first_name=first, class_level=class_level,
                phone=phone, group_name=group_name, comment=d.get(K_COMMENT, ""),
                action="create", paid_until=paid_until, debt_amount=debt_amount, debt_on=debt_on,
            ))

    return plan


# ════════════════════════════════════════════════════════════════════════════
# Public API: preview + commit
# ════════════════════════════════════════════════════════════════════════════
def _today() -> date:
    from . import payment_status as ps

    return ps.business_today()


def preview(org_id: str, data: bytes, db: Session, *, money: bool = True) -> ImportPreviewResponse:
    parsed = parse_workbook(data)
    if parsed.fatal:
        return ImportPreviewResponse(
            ok=False,
            counts=ImportCounts(),
            issues=[ImportIssue(sheet="-", row=0, level="error", message=parsed.fatal)],
        )

    plan = build_plan(org_id, parsed, db, money=money)
    return ImportPreviewResponse(
        ok=not plan.has_errors,
        counts=plan.counts(),
        issues=plan.issues,
        groups_preview=[
            {"name": g.name, "class_level": g.class_level,
             "teacher_email": g.teacher_email, "action": g.action}
            for g in plan.groups[:50]
        ],
        students_preview=[
            {"name": f"{s.last_name} {s.first_name}", "class_level": s.class_level,
             "phone": s.phone, "group": s.group_name, "action": s.action}
            for s in plan.students[:50]
        ],
    )


TEMPLATE_FILE_NAME = "Шаблон_импорта.xlsx"
DATE_HINT = "Формат даты: ДД.ММ.ГГГГ, например 31.10.2026"
PHONE_HINT = "Телефон с кодом страны: +998901234567"
INSTRUCTION_PARAGRAPHS = (
    "Заполните две вкладки: «Группы» и «Ученики». Колонки со звёздочкой (*) обязательные. Серая строка в каждой вкладке — пример заполнения: её можно "
    "удалить, а если оставить, ALGO её пропустит (в последней колонке у неё написано «пример — удалите»).",
    "1) Группы — «Название группы» обязательно. «Почта учителя» необязательна; если указана, учитель должен уже существовать в системе (добавьте его "
    "через интерфейс). Класс — число от 1 до 11. Старый шаблон с колонкой «Email учителя» тоже принимается.",
    "2) Ученики — Фамилия, Имя и Класс (1–11) обязательны. Телефон — по желанию, с кодом страны: +998901234567. «Группа» — название из вкладки «Группы» "
    "(ученик попадёт в эту группу).",
    "«Оплачено до (дата)», «Долг, сум», «Долг на дату» — необязательно: что было ДО запуска в ALGO. Даты пишите так: ДД.ММ.ГГГГ, например 31.10.2026. "
    "«Оплачено до» — ученик уже заплатил до этой даты (эти дни не считаются и не превращаются в аванс). «Долг» — сколько он был должен; дата долга по "
    "умолчанию — день перед зачислением. Остатки вносят владелец и администратор с доступом к оплатам.",
    "Повторная загрузка безопасна: группы сверяются по названию, ученики — по ФИО и телефону; существующие не дублируются.",
)


def build_template_xlsx() -> bytes:
    """The template of the import in the house style (``xlsx_style``): «Инструкция» FIRST as ordinary text, then «Группы» and «Ученики», each with the header on
    row 1 (the importer reads the headers by their text), one grey example row marked «пример — удалите» (the importer ignores it) and format hints for the dates
    and the phones (a comment on the header, a prompt on the cell, the number format of the column)."""
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.utils import get_column_letter
    from openpyxl.workbook.defined_name import DefinedName
    from openpyxl.worksheet.datavalidation import DataValidation

    from . import xlsx_style as xs

    wb = Workbook()

    # ── Инструкция ───────────────────────────────────────────────────────────
    ws_info = wb.active
    ws_info.title = SHEET_INSTRUCTIONS
    ws_info.column_dimensions["A"].width = 110
    xs.put(ws_info, 1, 1, "Импорт данных в ALGO — как заполнять", font=xs.FONT_TITLE, alignment=xs.ALIGN_LEFT)
    ws_info.row_dimensions[1].height = 21
    for i, text in enumerate(INSTRUCTION_PARAGRAPHS, start=3):
        xs.put(ws_info, i, 1, text, font=xs.FONT_BODY, alignment=xs.ALIGN_LEFT_TOP)
        lines = xs.lines_needed(text, 110)
        if lines > 1:
            ws_info.row_dimensions[i].height = xs.LINE_HEIGHT * lines
    xs.print_setup(ws_info, header=None)

    example_date = date.today().replace(day=1) - timedelta(days=1)          # the last day of the previous month: a date that is always «before»

    def data_sheet(title: str, cols: list[str], example: list, date_cols: tuple[str, ...] = (), phone_col: Optional[str] = None):
        ws = wb.create_sheet(title)
        headers = [*cols, NOTE_HEADER]
        xs.header_row(ws, 1, headers)
        xs.example_row(ws, 2, [*example, EXAMPLE_MARK])
        for i, name in enumerate(headers, start=1):
            plain = name.replace("*", "").strip()
            letter = get_column_letter(i)
            if plain in date_cols:
                ws.column_dimensions[letter].number_format = xs.DATE
                ws.cell(row=2, column=i).number_format = xs.DATE
                ws.cell(row=1, column=i).comment = Comment(DATE_HINT, "ALGO", width=260, height=60)
            if plain == phone_col:
                ws.column_dimensions[letter].number_format = "@"
                ws.cell(row=2, column=i).number_format = "@"
                ws.cell(row=1, column=i).comment = Comment(PHONE_HINT, "ALGO", width=260, height=60)
        ws.freeze_panes = "A2"
        return ws, headers

    def prompt(ws, headers: list[str], names: tuple[str, ...], dv: "DataValidation") -> None:
        ws.add_data_validation(dv)
        for i, name in enumerate(headers, start=1):
            if name.replace("*", "").strip() in names:
                letter = get_column_letter(i)
                dv.add(f"{letter}3:{letter}1000")

    # ── Группы ───────────────────────────────────────────────────────────────
    ws_g, head_g = data_sheet(SHEET_GROUPS, COLS_GROUPS, ["Алгебра-9А", 9, "a.karimov@example.com", 25])
    dv_class_g = DataValidation(type="whole", operator="between", formula1=1, formula2=11, allow_blank=True)
    ws_g.add_data_validation(dv_class_g)
    dv_class_g.add("B3:B1000")

    # ── Ученики ──────────────────────────────────────────────────────────────
    ws_s, head_s = data_sheet(
        SHEET_STUDENTS, COLS_STUDENTS,
        ["Усмонова", "Малика", 9, "+998901234567", "Алгебра-9А", None, example_date, None, None],
        date_cols=("Оплачено до (дата)", "Долг на дату"), phone_col="Телефон")
    dv_class_s = DataValidation(type="whole", operator="between", formula1=1, formula2=11, allow_blank=False)
    ws_s.add_data_validation(dv_class_s)
    dv_class_s.add("C3:C1000")
    prompt(ws_s, head_s, ("Оплачено до (дата)", "Долг на дату"), DataValidation(
        type="date", operator="greaterThanOrEqual", formula1="DATE(2000,1,1)", allow_blank=True, showInputMessage=True, promptTitle="Дата",
        prompt="ДД.ММ.ГГГГ, например 31.10.2026", showErrorMessage=True, errorTitle="Дата", error="Нужна дата в формате ДД.ММ.ГГГГ"))
    prompt(ws_s, head_s, ("Телефон",), DataValidation(
        type="textLength", operator="lessThanOrEqual", formula1=20, allow_blank=True, showInputMessage=True, promptTitle="Телефон",
        prompt="+998901234567 (с кодом страны)"))

    # Group-name dropdown in Ученики, sourced from Группы!A (via defined name); the example group of the first row is part of the list - harmless.
    wb.defined_names.add(
        DefinedName("group_names", attr_text=f"'{SHEET_GROUPS}'!$A$2:$A$1000")
    )
    dv_group = DataValidation(type="list", formula1="group_names", allow_blank=True)
    ws_s.add_data_validation(dv_group)
    dv_group.add("E3:E1000")

    for ws in (ws_g, ws_s):
        xs.autosize(ws, header_row_number=1, last_col=ws.max_column, min_width=14)
        ws.column_dimensions[get_column_letter(ws.max_column)].width = max(ws.column_dimensions[get_column_letter(ws.max_column)].width or 0, len(EXAMPLE_MARK) + 2)
        xs.print_setup(ws, header=1)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
