#!/usr/bin/env python3
"""LOGIC-B contract fixtures: tests/fixtures/logicb/*.json, the golden answers of the chains 1-7 of the SPEC (section 2).

The data set D0 ("today" = Wednesday 21.10.2026, 14:00 Tashkent = 09:00 UTC) is written down here and every figure of a
fixture is computed by a deliberately naive calculator of this script (calendar walk, UTC+5 arithmetic), NOT by the
production code; the headline numbers are asserted against the figures written in the SPEC.  The production code must
reproduce these files (``tests/test_ops_chains_pg.py``); the front engineer builds mocks on the same files.

    python scripts/logicb_gen_fixtures.py          # rewrite tests/fixtures/logicb
    python scripts/logicb_gen_fixtures.py --check  # fail when the files on disk differ (CI / test)
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("SECRET_KEY", "ci-tests-only-secret-at-least-32-characters")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://x:x@localhost/x")  # never connected

OUT = ROOT / "tests" / "fixtures" / "logicb"
NS = uuid.UUID("3d1c7a52-8f0e-4b6a-9a51-5e0c2b7d4f10")  # the namespace of D0 ids


def uid(name: str) -> str:
    return str(uuid.uuid5(NS, name))


# ---------------------------------------------------------------------------------------------- D0 (SPEC 2)
NOW_LOCAL = datetime(2026, 10, 21, 14, 0)  # Wednesday, Tashkent
TZ_SHIFT = timedelta(hours=5)  # Tashkent = UTC+5, no DST


def utc(local: datetime) -> datetime:
    return local - TZ_SHIFT


assert NOW_LOCAL.isoweekday() == 3 and utc(NOW_LOCAL) == datetime(2026, 10, 21, 9, 0)

PEOPLE = {  # key: (first, last)
    "owner": ("Улугбек", "Усманов"),
    "admin": ("Нигора", "Каримова"),
    "A": ("Иван", "Иванов"),
    "B": ("Пётр", "Петров"),
    "C": ("Сергей", "Сидоров"),
    "D": ("Тимур", "Рахимов"),
}
IDS = {key: uid(f"user-{key}") for key in PEOPLE}
IDS.update({f"s{i}": uid(f"student-{i}") for i in range(1, 7)})
IDS.update({"org": uid("org"), "G1": uid("group-G1"), "G2": uid("group-G2"), "G3": uid("group-G3"), "G5": uid("group-G5")})
IDS.update({"member_A": uid("member-A"), "exam": "EX_123", "exam_E": "EX_9"})
LESSON = {"L1": "lesson-l1", "L2": "lesson-l2", "L3": "lesson-l3", "L4": "lesson-l4"}


def short(key: str) -> str:
    first, last = PEOPLE[key]
    return f"{last} {first[0]}."


GROUPS = {
    "G1": {"name": "9-А", "teacher": "A", "max": 3, "slots": {"L1": (1, "10:00", "11:30", "2026-10-12"), "L2": (3, "16:00", "17:30", "2026-10-14")}},
    "G2": {"name": "7-Б", "teacher": "A", "max": 30, "slots": {"L3": (2, "10:00", "11:00", "2026-10-13")}},
    "G3": {"name": "8-В", "teacher": "B", "max": 30, "slots": {"L4": (1, "10:00", "11:30", "2026-10-19")}},
}


def occurrences(group: str, start: date, end: date):
    """Naive calendar walk: every (date, lesson key, start, end) of the group's slots in [start, end]."""
    out = []
    day = start
    while day <= end:
        for key, (dow, st, en, valid_from) in GROUPS[group]["slots"].items():
            if day.isoweekday() == dow and day.isoformat() >= valid_from:
                out.append((day, key, st, en))
        day += timedelta(days=1)
    return out


def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


TODAY = NOW_LOCAL.date()
HORIZON = 56  # days looked ahead for the overlaps of a teacher and for the future lessons of a group

# ----------------------------------------------------------------------------------------------- helpers


def slot_json(key: str, group: str) -> Dict[str, Any]:
    dow, st, en, valid_from = GROUPS[group]["slots"][key]
    return {
        "id": LESSON[key], "dayOfWeek": dow, "startTime": st, "endTime": en, "subject": "Математика",
        "room": None, "format": "offline", "color": "blue", "topic": None, "validFrom": valid_from,
    }


