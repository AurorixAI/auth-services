#!/usr/bin/env python3
"""LOGIC-B2 contract fixtures: tests/fixtures/logicb2/*.json, the golden answers of the chains 1-9 of logicB2_spec.md (section 2) on the data set D2
(= D0 of package B + the group G4 «6-Г» with the teacher C, today = Wednesday 21.10.2026 14:00 Tashkent = 09:00 UTC).

Every number (minutes to the start, UTC instants, who is busy) is computed here by a naive calculator, not by the production code, and the headline figures are
asserted against the SPEC.  ``tests/test_ops_b2_chains_pg.py`` makes the production code reproduce these files.

    python scripts/logicb2_gen_fixtures.py          # rewrite tests/fixtures/logicb2
    python scripts/logicb2_gen_fixtures.py --check  # fail when the files on disk differ
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("SECRET_KEY", "ci-tests-only-secret-at-least-32-characters")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://x:x@localhost/x")  # never connected

_spec = importlib.util.spec_from_file_location("logicb_gen_fixtures", ROOT / "scripts" / "logicb_gen_fixtures.py")
B = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(B)

OUT = ROOT / "tests" / "fixtures" / "logicb2"
uid, short, IDS, LESSON, utc = B.uid, B.short, B.IDS, B.LESSON, B.utc
IDS.update({"G4": uid("group-G4"), "s7": uid("student-7")})
NOW = datetime(2026, 10, 21, 14, 0)          # Tashkent wall time
REASONS = {"sick": "Болезнь", "personal": "Личное", "other": "Другое"}
SCHEDULE = {  # lesson -> (group, weekday, start, end)
    "L1": ("G1", 1, "10:00", "11:30"), "L2": ("G1", 3, "16:00", "17:30"), "L3": ("G2", 2, "10:00", "11:00"),
    "L4": ("G3", 1, "10:00", "11:30"), "L5": ("G4", 4, "16:00", "17:30"),
}
GROUP_NAME = {"G1": "9-А", "G2": "7-Б", "G3": "8-В", "G4": "6-Г"}
STUDENTS = {"G1": 3, "G2": 2, "G3": 1, "G4": 1}
FILES: Dict[str, Any] = {}
MODEL: Dict[str, str] = {}


def put(name: str, model: str, content: Any) -> None:
    FILES[name], MODEL[name] = content, model


def iso(dt: datetime) -> str:
    return dt.isoformat()


def lesson_start(day: str, hhmm: str) -> datetime:
    """Planned start as a naive UTC instant (Tashkent = UTC+5)."""
    h, m = hhmm.split(":")
    return datetime.fromisoformat(day) + timedelta(hours=int(h), minutes=int(m)) - timedelta(hours=5)


def minutes_between(now_local: datetime, day: str, hhmm: str) -> int:
    return int((lesson_start(day, hhmm) - utc(now_local)).total_seconds() // 60)


def person(key: str) -> Dict[str, str]:
    return {"id": IDS[key], "name": short(key), "role": "owner" if key == "owner" else "admin" if key == "admin" else "teacher"}      # W1: the org role in the centre


CLOSED_OUTCOME = {"LESSON_OUTSIDE_WINDOW": "not_resolved_in_time", "LESSON_STARTED": "lesson_started", "LESSON_CANCELLED": "lesson_already_cancelled", "LESSON_REMOVED": "lesson_removed",
                  "GROUP_ARCHIVED": "group_archived", "GROUP_TEACHER_CHANGED": "group_teacher_changed", "TEACHER_REMOVED": "teacher_removed", "ROLE_CHANGED": "role_changed"}
CLOSING_TRIGGER = {"GROUP_ARCHIVED": "group_archived", "GROUP_TEACHER_CHANGED": "revalidate", "LESSON_OUTSIDE_WINDOW": "window_expired", "LESSON_REMOVED": "revalidate",
                   "LESSON_STARTED": "journal_row", "LESSON_CANCELLED": "journal_row", "TEACHER_REMOVED": "revalidate"}


def outcome_of(status: str, decision: Optional[Dict[str, Any]]) -> str:
    """#157 A11 by the naive rules of P157_API.md §2 (the chains here have no fate of a substitution and no day off)."""
    return CLOSED_OUTCOME[decision["closedReason"]] if status == "closed" else status


