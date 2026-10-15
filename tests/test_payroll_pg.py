"""N13 P3: payroll - the month, the coverage by payments, sheets, revisions, diff, Excel, permissions (PostgreSQL)."""
import calendar
import io
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event

from src.models.auth import (
    AttendanceExcuse, Group, GroupAttendance, GroupAudit, Organization, PayrollSheet, PayrollSheetRevision,
    Student, StudentGroup, StudentPayment,
)

from tests.pg_world import (  # noqa: F401  (fixtures)
    business_today, make_client, make_group, make_member, make_student, make_user, needs_pg, pg_session, world,
)

pytestmark = needs_pg

API = "/api/v1/auth"
FEE = 450_000


# ---- the month under test -------------------------------------------------------------------------


def previous_month():
    """The whole previous calendar month (Tashkent): ``.d(n)`` is its n-th day."""
    today = business_today()
    last = today.replace(day=1) - timedelta(days=1)
    first = last.replace(day=1)
    return SimpleNamespace(first=first, last=last, label=first.strftime("%Y-%m"), d=lambda n: first.replace(day=n))


def current_month():
    today = business_today()
    first = today.replace(day=1)
    return SimpleNamespace(
        first=first, last=first.replace(day=calendar.monthrange(first.year, first.month)[1]),
        label=first.strftime("%Y-%m"), d=lambda n: first.replace(day=n),
    )


# ---- helpers: data ---------------------------------------------------------------------------------


def student_group(w, student, group=None):
    w.db.expire_all()
    return w.db.query(StudentGroup).filter_by(student_id=student.id, group_id=(group or w.group).id).one()


def set_fee(w, student, fee, group=None):
    student_group(w, student, group).monthly_fee = fee
    w.db.commit()


def lesson(w, day_, marks, *, group=None, teacher=None, roster=None, status="completed", lesson_id=None):
    """A lesson row: ``marks`` = {student: mark}; ``roster`` (students) defaults to the marked ones."""
    group = group or w.group
    members = list(marks) if roster is None else list(roster)
    row = GroupAttendance(
        org_id=w.org.id, group_id=group.id, lesson_date=day_.isoformat(), lesson_id=lesson_id or f"s{day_.isoweekday()}",
        status=status, records={str(s.id): m for s, m in marks.items()},
        roster_snapshot=[{"student_id": str(s.id)} for s in members],
        teacher_id=(teacher or w.teacher).id if teacher is not False else None,
    )
    w.db.add(row)
    w.db.commit()
    return row


def pay_calendar(w, student, first, last, *, group=None, fee=FEE, paid_on=None):
    membership = student_group(w, student, group)
    if fee is not None:
        membership.monthly_fee = fee
    w.db.add(StudentPayment(
        org_id=w.org.id, membership_id=membership.id, student_id=student.id, group_id=membership.group_id,
        amount=fee or 1, paid_on=paid_on or first, valid_from=first, valid_to=last, created_by=w.owner.id,
    ))
    w.db.commit()


def pay_package(w, student, valid_from, size, *, group=None, fee=FEE, paid_on=None):
    membership = student_group(w, student, group)
    if fee is not None:
        membership.monthly_fee = fee
    payment = StudentPayment(
        org_id=w.org.id, membership_id=membership.id, student_id=student.id, group_id=membership.group_id,
        amount=fee or 1, paid_on=paid_on or valid_from, valid_from=valid_from, lessons_count=size, created_by=w.owner.id,
    )
    w.db.add(payment)
    w.db.commit()
    return payment


def excuse(w, att, student, status="pending"):
    w.db.add(AttendanceExcuse(
        org_id=w.org.id, attendance_id=att.id, student_id=student.id, reason="Болел", reason_by=w.admin.id,
        reason_at=datetime.utcnow(), status=status,
    ))
    w.db.commit()


# ---- helpers: HTTP ------------------------------------------------------------------------------------


def owner(w):
    return make_client(w, user=w.owner)


def pct(w, value="50", lesson_type="group", teacher=None):
    return {"teacher_id": str((teacher or w.teacher).id), "lesson_type": lesson_type, "percent": value}


