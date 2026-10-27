"""P1.1: the teacher moves HIS OWN lesson ahead of time - one path with #124 (the administrator's move), the author is the teacher.  Spec: artifacts/p1/P1_1_TEACHER_MOVE_SPEC.md
(scenarios T1..T14 = the table 1..14).  World D2 of LOGIC-B2, today Wed 21.10.2026 14:00: A leads G1 (lesson-l1 Mon 10:00, lesson-l2 Wed 16:00) and G2 (lesson-l3 Tue 10:00),
B leads G3, C leads G4, D is a free colleague."""
import pytest
from sqlalchemy import func, text

from datetime import date, datetime

from src.auth.lesson_substitutions import moved_one_off_keys
from src.models.auth import GroupAttendance, GroupAudit, GroupScheduleOneOffLesson

from tests.b2_helpers import API, client, clock, code_of, decide, events_of, needs_pg, pg_session, requests_of, row_of  # noqa: F401  (fixtures)
from tests.ops_helpers import history_row
from tests.test_lesson_move_direct_pg import BODY, check, move, nothing_written  # noqa: F401
from tests.test_integration_ab_pg import member, money, pay, sheet_of
from tests.test_ops_b2_decide_pg import L, Sync, one, sync, w  # noqa: F401

pytestmark = needs_pg
MON = dict(key="L1", day="2026-10-26", newDate="2026-10-28", startTime="14:00", endTime="15:30", reason="Еду на олимпиаду")      # Mon 26.10 10:00 -> Wed 28.10 14:00


def occurrences(w, who, day):
    return client(w, who).get(f"{API}/org/schedule/occurrences", params={"date_from": day, "date_to": day}).json()


def journal(w, **params):
    return client(w, "admin").get(f"{API}/org/substitutions", params={"status": "decided", "dateFrom": "2026-10-01", "dateTo": "2026-12-31", **params}).json()["items"]


def money_rows(w):
    """Row counts of every table that holds money (ledger / payments / payroll): a move must not touch them."""
    names = [r[0] for r in w.db.execute(text("select table_name from information_schema.tables where table_schema = 'public' "
                                             "and (table_name like '%ledger%' or table_name like '%payment%' or table_name like '%payroll%') order by 1")).all()]
    return {n: w.db.execute(text(f'select count(*) from "{n}"')).scalar() for n in names}


# ------------------------------------------------------------------------------------------------ T1 / T14
def test_t1_the_teacher_moves_his_lesson_to_another_day(w, sync):
    """[1] 200; Monday disappears, Wednesday appears with the label; the journal says WHO moved; next Monday is untouched; the student sees the label."""
    r = move(w, "A", **MON)
    assert r.status_code == 200, r.text
    out = r.json()
    new = out["result"]["newLesson"]
    assert (new["date"], new["startTime"], new["endTime"]) == ("2026-10-28", "14:00", "15:30")
    item = out["item"]
    assert item["status"] == "rescheduled" and item["origin"] == "system" and item["teacher"]["id"] == str(w.A.id)
    assert item["decision"]["by"]["id"] == str(w.A.id) and item["decision"]["via"] == "teacher_move" and item["decision"]["note"] == MON["reason"]
    assert item["decision"]["movedTo"] == {"lessonId": new["id"], "date": "2026-10-28", "startTime": "14:00", "endTime": "15:30"}
    original = next(o for o in occurrences(w, "A", "2026-10-26") if o["id"] == "lesson-l1")
    assert original["substitutionOutcome"]["kind"] == "rescheduled"
    moved = [o for o in occurrences(w, "A", "2026-10-28") if o["id"] == new["id"]]
    assert len(moved) == 1 and moved[0]["movedFrom"] == {"lessonId": "lesson-l1", "date": "2026-10-26", "startTime": "10:00", "endTime": "11:30"}
    next_week = next(o for o in occurrences(w, "A", "2026-11-02") if o["id"] == "lesson-l1")
    assert (next_week["startTime"], next_week["endTime"], next_week["movedFrom"]) == ("10:00", "11:30", None) and next_week["substitutionOutcome"] is None
    (rec,) = requests_of(w)
    assert (rec.status, rec.origin, rec.requester_id, rec.decided_by, rec.decided_via) == ("rescheduled", "system", w.A.id, w.A.id, "teacher_move")
    requested, rescheduled = events_of(w, rec.id)
    assert (requested.action, requested.actor_id, requested.snapshot["trigger"]) == ("requested", w.A.id, "teacher_move")
    assert (rescheduled.action, rescheduled.actor_id, rescheduled.snapshot["via"]) == ("rescheduled", w.A.id, "teacher_move")
    listed = journal(w)
    assert [i["id"] for i in listed] == [item["id"]] and listed[0]["decision"]["via"] == "teacher_move" and listed[0]["decision"]["by"]["name"] == item["teacher"]["name"]
    notices = client(w, "admin").get(f"{API}/org/students/{w.s1.id}/attendance", params={"month": "2026-10"}).json()["notices"]
    assert any(n["kind"] == "rescheduled" and n["date"] == "2026-10-26" and n["movedTo"]["date"] == "2026-10-28" for n in notices)