def settled(base: Dict[str, Any], **changes) -> Dict[str, Any]:
    """``base`` with the status / decision of a decided record, and the three derived fields of #157 recomputed from them."""
    d = dict(base, **changes)
    d.update(outcome=outcome_of(d["status"], d["decision"]), lastActivityAt=(d["decision"] or {}).get("at") or d["createdAt"],
             closedTrigger=CLOSING_TRIGGER.get((d["decision"] or {}).get("closedReason")) if d["status"] == "closed" else None)
    return d


def item(name: str, lesson: str, day: str, requester: str, now_local: datetime, *, reason="sick", comment=None, suggested: Optional[str] = None,
         status="pending", origin="teacher", decision=None, previous=None, room=None, batch: Optional[str] = None, suggested_active=True) -> Dict[str, Any]:
    group, _dow, start, end = SCHEDULE[lesson]
    minutes = minutes_between(now_local, day, start)
    pending = status == "pending"
    return {
        "id": uid("req-" + name), "batchId": uid("batch-" + (batch or name)), "status": status, "origin": origin,
        "group": {"id": IDS[group], "name": GROUP_NAME[group], "studentCount": STUDENTS[group]},
        "lesson": {"lessonId": LESSON[lesson], "date": day, "startTime": start, "endTime": end, "room": room, "format": "offline",
                   "startsAtUtc": iso(lesson_start(day, start)), "exists": True},
        "teacher": person(requester), "reasonCode": reason, "reasonLabel": REASONS[reason], "comment": comment,
        "suggestedTeacher": ({"id": IDS[suggested], "name": short(suggested), "active": suggested_active} if suggested else None),
        "createdAt": iso(utc(now_local)), "minutesToStart": minutes, "urgent": bool(pending and 0 < minutes < 1440),
        "overdue": bool(pending and minutes <= 0), "decision": decision, "previous": previous,
        "trigger": None, "clearedAt": None, "clearedBy": None, "clearedTo": None, "clearedTrigger": None,
        "outcome": outcome_of(status, decision), "closedTrigger": CLOSING_TRIGGER.get((decision or {}).get("closedReason")) if status == "closed" else None,
        "lastActivityAt": (decision or {}).get("at") or iso(utc(now_local)),      # #157: no event of these chains comes later than the decision (or the creation)
    }


def decision(kind: str, by: Optional[str], at_local: datetime, *, via="request", note=None, substitute: Optional[str] = None, moved=None,
             closed=None, closed_text=None) -> Dict[str, Any]:
    return {"kind": kind, "by": person(by) if by else None, "at": iso(utc(at_local)), "via": via, "note": note,
            "substitute": person(substitute) if substitute else None, "movedTo": moved, "closedReason": closed, "closedReasonText": closed_text}


def result(kind: str, **kw) -> Dict[str, Any]:
    base = {"kind": kind, "teacherId": None, "teacherName": None, "isSubstitution": None, "attendanceUpdated": None,
            "payrollSheetsAffected": None, "cancelledVersion": None, "newLesson": None, "version": None, "warnings": None, "cancellationKind": None}
    base.update(kw)
    return base


# ================================================================================================ chain 1
c1 = item("c1", "L1", "2026-10-26", "A", NOW, reason="sick", comment="Температура", suggested="D")
assert c1["minutesToStart"] == 6960 and c1["lesson"]["startsAtUtc"] == "2026-10-26T05:00:00" and not c1["urgent"]
put("b2c1_create.json", "CreateSubstitutionOut", {"batchId": c1["batchId"], "created": [c1], "alreadyRequested": [], "skipped": []})
put("b2c1_create_repeat.json", "CreateSubstitutionOut", {"batchId": c1["batchId"], "created": [], "alreadyRequested": [c1], "skipped": []})
put("b2c1_summary_pending.json", "SubstitutionSummary", {"pendingTotal": 1, "urgentTotal": 0, "overdueTotal": 0, "expiringTotal": 0})
cand = []
free = ["C"]           # W1 round 2: only a teacher leads a lesson (no owner, no admin among the candidates)
cand.append({"teacherId": IDS["D"], "name": short("D"), "orgRole": "teacher", "suggested": True, "isDefaultTeacher": False, "busy": False,
             "busyWith": None, "substitutePayEstimate": None, "substitutePayEstimateReason": "HIDDEN", "rateMissing": True})