def calc(w, m, percents=None, teacher_ids=None, expect=200, client=None):
    body = {"percents": percents if percents is not None else [pct(w)]}
    if teacher_ids is not None:
        body["teacher_ids"] = teacher_ids
    r = (client or owner(w)).post(f"{API}/org/payroll/months/{m.label}/calculate", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def preview(w, m, teacher=None, value="50"):
    """The preview sheet of one teacher."""
    teacher = teacher or w.teacher
    body = calc(w, m, [pct(w, value, teacher=teacher)], teacher_ids=[str(teacher.id)])
    (sheet,) = body["sheets"]
    return sheet


def save(w, m, teacher=None, percents=None, expect=201, **extra):
    teacher = teacher or w.teacher
    body = {"month": m.label, "teacher_id": str(teacher.id), "percents": percents or [pct(w, teacher=teacher)], **extra}
    r = owner(w).post(f"{API}/org/payroll/sheets", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def patch(w, sheet, expect=200, **body):
    body.setdefault("revision", sheet["revision"])
    r = owner(w).patch(f"{API}/org/payroll/sheets/{sheet['id']}", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def by_student(sheet):
    return {line["student_id"]: line for line in sheet["lines"]}


def line_of(sheet, student):
    return by_student(sheet)[str(student.id)]


def overview(w, m, client=None):
    r = (client or owner(w)).get(f"{API}/org/payroll/months/{m.label}")
    assert r.status_code == 200, r.text
    return r.json()


def teacher_row(ov, teacher):
    return next(t for t in ov["teachers"] if t["teacher_id"] == str(teacher.id))


def full_month(w, m, students=None, days=range(1, 14), fee=FEE, **kw):
    """Everybody present on ``days`` of the month, everybody paid for the whole month."""
    students = students or w.students
    for n in days:
        lesson(w, m.d(n), {s: "present" for s in students}, **kw)
    for s in students:
        pay_calendar(w, s, m.first, m.last, fee=fee)


# ============================================================================================
# the partner's table, the arithmetic end to end
# ============================================================================================


def test_partner_table_through_the_api(world):
    """450 000, 50 %: 11 / 13 / 5 lessons -> 206 250 / 243 750 / 93 750."""
    m = previous_month()
    s0, s1, s2 = world.students
    for n in range(1, 14):
        roster = [s for s, (a, b) in ((s0, (1, 11)), (s1, (1, 13)), (s2, (9, 13))) if a <= n <= b]
        lesson(world, m.d(n), {s: "present" for s in roster}, roster=roster)
    for s in world.students:
        pay_calendar(world, s, m.first, m.last)

    sheet = preview(world, m)
    assert [line_of(sheet, s)["amount"] for s in (s0, s1, s2)] == [206_250, 243_750, 93_750]
    assert [line_of(sheet, s)["counted"] for s in (s0, s1, s2)] == [11, 13, 5]
    assert sheet["total_amount"] == 543_750 and sheet["counted_total"] == 29 and sheet["unpaid_total"] == 0
    assert sheet["saved"] is False and sheet["id"] is None and sheet["revision"] is None
    assert sheet["month"] == m.label and sheet["period_start"] == m.first.isoformat() and sheet["period_end"] == m.last.isoformat()
    assert sheet["month_open"] is False and sheet["percents"] == {"group": "50"} and sheet["divisor_default"] == 12
    line = line_of(sheet, s2)
    assert line["student_name"] == "Орлова Дана" and line["display_id"] == "N13-003"      # «Фамилия Имя»
    assert line["group_name"] == "Группа" and line["lesson_type"] == "group" and line["lesson_time"] == "10:00–11:00"
    assert (line["monthly_fee"], line["source_fee"], line["fee_overridden"], line["percent"], line["divisor"]) == (FEE, FEE, False, "50", 12)
    assert len(line["marks"]) == 13 and [x["mark"] for x in line["marks"]] == [None] * 8 + [1] * 5
    assert [x["state"] for x in line["marks"]][:2] == ["not_in_group", "not_in_group"] and line["marks"][8]["state"] == "counted"
    (block,) = sheet["groups"]
    assert block["group_name"] == "Группа" and block["line_count"] == 3 and len(block["dates"]) == 13
    assert block["total_amount"] == 543_750 and block["percent"] == "50"

    saved = save(world, m)                                                  # the saved sheet is the same sheet
    assert saved["total_amount"] == 543_750 and saved["revision"] == 1 and saved["saved"] is True and saved["stale"] is False
    got = owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}").json()
    assert [line_of(got, s)["amount"] for s in (s0, s1, s2)] == [206_250, 243_750, 93_750]
    assert got["created_by_name"] == "Иван Owner" and got["teacher_name"] == "Пётр Teacher"


def test_the_divisor_is_a_centre_setting(world):
    m = previous_month()
    full_month(world, m, days=range(1, 7))
    c = owner(world)
    assert c.get(f"{API}/org/payroll/settings").json()["divisor"] == 12
    base = preview(world, m)
    assert line_of(base, world.students[0])["amount"] == FEE * 50 * 6 // 1200
    r = c.put(f"{API}/org/payroll/settings", json={"divisor": 10})
    assert r.status_code == 200 and r.json()["divisor"] == 10 and r.json()["updated_by"] == str(world.owner.id)
    ten = preview(world, m)
    assert line_of(ten, world.students[0])["amount"] == FEE * 50 * 6 // 1000 and ten["divisor_default"] == 10
    assert line_of(ten, world.students[0])["divisor"] == 10
    for bad in (0, 32, -1, "x"):
        assert c.put(f"{API}/org/payroll/settings", json={"divisor": bad}).status_code == 422
    # the payments settings and the payroll settings share one JSON column and do not erase each other
    assert c.put(f"{API}/org/settings/payments", json={"period_mode": "lessons"}).status_code == 200
    assert c.get(f"{API}/org/payroll/settings").json()["divisor"] == 10
    assert c.put(f"{API}/org/payroll/settings", json={"divisor": 31}).status_code == 200
    assert c.get(f"{API}/org/settings/payments").json()["period_mode"] == "lessons"
    # garbage in the column falls back to the default, the page still opens
    world.org.settings = {"payroll": {"divisor": 99}}
    world.db.commit()
    assert c.get(f"{API}/org/payroll/settings").json()["divisor"] == 12


# ============================================================================================
# who teaches, which lessons count
# ============================================================================================


def test_only_completed_lessons_of_the_teacher_by_the_snapshot_count(world):
    m = previous_month()
    s0 = world.students[0]
    pay_calendar(world, s0, m.first, m.last)
    lesson(world, m.d(1), {s0: "present"})
    lesson(world, m.d(2), {s0: "present"}, status="cancelled")
    lesson(world, m.d(3), {s0: "present"}, status="in_progress")
    lesson(world, m.d(4), {s0: "late"}, teacher=world.teacher2)          # taught by somebody else
    lesson(world, m.d(5), {s0: "present"}, teacher=False)                # nobody was the teacher
    outside = lesson(world, m.d(6), {s0: "present"})
    outside.lesson_date = (m.first - timedelta(days=1)).isoformat()      # last day of the month before
    after = lesson(world, m.d(7), {s0: "present"}, lesson_id="late-one")
    after.lesson_date = (m.last + timedelta(days=1)).isoformat()
    world.db.commit()
    sheet = preview(world, m)
    line = line_of(sheet, s0)
    assert line["counted"] == 1 and [x["date"] for x in line["marks"]] == [m.d(1).isoformat()]
    other = preview(world, m, teacher=world.teacher2)
    assert line_of(other, s0)["counted"] == 1 and line_of(other, s0)["marks"][0]["date"] == m.d(4).isoformat()
    ov = overview(world, m)
    assert ov["lessons_without_teacher"] == 1
    assert teacher_row(ov, world.teacher)["lessons_count"] == 1 and teacher_row(ov, world.teacher2)["lessons_count"] == 1


def test_marks_present_late_absent_count_and_a_student_outside_the_roster_does_not(world):
    m = previous_month()
    s0, s1, s2 = world.students
    for s in world.students:
        pay_calendar(world, s, m.first, m.last)
    lesson(world, m.d(1), {s0: "present", s1: "present", s2: "present"}, roster=[s0, s1])     # s2 not in the roster
    lesson(world, m.d(2), {s0: "late", s1: "absent"})
    lesson(world, m.d(3), {s0: "absent"}, roster=[s0, s1])                                    # s1 in the roster, never marked
    sheet = preview(world, m)
    assert (line_of(sheet, s0)["counted"], line_of(sheet, s1)["counted"]) == (3, 2)
    assert [x["state"] for x in line_of(sheet, s1)["marks"]] == ["counted", "counted", "not_in_group"]
    assert str(s2.id) not in by_student(sheet)                  # in the records of a lesson but never on a roster: no line


def test_excuses_confirmed_pending_rejected_and_without_a_decision(world):
    m = previous_month()
    s0, s1, s2 = world.students
    for s in world.students:
        pay_calendar(world, s, m.first, m.last)
    att = lesson(world, m.d(1), {s0: "excused", s1: "excused", s2: "excused"})
    excuse(world, att, s0, "confirmed")
    excuse(world, att, s1, "pending")
    excuse(world, att, s2, "rejected")
    att2 = lesson(world, m.d(2), {s0: "excused", s1: "present", s2: "present"})                 # excused, no row at all
    sheet = preview(world, m)
    l0, l1, l2 = (line_of(sheet, s) for s in (s0, s1, s2))
    assert [x["state"] for x in l0["marks"]] == ["excused_confirmed", "counted"] and [x["mark"] for x in l0["marks"]] == [0, 1]
    assert l0["counted"] == 1 and l0["amount"] == FEE * 50 // 1200 and l0["pending_excused"] == 0
    assert [x["state"] for x in l1["marks"]] == ["excused_pending", "counted"] and l1["counted"] == 2 and l1["pending_excused"] == 1
    assert [w["code"] for w in l1["warnings"]] == ["EXCUSE_PENDING"]
    assert [x["state"] for x in l2["marks"]] == ["counted", "counted"] and l2["counted"] == 2 and l2["pending_excused"] == 0
    assert sheet["pending_excused_total"] == 1 and sheet["warnings"]["pending_excused"] == 1
    ov = overview(world, m)
    assert teacher_row(ov, world.teacher)["warnings"]["pending_excused"] == 1
    # the owner confirms it: the lesson is no longer counted, and the saved sheet notices
    saved = save(world, m)
    world.db.query(AttendanceExcuse).filter_by(attendance_id=att.id, student_id=s1.id).update({"status": "confirmed"})
    world.db.commit()
    again = preview(world, m)
    assert line_of(again, s1)["counted"] == 1 and [x["mark"] for x in line_of(again, s1)["marks"]] == [0, 1]
    assert owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}").json()["stale"] is True


def test_a_student_who_left_keeps_the_lessons_he_attended(world):
    m = previous_month()
    s0, s1, _ = world.students
    full_month(world, m, days=range(1, 6))
    membership = student_group(world, s1)
    membership.is_active = False
    membership.left_at = datetime.utcnow()
    world.db.commit()
    sheet = preview(world, m)
    assert line_of(sheet, s1)["counted"] == 5 and line_of(sheet, s1)["amount"] == FEE * 50 * 5 // 1200


def test_two_teachers_in_one_group_split_the_month_by_the_snapshot(world):
    m = previous_month()
    s0 = world.students[0]
    pay_calendar(world, s0, m.first, m.last)
    for n in range(1, 7):
        lesson(world, m.d(n), {s0: "present"})
    for n in range(7, 13):
        lesson(world, m.d(n), {s0: "present"}, teacher=world.teacher2)
    a, b = preview(world, m), preview(world, m, teacher=world.teacher2, value="40")
    assert line_of(a, s0)["counted"] == 6 and line_of(a, s0)["amount"] == FEE * 50 * 6 // 1200
    assert line_of(b, s0)["counted"] == 6 and line_of(b, s0)["amount"] == FEE * 40 * 6 // 1200
    assert line_of(a, s0)["membership_id"] == line_of(b, s0)["membership_id"]       # one membership, a line per teacher
    ov = overview(world, m)
    assert {t["teacher_id"] for t in ov["teachers"]} == {str(world.teacher.id), str(world.teacher2.id)}


def test_lines_per_group_and_the_kind_of_lessons_set_the_percent(world):
    m = previous_month()
    s0 = world.students[0]
    second = make_group(world.db, world.org, world.teacher, "Парные")
    second.lesson_type = "pair"
    world.db.add(StudentGroup(student_id=s0.id, group_id=second.id, is_active=True, joined_at=datetime.utcnow() - timedelta(days=60)))
    world.db.commit()
    for n in range(1, 5):
        lesson(world, m.d(n), {s0: "present"})
        lesson(world, m.d(n), {s0: "present"}, group=second)
    pay_calendar(world, s0, m.first, m.last)
    pay_calendar(world, s0, m.first, m.last, group=second, fee=600_000)
    # both kinds need a percent: one missing is refused and says which
    r = owner(world).post(f"{API}/org/payroll/months/{m.label}/calculate", json={"percents": [pct(world)]})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "PERCENT_REQUIRED"
    (missing,) = r.json()["detail"]["missing"]
    assert missing["lesson_type"] == "pair" and missing["teacher_id"] == str(world.teacher.id) and missing["lesson_type_label"] == "Парный"
    body = calc(world, m, [pct(world, "50"), pct(world, "70", "pair")])
    (sheet,) = body["sheets"]
    lines = {x["group_name"]: x for x in sheet["lines"]}
    assert lines["Группа"]["amount"] == FEE * 50 * 4 // 1200 and lines["Парные"]["amount"] == 600_000 * 70 * 4 // 1200
    assert sheet["percents"] == {"group": "50", "pair": "70"} and [b["lesson_type"] for b in sheet["groups"]] == ["group", "pair"]


# ============================================================================================
# coverage by payments
# ============================================================================================


def test_calendar_lessons_outside_the_paid_period_are_unpaid_and_can_be_included(world):
    m = previous_month()
    s0 = world.students[0]
    for n in range(1, 11):
        lesson(world, m.d(n), {s0: "present"})
    pay_calendar(world, s0, m.first, m.d(6))                       # the first six lessons are paid
    sheet = preview(world, m)
    line = line_of(sheet, s0)
    assert (line["counted"], line["unpaid"], line["amount"]) == (6, 4, FEE * 50 * 6 // 1200)
    assert [x["state"] for x in line["marks"]] == ["counted"] * 6 + ["unpaid"] * 4
    assert [w["code"] for w in line["warnings"]] == ["UNPAID_LESSONS"] and sheet["warnings"]["unpaid"] == 4
    assert teacher_row(overview(world, m), world.teacher)["warnings"]["unpaid"] == 4
    saved = save(world, m)
    assert line_of(saved, s0)["include_unpaid"] is False
    ticked = patch(world, saved, line_overrides=[{"membership_id": line["membership_id"], "include_unpaid": True}])
    tl = line_of(ticked, s0)
    assert tl["include_unpaid"] is True and tl["amount"] == FEE * 50 * 10 // 1200 and (tl["counted"], tl["unpaid"]) == (6, 4)
    assert ticked["revision"] == 2 and ticked["total_amount"] == tl["amount"]
    # a save with the tick from the start
    again = save(world, m, revision=2, line_overrides=[{"membership_id": line["membership_id"], "include_unpaid": True}])
    assert line_of(again, s0)["amount"] == FEE * 50 * 10 // 1200 and again["revision"] == 3


def test_a_lesson_before_the_first_payment_is_unpaid_and_a_voided_payment_covers_nothing(world):
    m = previous_month()
    s0, s1, _ = world.students
    for n in range(1, 4):
        lesson(world, m.d(n), {s0: "present", s1: "present"})
    pay_calendar(world, s0, m.d(2), m.last)
    pay_calendar(world, s1, m.first, m.last)
    world.db.query(StudentPayment).filter_by(student_id=s1.id).update(
        {"voided_at": datetime.utcnow(), "voided_by": world.owner.id, "void_reason": "ошибка"})
    world.db.commit()
    sheet = preview(world, m)
    assert [x["state"] for x in line_of(sheet, s0)["marks"]] == ["unpaid", "counted", "counted"]
    assert [x["state"] for x in line_of(sheet, s1)["marks"]] == ["unpaid"] * 3 and line_of(sheet, s1)["amount"] == 0


def test_a_package_gives_its_size_as_the_divisor(world):
    """8 lessons of an 8-lesson package: the teacher gets exactly p % of the price, not 8/12."""
    m = previous_month()
    s0, s1, _ = world.students
    for n in range(1, 9):
        lesson(world, m.d(n), {s0: "present", s1: "present"})
    pay_package(world, s0, m.first, 8, fee=480_000)
    pay_calendar(world, s1, m.first, m.last, fee=480_000)                      # the same fee, paid by months
    sheet = preview(world, m)
    pk, cal = line_of(sheet, s0), line_of(sheet, s1)
    assert (pk["counted"], pk["divisor"], pk["amount"]) == (8, 8, 240_000)
    assert (cal["counted"], cal["divisor"], cal["amount"]) == (8, 12, 160_000)
    assert {x["divisor"] for x in pk["marks"]} == {8} and {x["divisor"] for x in cal["marks"]} == {12}


def test_package_runs_out_the_rest_is_unpaid_and_the_status_page_agrees(world):
    """Parity: the lessons the payments page counts as consumed are exactly the covered ones."""
    m = previous_month()
    s0, s1, _ = world.students
    world.org.settings = {"payments": {"period_mode": "lessons", "lessons_per_payment": 5}}
    world.db.commit()
    for n in range(1, 9):
        lesson(world, m.d(n), {s0: "present", s1: "present"})
    held = lesson(world, m.d(9), {s0: "excused", s1: "present"})
    excuse(world, held, s0, "confirmed")                                        # not counted, not consumed
    pay_package(world, s0, m.first, 5)
    pay_package(world, s1, m.first, 5)
    pay_package(world, s1, m.d(7), 4)                                           # a second package, starts on the 7th
    page = {r["student_id"]: r for r in owner(world).get(f"{API}/org/payments/status", params={"limit": 50}).json()["items"]}
    sheet = preview(world, m)
    l0, l1 = line_of(sheet, s0), line_of(sheet, s1)
    # s0: 8 counted lessons, the package of 5 takes the first five
    assert (l0["counted"], l0["unpaid"]) == (5, 3)
    assert [x["state"] for x in l0["marks"]] == ["counted"] * 5 + ["unpaid"] * 3 + ["excused_confirmed"]
    # s1: the first package takes days 1-5, day 6 is before the second package starts, which takes 7, 8, 9
    assert [x["state"] for x in l1["marks"]] == ["counted"] * 5 + ["unpaid"] + ["counted"] * 3
    assert [x["divisor"] for x in l1["marks"]] == [5] * 5 + [4] + [4] * 3          # the unpaid one: size of his latest package
    assert (l1["counted"], l1["unpaid"]) == (8, 1)
    for line, student in ((l0, s0), (l1, s1)):
        assert page[str(student.id)]["lessons_used"] == line["counted"], student.first_name   # the page's number = ours
    assert l0["divisor"] == 5 and l0["amount"] == FEE * 50 * 5 // (100 * 5)         # five lessons of a 5-lesson package
    assert l1["amount"] == (5 * FEE * 50 // 500) + (3 * FEE * 50 // 400)             # 5 * 45 000 + 3 * 56 250


def test_an_included_unpaid_lesson_of_a_package_student_is_priced_by_his_package(world):
    m = previous_month()
    s0 = world.students[0]
    for n in range(1, 11):
        lesson(world, m.d(n), {s0: "present"})
    pay_package(world, s0, m.first, 8, fee=480_000)
    sheet = preview(world, m)
    line = line_of(sheet, s0)
    assert (line["counted"], line["unpaid"], line["amount"]) == (8, 2, 240_000)
    assert [x["divisor"] for x in line["marks"]] == [8] * 10 and line["divisor"] == 8
    saved = save(world, m)
    ticked = patch(world, saved, line_overrides=[{"membership_id": line["membership_id"], "include_unpaid": True}])
    assert line_of(ticked, s0)["amount"] == 10 * 480_000 * 50 // 800                    # ten lessons at 1/8 of the price
    # a student without any package: the centre's divisor
    s1 = world.students[1]
    for n in range(1, 4):
        lesson(world, m.d(n), {s1: "present"}, lesson_id=f"extra{n}")
    pay_calendar(world, s1, m.d(25), m.last)
    unpaid = line_of(preview(world, m), s1)
    assert unpaid["unpaid"] == 3 and {x["divisor"] for x in unpaid["marks"] if x["state"] == "unpaid"} == {12}


def test_calendar_and_package_payments_of_one_membership_both_cover(world):
    m = previous_month()
    s0 = world.students[0]
    for n in range(1, 9):
        lesson(world, m.d(n), {s0: "present"})
    pay_calendar(world, s0, m.first, m.d(4))                                     # days 1-4 by the month
    pay_package(world, s0, m.d(5), 4, fee=None)                                  # days 5-8 by a package (the later payment)
    line = line_of(preview(world, m), s0)
    assert [(x["state"], x["divisor"]) for x in line["marks"]] == [("counted", 12)] * 4 + [("counted", 4)] * 4
    assert line["divisor"] is None and line["unpaid"] == 0
    assert line["amount"] == (4 * FEE * 50 // 1200) + (4 * FEE * 50 // 400)       # 4 * 18 750 + 4 * 56 250


def test_no_fee_gives_zero_and_a_warning(world):
    m = previous_month()
    s0, s1, _ = world.students
    for n in range(1, 4):
        lesson(world, m.d(n), {s0: "present", s1: "present"})
    pay_calendar(world, s0, m.first, m.last, fee=None)
    pay_calendar(world, s1, m.first, m.last)
    sheet = preview(world, m)
    zero = line_of(sheet, s0)
    assert zero["monthly_fee"] is None and zero["amount"] == 0 and [w["code"] for w in zero["warnings"]] == ["NO_FEE"]
    assert sheet["warnings"]["no_fee"] == 1 and teacher_row(overview(world, m), world.teacher)["warnings"]["no_fee"] == 1
    saved = save(world, m, line_overrides=[{"membership_id": zero["membership_id"], "monthly_fee": 300_000}])
    fixed = line_of(saved, s0)
    assert fixed["monthly_fee"] == 300_000 and fixed["source_fee"] is None and fixed["fee_overridden"] is True
    assert fixed["amount"] == 300_000 * 50 * 3 // 1200 and fixed["warnings"] == []


# ============================================================================================
# transfers and carried-over payments (the real transfer code)
# ============================================================================================


from tests.test_payment_carry_over_pg import clock, transfer, two  # noqa: E402,F401


def test_transfer_in_the_middle_of_the_month_splits_the_lessons_and_the_payment_follows(two, monkeypatch):
    w, m = two, previous_month()
    s = w.student
    moved = m.d(15)
    pay_calendar(w, s, m.first, m.last, fee=FEE)
    for n in range(10, 15):
        lesson(w, m.d(n), {s: "present"})                                               # first group, teacher 1
    with clock(monkeypatch, moved, 10):
        transfer(w, w.second)
    for n in range(16, 21):
        lesson(w, m.d(n), {s: "present"}, group=w.second, teacher=w.teacher2)           # second group, teacher 2
    first = preview(w, m)
    second = preview(w, m, teacher=w.teacher2)
    a, b = line_of(first, s), line_of(second, s)
    assert a["group_name"] == "Группа" and b["group_name"] == "Вторая" and a["membership_id"] != b["membership_id"]
    assert (a["counted"], a["unpaid"], a["amount"]) == (5, 0, FEE * 50 * 5 // 1200)
    assert (b["counted"], b["unpaid"]) == (5, 0), b["marks"]                           # covered by the carry-over
    assert b["monthly_fee"] == FEE and b["amount"] == FEE * 50 * 5 // 1200            # the fee travelled with the student
    # the same student is in two lines of two teachers: nothing is counted twice
    assert a["membership_id"] != b["membership_id"] and len(first["lines"]) == 1 and len(second["lines"]) == 1


def test_a_lesson_after_the_transfer_in_the_old_group_is_not_the_old_payments(two, monkeypatch):
    """The old payment is cut at the transfer day; the carry-over covers the new group from there."""
    w, m = two, previous_month()
    s = w.student
    pay_calendar(w, s, m.first, m.d(20), fee=FEE)                                    # paid until the 20th
    with clock(monkeypatch, m.d(12), 10):
        transfer(w, w.second)
    lesson(w, m.d(14), {s: "present"}, group=w.second, teacher=w.teacher2)
    lesson(w, m.d(18), {s: "present"}, group=w.second, teacher=w.teacher2)
    lesson(w, m.d(22), {s: "present"}, group=w.second, teacher=w.teacher2)           # after the paid period
    b = line_of(preview(w, m, teacher=w.teacher2), s)
    assert [x["state"] for x in b["marks"]] == ["counted", "counted", "unpaid"]


def test_package_carry_over_keeps_the_size_of_the_original_package(two, monkeypatch):
    w, m = two, previous_month()
    s = w.student
    pay_package(w, s, m.first, 8, fee=480_000)
    for n in range(2, 5):
        lesson(w, m.d(n), {s: "present"})                                                # 3 lessons in group 1
    with clock(monkeypatch, m.d(10), 10):
        transfer(w, w.second)
    for n in range(12, 17):
        lesson(w, m.d(n), {s: "present"}, group=w.second, teacher=w.teacher2)           # 5 left: exactly the rest
    lesson(w, m.d(18), {s: "present"}, group=w.second, teacher=w.teacher2)              # the 9th lesson: no package
    a = line_of(preview(w, m), s)
    b = line_of(preview(w, m, teacher=w.teacher2), s)
    assert (a["counted"], a["divisor"], a["amount"]) == (3, 8, 3 * 480_000 * 50 // 800)
    assert (b["counted"], b["unpaid"], b["divisor"]) == (5, 1, 8), [x["state"] for x in b["marks"]]
    assert b["amount"] == 5 * 480_000 * 50 // 800
    # the whole package is paid out exactly once: 8 lessons * a lesson price * p %
    assert a["amount"] + b["amount"] == 480_000 * 50 // 100


# ============================================================================================
# the month page
# ============================================================================================


def test_the_month_page_lists_teachers_types_and_suggestions(world):
    m = previous_month()
    s0 = world.students[0]
    pay_calendar(world, s0, m.first, m.last)
    lesson(world, m.d(1), {s0: "present"})
    lesson(world, m.d(2), {s0: "present"}, teacher=world.teacher2)
    ov = overview(world, m)
    assert ov["month"] == m.label and ov["month_open"] is False and ov["divisor"] == 12
    assert [t["teacher_name"] for t in ov["teachers"]] == ["Мария Teacher", "Пётр Teacher"]       # by name
    row = teacher_row(ov, world.teacher)
    assert row["lesson_types"] == [{"lesson_type": "group", "groups": 1, "suggested_percent": None}]
    assert row["sheet"] is None
    assert {k: v for k, v in row["warnings"].items() if k != "open_lessons"} == {"pending_excused": 0, "no_fee": 0, "unpaid": 0}
    save(world, m, percents=[pct(world, "55.5")])
    ov = overview(world, m)
    row = teacher_row(ov, world.teacher)
    assert row["sheet"]["revision"] == 1 and row["sheet"]["stale"] is False and row["sheet"]["total_amount"] > 0
    assert row["lesson_types"][0]["suggested_percent"] == "55.5"
    # the suggestion is the last saved percent of THAT teacher for that kind; the other teacher has none
    assert teacher_row(ov, world.teacher2)["lesson_types"][0]["suggested_percent"] is None
    save(world, m, percents=[pct(world, "60")], revision=1)
    assert teacher_row(overview(world, m), world.teacher)["lesson_types"][0]["suggested_percent"] == "60"


def test_unfinished_lessons_are_counted_per_teacher_in_the_month(world):
    """The very list of «Незакрытые уроки»: a started lesson left open and a scheduled one never started."""
    m = previous_month()
    s0 = world.students[0]
    world.group.schedule = [{"id": "mon", "dayOfWeek": 1, "startTime": "10:00", "endTime": "11:00", "format": "offline"}]
    world.db.commit()
    pay_calendar(world, s0, m.first, m.last)
    lesson(world, m.d(1), {s0: "present"})
    started = lesson(world, m.d(3), {}, status="in_progress")
    started.started_at = datetime.utcnow()
    lesson(world, m.d(4), {}, status="in_progress", teacher=world.teacher2)
    world.db.commit()
    mondays = [n for n in range(1, m.last.day + 1) if m.d(n).isoweekday() == 1]
    never_started = len([n for n in mondays if n not in (1, 3, 4)])           # a Monday with no row at all
    ov = overview(world, m)
    assert teacher_row(ov, world.teacher)["warnings"]["open_lessons"] == 1 + never_started
    # a teacher that only has an unfinished lesson has nothing to be paid yet and is not listed
    assert all(t["teacher_id"] != str(world.teacher2.id) for t in ov["teachers"])
    assert preview(world, m)["warnings"]["open_lessons"] == 1 + never_started


def test_months_in_the_future_or_malformed_are_refused_and_the_current_month_is_a_preview(world):
    c = owner(world)
    for bad in ("2026-13", "2026-00", "26-09", "2026-9", "abcd-ef", "1999-01", "2026-09-01"):
        r = c.get(f"{API}/org/payroll/months/{bad}")
        assert r.status_code == 422 and r.json()["detail"]["code"] == "MONTH_INVALID", bad
    future = business_today().replace(day=1) + timedelta(days=40)
    r = c.get(f"{API}/org/payroll/months/{future:%Y-%m}")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "MONTH_INVALID"
    assert c.post(f"{API}/org/payroll/months/2026-99/calculate", json={"percents": []}).status_code == 422
    assert c.get(f"{API}/org/payroll/sheets", params={"month": "nope"}).status_code == 422
    cur = current_month()
    s0 = world.students[0]
    lesson(world, business_today() - timedelta(days=1) if business_today().day > 1 else business_today(), {s0: "present"})
    pay_calendar(world, s0, cur.first, cur.last)
    ov = overview(world, cur)
    assert ov["month_open"] is True
    sheet = preview(world, cur)
    assert sheet["month_open"] is True
    saved = save(world, cur)                                                  # saving an open month is allowed
    assert saved["month_open"] is True and saved["saved"] is True


# ============================================================================================
# sheets: saving, revisions, conflicts, the "data changed" banner
# ============================================================================================


def test_percent_is_required_before_anything_is_calculated_or_saved(world):
    m = previous_month()
    full_month(world, m, days=range(1, 4))
    c = owner(world)
    r = c.post(f"{API}/org/payroll/months/{m.label}/calculate", json={"percents": []})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "PERCENT_REQUIRED"
    r = c.post(f"{API}/org/payroll/sheets", json={"month": m.label, "teacher_id": str(world.teacher.id), "percents": []})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "PERCENT_REQUIRED"
    assert world.db.query(PayrollSheet).filter_by(org_id=world.org.id).count() == 0
    for bad in ("101", "-1", "12.345", "abc", None, True):
        r = c.post(f"{API}/org/payroll/months/{m.label}/calculate", json={"percents": [pct(world, bad)]})
        assert r.status_code == 422, bad
    # a number or a decimal string, either form of the list
    for value in (50, 50.5, "50.5", "50,5"):
        assert calc(world, m, [pct(world, value)])["sheets"][0]["percents"]["group"] in ("50", "50.5")
    both = c.post(f"{API}/org/payroll/months/{m.label}/calculate", json={"percents": [pct(world), pct(world, "60")]})
    assert both.status_code == 422                                           # the same pair twice
    # a teacher without lessons needs no percent and gets an empty sheet
    empty = calc(world, m, [], teacher_ids=[str(world.teacher2.id)])
    assert empty["sheets"][0]["lines"] == [] and empty["sheets"][0]["total_amount"] == 0
    assert c.post(f"{API}/org/payroll/months/{m.label}/calculate",
                  json={"percents": [], "teacher_ids": [str(uuid4())]}).status_code == 404


def test_calculate_writes_nothing(world):
    m = previous_month()
    full_month(world, m, days=range(1, 4))
    calc(world, m)
    world.db.expire_all()
    assert world.db.query(PayrollSheet).filter_by(org_id=world.org.id).count() == 0
    assert owner(world).get(f"{API}/org/payroll/sheets", params={"month": m.label}).json() == []
    assert owner(world).get(f"{API}/org/payroll/months/{m.label}/export.xlsx").status_code == 404      # nothing saved


def test_save_edit_history_and_conflicts(world):
    m = previous_month()
    s0 = world.students[0]
    full_month(world, m, days=range(1, 7))
    first = save(world, m, note="Первый расчёт")
    assert first["revision"] == 1 and first["saved"] and first["note"] == "Первый расчёт"
    sid = first["id"]
    # a second POST without the revision is a conflict (and so is a stale one) - nothing is overwritten
    r = owner(world).post(f"{API}/org/payroll/sheets", json={"month": m.label, "teacher_id": str(world.teacher.id), "percents": [pct(world)]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "SHEET_REVISION_CONFLICT" and r.json()["detail"]["current_revision"] == 1
    r = owner(world).post(f"{API}/org/payroll/sheets", json={"month": m.label, "teacher_id": str(world.teacher.id), "percents": [pct(world)], "revision": 7})
    assert r.status_code == 409
    # a fee, a percent: new revisions computed from the saved lines
    line = line_of(first, s0)
    second = patch(world, first, line_overrides=[{"membership_id": line["membership_id"], "monthly_fee": 300_000}], note="Скидка")
    assert second["revision"] == 2 and line_of(second, s0)["amount"] == 300_000 * 50 * 6 // 1200 and line_of(second, s0)["fee_overridden"]
    assert second["total_amount"] == sum(x["amount"] for x in second["lines"])
    third = patch(world, second, percents=[pct(world, "60")])
    assert third["revision"] == 3 and third["percents"] == {"group": "60"}
    assert line_of(third, s0)["amount"] == 300_000 * 60 * 6 // 1200 and line_of(third, s0)["monthly_fee"] == 300_000   # the override stays
    back = patch(world, third, line_overrides=[{"membership_id": line["membership_id"], "monthly_fee": None}])
    assert line_of(back, s0)["monthly_fee"] == FEE and line_of(back, s0)["fee_overridden"] is False and back["revision"] == 4
    # the revision protects against somebody else's change: a stale revision is a 409 and changes nothing
    r = owner(world).patch(f"{API}/org/payroll/sheets/{sid}", json={"revision": 2, "percents": [pct(world, "10")]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "SHEET_REVISION_CONFLICT" and r.json()["detail"]["current_revision"] == 4
    assert owner(world).get(f"{API}/org/payroll/sheets/{sid}").json()["percents"] == {"group": "60"}
    # history: newest first, with the author, the note and the total of each revision
    history = owner(world).get(f"{API}/org/payroll/sheets/{sid}/revisions").json()
    assert [h["revision"] for h in history] == [4, 3, 2, 1]
    assert history[3]["change_note"] == "Первый расчёт" and history[2]["change_note"] == "Скидка"
    assert history[3]["total_amount"] == first["total_amount"] and history[3]["percents"] == {"group": "50"}
    assert history[0]["changed_by"] == str(world.owner.id) and history[0]["changed_by_name"] == "Иван Owner" and history[0]["changed_at"]
    assert world.db.query(PayrollSheetRevision).filter_by(sheet_id=sid).count() == 4
    # nothing to change is not a revision
    r = owner(world).patch(f"{API}/org/payroll/sheets/{sid}", json={"revision": 4})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "NOTHING_TO_CHANGE"
    # unknown line / kind of lessons
    r = owner(world).patch(f"{API}/org/payroll/sheets/{sid}", json={"revision": 4, "line_overrides": [{"membership_id": str(uuid4()), "monthly_fee": 1}]})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "LINE_NOT_FOUND"
    r = owner(world).patch(f"{API}/org/payroll/sheets/{sid}", json={"revision": 4, "percents": [pct(world, "10", "pair")]})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "LESSON_TYPE_NOT_IN_SHEET"
    assert owner(world).get(f"{API}/org/payroll/sheets/{sid}").json()["revision"] == 4
    # the list and the single sheet
    listed = owner(world).get(f"{API}/org/payroll/sheets", params={"month": m.label}).json()
    assert [(x["id"], x["revision"], x["month"], x["teacher_name"]) for x in listed] == [(sid, 4, m.label, "Пётр Teacher")]
    assert listed[0]["stale"] is False and listed[0]["total_amount"] == back["total_amount"]
    assert len(owner(world).get(f"{API}/org/payroll/sheets").json()) == 1                     # no month: every month


def test_a_failed_first_save_leaves_nothing_behind_and_a_lost_race_is_a_409(world, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    from src.auth.payroll_service import PayrollService

    m = previous_month()
    full_month(world, m, days=range(1, 3))

    def boom(self, *a, **k):
        raise IntegrityError("insert", {}, Exception("duplicate key value violates unique constraint"))

    with monkeypatch.context() as patched:
        patched.setattr(PayrollService, "_add_revision", boom)
        r = owner(world).post(f"{API}/org/payroll/sheets", json={"month": m.label, "teacher_id": str(world.teacher.id), "percents": [pct(world)]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "SHEET_REVISION_CONFLICT"
    world.db.expire_all()
    assert world.db.query(PayrollSheet).filter_by(org_id=world.org.id).count() == 0
    assert save(world, m)["revision"] == 1


def test_revisions_are_unique_per_sheet_and_cascade(world):
    from sqlalchemy.exc import IntegrityError

    m = previous_month()
    full_month(world, m, days=range(1, 3))
    sheet = save(world, m)
    row = world.db.query(PayrollSheetRevision).filter_by(sheet_id=sheet["id"], revision=1).one()
    world.db.add(PayrollSheetRevision(sheet_id=row.sheet_id, revision=1, changed_by=world.owner.id, percent_inputs={}, lines=[], total_amount=0))
    with pytest.raises(IntegrityError):
        world.db.commit()
    world.db.rollback()
    world.db.query(PayrollSheet).filter_by(id=sheet["id"]).delete()
    world.db.commit()
    assert world.db.query(PayrollSheetRevision).filter_by(sheet_id=sheet["id"]).count() == 0


def test_a_saved_sheet_never_changes_by_itself_and_the_diff_says_what_would(world):
    m = previous_month()
    s0, s1, s2 = world.students
    full_month(world, m, days=range(1, 7))
    saved = save(world, m)
    before = {x["student_id"]: x["amount"] for x in saved["lines"]}
    fp = saved["source_fingerprint"]
    assert len(fp) == 64 and owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}").json()["stale"] is False
    clean = owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}/diff").json()
    assert clean["stale"] is False and clean["lines"] == [] and clean["delta_amount"] == 0 and clean["saved_total"] == clean["fresh_total"]

    # the attendance is corrected afterwards: a lesson of s0 is found, a mark of s1 becomes "excused"
    lesson(world, m.d(20), {s0: "present"})
    day2 = world.db.query(GroupAttendance).filter_by(group_id=world.group.id, lesson_date=m.d(2).isoformat()).one()
    day2.records = {str(s0.id): "present", str(s1.id): "excused", str(s2.id): "present"}
    world.db.commit()
    excuse(world, day2, s1, "confirmed")
    stale = owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}").json()
    assert stale["stale"] is True and stale["revision"] == 1
    assert {x["student_id"]: x["amount"] for x in stale["lines"]} == before                  # the sheet itself did not move
    ov_row = teacher_row(overview(world, m), world.teacher)
    assert ov_row["sheet"]["stale"] is True and ov_row["sheet"]["revision"] == 1
    assert owner(world).get(f"{API}/org/payroll/sheets", params={"month": m.label}).json()[0]["stale"] is True
    diff = owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}/diff").json()
    assert diff["stale"] is True and diff["revision"] == 1
    rows = {x["student_name"]: x for x in diff["lines"]}
    assert set(rows) == {"Каримова Алия", "Юсупов Бек"}                                       # Орлова is untouched
    a, b = rows["Каримова Алия"], rows["Юсупов Бек"]
    assert a["status"] == "changed" and a["saved"]["counted"] == 6 and a["fresh"]["counted"] == 7
    assert a["delta_amount"] == FEE * 50 // 1200 and "засчитано" in a["changes"]
    assert b["fresh"]["counted"] == 5 and b["delta_amount"] == -(FEE * 50 // 1200)
    assert diff["delta_amount"] == 0 and diff["fresh_total"] == diff["saved_total"]
    # the sheet is a snapshot: recalculating is an explicit new revision that keeps the owner's decisions
    line = line_of(saved, s2)
    keep = patch(world, saved, line_overrides=[{"membership_id": line["membership_id"], "monthly_fee": 100_000}])
    assert keep["stale"] is True and keep["source_fingerprint"] == fp                          # a fee change reads no data
    fresh = patch(world, keep, recalculate=True, note="Пересчёт")
    assert fresh["revision"] == 3 and fresh["stale"] is False and fresh["source_fingerprint"] != fp
    assert line_of(fresh, s0)["counted"] == 7 and line_of(fresh, s1)["counted"] == 5
    assert line_of(fresh, s2)["monthly_fee"] == 100_000 and line_of(fresh, s2)["fee_overridden"]      # the override survived
    assert owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}/diff").json()["lines"] == []


def test_the_fingerprint_is_stable_and_follows_the_inputs(world):
    m = previous_month()
    s0 = world.students[0]
    full_month(world, m, days=range(1, 4))
    a = preview(world, m)["source_fingerprint"]
    assert preview(world, m)["source_fingerprint"] == a                       # no state, no clock, no order
    assert preview(world, m, value="77")["source_fingerprint"] == a            # percents are not data
    s0.first_name = "Иная"
    world.db.commit()
    assert preview(world, m)["source_fingerprint"] == a                       # a name is not data
    set_fee(world, s0, FEE + 1)
    b = preview(world, m)["source_fingerprint"]
    assert b != a                                                             # a fee is
    lesson(world, m.d(9), {s0: "present"})
    c = preview(world, m)["source_fingerprint"]
    assert c not in (a, b)
    world.db.query(GroupAttendance).filter_by(lesson_date=m.d(9).isoformat()).update({"teacher_id": world.teacher2.id})
    world.db.commit()
    assert preview(world, m)["source_fingerprint"] == b                       # the lesson moved to another teacher
    assert owner(world).put(f"{API}/org/payroll/settings", json={"divisor": 11}).status_code == 200
    assert preview(world, m)["source_fingerprint"] != b                       # the divisor is data too


def test_a_fee_change_on_the_membership_after_saving_is_noticed_but_the_sheet_keeps_its_fee(world):
    m = previous_month()
    s0 = world.students[0]
    full_month(world, m, days=range(1, 4))
    saved = save(world, m)
    set_fee(world, s0, 500_000)
    now = owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}").json()
    assert now["stale"] is True and line_of(now, s0)["monthly_fee"] == FEE
    (row,) = [x for x in owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}/diff").json()["lines"] if x["student_name"] == "Каримова Алия"]
    assert row["saved"]["monthly_fee"] == FEE and row["fresh"]["monthly_fee"] == 500_000


# ============================================================================================
# permissions and tenancy
# ============================================================================================


def endpoints(w, sheet_id):
    m = previous_month()
    return [
        ("get", f"{API}/org/payroll/settings", None),
        ("put", f"{API}/org/payroll/settings", {"divisor": 12}),
        ("get", f"{API}/org/payroll/months/{m.label}", None),
        ("post", f"{API}/org/payroll/months/{m.label}/calculate", {"percents": []}),
        ("get", f"{API}/org/payroll/months/{m.label}/export.xlsx", None),
        ("post", f"{API}/org/payroll/sheets", {"month": m.label, "teacher_id": str(w.teacher.id), "percents": []}),
        ("get", f"{API}/org/payroll/sheets", None),
        ("get", f"{API}/org/payroll/sheets/{sheet_id}", None),
        ("patch", f"{API}/org/payroll/sheets/{sheet_id}", {"revision": 1, "note": "x"}),
        ("get", f"{API}/org/payroll/sheets/{sheet_id}/revisions", None),
        ("get", f"{API}/org/payroll/sheets/{sheet_id}/diff", None),
        ("get", f"{API}/org/payroll/sheets/{sheet_id}/export.xlsx", None),
    ]


def call(client, method, url, body):
    return getattr(client, method)(url, json=body) if body is not None else getattr(client, method)(url)


def test_an_admin_is_refused_even_with_the_right_to_payments_and_so_is_a_teacher(world):
    m = previous_month()
    full_month(world, m, days=range(1, 3))
    saved = save(world, m)
    # the owner gives the administrator the right to payments - payroll is not part of it
    assert owner(world).put(f"{API}/org/settings/payments", json={"admin_can_manage": True}).status_code == 200
    assert make_client(world, user=world.admin).get(f"{API}/org/payments/summary").status_code == 200
    for who in (world.admin, world.teacher):
        client = make_client(world, user=who)
        for method, url, body in endpoints(world, saved["id"]):
            r = call(client, method, url, body)
            assert r.status_code == 403, (who.role, method, url, r.text)
    assert owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}").status_code == 200
    # nothing was changed by the refused writes
    world.db.expire_all()
    assert world.db.query(PayrollSheet).filter_by(id=saved["id"]).one().revision == 1


def test_no_organisation_and_a_platform_account_are_refused(world):
    for client in (
        make_client(world, user=world.owner, org=None),                                  # a token without an organisation
        make_client(world, user=world.owner, org_role="admin"),                           # the live membership says admin
        make_client(world, user=world.owner, role="superadmin", org_role="superadmin"),   # superadmin passes the role gate
        make_client(world, user=world.owner, role="superadmin", org=None),                # ... even without an organisation
    ):
        for method, url, body in endpoints(world, uuid4()):
            r = call(client, method, url, body)
            assert r.status_code == 403, (method, url, r.status_code)


def test_another_centre_sees_nothing_and_gets_404(world):
    m = previous_month()
    full_month(world, m, days=range(1, 4))
    saved = save(world, m)
    foreign_owner = make_user(world.db, "owner")
    make_member(world.db, world.other_org, foreign_owner, "owner")
    world.db.commit()
    foreign = make_client(world, user=foreign_owner, org="other")
    base = f"{API}/org/payroll/sheets/{saved['id']}"
    for suffix in ("", "/revisions", "/diff", "/export.xlsx"):
        assert foreign.get(base + suffix).status_code == 404, suffix
    assert foreign.patch(base, json={"revision": 1, "note": "x"}).status_code == 404
    assert foreign.get(f"{API}/org/payroll/sheets", params={"month": m.label}).json() == []
    assert foreign.get(f"{API}/org/payroll/months/{m.label}").json()["teachers"] == []
    assert foreign.get(f"{API}/org/payroll/months/{m.label}/export.xlsx").status_code == 404
    # our teacher is not theirs, ours cannot be saved or previewed from there
    r = foreign.post(f"{API}/org/payroll/sheets", json={"month": m.label, "teacher_id": str(world.teacher.id), "percents": []})
    assert r.status_code == 404
    assert foreign.post(f"{API}/org/payroll/months/{m.label}/calculate", json={"percents": [], "teacher_ids": [str(world.teacher.id)]}).status_code == 404
    # their settings are theirs
    assert foreign.put(f"{API}/org/payroll/settings", json={"divisor": 20}).status_code == 200
    assert owner(world).get(f"{API}/org/payroll/settings").json()["divisor"] == 12
    # and the lessons of another centre never reach our sheet
    foreign_student = world.foreign_student
    lesson(world, m.d(1), {foreign_student: "present"}, group=world.foreign_group, teacher=world.foreign_admin)
    assert preview(world, m)["lines"] and all(x["student_id"] != str(foreign_student.id) for x in preview(world, m)["lines"])


# ============================================================================================
# Excel
# ============================================================================================


def workbook(response):
    from openpyxl import load_workbook

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert response.headers["content-disposition"].startswith("attachment;") and "no-store" in response.headers["cache-control"]
    return load_workbook(io.BytesIO(response.content))


def cells(ws):
    return [c for row in ws.iter_rows() for c in row if c.value is not None]


def test_excel_follows_the_partner_table_with_red_zeros_and_a_total(world):
    m = previous_month()
    s0, s1, s2 = world.students
    att = lesson(world, m.d(1), {s0: "excused", s1: "excused", s2: "present"})
    excuse(world, att, s0, "confirmed")
    excuse(world, att, s1, "pending")
    for n in (2, 3, 4):
        lesson(world, m.d(n), {s0: "present", s1: "present", s2: "present"})
    for s in world.students:
        pay_calendar(world, s, m.first, m.d(3))                                   # the 4th lesson is unpaid
    saved = save(world, m)
    r = owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}/export.xlsx")
    wb = workbook(r)
    assert wb.sheetnames == ["Пётр Teacher"]
    ws = wb.active
    values = {c.coordinate: c.value for c in cells(ws)}
    assert "Пётр Teacher" in values["A1"] and m.label.split("-")[0] in values["A1"]
    assert "Редакция 1" in values["A2"]
    header = [c.value for c in ws[5] if c.value is not None]
    assert header[:6] == ["Группа", "Тип занятий", "Время", "Ученик", "Оплата в месяц", "%"]
    assert header[6:10] == [f"{m.d(n):%d.%m}" for n in (1, 2, 3, 4)] and header[10:] == ["Засчитано", "Не оплачено", "Включено", "Начислено"]
    rows = {ws.cell(row=r_, column=4).value: r_ for r_ in range(6, ws.max_row + 1) if ws.cell(row=r_, column=4).value}
    karimova, yusupov, orlova = (rows[n] for n in ("Каримова Алия", "Юсупов Бек", "Орлова Дана"))
    # zero (a confirmed excuse) is red; the pending excuse is yellow text; the unpaid lesson is grey
    zero = ws.cell(row=karimova, column=7)
    assert zero.value == 0 and zero.font.color.rgb.endswith("C00000")
    pending = ws.cell(row=yusupov, column=7)
    assert pending.value == "Ув.?" and pending.fill.start_color.rgb.endswith("FFF2B3")
    unpaid = ws.cell(row=orlova, column=10)
    assert unpaid.value == 1 and unpaid.fill.start_color.rgb.endswith("E5E7EB")
    assert ws.cell(row=orlova, column=7).value == 1
    assert ws.cell(row=orlova, column=5).value == FEE and ws.cell(row=orlova, column=6).value == 50
    # only the zero in the date columns is red
    reds = [c for c in cells(ws) if c.value == 0 and c.font.color is not None and c.font.color.rgb.endswith("C00000")]
    assert len(reds) == 1
    # the totals: counted / unpaid / amount; the sheet's total
    total_row = max(r_ for r_ in range(6, ws.max_row + 1) if ws.cell(row=r_, column=1).value == "ИТОГО")
    assert ws.cell(row=total_row, column=14).value == saved["total_amount"]
    assert ws.cell(row=total_row, column=11).value == saved["counted_total"] and ws.cell(row=total_row, column=12).value == 3
    assert ws.cell(row=orlova, column=14).value == line_of(saved, s2)["amount"]
    assert any(ws.cell(row=r_, column=1).value == "Итого: Группа" for r_ in range(6, ws.max_row + 1))


def test_excel_of_a_month_has_a_sheet_per_saved_teacher(world):
    m = previous_month()
    s0 = world.students[0]
    pay_calendar(world, s0, m.first, m.last)
    lesson(world, m.d(1), {s0: "present"})
    lesson(world, m.d(2), {s0: "present"}, teacher=world.teacher2)
    save(world, m)
    only = workbook(owner(world).get(f"{API}/org/payroll/months/{m.label}/export.xlsx"))
    assert only.sheetnames == ["Пётр Teacher"]
    save(world, m, teacher=world.teacher2, percents=[pct(world, "40", teacher=world.teacher2)])
    both = owner(world).get(f"{API}/org/payroll/months/{m.label}/export.xlsx")
    wb = workbook(both)
    assert wb.sheetnames == ["Мария Teacher", "Пётр Teacher"]                      # by the name of the teacher
    assert "filename*=UTF-8''" in both.headers["content-disposition"] and ".xlsx" in both.headers["content-disposition"]


def test_excel_cells_from_names_are_never_formulas(world):
    m = previous_month()
    s0, s1, s2 = world.students
    s0.last_name, s0.first_name = "=HYPERLINK(\"http://evil\",\"x\")", "+1"
    s1.last_name, s1.first_name = "@SUM(1+1)", "-2"
    world.group.name = "=cmd|' /C calc'!A0"
    world.teacher.first_name, world.teacher.last_name = "=1+1", "x"
    world.db.commit()
    for n in (1, 2):
        lesson(world, m.d(n), {s0: "present", s1: "present", s2: "present"})
    for s in world.students:
        pay_calendar(world, s, m.first, m.last)
    saved = save(world, m, note="=2+2")
    ws = workbook(owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}/export.xlsx")).active
    for cell in cells(ws):
        assert cell.data_type != "f", (cell.coordinate, cell.value)
    dangerous = [c for c in cells(ws) if isinstance(c.value, str) and c.value.startswith("'") and c.value[1:2] in "=+-@"]
    assert {c.value[:8] for c in dangerous} >= {"'=HYPERL", "'@SUM(1+", "'=cmd|' "}, [c.value for c in dangerous]
    assert all(c.data_type == "s" for c in dangerous)
    assert ws.title == "=1+1 x"                                                   # a worksheet name is not evaluated
    note = [c.value for c in cells(ws) if isinstance(c.value, str) and "2+2" in c.value]
    assert note == ["Примечание: =2+2"]


def test_excel_helpers():
    from src.auth import payroll_export as px

    assert px.safe_text("=A1") == "'=A1" and px.safe_text("+1") == "'+1" and px.safe_text("-1") == "'-1"
    assert px.safe_text("a\x00b\x1fc\x0bd") == "abcd" and px.safe_text("\x00=1") == "'=1"            # control characters are dropped
    assert px.safe_text("@x") == "'@x" and px.safe_text("\t=1") == "'\t=1" and px.safe_text("Алия") == "Алия" and px.safe_text(None) == ""
    assert px.sheet_title("a/b:c*d?e[f]g\\h", []) == "a b c d e f g h"
    assert px.sheet_title("x" * 50, []) == "x" * 31
    assert px.sheet_title("Иван", ["иван"]) == "Иван (2)" and px.sheet_title("'quoted'", []) == "quoted"
    assert px.sheet_title(None, []) == "Преподаватель" and px.sheet_title("", ["Преподаватель"]) == "Преподаватель (2)"
    assert px.export_filename("2026-09", 'Иван "Эй"/\\') == "Зарплата 2026-09 Иван Эй.xlsx"


# ============================================================================================
# lessons_type of a group
# ============================================================================================


def test_group_lesson_type_is_stored_audited_and_returned(world):
    c = owner(world)
    listed = {g["id"]: g for g in c.get(f"{API}/org/groups").json()}
    assert listed[str(world.group.id)]["lesson_type"] == "group"                    # every old group is "group"
    r = c.post(f"{API}/org/groups", json={"name": "Парная", "class_level": 9, "teacher_id": str(world.teacher.id), "lesson_type": "pair"})
    assert r.status_code == 201 and r.json()["lesson_type"] == "pair"
    gid = r.json()["id"]
    assert c.post(f"{API}/org/groups", json={"name": "Без типа", "class_level": 9}).json()["lesson_type"] == "group"
    assert c.post(f"{API}/org/groups", json={"name": "Плохая", "lesson_type": "huge"}).status_code == 422
    created = world.db.query(GroupAudit).filter_by(group_id=gid, action="created").one()
    assert created.snapshot["lesson_type"] == "pair"
    # an administrator changes it: the change is in the audit log
    admin = make_client(world, user=world.admin)
    r = admin.put(f"{API}/org/groups/{gid}", json={"lesson_type": "individual"})
    assert r.status_code == 200 and r.json()["lesson_type"] == "individual"
    audit = world.db.query(GroupAudit).filter_by(group_id=gid, action="updated").one()
    assert audit.snapshot == {"lesson_type": ["pair", "individual"]} and audit.actor_id == world.admin.id
    assert admin.put(f"{API}/org/groups/{gid}", json={"lesson_type": "individual"}).status_code == 200
    assert world.db.query(GroupAudit).filter_by(group_id=gid, action="updated").count() == 1    # no change, no row
    assert admin.put(f"{API}/org/groups/{gid}", json={"lesson_type": "nope"}).status_code == 422
    assert admin.put(f"{API}/org/groups/{gid}", json={"name": "Другое имя"}).json()["lesson_type"] == "individual"   # untouched
    assert c.get(f"{API}/org/groups/{gid}").json()["lesson_type"] == "individual"


def test_a_teacher_cannot_pick_the_kind_of_lessons_that_sets_the_own_percent(world):
    teacher = make_client(world, user=world.teacher)
    r = teacher.post(f"{API}/org/groups", json={"name": "Моя", "class_level": 9, "lesson_type": "individual"})
    assert r.status_code == 403 and "администратор" in r.json()["detail"]
    r = teacher.post(f"{API}/org/groups", json={"name": "Моя", "class_level": 9})
    assert r.status_code == 201 and r.json()["lesson_type"] == "group"
    r = teacher.post(f"{API}/org/groups", json={"name": "Моя 2", "class_level": 9, "lesson_type": "group"})
    assert r.status_code == 201
    assert teacher.put(f"{API}/org/groups/{r.json()['id']}", json={"lesson_type": "pair"}).status_code == 403   # edit is for managers


# ============================================================================================
# queries do not grow with the size of the centre
# ============================================================================================


def count_queries(w, fn):
    from src.auth.service import AuthService  # noqa: F401

    engine = w.db.get_bind()
    n = {"queries": 0}

    def before(conn, cursor, statement, parameters, context, executemany):
        n["queries"] += 1

    event.listen(engine, "before_cursor_execute", before)
    try:
        result = fn()
    finally:
        event.remove(engine, "before_cursor_execute", before)
    return n["queries"], result


def build_centre(w, teachers, students_per, lessons=8):
    """``teachers`` teachers, each with a group of ``students_per`` students and ``lessons`` lessons."""
    m = previous_month()
    percents = []
    for t in range(teachers):
        teacher = make_user(w.db, "teacher", f"Учитель{t:02d}")
        make_member(w.db, w.org, teacher, "teacher")
        group = make_group(w.db, w.org, teacher, f"Группа {t:02d}")
        group.lesson_type = "group" if t % 2 else "pair"
        pupils = []
        for i in range(students_per):
            pupils.append(make_student(w.db, w.org, group, f"Имя{i:03d}", f"Фам{t:02d}{i:03d}"))
        w.db.flush()
        memberships = {x.student_id: x for x in w.db.query(StudentGroup).filter(StudentGroup.group_id == group.id)}
        for n in range(1, lessons + 1):
            w.db.add(GroupAttendance(
                org_id=w.org.id, group_id=group.id, lesson_date=m.d(n).isoformat(), lesson_id=f"s{m.d(n).isoweekday()}",
                status="completed", records={str(s.id): ("present" if (i + n) % 5 else "absent") for i, s in enumerate(pupils)},
                roster_snapshot=[{"student_id": str(s.id)} for s in pupils], teacher_id=teacher.id))
        for i, s in enumerate(pupils):
            membership = memberships[s.id]
            membership.monthly_fee = 300_000 + 1000 * i
            if i % 3 == 0:
                w.db.add(StudentPayment(org_id=w.org.id, membership_id=membership.id, student_id=s.id, group_id=group.id,
                                        amount=1, paid_on=m.first, valid_from=m.first, valid_to=m.last, created_by=w.owner.id))
            elif i % 3 == 1:
                w.db.add(StudentPayment(org_id=w.org.id, membership_id=membership.id, student_id=s.id, group_id=group.id,
                                        amount=1, paid_on=m.first, valid_from=m.first, lessons_count=6, created_by=w.owner.id))
        percents.append({"teacher_id": str(teacher.id), "lesson_type": group.lesson_type, "percent": "40"})
    w.db.commit()
    return m, percents


def test_a_month_of_ten_teachers_and_two_hundred_students_costs_a_fixed_number_of_queries(world):
    small_m, small_percents = build_centre(world, 2, 3)
    small_client = owner(world)
    q_small, small = count_queries(world, lambda: small_client.post(
        f"{API}/org/payroll/months/{small_m.label}/calculate", json={"percents": small_percents}))
    assert small.status_code == 200 and len(small.json()["sheets"]) == 2
    q_overview_small, _ = count_queries(world, lambda: small_client.get(f"{API}/org/payroll/months/{small_m.label}"))

    # a second centre, five times the teachers and ~ 30 times the students
    org = Organization(name="Big centre", status="active")
    world.db.add(org)
    world.db.flush()
    owner2 = make_user(world.db, "owner")
    make_member(world.db, org, owner2, "owner")
    world.db.commit()
    wb = SimpleNamespace(**{**vars(world), "org": org, "owner": owner2})
    m, percents = build_centre(wb, 10, 20)
    client = make_client(wb, user=owner2)
    q_big, resp = count_queries(world, lambda: client.post(f"{API}/org/payroll/months/{m.label}/calculate", json={"percents": percents}))
    assert resp.status_code == 200, resp.text
    sheets = resp.json()["sheets"]
    assert len(sheets) == 10 and sum(len(s["lines"]) for s in sheets) == 200
    assert all(s["total_amount"] > 0 for s in sheets)
    q_overview_big, ov = count_queries(world, lambda: client.get(f"{API}/org/payroll/months/{m.label}"))
    assert ov.status_code == 200 and len(ov.json()["teachers"]) == 10
    # bounded: the same number of queries for 2 teachers x 3 students and for 10 teachers x 20 students
    assert q_big == q_small and q_overview_big == q_overview_small, (q_small, q_big, q_overview_small, q_overview_big)
    assert q_big <= 45 and q_overview_big <= 45, (q_big, q_overview_big)
    # saving one sheet and opening it is bounded as well
    teacher_id = sheets[0]["teacher_id"]
    q_save, saved = count_queries(world, lambda: client.post(f"{API}/org/payroll/sheets", json={
        "month": m.label, "teacher_id": teacher_id, "percents": [p for p in percents if p["teacher_id"] == teacher_id]}))
    assert saved.status_code == 201 and q_save <= 60, q_save