def test_t14_a_move_neither_charges_nor_pays_twice(w, clock, sync):
    """[14] Computed, not assumed: packages of 12 (600 000), 30 % of the lesson to the teacher.  The move itself changes neither the package nor the payroll sheet; once the MOVED
    lesson is held, exactly ONE lesson is added to the package of each student and to the sheet of the teacher (15 000 x 3), on the NEW date; the original date counts nothing."""
    money(w, w.G1, 600_000, block={"period_mode": "lessons", "lessons_per_payment": 12, "billing_history": [{"from": "min", "period_mode": "lessons", "lessons_per_payment": 12}]})
    for student in (w.s1, w.s2, w.s3):
        pay(w, member(w, student, w.G1), 600_000, date(2026, 10, 1))
    before_money = money_rows(w)

    def package():
        r = client(w, "owner").get(f"{API}/org/payments/status", params={"limit": 100})
        assert r.status_code == 200, r.text
        return {i["student_id"]: (i["lessons_used"], i["lessons_left"], i["debt"]) for i in r.json()["items"] if i["student_id"] in {str(x.id) for x in (w.s1, w.s2, w.s3)}}

    def payroll():
        sheet = sheet_of(w, w.A)
        return sheet["total_amount"], sorted((m["date"], line["student_id"]) for line in sheet["lines"] for m in line["marks"])

    package_0, payroll_0 = package(), payroll()
    new = move(w, "A", **MON).json()["result"]["newLesson"]["id"]
    assert (package(), payroll()) == (package_0, payroll_0)                    # the move alone: neutral
    assert money_rows(w) == before_money
    row = row_of(w, w.G1, "lesson-l1", "2026-10-26")
    assert row.status == "cancelled" and row.teacher_id == w.A.id and row.marked_by == w.A.id
    assert moved_one_off_keys(w.db, [w.G1.id]) == {(str(w.G1.id), new)}

    clock.set("2026-10-28 14:05")
    package_1, payroll_1 = package(), payroll()                                # the day came, nobody has held the lesson yet
    teacher, base = client(w, "A"), f"{API}/org/groups/{w.G1.id}/attendance"
    assert teacher.post(base + "/start", json={"lessonDate": "2026-10-28", "lessonId": new}).status_code == 200
    clock.set("2026-10-28 15:25")
    marks = {str(x.id): "present" for x in (w.s1, w.s2, w.s3)}
    done = teacher.post(base, json={"lessonDate": "2026-10-28", "lessonId": new, "status": "completed", "records": marks, "expectedVersion": 1})
    assert done.status_code == 200, done.text
    package_2, (total_2, marks_2) = package(), payroll()
    for sid, used in package_1.items():
        assert package_2[sid][0] == used[0] + 1 and package_2[sid][1] == used[1] - 1 and package_2[sid][2] == used[2], sid          # one lesson of the package, no debt
    assert total_2 - payroll_1[0] == 3 * 15_000
    new_marks = sorted(set(marks_2) - set(payroll_1[1]))
    assert new_marks == sorted(("2026-10-28", str(x.id)) for x in (w.s1, w.s2, w.s3)) and not [m for m in marks_2 if m[0] == "2026-10-26"]
    assert money_rows(w) == before_money                                       # still no row of a ledger / payment / payroll table of its own
    assert w.db.query(GroupScheduleOneOffLesson).filter_by(group_id=w.G1.id).count() == 1
    assert w.db.query(func.count(GroupAttendance.id)).filter(GroupAttendance.group_id == w.G1.id, GroupAttendance.lesson_id == "lesson-l1",
                                                              GroupAttendance.lesson_date == "2026-10-26").scalar() == 1