def group_response(group: str, *, status="active", teacher=None, students=None, archived_at=None, reason=None,
                   extra=None) -> Dict[str, Any]:
    spec = GROUPS[group]
    teacher = teacher or spec["teacher"]
    out = {
        "id": IDS[group], "org_id": IDS["org"], "name": spec["name"], "class_level": 8 if group == "G3" else 9,
        "teacher_id": IDS[teacher] if teacher else None,
        "teacher_name": f"{PEOPLE[teacher][0]} {PEOPLE[teacher][1]}" if teacher else None,
        "description": None, "schedule": [slot_json(k, group) for k in spec["slots"]], "status": status,
        "max_students": spec["max"], "student_count": students if students is not None else 0, "lesson_type": "group",
        "created_at": "2026-09-01T00:00:00", "archivedAt": archived_at, "archiveReason": reason, "substitution": False,
    }
    out.update(extra or {})
    return out


def error(status: int, code: str, message: str, **extra) -> Dict[str, Any]:
    return {"status": status, "body": {"detail": {"code": code, "message": message, **extra}}}


EXCLUDE_NONE = {"ArchiveImpactOut", "VerifyStudentAsOfOut"}  # the routes that answer without the null keys (response_model_exclude_none)
FILES: Dict[str, Any] = {}   # file name -> content
MODEL: Dict[str, str] = {}   # file name -> response model of ops_schemas ("-" = not a model, a plain expectation)


def put(name: str, model: str, content: Any) -> None:
    FILES[name] = content
    MODEL[name] = model


# ================================================================================================ chain 1
# 1. removal-impact of A at 14:00 Wed 21.10
a_groups = []
for g in ("G1", "G2"):
    future = [o for o in occurrences(g, TODAY, TODAY + timedelta(days=14)) if minutes(o[2]) > NOW_LOCAL.hour * 60 + NOW_LOCAL.minute or o[0] > TODAY]
    nxt = future[0]
    a_groups.append({
        "id": IDS[g], "name": GROUPS[g]["name"], "studentCount": 3 if g == "G1" else 2,
        "nextLesson": {"date": nxt[0].isoformat(), "startTime": nxt[2]},
    })
assert a_groups[0]["nextLesson"] == {"date": "2026-10-21", "startTime": "16:00"}      # L2 today 16:00, not started
assert a_groups[1]["nextLesson"] == {"date": "2026-10-27", "startTime": "10:00"}      # L3 Tue 27.10
# the missed lessons of A: L3 on 20.10 (Tuesday), never started; G1 lessons are all held
missed_A = [o for o in occurrences("G2", date(2026, 9, 1), TODAY) if o[0] not in (date(2026, 10, 13),) and (o[0] < TODAY)]
assert [m[0].isoformat() for m in missed_A] == ["2026-10-20"]
a_groups.sort(key=lambda g: g["name"])  # removal-impact lists the groups by name
put("c1_removal_impact_A.json", "RemovalImpactOut", {
    "memberId": IDS["member_A"], "userId": IDS["A"], "name": short("A"), "orgRole": "teacher", "canRemove": False,
    "groups": a_groups, "inProgressLessons": [], "futureSubstitutions": {"count": 0},
    "missedLessons": {"count": len(missed_A)}, "pendingSubstitutionRequests": {"count": 0},
})

# 2. handover G1 -> B is refused: Mondays 10:00-11:30 of G1 (L1) and G3 (L4) overlap
window_end = TODAY + timedelta(days=HORIZON - 1)
l1 = [o for o in occurrences("G1", TODAY, window_end) if o[1] == "L1"]
l4 = [o for o in occurrences("G3", TODAY, window_end) if o[1] == "L4"]
conflicts = []
for d1, _, s1, e1 in l1:
    for d4, _, s4, e4 in l4:
        if d1 == d4 and minutes(s1) < minutes(e4) and minutes(e1) > minutes(s4):
            conflicts.append({"date": d1.isoformat(), "startTime": s1, "endTime": e1, "groupId": IDS["G1"],
                              "groupName": "9-А", "withGroupName": "8-В"})
assert len(conflicts) == 8 and conflicts[0]["date"] == "2026-10-26" and conflicts[-1]["date"] == "2026-12-14"
put("c1_handover_busy.json", "-", error(409, "TEACHER_BUSY", f"У преподавателя {short('B')} уже есть урок «8-В» 2026-10-26 10:00–11:30",
                                         conflicts=conflicts, total=len(conflicts)))