for key in free:
    role = "owner" if key == "owner" else "admin" if key == "admin" else "teacher"
    cand.append({"teacherId": IDS[key], "name": short(key), "orgRole": role, "suggested": False, "isDefaultTeacher": False, "busy": False,
                 "busyWith": None, "substitutePayEstimate": None, "substitutePayEstimateReason": "HIDDEN", "rateMissing": key != "C"})
cand.append({"teacherId": IDS["B"], "name": short("B"), "orgRole": "teacher", "suggested": False, "isDefaultTeacher": False, "busy": True,
             "busyWith": {"kind": "lesson", "groupName": "8-В", "startTime": "10:00", "endTime": "11:30", "pendingRequest": False}, "substitutePayEstimate": None, "substitutePayEstimateReason": "HIDDEN", "rateMissing": False})
put("b2c1_candidates_admin.json", "CandidatesOut", {
    "requestId": c1["id"], "lesson": {"groupId": IDS["G1"], "groupName": "9-А", "lessonId": LESSON["L1"], "date": "2026-10-26",
                                      "startTime": "10:00", "endTime": "11:30"},
    "requesterId": IDS["A"], "candidates": cand})
DEC1 = datetime(2026, 10, 21, 14, 12)
c1_done = settled(c1, status="substituted", urgent=False, decision=decision("substitute", "admin", DEC1, substitute="D"),
               minutesToStart=minutes_between(DEC1, "2026-10-26", "10:00"))
put("b2c1_decide_substitute.json", "DecideOut", {"item": c1_done, "result": result(
    "substitute", teacherId=IDS["D"], teacherName=short("D"), isSubstitution=True, attendanceUpdated=False, payrollSheetsAffected=[])})
put("b2c1_summary_after.json", "SubstitutionSummary", {"pendingTotal": 0, "urgentTotal": 0, "overdueTotal": 0, "expiringTotal": 0})
put("b2c1_not_lesson_teacher.json", "-", B.error(403, "NOT_LESSON_TEACHER", f"Этот урок ведёт {short('D')}"))

# ================================================================================================ chain 2
NOW2 = datetime(2026, 10, 21, 14, 30)
c2 = item("c2", "L2", "2026-10-28", "A", NOW2, reason="personal")
assert c2["minutesToStart"] == 10170 and c2["lesson"]["startsAtUtc"] == "2026-10-28T11:00:00"        # SPEC says 10 530: a slip of its arithmetic (7 d 1 h 30 min)
put("b2c2_create.json", "CreateSubstitutionOut", {"batchId": c2["batchId"], "created": [c2], "alreadyRequested": [], "skipped": []})
# Tuesday 27.10 10:00-11:30 for the group G1: A already teaches L3 of 7-Б on Tuesdays 10:00-11:00
tue = [o for o in B.occurrences("G2", date(2026, 10, 27), date(2026, 10, 27))]
assert tue and tue[0][2:] == ("10:00", "11:00")
conf = {"date": "2026-10-27", "startTime": "10:00", "endTime": "11:30", "groupId": IDS["G1"], "groupName": "9-А", "withGroupName": "7-Б"}
put("b2c2_move_check_busy.json", "MoveCheckOut", {"ok": False, "teacherId": IDS["A"], "teacherName": short("A"), "teacherConflicts": [conf],
                                                  "groupConflicts": [], "warnings": []})
put("b2c2_move_check_ok.json", "MoveCheckOut", {"ok": True, "teacherId": IDS["A"], "teacherName": short("A"), "teacherConflicts": [], "groupConflicts": [], "warnings": []})
put("b2c2_decide_busy.json", "-", B.error(409, "TEACHER_BUSY", f"У преподавателя {short('A')} уже есть урок «7-Б» 2026-10-27 10:00–11:30", conflicts=[conf], total=1))
moved = {"lessonId": "one-off-1", "date": "2026-10-29", "startTime": "16:00", "endTime": "17:30"}
c2_done = settled(c2, status="rescheduled", decision=decision("reschedule", "admin", NOW2 + timedelta(minutes=5), note="Личные обстоятельства", moved=moved),
               minutesToStart=minutes_between(NOW2 + timedelta(minutes=5), "2026-10-28", "16:00"))
put("b2c2_decide_reschedule.json", "DecideOut", {"item": c2_done, "result": result(
    "reschedule", cancelledVersion=1, warnings=[], newLesson={"id": "one-off-1", "groupId": IDS["G1"], "date": "2026-10-29", "startTime": "16:00",
                                                                "endTime": "17:30", "room": None, "format": "offline"})})