# ------------------------------------------------------------------------------------------------ T2 / T3
def test_t2_today_at_another_time_two_hours_ahead(w, clock, sync):
    """[2] What used to be «only for today» is the same move with the date of today."""
    clock.set("2026-10-26 08:00")
    r = move(w, "A", **{**MON, "newDate": "2026-10-26", "startTime": "14:00", "endTime": "15:30"})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["newLesson"]["date"] == "2026-10-26" and r.json()["item"]["decision"]["via"] == "teacher_move"


def test_t3_sixty_minutes_before_the_start_the_teacher_is_late_the_administrator_is_not(w, clock, sync):
    """[3] 61 min: 200 would pass; 60 and 59 min: 409 MOVE_TOO_LATE (the live check says the same); the administrator at the same moment: 200."""
    target = {**MON, "newDate": "2026-10-26", "startTime": "14:00", "endTime": "15:30"}
    for now in ("2026-10-26 09:00", "2026-10-26 09:01"):
        clock.set(now)
        late = move(w, "A", **target)
        assert late.status_code == 409 and late.json()["detail"]["code"] == "MOVE_TOO_LATE", now
        assert "не позднее чем за 60 минут" in late.json()["detail"]["message"] and "Не смогу провести" in late.json()["detail"]["message"]
        probe = check(w, "A", key="L1", day="2026-10-26", newDate="2026-10-26", startTime="14:00", endTime="15:30").json()
        assert probe["ok"] is False and probe["code"] == "MOVE_TOO_LATE" and probe["message"] == late.json()["detail"]["message"]
        assert requests_of(w) == [] and row_of(w, w.G1, "lesson-l1", "2026-10-26") is None
    clock.set("2026-10-26 08:59")
    assert check(w, "A", key="L1", day="2026-10-26", newDate="2026-10-26", startTime="14:00", endTime="15:30").json()["ok"] is True
    clock.set("2026-10-26 09:01")
    by_admin = move(w, "admin", **target)
    assert by_admin.status_code == 200, by_admin.text
    assert by_admin.json()["item"]["decision"]["via"] == "request" and by_admin.json()["item"]["decision"]["by"]["id"] == str(w.admin.id)
    clock.set("2026-10-27 08:59")                                              # 61 minutes before lesson-l3 (Tue 10:00): the teacher's own move goes through
    ok = move(w, "A", key="L3", day="2026-10-27", newDate="2026-10-27", startTime="14:00", endTime="15:00", reason="Приём у врача")
    assert ok.status_code == 200, ok.text
    assert ok.json()["item"]["decision"]["via"] == "teacher_move" and ok.json()["item"]["decision"]["by"]["id"] == str(w.A.id)


# ------------------------------------------------------------------------------------------------ T4 / T5
def test_t4_a_substitute_of_one_lesson_and_the_replaced_teacher_cannot_move_it(w, sync):
    """[4] 403 NOT_LESSON_TEACHER for the substitute (and for the check); the teacher who asked to be replaced does not lead it any more either."""
    item = one(w, "A", "L2", "2026-10-28")
    assert decide(w, "admin", item["id"], decision="substitute", teacherId=str(w.D.id)).status_code == 200
    for who in ("D", "A"):
        r = move(w, who)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "NOT_LESSON_TEACHER", who
        assert r.json()["detail"]["message"] == "Перенести урок может преподаватель группы или администратор"
        assert check(w, who).status_code == 403
    assert [x.status for x in requests_of(w)] == ["substituted"] and w.db.query(GroupScheduleOneOffLesson).filter_by(org_id=w.org.id).count() == 0