# 3. G1 -> C, G2 -> B: free; A is removed afterwards
tue = [o for o in occurrences("G2", TODAY, window_end)]
b_own = [o for o in occurrences("G3", TODAY, window_end)]
assert not any(a[0] == b[0] and minutes(a[2]) < minutes(b[3]) and minutes(a[3]) > minutes(b[2]) for a in tue for b in b_own)  # Tue 10:00 free
put("c1_handover_ok.json", "HandoverOut", {
    "handedOver": [
        {"groupId": IDS["G1"], "groupName": "9-А", "fromTeacherId": IDS["A"], "toTeacherId": IDS["C"]},
        {"groupId": IDS["G2"], "groupName": "7-Б", "fromTeacherId": IDS["A"], "toTeacherId": IDS["B"]},
    ],
    "remainingGroups": [], "releasedSubstitutions": 0, "closedSubstitutionRequests": 0,
})
put("c1_delete_member_A.json", "RemoveMemberOut", {"message": "Сотрудник удалён", "releasedSubstitutions": 0, "closedSubstitutionRequests": 0})
put("c1_group_audit_handover.json", "-", [  # group_audit rows written by the handover (09:00 UTC)
    {"group": "G1", "action": "teacher_changed", "created_at": utc(NOW_LOCAL).isoformat(),
     "snapshot": {"teacher_id": [IDS["A"], IDS["C"]], "via": "handover"}},
    {"group": "G2", "action": "teacher_changed", "created_at": utc(NOW_LOCAL).isoformat(),
     "snapshot": {"teacher_id": [IDS["A"], IDS["B"]], "via": "handover"}},
])
# 4. the admin reconciles L3 of 20.10: the teacher at the END of the lesson (11:00 TST = 06:00 UTC on 20.10) is A
L3_END_UTC = utc(datetime(2026, 10, 20, 11, 0))
HANDOVER_UTC = utc(NOW_LOCAL)
assert L3_END_UTC == datetime(2026, 10, 20, 6, 0) and L3_END_UTC < HANDOVER_UTC  # handover is later => still A
# 5. L2 of 21.10: the lesson ends 17:30 TST = 12:30 UTC, after the handover at 09:00 UTC => C
L2_END_UTC = utc(datetime(2026, 10, 21, 17, 30))
assert L2_END_UTC > HANDOVER_UTC
put("c1_attendance_rows.json", "-", {
    "L3_2026-10-20": {"teacher_id": IDS["A"], "marked_by": IDS["admin"], "started_at": None},
    "L2_2026-10-21": {"teacher_id": IDS["C"], "marked_by": IDS["C"]},
    "L1_2026-10-26": {"teacher_id": IDS["D"], "marked_by": IDS["D"]},
})
# 6. the substitution of L1 on 26.10: D
put("c1_lesson_teacher_L1_D.json", "LessonTeacherOut", {
    "groupId": IDS["G1"], "lessonId": LESSON["L1"], "lessonDate": "2026-10-26", "teacherId": IDS["D"],
    "teacherName": short("D"), "isSubstitution": True, "defaultTeacherId": IDS["C"], "defaultTeacherName": short("C"),
    "attendanceUpdated": False, "version": None, "payrollSheetsAffected": [], "requestClosed": None,
})
# 7. lessons_count of October for the payroll
completed = {  # (teacher, lesson) of every completed lesson of October after the chain
    "A": [("L1", "2026-10-12"), ("L1", "2026-10-19"), ("L2", "2026-10-14"), ("L3", "2026-10-13"), ("L3", "2026-10-20")],
    "C": [("L2", "2026-10-21")], "D": [("L1", "2026-10-26")], "B": [], "admin": [],
}
put("c1_expected_lessons_count.json", "-", {k: len(v) for k, v in completed.items()})
assert FILES["c1_expected_lessons_count.json"] == {"A": 5, "C": 1, "D": 1, "B": 0, "admin": 0}

