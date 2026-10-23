"""Excel import by the rules of the interface (LOGIC-B O9, SPEC 1 "O9" and 4.7).

The legacy ``import_service.preview / commit`` wrote groups and students straight into the tables (no seat limit, archived groups accepted, any
role as a teacher, no audit, status "new").  This module plans and writes the same workbook through the SAME service methods the interface uses:

* a new group = ``AuthService.create_group`` (audit ``created`` with ``source: "import"``); a student is enrolled by
  ``AuthService.add_student_to_group(mode="transfer")`` (seat limit under the group lock, archived group refused, membership period, ``status = active``,
  ``display_id``).  A new student is created without commit and enrolled in the same transaction: a refusal rolls the row back, no orphan stays;
* row by row, one transaction each; a second run changes nothing (existing groups / students are recognised);
* blocking errors (structure and teachers) refuse the whole file: a wrong class, an empty name, an unknown group, ``TEACHER_NOT_FOUND`` /
  ``TEACHER_NOT_TEACHER`` / ``TEACHER_INACTIVE``, ``max_students`` outside 1..100;
* refusals by STATE skip the row with a code in ``skipped``: ``GROUP_FULL``, ``GROUP_ARCHIVED``, ``IN_OTHER_GROUP``, ``STUDENT_ARCHIVED``,
  ``HOMONYM_NO_PHONE``, ``DUPLICATE_IN_FILE`` (information: ``ALREADY_IN_GROUP``);
* one import per centre at a time (advisory lock): a parallel one gets 409 ``IMPORT_IN_PROGRESS``.

Extension point for the payment package: ``payroll_hooks.after_enrol(db, student, membership, row)``.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from . import import_service as legacy
from . import payroll_hooks
from .models import Group, GroupAudit, OrganizationMember, Student, StudentGroup, User
from .lesson_teachers import TEACHING_ROLES
from .ops_rules import ERROR_CATALOGUE, OpsError, conflict
from .ops_schemas import (
    CreatedGroup,
    ImportCommitResponseB,
    ImportCountsB,
    ImportIssueB,
    ImportPreviewResponseB,
    ImportSkipped,
)
from .student_archive import student_not_archived

logger = logging.getLogger(__name__)

ROLE_RU = {"owner": "владелец", "admin": "администратор", "teacher": "преподаватель"}
SKIP_TEXT = {
    "GROUP_ARCHIVED": "Группа «{group}» в архиве. Верните её из архива или выберите другую",
    "IN_OTHER_GROUP": "Ученик уже есть в системе и состоит в группе «{other}». Перевод выполните вручную — при переводе переносятся оплаты",
    "STUDENT_ARCHIVED": "Ученик в архиве. Верните его из архива в карточке",
    "HOMONYM_NO_PHONE": "В системе уже есть ученик с таким ФИО без телефона. Если это тёзка, добавьте вручную",
    "DUPLICATE_IN_FILE": "Строка повторяется в файле",
    "ALREADY_IN_GROUP": "Уже в этой группе",
    "GROUP_FULL": "В группе «{group}» нет мест: лимит {limit}, занято {taken}",
    "ROW_ERROR": "Строку не удалось обработать: {reason}",
    "ROW_LOCKED": "Группа или ученик заняты другой операцией. Загрузите файл ещё раз — уже добавленные строки пропустятся",
}


# ------------------------------------------------------------------------------------------------- plan
class _Plan:
    def __init__(self) -> None:
        self.issues: List[ImportIssueB] = []
        self.groups: List[Dict[str, Any]] = []        # {row, name, class_level, teacher_id, teacher_email, max, action, id}
        self.students: List[Dict[str, Any]] = []      # {row, last, first, class_level, phone, comment, group_name, parsed}
        self.skipped: List[ImportSkipped] = []
        self.counts = ImportCountsB()

    @property
    def has_errors(self) -> bool:
        return any(i.level == "error" for i in self.issues)

    def err(self, sheet: str, row: int, field: Optional[str], msg: str, code: Optional[str] = None) -> None:
        self.issues.append(ImportIssueB(sheet=sheet, row=row, field=field, level="error", message=msg, code=code))

    def warn(self, sheet: str, row: int, field: Optional[str], msg: str, code: Optional[str] = None) -> None:
        self.issues.append(ImportIssueB(sheet=sheet, row=row, field=field, level="warning", message=msg, code=code))

    def skip(self, row: int, code: str, message: str, student: str, group: str) -> None:
        self.skipped.append(ImportSkipped(sheet=legacy.SHEET_STUDENTS, row=row, code=code, message=message, student=student, group=group))


def _org_staff(db: Session, org_id: Any) -> Dict[str, Tuple[User, OrganizationMember]]:
    out: Dict[str, Tuple[User, OrganizationMember]] = {}
    rows = (
        db.query(User, OrganizationMember)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .filter(OrganizationMember.org_id == org_id)
        .all()
    )
    for user, member in rows:
        if user.email:
            out[user.email.lower()] = (user, member)
    return out


def _live_groups_of(db: Session, org_id: Any, student_ids: List[Any]) -> Dict[Any, List[Group]]:
    """Student id -> the live groups he is in now."""
    result: Dict[Any, List[Group]] = {}
    if not student_ids:
        return result
    rows = (
        db.query(StudentGroup.student_id, Group)
        .join(Group, Group.id == StudentGroup.group_id)
        .filter(StudentGroup.student_id.in_(student_ids), StudentGroup.is_active.is_(True), Group.org_id == org_id, Group.status == "active")
        .all()
    )
    for student_id, group in rows:
        result.setdefault(student_id, []).append(group)
    return result


def _occupancy(db: Session, group_id: Any) -> int:
    return (
        db.query(func.count(StudentGroup.id))
        .filter(StudentGroup.group_id == group_id, StudentGroup.is_active.is_(True), student_not_archived())
        .scalar()
        or 0
    )


def build_plan(org_id: Any, parsed: "legacy.ParsedWorkbook", db: Session, *, money: bool = True) -> _Plan:
    """``money``: the caller may enter money (owner / an admin with the switch).  LOGIC-A: without it the opening-balance columns G-I are
    ignored with a warning."""
    from .payments_service import PaymentService

    plan = _Plan()
    staff = _org_staff(db, org_id)
    accounting_start = PaymentService(db)._loader(org_id).settings.accounting_start
    enrol_day = legacy._today()

    # ---- groups
    known_names: set = set()
    seen: set = set()
    group_by_key: Dict[str, Dict[str, Any]] = {}
    for r in parsed.groups:
        d = r.data
        name = d.get(legacy.K_GROUP_NAME, "")
        if not name:
            plan.err(legacy.SHEET_GROUPS, r.row, "Название", "Название группы обязательно.")
            continue
        key = name.lower()
        if key in seen:
            plan.warn(legacy.SHEET_GROUPS, r.row, "Название", f"Группа '{name}' повторяется — строка пропущена.")
            continue
        seen.add(key)
        known_names.add(key)
        class_raw = d.get(legacy.K_CLASS, "")
        class_level = legacy._parse_class_level(class_raw)
        if class_raw and class_level is None:
            plan.err(legacy.SHEET_GROUPS, r.row, "Класс", f"Класс должен быть числом 1–11: '{class_raw}'")
            continue
        if class_level is not None and not (1 <= class_level <= 11):
            plan.err(legacy.SHEET_GROUPS, r.row, "Класс", f"Класс вне диапазона 1–11: {class_level}")
            continue
        max_raw = d.get(legacy.K_MAX_STUDENTS, "")
        max_students = 30
        if max_raw:
            max_students = legacy._parse_class_level(max_raw)
            if max_students is None or not (1 <= max_students <= 100):
                plan.err(legacy.SHEET_GROUPS, r.row, "Макс. учеников", f"Максимум учеников должен быть числом 1–100: '{max_raw}'", "MAX_STUDENTS_RANGE")
                continue
        email = d.get(legacy.K_TEACHER_EMAIL, "").lower()
        teacher_id = None
        if email:
            entry = staff.get(email)
            if entry is None:
                plan.err(legacy.SHEET_GROUPS, r.row, "Почта учителя",
                         f"Преподаватель {email} не найден в системе. Добавьте его через интерфейс.", "TEACHER_NOT_FOUND")
                continue
            user, member = entry
            if member.org_role not in TEACHING_ROLES:
                plan.err(legacy.SHEET_GROUPS, r.row, "Почта учителя",
                         f"{email} — не преподаватель (роль: {ROLE_RU.get(member.org_role, member.org_role)})", "TEACHER_NOT_TEACHER")
                continue
            if not member.is_active or user.is_active is False:
                plan.err(legacy.SHEET_GROUPS, r.row, "Почта учителя", f"{email} больше не работает в центре", "TEACHER_INACTIVE")
                continue
            teacher_id = user.id
        existing = db.query(Group).filter(Group.org_id == org_id, func.lower(Group.name) == key).first()
        entry_plan = {
            "row": r.row, "name": name, "class_level": class_level, "teacher_id": teacher_id, "teacher_email": email,
            "max": max_students, "action": "existing" if existing else "create", "id": str(existing.id) if existing else None,
            "group": existing,
        }
        if existing is not None:
            plan.counts.groups_existing += 1
            if teacher_id is not None and existing.teacher_id != teacher_id:
                plan.warn(legacy.SHEET_GROUPS, r.row, "Почта учителя",
                          f"Преподаватель группы «{existing.name}» в файле отличается от текущего — изменений нет.", "TEACHER_DIFFERS")
        else:
            plan.counts.groups_new += 1
        plan.groups.append(entry_plan)
        group_by_key[key] = entry_plan

    # ---- students (structure first)
    seen_students: set = set()
    rows: List[Dict[str, Any]] = []
    for r in parsed.students:
        d = r.data
        last, first = d.get(legacy.K_LAST, ""), d.get(legacy.K_FIRST, "")
        if not last or not first:
            plan.err(legacy.SHEET_STUDENTS, r.row, "Фамилия/Имя", "Фамилия и имя обязательны.")
            continue
        class_level = legacy._parse_class_level(d.get(legacy.K_STUDENT_CLASS, ""))
        if class_level is None:
            plan.err(legacy.SHEET_STUDENTS, r.row, "Класс", "Класс обязателен и должен быть числом 1–11.")
            continue
        if not (1 <= class_level <= 11):
            plan.err(legacy.SHEET_STUDENTS, r.row, "Класс", f"Класс вне диапазона 1–11: {class_level}")
            continue
        phone = d.get(legacy.K_PHONE, "") or None
        group_name = d.get(legacy.K_GROUP, "")
        if not group_name:
            plan.err(legacy.SHEET_STUDENTS, r.row, "Группа", "Группа обязательна")
            continue
        gk = group_name.lower()
        in_db = db.query(Group).filter(Group.org_id == org_id, func.lower(Group.name) == gk).first()
        if gk not in known_names and in_db is None:
            plan.err(legacy.SHEET_STUDENTS, r.row, "Группа", f"Группа '{group_name}' не найдена во вкладке «Группы» и не существует.")
            continue
        ok, paid_until, debt_amount, debt_on = legacy.parse_opening(
            d, r.row, money=money, accounting_start=accounting_start, enrol_day=enrol_day, err=plan.err, warn=plan.warn)
        if not ok:
            continue
        rows.append({"row": r.row, "last": last, "first": first, "class_level": class_level, "phone": phone,
                     "comment": d.get(legacy.K_COMMENT, ""), "group_name": group_name, "key": gk, "data": r,
                     "opening": (paid_until, debt_amount, debt_on) if (paid_until or debt_amount) else None})
    if plan.has_errors:
        return plan  # the enrolment is simulated only for a file that can be written

    # ---- students: what happens to each row (state refusals)
    people_cache: Dict[Tuple[str, str, Optional[str]], Any] = {}
    planned_seats: Dict[str, int] = {}
    pending: List[Tuple[Dict[str, Any], Optional[Student]]] = []
    for item in rows:
        full = f"{item['last']} {item['first']}"
        dedup = (item["last"].lower(), item["first"].lower(), (item["phone"] or "").strip())
        if dedup in seen_students:
            plan.skip(item["row"], "DUPLICATE_IN_FILE", SKIP_TEXT["DUPLICATE_IN_FILE"], full, item["group_name"])
            plan.counts.students_skipped += 1
            continue
        seen_students.add(dedup)
        q = db.query(Student).filter(Student.org_id == org_id, func.lower(Student.last_name) == item["last"].lower(),
                                     func.lower(Student.first_name) == item["first"].lower())
        matches = q.all()
        found: Optional[Student] = None
        if item["phone"]:
            found = next((s for s in matches if s.student_phone == item["phone"]), None)
        else:
            found = next((s for s in matches if not s.student_phone), None)
            if found is None and matches:
                plan.skip(item["row"], "HOMONYM_NO_PHONE", SKIP_TEXT["HOMONYM_NO_PHONE"], full, item["group_name"])
                plan.counts.students_skipped += 1
                continue
        pending.append((item, found))
    live = _live_groups_of(db, org_id, [s.id for _i, s in pending if s is not None])
    for item, found in pending:
        full = f"{item['last']} {item['first']}"
        target = group_by_key.get(item["key"])
        group = target["group"] if target else db.query(Group).filter(Group.org_id == org_id, func.lower(Group.name) == item["key"]).first()
        name = group.name if group is not None else item["group_name"]
        if group is not None and group.status != "active":
            plan.skip(item["row"], "GROUP_ARCHIVED", SKIP_TEXT["GROUP_ARCHIVED"].format(group=name), full, name)
            plan.counts.students_skipped += 1
            if found is not None:
                plan.counts.students_existing += 1
            continue
        if found is not None:
            plan.counts.students_existing += 1
            if found.status == "archived":
                plan.skip(item["row"], "STUDENT_ARCHIVED", SKIP_TEXT["STUDENT_ARCHIVED"], full, name)
                plan.counts.students_skipped += 1
                continue
            groups_now = live.get(found.id, [])
            if group is not None and any(g.id == group.id for g in groups_now):
                plan.skip(item["row"], "ALREADY_IN_GROUP", SKIP_TEXT["ALREADY_IN_GROUP"], full, name)
                plan.counts.students_unchanged += 1
                continue
            if groups_now:
                plan.skip(item["row"], "IN_OTHER_GROUP", SKIP_TEXT["IN_OTHER_GROUP"].format(other=groups_now[0].name), full, name)
                plan.counts.students_skipped += 1
                continue
        # an existing group has its own limit (the file's "max" only applies to a group the file creates)
        limit = (group.max_students or 30) if group is not None else (target["max"] if target else 30)
        taken = (_occupancy(db, group.id) if group is not None else 0) + planned_seats.get(item["key"], 0)
        if taken >= limit:
            plan.skip(item["row"], "GROUP_FULL", SKIP_TEXT["GROUP_FULL"].format(group=name, limit=limit, taken=taken), full, name)
            plan.counts.students_skipped += 1
            continue
        planned_seats[item["key"]] = planned_seats.get(item["key"], 0) + 1
        plan.counts.students_enrolled += 1
        if found is None:
            plan.counts.students_new += 1
            if item["opening"] is not None:
                plan.counts.openings += 1
        elif item["opening"] is not None:
            plan.warn(legacy.SHEET_STUDENTS, item["row"], "Остаток", "Ученик уже есть в системе — остаток не внесён.")
            item = {**item, "opening": None}
        plan.students.append({**item, "existing": found})
    plan.skipped.sort(key=lambda s: s.row)
    return plan


# ---------------------------------------------------------------------------------------- public API
def _fatal(parsed) -> ImportPreviewResponseB:
    return ImportPreviewResponseB(
        ok=False, counts=ImportCountsB(), issues=[ImportIssueB(sheet="-", row=0, level="error", message=parsed.fatal)]
    )


def preview(org_id: Any, data: bytes, db: Session, *, money: bool = True) -> ImportPreviewResponseB:
    parsed = legacy.parse_workbook(data)
    if parsed.fatal:
        return _fatal(parsed)
    plan = build_plan(org_id, parsed, db, money=money)
    return ImportPreviewResponseB(
        ok=not plan.has_errors,
        counts=plan.counts,
        issues=plan.issues,
        skipped=plan.skipped,
        groups_preview=[
            {"name": g["name"], "class_level": g["class_level"], "teacher_email": g["teacher_email"], "action": g["action"]} for g in plan.groups[:50]
        ],
        students_preview=[
            {"name": f"{s['last']} {s['first']}", "class_level": s["class_level"], "phone": s["phone"], "group": s["group_name"],
             "action": "existing" if s["existing"] is not None else "create"} for s in plan.students[:50]
        ],
    )


@contextmanager
def _org_lock(db: Session, org_id: Any):
    """One import per centre at a time: a session-level advisory lock on a DEDICATED connection (the row transactions of the session
    commit and may change connection)."""
    conn = db.get_bind().connect()
    key = text("SELECT hashtextextended(:k, 0)")
    try:
        lock_id = conn.execute(key, {"k": f"import:{org_id}"}).scalar()
        if not conn.execute(text("SELECT pg_try_advisory_lock(:i)"), {"i": lock_id}).scalar():
            raise conflict("IMPORT_IN_PROGRESS", ERROR_CATALOGUE["IMPORT_IN_PROGRESS"][1])
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:i)"), {"i": lock_id})
    finally:
        conn.close()


def commit(org_id: Any, creator_id: Any, data: bytes, db: Session, *, money: bool = True) -> ImportCommitResponseB:
    from .schemas import GroupCreateRequest
    from .service import AuthService

    parsed = legacy.parse_workbook(data)
    if parsed.fatal:
        return ImportCommitResponseB(ok=False, counts=ImportCountsB(),
                                     issues=[ImportIssueB(sheet="-", row=0, level="error", message=parsed.fatal)])
    with _org_lock(db, org_id):
        plan = build_plan(org_id, parsed, db, money=money)
        if plan.has_errors:
            return ImportCommitResponseB(ok=False, counts=plan.counts, issues=plan.issues, skipped=plan.skipped)
        svc = AuthService(db)
        actor_role = (
            db.query(OrganizationMember.org_role).filter(OrganizationMember.org_id == org_id, OrganizationMember.user_id == creator_id).scalar()
            or "admin"
        )
        created: List[CreatedGroup] = []
        group_ids: Dict[str, Any] = {}
        try:
            for g in plan.groups:
                if g["action"] == "existing":
                    group_ids[g["name"].lower()] = UUID(g["id"])
                    continue
                request = GroupCreateRequest(
                    name=g["name"], class_level=g["class_level"], teacher_id=str(g["teacher_id"]) if g["teacher_id"] else None,
                    max_students=g["max"], lesson_type="group",
                )
                response = svc.create_group(str(org_id), request, actor_id=str(creator_id), is_org_admin=True, actor_role=actor_role)
                audit = (
                    db.query(GroupAudit).filter(GroupAudit.group_id == UUID(response.id), GroupAudit.action == "created")
                    .order_by(GroupAudit.created_at.desc()).first()
                )
                if audit is not None:
                    audit.snapshot = {**(audit.snapshot or {}), "source": "import"}
                    db.commit()
                group_ids[g["name"].lower()] = UUID(response.id)
                created.append(CreatedGroup(id=response.id, name=response.name))
        except Exception as exc:  # noqa: BLE001 - a group that cannot be created stops the file before any student is touched
            db.rollback()
            return ImportCommitResponseB(
                ok=False, counts=ImportCountsB(), issues=[ImportIssueB(sheet="-", row=0, level="error", message=f"Импорт не выполнен: {exc}")],
                created_groups=created,
            )
        result = ImportCountsB(**plan.counts.model_dump())
        result.students_new = result.students_enrolled = result.students_skipped = result.students_unchanged = result.openings = 0
        skipped: List[ImportSkipped] = list(plan.skipped)
        for item in plan.students:
            try:
                outcome = _enrol(db, svc, org_id, creator_id, item, group_ids[item["key"]])
            except Exception as exc:  # noqa: BLE001 - one bad row must not end the file with a 500
                db.rollback()
                logger.exception("import: row %s failed", item.get("row"))
                outcome = ("skipped", "ROW_ERROR", SKIP_TEXT["ROW_ERROR"].format(reason=exc.__class__.__name__), item["group_name"])
            full = f"{item['last']} {item['first']}"
            if outcome[0] == "enrolled":
                result.students_enrolled += 1
                if item["existing"] is None:
                    result.students_new += 1
                    if item.get("opening") is not None:
                        result.openings += 1
                continue
            code, message, group_name = outcome[1], outcome[2], outcome[3]
            skipped.append(ImportSkipped(sheet=legacy.SHEET_STUDENTS, row=item["row"], code=code, message=message, student=full, group=group_name))
            if code == "ALREADY_IN_GROUP":
                result.students_unchanged += 1
            else:
                result.students_skipped += 1
        # the planned skips (DUPLICATE_IN_FILE, HOMONYM, ...) are counted by the plan
        result.students_skipped += sum(1 for s in plan.skipped if s.code not in ("ALREADY_IN_GROUP",))
        result.students_unchanged += sum(1 for s in plan.skipped if s.code == "ALREADY_IN_GROUP")
        skipped.sort(key=lambda s: s.row)
        return ImportCommitResponseB(ok=True, counts=result, issues=plan.issues, skipped=skipped, created_groups=created)


def _enrol(db: Session, svc, org_id: Any, creator_id: Any, item: Dict[str, Any], group_id: UUID) -> Tuple[str, ...]:
    """One student row in one transaction: ``("enrolled",)`` or ``("skipped", code, message, group_name)``."""
    from .ops_rules import GroupArchivedForEnrolment

    group = db.query(Group).filter(Group.id == group_id).first()
    name = group.name if group else item["group_name"]
    student = item["existing"]
    try:
        if student is not None:
            student = db.query(Student).filter(Student.id == student.id).with_for_update().first()
            if student.status == "archived":
                db.rollback()
                return ("skipped", "STUDENT_ARCHIVED", SKIP_TEXT["STUDENT_ARCHIVED"], name)
            now_groups = _live_groups_of(db, org_id, [student.id]).get(student.id, [])
            if any(g.id == group_id for g in now_groups):
                db.rollback()
                return ("skipped", "ALREADY_IN_GROUP", SKIP_TEXT["ALREADY_IN_GROUP"], name)
            if now_groups:
                other = now_groups[0].name
                db.rollback()
                return ("skipped", "IN_OTHER_GROUP", SKIP_TEXT["IN_OTHER_GROUP"].format(other=other), name)
        else:
            student = Student(
                org_id=org_id, first_name=item["first"], last_name=item["last"], class_level=item["class_level"],
                student_phone=item["phone"], comment=item["comment"] or None, status="new", created_by=creator_id,
            )
            db.add(student)
            db.flush()
        # LOGIC-A (owner decision 1): the opening balance of a NEW student goes INTO the enrolment (same transaction, same commit): when A refuses it the
        # whole row is rolled back, the student does not stay behind without the balance he was imported with
        opening = None
        if item["existing"] is None and item.get("opening") is not None:
            from .payments_schemas import OpeningIn

            paid_until, debt_amount, debt_on = item["opening"]
            opening = OpeningIn(paid_until=paid_until, debt_amount=debt_amount, debt_on=debt_on)
        svc.add_student_to_group(str(org_id), str(group_id), str(student.id), mode="transfer", actor_id=str(creator_id),
                                 opening_balance=opening, opening_source="import")
        membership = db.query(StudentGroup).filter(StudentGroup.student_id == student.id, StudentGroup.group_id == group_id).first()
        payroll_hooks.after_enrol(db, student, membership, item["data"])
        db.commit()
        return ("enrolled",)
    except GroupArchivedForEnrolment:
        db.rollback()
        return ("skipped", "GROUP_ARCHIVED", SKIP_TEXT["GROUP_ARCHIVED"].format(group=name), name)
    except ValueError as exc:
        db.rollback()
        text_ = str(exc)
        if "лимит" in text_:
            limit = (group.max_students if group else None) or 30
            return ("skipped", "GROUP_FULL", SKIP_TEXT["GROUP_FULL"].format(group=name, limit=limit, taken=limit), name)
        if "архивн" in text_ and "группу" in text_:
            return ("skipped", "GROUP_ARCHIVED", SKIP_TEXT["GROUP_ARCHIVED"].format(group=name), name)
        if "Архивного ученика" in text_:
            return ("skipped", "STUDENT_ARCHIVED", SKIP_TEXT["STUDENT_ARCHIVED"], name)
        # any other refusal of the row is the row's problem, never the file's: a bare 500 would lose the rows that were enrolled before
        return ("skipped", "ROW_ERROR", SKIP_TEXT["ROW_ERROR"].format(reason=text_[:200] or "ошибка данных"), name)
    except OperationalError as exc:  # a lock timeout / a deadlock victim: the row can be repeated
        db.rollback()
        logger.warning("import: row %s skipped, database busy: %s", item.get("row"), exc.__class__.__name__)
        return ("skipped", "ROW_LOCKED", SKIP_TEXT["ROW_LOCKED"], name)