put("b2c2_student_notices.json", "-", [
    {"lessonId": LESSON["L2"], "date": "2026-10-28", "groupId": IDS["G1"], "groupName": "9-А", "kind": "rescheduled",
     "movedTo": {"lessonId": "one-off-1", "date": "2026-10-29", "startTime": "16:00", "endTime": "17:30"}}])
put("b2c2_cancelled_row.json", "-", {"lesson_date": "2026-10-28", "status": "cancelled", "version": 1, "teacher_id": IDS["A"], "marked_by": IDS["admin"],
                                     "cancellation_reason": "Перенесён на 29.10, 16:00–17:30: Личные обстоятельства"})

# ================================================================================================ chain 3
c3 = item("c3", "L1", "2026-11-02", "A", NOW, reason="sick")
assert c3["minutesToStart"] == 17040
c3_done = settled(c3, status="cancelled", decision=decision("cancel", "admin", NOW + timedelta(minutes=2), note="Преподаватель болен, замены нет"),
               minutesToStart=minutes_between(NOW + timedelta(minutes=2), "2026-11-02", "10:00"))
put("b2c3_decide_cancel.json", "DecideOut", {"item": c3_done, "result": result("cancel", version=1, warnings=[], cancellationKind="centre")})
put("b2c3_cancelled_row.json", "-", {"lesson_date": "2026-11-02", "status": "cancelled", "version": 1, "records": {}, "teacher_id": IDS["A"],
                                     "marked_by": IDS["admin"], "cancellation_reason": "Преподаватель болен, замены нет"})

# ================================================================================================ chain 4
r4 = item("c4", "L1", "2026-10-26", "A", NOW)
r4_rejected = settled(r4, status="rejected", decision=decision("reject", "admin", NOW + timedelta(minutes=1), note="Замену не нашли"))
put("b2c4_already_decided_rejected.json", "-", B.error(409, "SUBSTITUTION_ALREADY_DECIDED", "Запрос уже решён", item=r4_rejected))
r4_withdrawn = settled(r4, status="withdrawn", decision=decision("withdraw", "A", NOW + timedelta(minutes=1), via="withdraw"))
put("b2c4_already_decided_withdrawn.json", "-", B.error(409, "SUBSTITUTION_ALREADY_DECIDED", "Учитель отозвал запрос", item=r4_withdrawn))

# ================================================================================================ chain 5
NOW5 = datetime(2026, 10, 21, 15, 40)
c5 = item("c5", "L2", "2026-10-21", "A", NOW5)
assert c5["minutesToStart"] == 20 and c5["urgent"]
put("b2c5_create_urgent.json", "CreateSubstitutionOut", {"batchId": c5["batchId"], "created": [c5], "alreadyRequested": [], "skipped": []})
c5_late = dict(c5, minutesToStart=minutes_between(datetime(2026, 10, 21, 16, 30), "2026-10-21", "16:00"), urgent=False, overdue=True)
assert c5_late["minutesToStart"] == -30
put("b2c5_item_overdue.json", "SubstitutionItem", c5_late)
put("b2c5_lesson_already_started.json", "-", B.error(409, "LESSON_ALREADY_STARTED", "Урок уже начался или прошёл. Обратитесь к администратору"))
put("b2c5_create_already_requested.json", "CreateSubstitutionOut", {"batchId": c5["batchId"], "created": [], "alreadyRequested": [c5_late], "skipped": []})
put("b2c5_summary_overdue.json", "SubstitutionSummary", {"pendingTotal": 1, "urgentTotal": 0, "overdueTotal": 1, "expiringTotal": 0})

# ================================================================================================ chain 6
c6a = item("c6a", "L1", "2026-11-02", "A", NOW, reason="sick")
DEL = datetime(2026, 10, 25, 10, 0)
c6a_done = settled(c6a, status="substituted", decision=decision("substitute", "admin", NOW + timedelta(minutes=5), substitute="D"),
                minutesToStart=minutes_between(NOW + timedelta(minutes=5), "2026-11-02", "10:00"), urgent=False)
c6b = item("c6b", "L1", "2026-11-02", "A", DEL, reason="other", origin="system", batch="c6b",
           comment="Заменяющий Рахимов Т. больше не работает в центре. Нужно новое решение.",
           previous={"id": c6a["id"], "status": "substituted", "teacherName": short("A"), "substituteName": short("D")})