# ================================================================================================ chain 2
ARCH_LOCAL = datetime(2026, 10, 22, 10, 0)
ARCH_UTC = utc(ARCH_LOCAL)
assert ARCH_UTC.isoformat() == "2026-10-22T05:00:00"
future_g3 = [o for o in occurrences("G3", ARCH_LOCAL.date(), ARCH_LOCAL.date() + timedelta(days=HORIZON - 1))
             if datetime.combine(o[0], datetime.min.time()) + timedelta(minutes=minutes(o[2])) >= ARCH_LOCAL]
missed_g3 = [o for o in occurrences("G3", date(2026, 9, 1), ARCH_LOCAL.date()) if o[0] < ARCH_LOCAL.date()]
assert [m[0].isoformat() for m in missed_g3] == ["2026-10-19"]
assert len(future_g3) == 8  # 26.10 ... 14.12
s6 = {"id": IDS["s6"], "name": "Каримов А.", "displayId": "AL09-004"}
put("c2_archive_impact.json", "ArchiveImpactOut", {
    "groupId": IDS["G3"], "groupName": "8-В", "status": "active", "canArchive": False, "examService": "ok",
    "blockers": [
        {"code": "ACTIVE_STUDENTS", "count": 1, "students": [s6]},
        {"code": "MISSED_LESSONS", "count": 1, "lessons": [
            {"lessonId": LESSON["L4"], "lessonDate": "2026-10-19", "startTime": "10:00", "endTime": "11:30", "teacherName": short("B")}]},
    ],
    "autoActions": [{"code": "RELEASE_EXAMS", "count": 1}, {"code": "STOP_FUTURE_LESSONS", "count": len(future_g3)}],
    "warnings": [], "legacyStudentsLeft": 0,
})
put("c2_archive_blocked.json", "-", error(409, "GROUP_ARCHIVE_BLOCKED", "Группу нельзя архивировать", blockers=[
    {"code": "ACTIVE_STUDENTS", "count": 1, "students": [s6]},
    {"code": "MISSED_LESSONS", "count": 1, "lessons": [
        {"lessonId": LESSON["L4"], "lessonDate": "2026-10-19", "startTime": "10:00", "endTime": "11:30", "teacherName": short("B")}]},
]))
put("c2_archive_ok.json", "ArchiveGroupOut", group_response(
    "G3", status="archived", students=0, archived_at=ARCH_UTC.isoformat(), reason="Группа закрыта",
    extra={"releasedExamIds": [IDS["exam_E"]], "cancelledMissedLessons": 1, "examReleasePending": False, "closedSubstitutionRequests": 0}))
put("c2_group_state_after_archive.json", "-", {
    "inactive_intervals": [{"from": ARCH_UTC.isoformat(), "to": None}],
    "cancelled_row": {"lesson_date": "2026-10-19", "status": "cancelled", "teacher_id": IDS["B"],
                      "cancellation_reason": "Группа закрыта: Группа закрыта"},
    "group_audit_action": "group_archived",
})
put("c2_start_archived.json", "-", error(409, "GROUP_ARCHIVED", "Группа в архиве: изменить её нельзя. Верните её из архива"))
RESTORE_LOCAL = datetime(2026, 11, 5, 14, 0)
RESTORE_UTC = utc(RESTORE_LOCAL)
put("c2_restore_teacher_inactive.json", "-", error(409, "TEACHER_INACTIVE", "Преподаватель группы больше не работает в центре", teacherName=short("B")))
put("c2_group_after_restore.json", "GroupResponseB", group_response("G3", teacher="C", students=0, extra={}))
put("c2_state_after_restore.json", "-", {
    "inactive_intervals": [{"from": ARCH_UTC.isoformat(), "to": RESTORE_UTC.isoformat()}],
    "no_occurrence_on": ["2026-10-26", "2026-11-02"], "occurrence_on": ["2026-11-09"],
})
for day in ("2026-10-26", "2026-11-02"):
    assert ARCH_UTC < utc(datetime.fromisoformat(day + "T10:00:00")) < RESTORE_UTC  # inside the interval: not generated
assert utc(datetime(2026, 11, 9, 10, 0)) > RESTORE_UTC

# ================================================================================================ chain 3
def skipped(row, code, message, student, group):
    return {"sheet": "Ученики", "row": row, "code": code, "message": message, "student": student, "group": group}