def test_t5_the_group_of_a_colleague_or_of_another_centre(w, sync):
    """[5] A colleague's group: 403; a group of another centre (even for a teacher of it): 404, nothing written."""
    from tests.pg_world import make_client

    assert move(w, "A", key="L4", day="2026-10-26").status_code == 403           # G3 is B's
    assert move(w, "B").status_code == 403 and check(w, "B").status_code == 403    # B does not lead G1
    foreign = make_client(w, user=w.foreign_admin, org="other", org_role="teacher", role="teacher")
    g, lesson, _ = L(w, "L2", "2026-10-28")
    r = foreign.post(f"{API}/org/groups/{g.id}/lesson-move", json={"lessonId": lesson, "lessonDate": "2026-10-28", **BODY})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "GROUP_NOT_FOUND"
    ghost = client(w, "A").post(f"{API}/org/groups/{w.G1.id}/lesson-move", json={"lessonId": "nope", "lessonDate": "2026-10-28", **BODY})
    assert ghost.status_code == 404 and ghost.json()["detail"]["code"] == "LESSON_NOT_FOUND"
    nothing_written(w)


# ------------------------------------------------------------------------------------------------ T6 / T7 / T8
def test_t6_the_new_time_overlaps_a_lesson_of_the_group(w, sync):
    """[6] Mon 02.11 10:00-11:30 is lesson-l1 of the same group."""
    r = move(w, "A", key="L2", day="2026-10-28", newDate="2026-11-02", startTime="10:30", endTime="11:30", reason="Перенос")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "GROUP_BUSY"
    assert check(w, "A", newDate="2026-11-02", startTime="10:30", endTime="11:30").json()["groupConflicts"]
    nothing_written(w)


def test_t7_the_teacher_is_busy_in_another_group(w, sync):
    """[7] A teaches 7-Б on Tue 27.10 10:00-11:00."""
    r = move(w, "A", newDate="2026-10-27", startTime="10:00", endTime="11:30")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "TEACHER_BUSY"
    probe = check(w, "A", newDate="2026-10-27", startTime="10:00", endTime="11:30").json()
    assert probe["ok"] is False and probe["teacherConflicts"] and probe["teacherId"] == str(w.A.id)
    nothing_written(w)


def test_t8_a_busy_room_only_warns(w, sync):
    """[8] ROOM_BUSY: 200 + the warning (a one-off lesson of 8-В holds «каб 12» on Fri 30.10 12:00-13:00)."""
    w.db.add(GroupScheduleOneOffLesson(id=f"one-off-room-{w.salt}", org_id=w.org.id, group_id=w.G3.id, lesson_date="2026-10-30",
                                       payload={"dayOfWeek": 5, "startTime": "12:00", "endTime": "13:00", "subject": "Математика", "room": "каб 12", "format": "offline", "color": "blue"},
                                       created_by=w.admin.id))
    w.db.commit()
    r = move(w, "A", newDate="2026-10-30", startTime="12:00", endTime="13:00", room="каб 12")
    assert r.status_code == 200, r.text
    assert [x["code"] for x in r.json()["result"]["warnings"]] == ["ROOM_BUSY"] and r.json()["result"]["newLesson"]["room"] == "каб 12"


# ------------------------------------------------------------------------------------------------ T9 / T10
def test_t9_a_double_click_is_the_same_answer(w, sync):
    """[9] The same request again: 200, the same item and lesson, one record, one new lesson; another slot: 409 with the winner; another person repeating: 409."""
    first = move(w, "A", **MON)
    again = move(w, "A", **MON)
    assert again.status_code == 200 and again.json()["item"] == first.json()["item"] and again.json()["result"]["newLesson"] == first.json()["result"]["newLesson"]
    assert w.db.query(GroupScheduleOneOffLesson).filter_by(group_id=w.G1.id).count() == 1 and len(requests_of(w)) == 1
    other = move(w, "A", **{**MON, "newDate": "2026-10-29"})
    assert other.status_code == 409 and other.json()["detail"]["code"] == "SUBSTITUTION_ALREADY_DECIDED"
    assert other.json()["detail"]["item"]["decision"]["movedTo"]["date"] == "2026-10-28"
    assert move(w, "admin", **MON).status_code == 409                         # an administrator repeating is not «the same decision» of the teacher