assert c6b["minutesToStart"] == 11520
put("b2c6_reopened.json", "SubstitutionItem", c6b)
put("b2c6_removal_impact_pending.json", "-", {"pendingSubstitutionRequests": {"count": 1}})

# ================================================================================================ chain 7
lessons = []
for day, key in (("2026-10-19", "L1"), ("2026-10-20", "L3"), ("2026-10-21", "L2"), ("2026-10-26", "L1"), ("2026-10-27", "L3"), ("2026-10-28", "L2"), ("2026-11-02", "L1")):
    group, _dow, start, end = SCHEDULE[key]
    past = lesson_start(day, start) <= utc(NOW)
    lessons.append({"groupId": IDS[group], "groupName": GROUP_NAME[group], "lessonId": LESSON[key], "lessonDate": day, "startTime": start, "endTime": end,
                    "room": None, "format": "offline", "isSubstitution": False, "status": "passed" if past else "scheduled", "requestId": None,
                    "canRequest": not past, "blockReason": "LESSON_ALREADY_STARTED" if past else None})
assert [l["canRequest"] for l in lessons] == [False, False, True, True, True, True, True]
put("b2c7_my_lessons.json", "MyLessonsOut", {"dateFrom": "2026-10-19", "dateTo": "2026-11-02", "lessons": lessons})
c7_items = [item(f"c7-{l['lessonDate']}-{l['lessonId']}", k, l["lessonDate"], "A", NOW, reason="personal", comment="Конференция", batch="c7")
            for l, k in zip(lessons, ("L1", "L3", "L2", "L1", "L3", "L2", "L1")) if l["canRequest"]]
c7_skipped = [{"groupId": l["groupId"], "lessonId": l["lessonId"], "lessonDate": l["lessonDate"], "code": "LESSON_ALREADY_STARTED",
               "message": "Урок уже начался или прошёл. Обратитесь к администратору"} for l in lessons if not l["canRequest"]]
assert len(c7_items) == 5 and len(c7_skipped) == 2 and sum(1 for i in c7_items if i["lesson"]["date"].startswith("2026-10")) == 4
put("b2c7_create_batch.json", "CreateSubstitutionOut", {"batchId": uid("batch-c7"), "created": c7_items, "alreadyRequested": [], "skipped": c7_skipped})
put("b2c7_bulk_ok.json", "-", {"updated": 5, "skipped": []})

# ================================================================================================ chain 8
NOW8 = datetime(2026, 10, 21, 14, 20)
c8 = item("c8", "L1", "2026-10-26", "D", NOW8, reason="sick", comment="Сам заболел",
          previous={"id": c1["id"], "status": "substituted", "teacherName": short("A"), "substituteName": short("D")})
put("b2c8_create.json", "CreateSubstitutionOut", {"batchId": c8["batchId"], "created": [c8], "alreadyRequested": [], "skipped": []})
cand8 = [
    {"teacherId": IDS["A"], "name": short("A"), "orgRole": "teacher", "suggested": False, "isDefaultTeacher": True, "busy": False, "busyWith": None, "substitutePayEstimate": None, "substitutePayEstimateReason": "HIDDEN", "rateMissing": False},
    {"teacherId": IDS["C"], "name": short("C"), "orgRole": "teacher", "suggested": False, "isDefaultTeacher": False, "busy": False, "busyWith": None, "substitutePayEstimate": None, "substitutePayEstimateReason": "HIDDEN", "rateMissing": False},
    {"teacherId": IDS["B"], "name": short("B"), "orgRole": "teacher", "suggested": False, "isDefaultTeacher": False, "busy": True,
     "busyWith": {"kind": "lesson", "groupName": "8-В", "startTime": "10:00", "endTime": "11:30", "pendingRequest": False}, "substitutePayEstimate": None, "substitutePayEstimateReason": "HIDDEN", "rateMissing": False},
]
put("b2c8_candidates.json", "CandidatesOut", {"requestId": c8["id"], "lesson": {"groupId": IDS["G1"], "groupName": "9-А", "lessonId": LESSON["L1"], "date": "2026-10-26",
                                                                              "startTime": "10:00", "endTime": "11:30"},
                                               "requesterId": IDS["D"], "candidates": cand8})