SKIPS = [
    skipped(2, "GROUP_FULL", "В группе «9-А» нет мест: лимит 3, занято 3", "Бобоев Али", "9-А"),
    skipped(5, "GROUP_FULL", "В группе «6-Д» нет мест: лимит 2, занято 2", "Султанова Нодира", "6-Д"),
    skipped(6, "IN_OTHER_GROUP", "Ученик уже есть в системе и состоит в группе «9-А». Перевод выполните вручную — при переводе переносятся оплаты", "Иванов Иван", "6-Д"),
    skipped(7, "GROUP_ARCHIVED", "Группа «5-В» в архиве. Верните её из архива или выберите другую", "Юсупов Тимур", "5-В"),
    skipped(8, "DUPLICATE_IN_FILE", "Строка повторяется в файле", "Каримова Лола", "6-Д"),
]
base_counts = {"teachers_new": 0, "teachers_existing": 0, "groups_new": 1, "groups_existing": 2, "students_new": 2,
               "students_existing": 1, "students_enrolled": 2, "students_unchanged": 0, "students_skipped": 5,
               "openings": 0}   # openings: LOGIC-A (opening balances written by the import; additive, 0 without columns G-I)
# students_existing = 1 (Иванов Иван, found in the system); the two new ones are r3, r4
put("c3_preview_teacher_not_teacher.json", "ImportPreviewResponseB", {
    "ok": False, "counts": {**base_counts, "students_new": 0, "students_enrolled": 0, "students_skipped": 0, "students_existing": 0, "groups_new": 0},
    "issues": [{"sheet": "Группы", "row": 4, "field": "Почта учителя", "level": "error", "code": "TEACHER_NOT_FOUND",
                "message": "Преподаватель nobody@algo.test не найден в системе. Добавьте его через интерфейс."}],
    "teachers_preview": [], "skipped": [], "students_preview": [],
    "groups_preview": [{"name": "9-А", "class_level": 9, "teacher_email": "", "action": "existing"},
                       {"name": "5-В", "class_level": 5, "teacher_email": "", "action": "existing"}],
})
put("c3_preview_ok.json", "ImportPreviewResponseB", {
    "ok": True, "counts": base_counts, "issues": [], "teachers_preview": [],
    "groups_preview": [{"name": "9-А", "class_level": 9, "teacher_email": "", "action": "existing"},
                       {"name": "5-В", "class_level": 5, "teacher_email": "", "action": "existing"},
                       {"name": "6-Д", "class_level": 6, "teacher_email": "TEACHER_EMAIL", "action": "create"}],
    "students_preview": [{"name": "Каримова Лола", "class_level": 6, "phone": "+998901112233", "group": "6-Д", "action": "create"},
                         {"name": "Рахимов Жасур", "class_level": 6, "phone": None, "group": "6-Д", "action": "create"}],
    "skipped": SKIPS,
})
put("c3_commit_ok.json", "ImportCommitResponseB", {
    "ok": True, "counts": base_counts, "issues": [], "credentials": [], "skipped": SKIPS,
    "created_groups": [{"id": uid("group-6D"), "name": "6-Д"}],
})
put("c3_commit_repeat.json", "ImportCommitResponseB", {
    "ok": True, "counts": {**base_counts, "groups_new": 0, "groups_existing": 3, "students_new": 0, "students_existing": 3,
                           "students_enrolled": 0, "students_unchanged": 2},
    "issues": [], "credentials": [], "created_groups": [],
    "skipped": sorted(
        [{"sheet": "Ученики", "row": 3, "code": "ALREADY_IN_GROUP", "message": "Уже в этой группе",
          "student": "Каримова Лола", "group": "6-Д"},
         {"sheet": "Ученики", "row": 4, "code": "ALREADY_IN_GROUP", "message": "Уже в этой группе",
          "student": "Рахимов Жасур", "group": "6-Д"}] + [dict(s) for s in SKIPS],
        key=lambda item: item["row"]),
})
FILES["c3_commit_repeat.json"]["counts"]["students_skipped"] = 5