def test_t9_a_repeat_inside_the_last_hour_is_still_the_same_answer(w, clock, sync):
    """[9] The double click that lands after the 60-minute line returns the answer of the first click, not MOVE_TOO_LATE."""
    clock.set("2026-10-26 08:00")
    target = {**MON, "newDate": "2026-10-26", "startTime": "14:00", "endTime": "15:30"}
    first = move(w, "A", **target)
    clock.set("2026-10-26 09:30")
    again = move(w, "A", **target)
    assert first.status_code == 200 and again.status_code == 200
    assert {**again.json()["item"], "minutesToStart": 0} == {**first.json()["item"], "minutesToStart": 0}              # only the countdown moved on


def test_t10_his_own_pending_request_is_closed_by_the_move(w, sync):
    """[10] «Не смогу провести» + «Перенести урок»: the request itself becomes ``rescheduled`` (author: the teacher), no system record; a request of somebody else: 409."""
    item = one(w, "A", "L2", "2026-10-28")
    r = move(w, "A")
    assert r.status_code == 200, r.text
    assert r.json()["item"]["id"] == item["id"] and r.json()["item"]["origin"] == "teacher" and r.json()["item"]["status"] == "rescheduled"
    (rec,) = requests_of(w)
    assert (rec.decided_by, rec.decided_via, rec.requester_id) == (w.A.id, "teacher_move", w.A.id)
    assert [(e.action, e.actor_id) for e in events_of(w, rec.id)] == [("requested", w.A.id), ("rescheduled", w.A.id)]
    assert move(w, "A").json()["item"] == r.json()["item"]                    # and the repeat is the same answer
    stranger = one(w, "A", "L1", "2026-11-02")
    rec2 = next(x for x in requests_of(w, "pending") if str(x.id) == stranger["id"])
    rec2.requester_id = w.B.id
    w.db.commit()
    blocked = move(w, "A", key="L1", day="2026-11-02", newDate="2026-11-03", startTime="14:00", endTime="15:00")
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "FOREIGN_REQUEST_PENDING"
    assert [x.status for x in requests_of(w, "pending")] == ["pending"] and row_of(w, w.G1, "lesson-l1", "2026-11-02") is None


