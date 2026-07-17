"""
ALGO — Bulk import (xlsx).

A learning center that already has students/teachers/groups on paper can fill
one .xlsx workbook (3 sheets: Учителя / Группы / Ученики) and load everything
at once instead of typing records one by one.

Flow (all under require_org_admin — owner/admin):
    GET  /org/import/template  → this module generates the .xlsx skeleton.
    POST /org/import/preview   → parse + validate, return a dry-run report
                                 (nothing is written).
    POST /org/import/commit    → parse + validate again, then write EVERYTHING
                                 in a single transaction (all-or-nothing).

Key data-model facts this module respects (see src/models/auth.py):
  * Students are login-less rows — created directly.
  * Teachers are Users with a globally-unique email + password. Import creates
    them with a generated TEMP password and must_change_password=True; the
    owner distributes the credentials and the teacher resets on first login.
  * There is no direct student↔teacher link — it flows through the group
    (student → group → group.teacher_id). So the workbook ties them via the
    group's name.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from io import BytesIO
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

from .service import generate_code, hash_password

# ── Sheet + column names (the template we dictate) ──────────────────────────
SHEET_TEACHERS = "Учителя"
SHEET_GROUPS = "Группы"
SHEET_STUDENTS = "Ученики"
SHEET_INSTRUCTIONS = "Инструкция"

# Column headers per sheet. "*" marks required — stripped when matching.
COLS_TEACHERS = ["Фамилия*", "Имя*", "Отчество", "Email*", "Предметы", "Роль (teacher/admin)"]
COLS_GROUPS = ["Название группы*", "Класс (1-11)", "Email учителя", "Макс. учеников"]
COLS_STUDENTS = ["Фамилия*", "Имя*", "Класс (1-11)*", "Телефон", "Группа*", "Комментарий"]

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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
    teachers: list[ParsedRow] = field(default_factory=list)
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


def _read_sheet(ws, expected_cols: list[str]) -> list[ParsedRow]:
    """Map columns by header name (order-independent, tolerant of extras)."""
    rows_iter = ws.iter_rows(values_only=False)
    try:
        header_cells = next(rows_iter)
    except StopIteration:
        return []

    # header index → normalized expected key
    wanted = {_norm_header(c): c for c in expected_cols}
    col_map: dict[int, str] = {}
    for idx, cell in enumerate(header_cells):
        key = _norm_header(cell.value)
        if key in wanted:
            col_map[idx] = key

    out: list[ParsedRow] = []
    for cells in rows_iter:
        values = {}
        any_value = False
        for idx, key in col_map.items():
            val = _cell(cells[idx].value if idx < len(cells) else None)
            if val:
                any_value = True
            values[key] = val
        if not any_value:
            continue  # skip fully-blank rows
        excel_row = cells[0].row if cells else 0
        out.append(ParsedRow(row=excel_row, data=values))
    return out


def parse_workbook(data: bytes) -> ParsedWorkbook:
    from openpyxl import load_workbook

    result = ParsedWorkbook()
    try:
        wb = load_workbook(BytesIO(data), data_only=True)
    except Exception:
        result.fatal = "Не удалось прочитать файл. Загрузите .xlsx по нашему шаблону."
        return result

    names = set(wb.sheetnames)
    missing = [s for s in (SHEET_TEACHERS, SHEET_GROUPS, SHEET_STUDENTS) if s not in names]
    if missing:
        result.fatal = (
            "В файле нет обязательных вкладок: "
            + ", ".join(missing)
            + ". Скачайте актуальный шаблон."
        )
        return result

    result.teachers = _read_sheet(wb[SHEET_TEACHERS], COLS_TEACHERS)
    result.groups = _read_sheet(wb[SHEET_GROUPS], COLS_GROUPS)
    result.students = _read_sheet(wb[SHEET_STUDENTS], COLS_STUDENTS)
    return result


# ════════════════════════════════════════════════════════════════════════════
# Validation → plan
# ════════════════════════════════════════════════════════════════════════════
# Normalized-header keys (must match _norm_header of the COLS_* entries)
K_LAST = "фамилия"
K_FIRST = "имя"
K_PATRONYMIC = "отчество"
K_EMAIL = "email"
K_SUBJECTS = "предметы"
K_ROLE = "роль (teacher/admin)"
K_GROUP_NAME = "название группы"
K_CLASS = "класс (1-11)"
K_TEACHER_EMAIL = "email учителя"
K_MAX_STUDENTS = "макс. учеников"
K_STUDENT_CLASS = "класс (1-11)"
K_PHONE = "телефон"
K_GROUP = "группа"
K_COMMENT = "комментарий"


@dataclass
class TeacherPlan:
    row: int
    email: str
    last_name: str
    first_name: str
    patronymic: str
    subjects: list[str]
    org_role: str          # teacher | admin
    action: str            # create | existing
    existing_user_id: Optional[str] = None


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


@dataclass
class ImportPlan:
    teachers: list[TeacherPlan] = field(default_factory=list)
    groups: list[GroupPlan] = field(default_factory=list)
    students: list[StudentPlan] = field(default_factory=list)
    issues: list[ImportIssue] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(i.level == "error" for i in self.issues)

    def counts(self) -> ImportCounts:
        c = ImportCounts()
        for t in self.teachers:
            if t.action == "create":
                c.teachers_new += 1
            else:
                c.teachers_existing += 1
        for g in self.groups:
            if g.action == "create":
                c.groups_new += 1
            else:
                c.groups_existing += 1
        for s in self.students:
            if s.action == "create":
                c.students_new += 1
            else:
                c.students_existing += 1
        return c


def _parse_class_level(raw: str) -> Optional[int]:
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def build_plan(org_id: str, parsed: ParsedWorkbook, db: Session) -> ImportPlan:
    plan = ImportPlan()

    def err(sheet, row, field_, msg):
        plan.issues.append(ImportIssue(sheet=sheet, row=row, field=field_, level="error", message=msg))

    def warn(sheet, row, field_, msg):
        plan.issues.append(ImportIssue(sheet=sheet, row=row, field=field_, level="warning", message=msg))

    # ── Teachers ────────────────────────────────────────────────────────────
    # email(lower) → org_role planned in this file (for group teacher resolution)
    file_teacher_emails: dict[str, str] = {}
    seen_emails: set[str] = set()
    for r in parsed.teachers:
        d = r.data
        last = d.get(K_LAST, "")
        first = d.get(K_FIRST, "")
        email = d.get(K_EMAIL, "").lower()
        role = (d.get(K_ROLE, "") or "teacher").lower()
        subjects = [s.strip() for s in d.get(K_SUBJECTS, "").split(",") if s.strip()]

        if not last or not first:
            err(SHEET_TEACHERS, r.row, "Фамилия/Имя", "Фамилия и имя обязательны.")
            continue
        if not email:
            err(SHEET_TEACHERS, r.row, "Email", "Email обязателен — из него создаётся аккаунт учителя.")
            continue
        if not _EMAIL_RE.match(email):
            err(SHEET_TEACHERS, r.row, "Email", f"Некорректный email: {email}")
            continue
        if role not in ("teacher", "admin"):
            warn(SHEET_TEACHERS, r.row, "Роль", f"Неизвестная роль '{role}', будет teacher.")
            role = "teacher"
        if email in seen_emails:
            warn(SHEET_TEACHERS, r.row, "Email", f"Email {email} повторяется — строка пропущена.")
            continue
        seen_emails.add(email)

        existing = db.query(User).filter(func.lower(User.email) == email).first()
        if existing:
            is_member = (
                db.query(OrganizationMember)
                .filter(
                    OrganizationMember.org_id == org_id,
                    OrganizationMember.user_id == existing.id,
                )
                .first()
                is not None
            )
            if not is_member:
                err(SHEET_TEACHERS, r.row, "Email",
                    f"Email {email} уже занят другим аккаунтом вне вашего центра.")
                continue
            plan.teachers.append(TeacherPlan(
                row=r.row, email=email, last_name=last, first_name=first,
                patronymic=d.get(K_PATRONYMIC, ""), subjects=subjects,
                org_role=role, action="existing", existing_user_id=str(existing.id),
            ))
        else:
            plan.teachers.append(TeacherPlan(
                row=r.row, email=email, last_name=last, first_name=first,
                patronymic=d.get(K_PATRONYMIC, ""), subjects=subjects,
                org_role=role, action="create",
            ))
        file_teacher_emails[email] = role

    # Existing org-member emails (so a group can reference a teacher already
    # in the system, not necessarily listed in this file's Учителя sheet).
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
            if teacher_email not in file_teacher_emails and teacher_email not in org_member_emails:
                err(SHEET_GROUPS, r.row, "Email учителя",
                    f"Учитель {teacher_email} не найден ни во вкладке «Учителя», ни в системе.")
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
            plan.students.append(StudentPlan(
                row=r.row, last_name=last, first_name=first, class_level=class_level,
                phone=phone, group_name=group_name, comment=d.get(K_COMMENT, ""),
                action="existing", existing_student_id=str(existing.id),
            ))
        else:
            plan.students.append(StudentPlan(
                row=r.row, last_name=last, first_name=first, class_level=class_level,
                phone=phone, group_name=group_name, comment=d.get(K_COMMENT, ""),
                action="create",
            ))

    return plan


# ════════════════════════════════════════════════════════════════════════════
# Public API: preview + commit
# ════════════════════════════════════════════════════════════════════════════
def preview(org_id: str, data: bytes, db: Session) -> ImportPreviewResponse:
    parsed = parse_workbook(data)
    if parsed.fatal:
        return ImportPreviewResponse(
            ok=False,
            counts=ImportCounts(),
            issues=[ImportIssue(sheet="-", row=0, level="error", message=parsed.fatal)],
        )

    plan = build_plan(org_id, parsed, db)
    return ImportPreviewResponse(
        ok=not plan.has_errors,
        counts=plan.counts(),
        issues=plan.issues,
        teachers_preview=[
            {"name": f"{t.last_name} {t.first_name}", "email": t.email,
             "role": t.org_role, "action": t.action}
            for t in plan.teachers[:50]
        ],
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


def commit(org_id: str, creator_id: str, data: bytes, db: Session) -> ImportCommitResponse:
    parsed = parse_workbook(data)
    if parsed.fatal:
        return ImportCommitResponse(
            ok=False,
            counts=ImportCounts(),
            issues=[ImportIssue(sheet="-", row=0, level="error", message=parsed.fatal)],
        )

    plan = build_plan(org_id, parsed, db)
    if plan.has_errors:
        # Never write a partially-valid file — return the errors, change nothing.
        return ImportCommitResponse(ok=False, counts=plan.counts(), issues=plan.issues)

    credentials: list[TeacherCredential] = []
    email_to_user_id: dict[str, str] = {}
    group_name_to_id: dict[str, str] = {}

    try:
        # 1) Teachers ---------------------------------------------------------
        for t in plan.teachers:
            if t.action == "existing":
                email_to_user_id[t.email] = t.existing_user_id
                continue
            temp_password = generate_code(10, digits_only=False, exclude_ambiguous=True)
            user = User(
                email=t.email,
                password_hash=hash_password(temp_password),
                first_name=t.first_name,
                last_name=t.last_name,
                patronymic=t.patronymic or None,
                role=t.org_role,
                subjects=t.subjects,
                default_org_id=org_id,
                is_active=True,
                is_verified=True,
                must_change_password=True,
            )
            db.add(user)
            db.flush()
            db.add(OrganizationMember(
                org_id=org_id, user_id=user.id, org_role=t.org_role, invited_by=creator_id,
            ))
            email_to_user_id[t.email] = str(user.id)
            credentials.append(TeacherCredential(
                full_name=f"{t.last_name} {t.first_name}".strip(),
                email=t.email, role=t.org_role, temp_password=temp_password,
            ))

        # Resolve org-member emails that groups may reference but weren't newly
        # created (already-existing teachers in this org).
        def resolve_teacher_id(email: str) -> Optional[str]:
            if not email:
                return None
            if email in email_to_user_id:
                return email_to_user_id[email]
            u = (
                db.query(User)
                .join(OrganizationMember, OrganizationMember.user_id == User.id)
                .filter(OrganizationMember.org_id == org_id, func.lower(User.email) == email)
                .first()
            )
            return str(u.id) if u else None

        # 2) Groups -----------------------------------------------------------
        for g in plan.groups:
            if g.action == "existing":
                group_name_to_id[g.name.lower()] = g.existing_group_id
                continue
            group = Group(
                org_id=org_id,
                name=g.name,
                class_level=g.class_level,
                teacher_id=resolve_teacher_id(g.teacher_email),
                max_students=g.max_students,
            )
            db.add(group)
            db.flush()
            group_name_to_id[g.name.lower()] = str(group.id)

        def resolve_group_id(name: str) -> Optional[str]:
            if not name:
                return None
            key = name.lower()
            if key in group_name_to_id:
                return group_name_to_id[key]
            grp = (
                db.query(Group)
                .filter(Group.org_id == org_id, func.lower(Group.name) == key)
                .first()
            )
            return str(grp.id) if grp else None

        # 3) Students ---------------------------------------------------------
        for s in plan.students:
            if s.action == "existing":
                continue
            group_id = resolve_group_id(s.group_name)
            student = Student(
                org_id=org_id,
                first_name=s.first_name,
                last_name=s.last_name,
                class_level=s.class_level,
                student_phone=s.phone,
                comment=s.comment or None,
                group_id=group_id,
                status="new",
                created_by=creator_id,
            )
            db.add(student)
            db.flush()
            if group_id:
                db.add(StudentGroup(student_id=student.id, group_id=group_id))

        db.commit()
    except Exception as exc:  # noqa: BLE001 — surface as a single import failure
        db.rollback()
        return ImportCommitResponse(
            ok=False,
            counts=ImportCounts(),
            issues=[ImportIssue(sheet="-", row=0, level="error",
                                message=f"Импорт не выполнен (откат): {exc}")],
        )

    return ImportCommitResponse(
        ok=True, counts=plan.counts(), issues=plan.issues, credentials=credentials,
    )


# ════════════════════════════════════════════════════════════════════════════
# Template generation
# ════════════════════════════════════════════════════════════════════════════
def build_template_xlsx() -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.workbook.defined_name import DefinedName
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="2F6FED")
    req_note = Font(italic=True, size=9, color="8A8A8A")

    def style_headers(ws, cols):
        for i, title in enumerate(cols, start=1):
            c = ws.cell(row=1, column=i, value=title)
            c.font = header_font
            c.fill = header_fill
            c.alignment = Alignment(horizontal="center", vertical="center")
            ws.column_dimensions[get_column_letter(i)].width = max(16, len(title) + 4)
        ws.freeze_panes = "A2"

    # ── Инструкция ───────────────────────────────────────────────────────────
    ws_info = wb.active
    ws_info.title = SHEET_INSTRUCTIONS
    ws_info.column_dimensions["A"].width = 100
    lines = [
        ("Импорт данных в ALGO — как заполнять", True),
        ("", False),
        ("Заполните три вкладки внизу: «Учителя», «Группы», «Ученики».", False),
        ("Колонки со звёздочкой (*) — обязательные.", False),
        ("", False),
        ("1) Учителя — каждому нужен уникальный Email (по нему создаётся вход в систему).", False),
        ("   После импорта учитель получит логин и временный пароль, который сменит при первом входе.", False),
        ("   Роль: teacher (учитель) или admin (администратор). По умолчанию — teacher.", False),
        ("", False),
        ("2) Группы — Название обязательно. «Email учителя» должен совпадать с Email из вкладки «Учителя»", False),
        ("   (или уже существующего сотрудника). Класс — число 1–11.", False),
        ("", False),
        ("3) Ученики — Фамилия, Имя и Класс (1–11) обязательны. Телефон — по желанию.", False),
        ("   «Группа» — название из вкладки «Группы» (ученик попадёт в эту группу).", False),
        ("", False),
        ("Повторная загрузка безопасна: учителя сверяются по email, группы по названию,", False),
        ("ученики по ФИО+телефону — существующие не дублируются.", False),
        ("", False),
        ("── Пример заполнения ──", True),
        ("Учителя:  Каримов | Азиз | | a.karimov@example.com | Алгебра, Геометрия | teacher", False),
        ("Группы:   Алгебра-9А | 9 | a.karimov@example.com | 25", False),
        ("Ученики:  Усмонова | Малика | 9 | +998901234567 | Алгебра-9А |", False),
    ]
    for idx, (text, bold) in enumerate(lines, start=1):
        c = ws_info.cell(row=idx, column=1, value=text)
        if bold:
            c.font = Font(bold=True, size=14)
        elif text == "":
            pass
        else:
            c.alignment = Alignment(wrap_text=True)

    # NOTE: data sheets are left EMPTY (headers only). Example values live only
    # in the Инструкция sheet — otherwise a user who forgets to delete the
    # sample rows would import "Каримов"/"Усмонова" as real records.

    # ── Учителя ──────────────────────────────────────────────────────────────
    ws_t = wb.create_sheet(SHEET_TEACHERS)
    style_headers(ws_t, COLS_TEACHERS)
    dv_role = DataValidation(type="list", formula1='"teacher,admin"', allow_blank=True)
    ws_t.add_data_validation(dv_role)
    dv_role.add("F2:F1000")

    # ── Группы ───────────────────────────────────────────────────────────────
    ws_g = wb.create_sheet(SHEET_GROUPS)
    style_headers(ws_g, COLS_GROUPS)
    dv_class_g = DataValidation(
        type="whole", operator="between", formula1=1, formula2=11, allow_blank=True)
    ws_g.add_data_validation(dv_class_g)
    dv_class_g.add("B2:B1000")

    # ── Ученики ──────────────────────────────────────────────────────────────
    ws_s = wb.create_sheet(SHEET_STUDENTS)
    style_headers(ws_s, COLS_STUDENTS)
    dv_class_s = DataValidation(
        type="whole", operator="between", formula1=1, formula2=11, allow_blank=False)
    ws_s.add_data_validation(dv_class_s)
    dv_class_s.add("C2:C1000")

    # Group-name dropdown in Ученики, sourced from Группы!A (via defined name).
    wb.defined_names.add(
        DefinedName("group_names", attr_text=f"'{SHEET_GROUPS}'!$A$2:$A$1000")
    )
    dv_group = DataValidation(type="list", formula1="group_names", allow_blank=True)
    ws_s.add_data_validation(dv_group)
    dv_group.add("E2:E1000")

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