# ================================================================================================ chain 4
put("c4_schedule_lesson_created.json", "ScheduleLessonResponseB", {
    "id": "lesson-ab12cd34ef", "groupId": IDS["G5"], "groupName": "10-А", "classLevel": 10, "subject": "Математика",
    "dayOfWeek": 1, "startTime": "10:00", "endTime": "11:30", "room": None, "format": "offline", "color": "blue",
    "topic": None, "validFrom": "2026-10-21", "warnings": [],
})
put("c4_reconcile_not_in_schedule.json", "-", {"status": 400, "body": {"detail": "Урок не принадлежит расписанию этой группы"}})
put("c4_not_lesson_teacher.json", "-", error(403, "NOT_LESSON_TEACHER", f"Этот урок ведёт {short('A')}"))
put("c4_lesson_too_old.json", "-", error(403, "LESSON_TOO_OLD", "Урок старше 7 дней — его закрывает администратор или владелец"))
assert date(2026, 10, 13) < TODAY - timedelta(days=7) and date(2026, 10, 14) >= TODAY - timedelta(days=7)

# ================================================================================================ chain 5
# DEVIATION from the SPEC text of chain 5: the existing rule "a lesson may be completed no earlier than 15 minutes before its end" (17:15 for a
# lesson that ends 17:30) stays, so the teacher completes at 17:15 and the exam (45 minutes) was started at 16:45: it still has 15 minutes left.
EXAM_START_LOCAL = datetime(2026, 10, 21, 16, 45)
EXAM_END_LOCAL = EXAM_START_LOCAL + timedelta(minutes=45)
CHECK_LOCAL = datetime(2026, 10, 21, 17, 15)
remaining = int((EXAM_END_LOCAL - CHECK_LOCAL).total_seconds())
assert remaining == 900 and utc(EXAM_END_LOCAL).isoformat() == "2026-10-21T12:30:00"
put("c5_live_exam_conflict.json", "-", error(409, "LESSON_HAS_LIVE_EXAM", "Экзамен «Контрольная №3» ещё идёт (осталось 15 мин). Он продолжится по своему таймеру и закроется сам.",
                                              exams=[{"examId": IDS["exam"], "title": "Контрольная №3", "status": "active", "remainingSeconds": remaining, "waitingCount": 0}]))
put("c5_save_completed_ack.json", "SaveAttendanceOut", {"ok": True, "saved": True, "version": 3, "examSync": {"released": [], "leftRunning": [IDS["exam"]], "warning": None}})
put("c5_save_lobby_released.json", "SaveAttendanceOut", {"ok": True, "saved": True, "version": 3, "examSync": {"released": [IDS["exam"]], "leftRunning": [], "warning": None}})
put("c5_completion_check.json", "CompletionCheckOut", {"ok": True, "exams": [{
    "examId": IDS["exam"], "title": "Контрольная №3", "status": "active", "deliveryMode": "online",
    "remainingSeconds": remaining, "waitingCount": 0, "endsAt": utc(EXAM_END_LOCAL).isoformat()}]})
put("c5_completed.json", "CompletedOut", {"ok": True, "released": [], "leftRunning": [IDS["exam"]]})
AS_OF = utc(EXAM_START_LOCAL)
assert AS_OF.isoformat() == "2026-10-21T11:45:00"
put("c5_verify_student_as_of.json", "VerifyStudentAsOfOut", {
    "enrolled": True, "group_name": "9-А",
    "student": {"id": IDS["s2"], "first_name": "Бек", "last_name": "Юсупов", "full_name": "Юсупов Бек", "display_id": "AL09-002", "class_level": 9}})
put("c5_group_archive_impact_exams.json", "GroupArchiveImpactOut", {"exams": [
    {"examId": IDS["exam_E"], "title": "Контрольная №4", "status": "lobby", "lessonDate": "2026-10-26", "canRelease": True, "blockerReason": None}]})
put("c5_group_archive_release.json", "GroupArchiveReleaseOut", {"ok": True, "releasedExamIds": [IDS["exam_E"]]})

# ================================================================================================ chain 6
INV_CREATED_UTC = utc(datetime(2026, 11, 10, 14, 0))
INV_EXPIRES = INV_CREATED_UTC + timedelta(days=7)
assert INV_EXPIRES.isoformat() == "2026-11-17T09:00:00"
put("c6_invitation_create.json", "InvitationOut", {
    "id": uid("invitation-A"), "code": "AB12CD34", "org_role": "teacher", "email": "a.ivanov@algo.test",
    "expires_at": INV_EXPIRES.isoformat() + "+00:00", "status": "pending", "created_at": None, "is_returning": True,
})
put("c6_invite_info_returning.json", "InviteInfoOut", {"org_name": "Алго Тест", "org_role": "teacher", "email": "a.ivanov@algo.test", "is_returning": True})
put("c6_invite_mismatch.json", "-", error(400, "INVITE_EMAIL_MISMATCH", "Это приглашение выдано для другого email"))
put("c6_after_accept.json", "-", {"user_id_unchanged": True, "user_is_active": True, "member_is_active": True,
                                  "member_audit_action": "reactivated_by_invite", "groups_returned": 0,
                                  "october_lessons_of_A": 5})