# ------------------------------------------------------------------------------------------------ T11 / T12 / T13
def test_t11_a_started_or_cancelled_lesson(w, clock, sync):
    """[11] The planned start has passed / a journal row exists: 409 LESSON_ALREADY_STARTED (not «too late»); a cancelled lesson: 409 LESSON_CANCELLED."""
    clock.set("2026-10-26 10:30")
    r = move(w, "A", **{**MON, "newDate": "2026-10-27", "startTime": "10:00", "endTime": "11:30"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "LESSON_ALREADY_STARTED"
    assert check(w, "A", key="L1", day="2026-10-26", newDate="2026-10-27", startTime="10:00", endTime="11:30").json()["code"] == "LESSON_ALREADY_STARTED"
    clock.set("2026-10-21 14:00")
    w.db.add(GroupAttendance(org_id=w.org.id, group_id=w.G1.id, teacher_id=w.A.id, lesson_date="2026-10-28", lesson_id="lesson-l2", status="in_progress",
                             records={}, roster_snapshot=[], marked_by=w.A.id, version=1))
    w.db.commit()
    started = move(w, "A")
    assert started.status_code == 409 and started.json()["detail"]["code"] == "LESSON_ALREADY_STARTED"
    cancelled = one(w, "A", "L1", "2026-11-02")
    assert decide(w, "admin", cancelled["id"], decision="cancel", reason="Нет замены").status_code == 200
    r = move(w, "A", key="L1", day="2026-11-02")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "LESSON_CANCELLED"
    assert w.db.query(GroupScheduleOneOffLesson).filter_by(org_id=w.org.id).count() == 0


def test_t12_a_moved_lesson_is_moved_again(w, sync):
    """[12] The ORIGINAL (already ``rescheduled``): 409 SUBSTITUTION_ALREADY_DECIDED with the item and a plain text; the NEW lesson can be moved once more by its teacher."""
    first = move(w, "A").json()["result"]["newLesson"]
    again = move(w, "A", newDate="2026-10-30")
    assert again.status_code == 409 and again.json()["detail"]["code"] == "SUBSTITUTION_ALREADY_DECIDED" and again.json()["detail"]["message"] == "Запрос уже решён"
    assert again.json()["detail"]["item"]["decision"]["movedTo"]["lessonId"] == first["id"]
    r = client(w, "A").post(f"{API}/org/groups/{w.G1.id}/lesson-move", json={"lessonId": first["id"], "lessonDate": first["date"], "newDate": "2026-10-30", "startTime": "16:00",
                                                                             "endTime": "17:30", "reason": "Ещё раз"})
    assert r.status_code == 200, r.text
    assert r.json()["item"]["decision"]["via"] == "teacher_move" and r.json()["result"]["newLesson"]["date"] == "2026-10-30"
    assert len(journal(w)) == 2 and all(i["decision"]["by"]["id"] == str(w.A.id) for i in journal(w))
    assert check(w, "A").json()["code"] == "SUBSTITUTION_ALREADY_DECIDED"


def test_t13_an_archived_group(w, sync):
    """[13] 409 GROUP_ARCHIVED for the move and for the live check."""
    w.G1.status = "archived"
    w.db.commit()
    r = move(w, "A")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "GROUP_ARCHIVED"
    assert check(w, "A").json()["code"] == "GROUP_ARCHIVED"
    nothing_written(w)


# ------------------------------------------------------------------------------------------------ rights and the check
def test_the_check_of_the_teacher_equals_the_check_of_the_administrator(w, sync):
    """The live check is the same answer for the teacher of the lesson and for the administrator (the 60-minute line aside); nothing is written."""
    for params in ({}, {"newDate": "2026-10-27", "startTime": "10:00", "endTime": "11:30"}):
        mine, theirs = check(w, "A", **params), check(w, "admin", **params)
        assert mine.status_code == 200 and mine.json() == theirs.json(), params
    assert requests_of(w) == []


def test_only_staff_may_ask(w, sync):
    """A teacher that is not a member of the centre's staff roles does not reach the service: a student / parent token is refused by the role check (403)."""
    from tests.pg_world import make_client

    parent = make_client(w, user=w.A, org="own", org_role="student", role="student")
    g, lesson, _ = L(w, "L2", "2026-10-28")
    assert parent.post(f"{API}/org/groups/{g.id}/lesson-move", json={"lessonId": lesson, "lessonDate": "2026-10-28", **BODY}).status_code == 403
    assert parent.get(f"{API}/org/groups/{g.id}/lesson-move-check", params={"lessonId": lesson, "lessonDate": "2026-10-28", "newDate": "2026-10-29",
                                                                           "startTime": "16:00", "endTime": "17:30"}).status_code == 403
    nothing_written(w)


def test_the_old_one_day_endpoint_is_gone(w):
    """P1.1: «только на сегодня» is the same move with today's date; the old endpoint answers 410 for everybody and changes nothing."""
    for who in ("A", "admin"):
        r = client(w, who).post(f"{API}/org/schedule/lessons/lesson-l1/reschedule", json={"lessonDate": "2026-10-21", "reason": "Болезнь"})
        assert r.status_code == 410 and "Перенести урок" in r.json()["detail"], who
    nothing_written(w)


def test_the_administrators_move_keeps_its_own_via_and_trigger(w, sync):
    """The administrator's journal keeps ``via = request`` / ``manager_move`` for his own moves."""
    r = move(w, "admin", **MON)
    assert r.status_code == 200 and r.json()["item"]["decision"]["via"] == "request" and r.json()["item"]["decision"]["by"]["id"] == str(w.admin.id)
    (rec,) = requests_of(w)
    assert events_of(w, rec.id)[0].snapshot["trigger"] == "manager_move" and events_of(w, rec.id)[1].snapshot["via"] == "request"


# ------------------------------------------------------------------------------------------------ the live check says no more than the move would
EMPTY = dict(teacherId=None, teacherName=None, teacherConflicts=[], groupConflicts=[])


def refused(r, code):
    body = r.json()
    assert r.status_code == 200 and body["ok"] is False and body["code"] == code, body
    assert {k: body[k] for k in EMPTY} == EMPTY, body


def test_the_check_of_a_bad_slot_is_answered_before_anything_of_anybody_is_looked_up(w, sync):
    """A date in the past / a time that has passed today / too far ahead: ok=false + the code + EMPTY lists (the busy time of a day nobody may plan for is not told)."""
    refused(check(w, "A", newDate="2026-10-19", startTime="10:00", endTime="11:30"), "MOVE_IN_PAST")             # the past: the lessons of that Monday are not told
    refused(check(w, "A", newDate="2026-10-21", startTime="10:00", endTime="11:30"), "MOVE_IN_PAST")             # today, the time has passed
    refused(check(w, "A", newDate="2026-12-31", startTime="10:00", endTime="11:30"), "MOVE_TOO_FAR")
    refused(check(w, "A", newDate="2026-10-28", startTime="16:00", endTime="17:30"), "MOVE_SAME_SLOT")
    refused(check(w, "admin", newDate="2026-10-19", startTime="10:00", endTime="11:30"), "MOVE_IN_PAST")           # one list for the manager too: what the move would refuse
    assert check(w, "A", newDate="2026-10-29", startTime="16:00", endTime="17:30").json()["ok"] is True
    assert check(w, "A", newDate="2026-10-28", startTime="1", endTime="2").status_code == 422
    nothing_written(w)


def test_the_check_of_a_day_led_by_somebody_else_tells_nothing_of_him(w, sync):
    """The group changes its teacher on 01.11 (A -> C) and C is busy on Mon 09.11 10:00: A moving his lesson to that day is refused (NOT_LESSON_TEACHER, the same as the move),
    and the check carries neither the name of C nor his lessons."""
    w.db.add(GroupAudit(org_id=w.org.id, group_id=w.G1.id, actor_id=w.owner.id, actor_role="owner", action="teacher_changed", snapshot={"teacher_id": [str(w.A.id), str(w.C.id)]},
                        created_at=datetime(2026, 11, 1, 5, 0)))
    w.G1.teacher_id = w.C.id
    w.db.add(GroupScheduleOneOffLesson(id=f"one-off-c-{w.salt}", org_id=w.org.id, group_id=w.G4.id, lesson_date="2026-11-09",
                                       payload={"dayOfWeek": 1, "startTime": "10:00", "endTime": "11:30", "subject": "Математика", "room": None, "format": "offline", "color": "blue"},
                                       created_by=w.admin.id))
    w.db.commit()
    refused(check(w, "A", newDate="2026-11-09", startTime="10:00", endTime="11:30"), "NOT_LESSON_TEACHER")
    r = move(w, "A", newDate="2026-11-09", startTime="10:00", endTime="11:30")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "NOT_LESSON_TEACHER" and "conflicts" not in r.text
    nothing_written(w)
    seen = check(w, "admin", newDate="2026-11-09", startTime="10:00", endTime="11:30").json()           # the manager sees whose time it is
    assert seen["teacherId"] == str(w.C.id) and seen["teacherConflicts"]


def test_the_check_refuses_a_foreign_pending_request_like_the_move(w, sync):
    """A request of somebody else waits on the lesson (not reachable by the rules, forced here): the check says FOREIGN_REQUEST_PENDING, as POST does."""
    item = one(w, "A", "L2", "2026-10-28")
    rec = next(x for x in requests_of(w, "pending") if str(x.id) == item["id"])
    rec.requester_id = w.B.id
    w.db.commit()
    refused(check(w, "A"), "FOREIGN_REQUEST_PENDING")
    assert move(w, "A").json()["detail"]["code"] == "FOREIGN_REQUEST_PENDING"


def test_the_exam_binding_of_the_move_dialog_answers_the_teacher_only_for_his_lesson(w, sync):
    """The move dialog checks the exam of the lesson (the warning before a move): the teacher of the lesson gets the answer, a colleague 403, a manager as before."""
    g, lesson, _ = L(w, "L2", "2026-10-28")
    params = {"lessonId": lesson, "lessonDate": "2026-10-28"}
    own = client(w, "A").get(f"{API}/org/groups/{g.id}/lesson-exam-binding", params=params)
    assert own.status_code == 200 and own.json()["state"] in ("none", "ok", "unavailable")
    assert client(w, "B").get(f"{API}/org/groups/{g.id}/lesson-exam-binding", params=params).status_code == 403   # B does not lead G1
    nothing_written(w)