# ================================================================================================ chain 9
put("b2c9_archive_warning.json", "-", {"code": "PENDING_SUBSTITUTIONS", "count": 1, "message": "Запросов на замену: 1 (будут закрыты)"})
c9 = item("c9", "L4", "2026-10-26", "B", NOW)
c9["group"]["studentCount"] = 0                       # s6 was transferred out of 8-В before the archive (an archive needs an empty group)
c9_closed = settled(c9, status="closed", decision=decision("closed", None, datetime(2026, 10, 22, 10, 0), via="system", closed="GROUP_ARCHIVED", closed_text="Группа закрыта"),
                 urgent=False, minutesToStart=minutes_between(datetime(2026, 10, 22, 10, 0), "2026-10-26", "10:00"))
put("b2c9_closed.json", "SubstitutionItem", c9_closed)

# ============================================================================================= misc shapes
put("b2x_summary.json", "SubstitutionSummary", {"pendingTotal": 3, "urgentTotal": 1, "overdueTotal": 0, "expiringTotal": 0})
put("b2x_list.json", "SubstitutionListOut", {"items": [c1], "total": 1, "pendingTotal": 1, "urgentTotal": 0, "expiringTotal": 0})
put("b2x_mine.json", "MineOut", {"items": [c1], "pendingTotal": 1, "assignedToMe": [
    {"groupId": IDS["G1"], "groupName": "9-А", "lessonId": LESSON["L1"], "lessonDate": "2026-10-26", "startTime": "10:00", "endTime": "11:30",
     "replacing": person("A"), "assignedAt": iso(utc(DEC1))}], "clearedForMe": []})
put("b2x_withdraw_batch.json", "WithdrawBatchOut", {"withdrawn": [settled(c1, status="withdrawn", decision=decision("withdraw", "A", NOW + timedelta(minutes=1), via="withdraw"))],
                                                    "skipped": [{"id": uid("req-x"), "code": "SUBSTITUTION_ALREADY_DECIDED", "message": "Запрос уже решён"}]})
put("b2x_detail.json", "SubstitutionDetailOut", {"item": c1, "chain": [], "next": [], "truncated": False, "events": [
    {"requestId": c1["id"], "action": "requested", "actor": person("A"), "at": iso(utc(NOW)), "snapshot": {"reasonCode": "sick"}, "mergedRequestId": None}], "examBinding": {"state": "none", "title": None, "canCancel": True}})
put("b2x_decide_bulk_busy.json", "DecideBulkOut", {"updated": 4, "items": [], "results": [], "warningsTotal": 0, "skipped": [
    {"id": uid("req-x"), "code": "TEACHER_BUSY", "message": "У преподавателя уже есть урок", "conflicts": [conf], "total": 1}]})
put("b2x_lesson_access.json", "LessonAccessOut", {"id": IDS["G1"], "org_id": IDS["org"], "name": "9-А", "class_level": 9, "teacher_id": IDS["A"],
                                                  "teacher_name": "Иван Иванов", "status": "active", "substitution": True,
                                                  "lessons": [{"lessonId": LESSON["L1"], "lessonDate": "2026-10-26"}]})
put("b2x_move_in_past.json", "-", B.error(422, "MOVE_IN_PAST", "Новое время урока уже прошло"))
put("b2x_group_busy.json", "-", B.error(409, "GROUP_BUSY", "У группы уже есть урок в это время", conflicts=[conf], total=1))
put("b2x_manager_assigns_directly.json", "-", B.error(403, "MANAGER_ASSIGNS_DIRECTLY", "Вы можете назначить замену сами: меню урока «Заменить преподавателя»"))


def render() -> Dict[str, str]:
    out = {n: json.dumps(c, ensure_ascii=False, indent=2, sort_keys=True) + "\n" for n, c in FILES.items()}
    out["index.json"] = json.dumps({n: MODEL[n] for n in sorted(FILES)}, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return out


def validate() -> None:
    from src.auth import substitution_schemas as S

    for name, model in MODEL.items():
        content = FILES[name]
        if model == "-":
            if isinstance(content, dict) and set(content) == {"status", "body"}:
                S.OpsErrorDetail.model_validate(content["body"]["detail"])
            continue
        parsed = getattr(S, model).model_validate(content)
        assert parsed.model_dump(mode="json") == content, f"{name} does not round-trip through {model}"


def main() -> int:
    validate()
    files = render()
    if "--check" in sys.argv:
        for name, text in files.items():
            path = OUT / name
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                print("differs:", name)
                return 1
        extra = {p.name for p in OUT.glob("*.json")} - set(files) - {"logicb2_contract.json", "matrix_ids.json"}
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