put("c6_members_removed.json", "MemberOut", {
    "id": IDS["A"], "email": "a.ivanov@algo.test", "first_name": "Иван", "last_name": "Иванов", "full_name": "Иванов Иван",
    "role": "teacher", "default_org_id": IDS["org"], "language": "ru", "is_active": False, "is_verified": False,
    "must_change_password": False, "created_at": "2026-09-01T00:00:00", "org_role": "teacher", "member_id": IDS["member_A"],
    "removed_at": utc(NOW_LOCAL).isoformat(),
})

# ================================================================================================ chain 7
L1_KEY = f"2026-10-26_{LESSON['L1']}"
put("c7_save_v2.json", "SaveAttendanceOut", {"ok": True, "saved": True, "version": 2, "examSync": None})
put("c7_conflict.json", "-", error(409, "ATTENDANCE_CONFLICT", "Отметки уже изменил(а) Рахимов Т. в 10:42", currentVersion=2,
                                    updatedBy={"id": IDS["D"], "name": short("D")}, updatedAt="2026-10-26T05:42:10",
                                    records={IDS["s1"]: "present"}))
put("c7_save_v3.json", "SaveAttendanceOut", {"ok": True, "saved": True, "version": 3, "examSync": None})
put("c7_complete_v4.json", "SaveAttendanceOut", {"ok": True, "saved": True, "version": 4, "examSync": {"released": [], "leftRunning": [], "warning": None}})
put("c7_correct_v5.json", "SaveAttendanceOut", {"ok": True, "saved": True, "version": 5, "examSync": None})
put("c7_journal_extras.json", "-", {
    "versions": {L1_KEY: 5},
    "lessonTeachers": {L1_KEY: {"teacherId": IDS["D"], "teacherName": short("D"), "isSubstitution": True,
                                "defaultTeacherId": IDS["C"], "defaultTeacherName": short("C")}},
})

# ===================================================================================== changed list items
put("x_open_action_requires_admin.json", "OpenAttendanceActionResponseB", {
    "actionType": "missed", "groupId": IDS["G2"], "groupName": "7-Б", "classLevel": 9, "lessonId": LESSON["L3"],
    "lessonDate": "2026-10-13", "subject": "Математика", "startTime": "10:00", "endTime": "11:00", "room": None,
    "format": "offline", "topic": None, "markedCount": 0, "rosterCount": 0, "remainingCount": 0, "teacherId": IDS["A"],
    "teacherName": "Иван Иванов", "version": None, "requiresAdmin": True, "ageDays": 8, "isSubstitution": False,
})
put("x_occurrence_substitution.json", "ScheduleOccurrenceResponseB", {
    "id": LESSON["L1"], "groupId": IDS["G1"], "groupName": "9-А", "classLevel": 9, "subject": "Математика", "dayOfWeek": 1,
    "startTime": "10:00", "endTime": "11:30", "room": None, "format": "offline", "color": "blue", "topic": None,
    "occurrenceDate": "2026-10-26", "isRescheduled": False, "originalStartTime": None, "originalEndTime": None,
    "originalRoom": None, "rescheduleReason": None, "isOneOff": False, "validFrom": "2026-10-12", "warnings": [],
    "teacherId": IDS["D"], "teacherName": short("D"), "isSubstitution": True, "defaultTeacherId": IDS["C"],
    "defaultTeacherName": short("C"), "substitutionRequest": None, "substitutionOutcome": None, "movedFrom": None,
})
put("x_student_no_group.json", "StudentResponseB", {
    "id": IDS["s6"], "org_id": IDS["org"], "first_name": "Азиз", "last_name": "Каримов", "full_name": "Каримов Азиз",
    "class_level": 8, "group_id": None, "student_number": None, "display_id": "AL09-004", "sequence_number": 4,
    "student_phone": "+998901112200", "comment": None, "status": "active", "created_at": "2026-09-01T00:00:00",
    "groups_count": 0, "current_group_id": None, "archived_at": None, "archive_reason": None, "archived_groups": None,
    "has_group": False, "status_view": "no_group",
})
put("x_org_stats.json", "OrgStatsOut", {
    "total_students": 6, "total_groups": 3, "total_members": 6, "active_diagnostics": 0, "completed_diagnostics": 0,
    "new_students": 0, "no_group_students": 1,
})
put("x_lesson_teacher_candidates.json", "LessonTeacherCandidatesOut", {
    "lessonId": LESSON["L1"], "lessonDate": "2026-10-26", "startTime": "10:00", "endTime": "11:30",
    "currentTeacherId": IDS["C"], "defaultTeacherId": IDS["C"],
    "candidates": [
        {"teacherId": IDS["D"], "name": short("D"), "busy": False, "busyWith": None},
        {"teacherId": IDS["B"], "name": short("B"), "busy": True, "busyWith": {"groupName": "8-В", "startTime": "10:00", "endTime": "11:30"}},
    ],
})
put("x_lesson_teacher_changed.json", "-", error(409, "LESSON_TEACHER_CHANGED", f"Преподаватель урока уже изменён: {short('D')} Обновите страницу",
                                                 currentTeacherId=IDS["D"], teacher_name=short("D")))
put("x_payroll_locked.json", "-", error(409, "PAYROLL_LOCKED", "Ведомость за этот месяц уже утверждена: смена преподавателя недоступна",
                                         sheets=[{"sheet_id": uid("sheet-A-2026-10"), "teacher_id": IDS["A"], "month": "2026-10", "status": "approved"}]))
put("x_member_has_assignments.json", "-", error(409, "MEMBER_HAS_ASSIGNMENTS", "У сотрудника есть группы или начатые уроки",
                                                 memberId=IDS["member_A"], canRemove=False, groups=a_groups,
                                                 inProgressLessons=[], futureSubstitutions={"count": 0}, missedLessons={"count": 1}))
put("x_no_students_start.json", "-", error(409, "NO_STUDENTS", "В группе нет учеников — урок начать нельзя. Добавьте учеников в группу."))
put("x_valid_from_past.json", "-", error(422, "VALID_FROM_PAST", "Дата начала не может быть в прошлом"))
put("x_duplicate_slot.json", "-", error(409, "DUPLICATE_SLOT", "Такой урок уже есть в расписании группы"))
put("x_email_required.json", "-", error(422, "EMAIL_REQUIRED", "Укажите email приглашаемого сотрудника"))
put("x_staff_role_required.json", "-", error(403, "STAFF_ROLE_REQUIRED", "Добавлять учеников может администратор центра"))
put("x_exam_service_unavailable.json", "-", error(503, "EXAM_SERVICE_UNAVAILABLE", "Не удалось проверить экзамены группы. Повторите попытку."))
put("x_import_in_progress.json", "-", error(409, "IMPORT_IN_PROGRESS", "Импорт уже выполняется. Подождите минуту"))


def render() -> Dict[str, str]:
    index = {name: MODEL[name] for name in sorted(FILES)}
    out = {name: json.dumps(FILES[name], ensure_ascii=False, indent=2, sort_keys=True) + "\n" for name in FILES}
    out["index.json"] = json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return out


def validate() -> None:
    """Every model fixture validates against the response model and round-trips; every error body against OpsErrorResponse."""
    from src.auth import ops_schemas

    for name, model in MODEL.items():
        content = FILES[name]
        if model == "-":
            if isinstance(content, dict) and set(content) == {"status", "body"} and isinstance(content["body"].get("detail"), dict):
                ops_schemas.OpsErrorResponse.model_validate(content["body"])
            continue
        parsed = getattr(ops_schemas, model).model_validate(content)
        dumped = parsed.model_dump(mode="json", exclude_none=model in EXCLUDE_NONE)
        assert dumped == content, f"{name} does not round-trip through {model}"


def main() -> int:
    validate()
    files = render()
    if "--check" in sys.argv:
        for name, text in files.items():
            path = OUT / name
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                print("differs:", name)
                return 1
        extra = {p.name for p in OUT.glob("*.json")} - set(files) - {"logicb_contract.json", "matrix_ids.json"}
        if extra:
            print("stale:", sorted(extra))
            return 1
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (OUT / name).write_text(text, encoding="utf-8")
    print(f"{len(files)} files -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
