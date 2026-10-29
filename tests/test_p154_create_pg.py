"""#154: the PURPOSE of a one-off lesson - creation, the rights, the refusals, the life of the pointer ``made_up_by`` (PostgreSQL).

Chains of the spec (artifacts/analysis/P154_SPEC.md §7): Ц2, Ц3, Ц4, Ц5, Ц6, Ц13, Ц14, Ц15 + the invariants 1, 5, 6 and the rights of §2.  The money of the chains is in ``test_p154_money_pg.py``.
The world is D2 of LOGIC-B2: Wednesday 21.10.2026 14:00; G1 «9-А» (teacher A) has Mon 10:00-11:30 ``lesson-l1`` and Wed 16:00-17:30 ``lesson-l2``; the lesson of Monday 26.10 is cancelled «по вине центра»."""
from datetime import datetime

import pytest

from src.models.auth import GroupAttendance, GroupAudit, GroupScheduleOneOffLesson
from tests.b2_helpers import API, build_d2, clock, needs_pg, pg_session, row_of, run_parallel  # noqa: F401  (fixtures)
from tests.ops_helpers import client, code_of, grant_money_right, new_group, new_slot
from tests.test_w1_cancel_pg import code, restore, sheet

pytestmark = needs_pg
ORIGIN, ORIGIN_DAY = "lesson-l1", "2026-10-26"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")                        # Wednesday
    w = build_d2(pg_session)
    grant_money_right(w)                                 # #159: the admin of #154 decides the money (the owner let him); the admin WITHOUT the right is in ``test_p159_money_pg.py``
    return w


def make(w, who, group, day, *, start="15:00", end="16:30", expect=201, **extra):
    r = client(w, who).post(f"{API}/org/schedule/occurrences", json={"groupId": str(group.id), "lessonDate": day, "startTime": start, "endTime": end, **extra})
    assert r.status_code == expect, r.text
    return r


def makeup(w, who, group, day, *, origin=ORIGIN, origin_day=ORIGIN_DAY, **kw):
    return make(w, who, group, day, purpose="makeup", madeUpLessonId=origin, madeUpDate=origin_day, **kw)


def cancel_origin(w, who="admin", lesson=ORIGIN, day=ORIGIN_DAY, **extra):
    return sheet(w, who, w.G1, lesson, day, **extra)


def one_off(w, lesson_id):
    w.db.expire_all()
    return w.db.query(GroupScheduleOneOffLesson).filter_by(id=lesson_id).one()


def pointer(w, lesson=ORIGIN, day=ORIGIN_DAY):
    row = row_of(w, w.G1, lesson, day)
    return row.made_up_by if row is not None else None


def audit(w, action):
    w.db.expire_all()
    return w.db.query(GroupAudit).filter_by(org_id=w.org.id, action=action).order_by(GroupAudit.created_at).all()


# ============================================================================================ the purpose is required, the money by role
def test_the_server_requires_the_purpose_of_a_manager_and_defaults_a_teacher_to_extra_wait(w):
    r = make(w, "admin", w.G1, "2026-10-29", expect=422)
    assert code(r) == "PURPOSE_REQUIRED"
    assert code(make(w, "owner", w.G1, "2026-10-29", purpose="moved", expect=422)) == "PURPOSE_INVALID"
    assert code(make(w, "owner", w.G1, "2026-10-29", purpose="other", expect=422)) == "PURPOSE_INVALID"
    # a teacher who sends nothing (an old tab, the API) = an additional lesson that asks to be paid (АД-10)
    body = make(w, "A", w.G1, "2026-10-29").json()
    row = one_off(w, body["id"])
    assert (row.purpose, row.money, row.made_up_lesson_id) == ("extra", "wait", None)
    assert (body["purpose"], body["money"], body["purposeLabel"], body["moneyLabel"]) == ("extra", "wait", "Дополнительный", "не решено")


def test_the_money_of_an_additional_lesson_by_role(w):
    # the teacher: wait (the default) or none; paid is the owner's
    assert code(make(w, "A", w.G1, "2026-10-29", purpose="extra", money="paid", expect=422)) == "MONEY_INVALID"
    assert one_off(w, make(w, "A", w.G1, "2026-10-29", purpose="extra", money="none").json()["id"]).money == "none"
    assert one_off(w, make(w, "A", w.G1, "2026-10-29", start="17:00", end="17:30", purpose="extra", money="wait").json()["id"]).money == "wait"
    # the owner / admin: none or paid, chosen (a radio, no default); wait is not theirs
    assert code(make(w, "admin", w.G1, "2026-10-30", purpose="extra", expect=422)) == "MONEY_REQUIRED"
    assert code(make(w, "admin", w.G1, "2026-10-30", purpose="extra", money="wait", expect=422)) == "MONEY_INVALID"
    paid = make(w, "admin", w.G1, "2026-10-30", purpose="extra", money="paid").json()
    assert (one_off(w, paid["id"]).money, paid["moneyLabel"]) == ("paid", "оплачивается")
    free = make(w, "owner", w.G1, "2026-10-30", start="17:00", end="17:30", purpose="extra", money="none").json()
    assert (one_off(w, free["id"]).money, free["moneyLabel"]) == ("none", "без оплаты")
    # an additional lesson names no lesson to make up; a make-up has no money of its own
    assert code(make(w, "admin", w.G1, "2026-10-31", purpose="extra", money="none", madeUpLessonId=ORIGIN, madeUpDate=ORIGIN_DAY, expect=422)) == "MAKEUP_TARGET_NOT_ALLOWED"
    assert code(make(w, "admin", w.G1, "2026-10-31", purpose="makeup", money="paid", madeUpLessonId=ORIGIN, madeUpDate=ORIGIN_DAY, expect=422)) == "MONEY_INVALID"
    assert code(make(w, "admin", w.G1, "2026-10-31", purpose="makeup", expect=422)) == "MAKEUP_TARGET_REQUIRED"


def test_c13_an_additional_lesson_of_a_group_without_a_teacher_is_refused_and_every_creation_writes_the_audit(w):
    nobody = new_group(w.db, w.org, w.ids, "G9", "Без преподавателя", None, [new_slot("lesson-n1", 2, "10:00", "11:00", "2026-10-13")])
    w.db.commit()
    r = make(w, "admin", nobody, "2026-10-29", purpose="extra", money="paid", expect=422)
    assert code(r) == "TEACHER_REQUIRED" and r.json()["detail"]["message"] == "Выберите преподавателя"
    assert w.db.query(GroupScheduleOneOffLesson).filter_by(group_id=nobody.id).count() == 0
    made = make(w, "admin", w.G1, "2026-10-29", purpose="extra", money="paid").json()
    (entry,) = audit(w, "one_off_created")
    assert (entry.group_id, entry.actor_id, entry.actor_role) == (w.G1.id, w.admin.id, "admin")
    assert entry.snapshot["lessonId"] == made["id"] and entry.snapshot["purpose"] == "extra" and entry.snapshot["money"] == "paid" and entry.snapshot["madeUp"] is None
    cancel_origin(w)
    mk = makeup(w, "A", w.G1, "2026-10-29", start="12:00", end="13:00").json()
    last = audit(w, "one_off_created")[-1]
    assert (last.actor_role, last.snapshot["purpose"], last.snapshot["madeUp"]) == ("teacher", "makeup", {"lessonId": ORIGIN, "date": ORIGIN_DAY}) and last.snapshot["lessonId"] == mk["id"]


# ============================================================================================ the make-up: creation and the pointer
def test_a_teacher_makes_up_a_cancelled_lesson_without_a_confirmation_and_the_pointer_is_set(w):
    cancel_origin(w)
    r = makeup(w, "A", w.G1, "2026-10-29", start="12:00", end="13:00", room=None)
    body = r.json()
    row = one_off(w, body["id"])
    assert (row.purpose, row.money, row.made_up_lesson_id, row.made_up_date, row.created_by) == ("makeup", None, ORIGIN, ORIGIN_DAY, w.A.id)
    assert pointer(w) == body["id"]
    assert (body["purpose"], body["purposeLabel"], body["money"], body["madeUpFor"]) == ("makeup", "Отработка за пн 26.10", None, {"lessonId": ORIGIN, "date": ORIGIN_DAY})
    assert body["isOneOff"] is True and body["createdBy"]["id"] == str(w.A.id)
    # the original says it is made up (the list of the schedule): «Отработан → чт 29.10»
    listed = client(w, "admin").get(f"{API}/org/schedule/occurrences", params={"date_from": "2026-10-26", "date_to": "2026-10-29"}).json()
    original = next(o for o in listed if o["id"] == ORIGIN and o["occurrenceDate"] == ORIGIN_DAY)
    assert original["madeUpBy"] == {"lessonId": body["id"], "date": "2026-10-29", "startTime": "12:00", "endTime": "13:00", "label": "Отработан → чт 29.10"}
    copy = next(o for o in listed if o["id"] == body["id"])
    assert (copy["purpose"], copy["purposeLabel"], copy["madeUpFor"]["date"]) == ("makeup", "Отработка за пн 26.10", ORIGIN_DAY)


def card_notices(w, who="admin", month="2026-10"):
    r = client(w, who).get(f"{API}/org/students/{w.s1.id}/attendance", params={"month": month})
    assert r.status_code == 200, r.text
    return {n["lessonId"] + "@" + n["date"]: n for n in r.json()["notices"]}


def test_the_card_of_a_student_says_where_the_cancelled_lesson_was_made_up_and_never_the_money(w):
    """Walk on stage 29.10 (D2): the notice of the cancelled original carries ``madeUpBy`` (the very marks of the schedule and the journal), nothing of the money.
    LESSONS 51 - every path of the answer: ``GET /org/students/{id}/attendance`` is owner / administrator only (a teacher 403, checked here); the notice has no
    ``money`` / ``moneyLabel`` / ``purpose`` / ``createdBy`` for either of them."""
    cancel_origin(w)
    cancel_origin(w, lesson="lesson-l2", day="2026-10-28")                                      # a cancelled lesson nobody made up
    key, other = f"{ORIGIN}@{ORIGIN_DAY}", "lesson-l2@2026-10-28"
    assert card_notices(w)[key]["madeUpBy"] is None and card_notices(w)[other]["madeUpBy"] is None
    body = makeup(w, "A", w.G1, "2026-10-29", start="12:00", end="13:00").json()
    expected = {"lessonId": body["id"], "date": "2026-10-29", "startTime": "12:00", "endTime": "13:00", "label": "Отработан → чт 29.10"}
    for who in ("admin", "owner"):
        notices = card_notices(w, who)
        assert notices[key]["madeUpBy"] == expected, who
        assert notices[other]["madeUpBy"] is None
        assert notices[key]["kind"] == "cancelled" and notices[key]["groupName"] == w.G1.name
        assert not {"money", "moneyLabel", "purpose", "purposeLabel", "madeUpFor", "createdBy"} & set(notices[key]), who
        assert "wait" not in str(notices[key]) and "none" not in str(notices[key].get("madeUpBy")) and "paid" not in str(notices[key])
    assert client(w, "A").get(f"{API}/org/students/{w.s1.id}/attendance", params={"month": "2026-10"}).status_code == 403
    # the same label as the schedule gives (one function, no second logic)
    listed = client(w, "admin").get(f"{API}/org/schedule/occurrences", params={"date_from": "2026-10-26", "date_to": "2026-10-29"}).json()
    assert next(o for o in listed if o["id"] == ORIGIN and o["occurrenceDate"] == ORIGIN_DAY)["madeUpBy"] == expected
    # the make-up is deleted: the original is free again, the card stops saying so
    sheet(w, "admin", w.G1, body["id"], "2026-10-29")
    assert card_notices(w)[key]["madeUpBy"] is None


def test_the_room_of_the_make_up_is_the_room_of_the_cancelled_lesson_unless_sent(w):
    from src.models.auth import Group

    group = w.db.get(Group, w.G1.id)
    group.schedule = [dict(s, room="Каб. 7") if s["id"] == ORIGIN else s for s in group.schedule]
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(group, "schedule")
    w.db.commit()
    cancel_origin(w)
    assert makeup(w, "admin", w.G1, "2026-10-29", start="12:00", end="13:00").json()["room"] == "Каб. 7"
    cancel_origin(w, lesson="lesson-l2", day="2026-10-28")
    assert makeup(w, "admin", w.G1, "2026-10-30", origin="lesson-l2", origin_day="2026-10-28", room="Каб. 3").json()["room"] == "Каб. 3"


def test_c15_what_cannot_be_made_up_comes_back_with_a_reason(w):
    refused = lambda day="2026-10-29", **kw: makeup(w, "admin", w.G1, day, start="12:00", end="13:00", expect=422, **kw).json()["detail"]
    # not cancelled at all / not in the schedule
    assert refused(origin="lesson-l2", origin_day="2026-10-28")["reasonCode"] == "NOT_CANCELLED"
    assert refused(origin="lesson-zzz", origin_day=ORIGIN_DAY)["reasonCode"] == "NOT_FOUND"
    # a holiday, an old cancellation without a kind
    cancel_origin(w, lesson="lesson-l2", day="2026-10-28", cancellationKind="holiday")
    d = refused(origin="lesson-l2", origin_day="2026-10-28")
    assert d["reasonCode"] == "HOLIDAY" and d["code"] == "MAKEUP_NOT_ALLOWED" and "праздник" in d["message"]
    cancel_origin(w, lesson="lesson-l2", day="2026-11-04")
    row = row_of(w, w.G1, "lesson-l2", "2026-11-04")
    row.cancellation_kind = None
    w.db.commit()
    assert refused(origin="lesson-l2", origin_day="2026-11-04")["reasonCode"] == "NO_KIND"
    # a one-off lesson (cancelled) is not made up: a new one is created
    extra = make(w, "admin", w.G1, "2026-10-30", start="09:00", end="09:30", purpose="extra", money="none").json()["id"]
    sheet(w, "admin", w.G1, extra, "2026-10-30")
    assert refused(origin=extra, origin_day="2026-10-30")["reasonCode"] == "ONE_OFF"
    # a moved lesson
    moved = client(w, "admin").post(f"{API}/org/groups/{w.G1.id}/lesson-move", json={"lessonId": "lesson-l1", "lessonDate": "2026-11-02", "newDate": "2026-11-03", "startTime": "13:00", "endTime": "14:30",
                                                                                      "reason": "Перенос"})
    assert moved.status_code == 200, moved.text
    assert refused(origin="lesson-l1", origin_day="2026-11-02")["reasonCode"] == "MOVED"


def test_c15_a_lesson_older_than_sixty_days_is_refused_and_a_foreign_group_is_not_reachable(w):
    old_group = new_group(w.db, w.org, w.ids, "G8", "Старая", w.A, [new_slot("lesson-o1", 1, "09:00", "10:00", "2026-06-01")])
    w.db.add(GroupAttendance(org_id=w.org.id, group_id=old_group.id, lesson_date="2026-08-17", lesson_id="lesson-o1", status="cancelled", records={}, roster_snapshot=[],
                             cancellation_kind="centre", cancellation_reason="Давно", version=1))
    w.db.commit()
    d = makeup(w, "admin", old_group, "2026-10-29", start="12:00", end="13:00", origin="lesson-o1", origin_day="2026-08-17", expect=422).json()["detail"]
    assert (d["code"], d["reasonCode"]) == ("MAKEUP_NOT_ALLOWED", "TOO_OLD") and "60" in d["message"]
    cancel_origin(w)
    # a teacher of ANOTHER group (B teaches G3) does not make up the lesson of G1; a foreign centre does not even find the group
    assert makeup(w, "B", w.G1, "2026-10-29", start="12:00", end="13:00", expect=403)
    assert makeup(w, "B", w.G3, "2026-10-29", start="12:00", end="13:00", expect=422).json()["detail"]["reasonCode"] == "NOT_FOUND"          # G3 has no such lesson
    assert client(w, "foreign_admin", org="other").post(f"{API}/org/schedule/occurrences", json={"groupId": str(w.G1.id), "lessonDate": "2026-10-29", "startTime": "12:00", "endTime": "13:00",
                                                                                                "purpose": "makeup", "madeUpLessonId": ORIGIN, "madeUpDate": ORIGIN_DAY}).status_code == 400


def test_the_slot_of_a_make_up_follows_the_rules_of_a_move(w):
    cancel_origin(w)
    # the past / more than 60 days ahead / the very slot of the cancelled lesson: the codes of a move
    assert code(makeup(w, "admin", w.G1, "2026-10-21", start="09:00", end="10:00", expect=422)) == "MOVE_IN_PAST"
    assert code(makeup(w, "admin", w.G1, "2027-01-05", start="12:00", end="13:00", expect=422)) == "MOVE_TOO_FAR"
    assert code(makeup(w, "admin", w.G1, ORIGIN_DAY, start="10:00", end="11:30", expect=422)) == "MOVE_SAME_SLOT"
    # the group has a lesson then (Wednesday 28.10 16:00-17:30): GROUP_BUSY, and nothing is written
    r = makeup(w, "admin", w.G1, "2026-10-28", start="16:30", end="17:00", expect=409)
    assert code(r) == "GROUP_BUSY" and r.json()["detail"]["conflicts"]
    assert pointer(w) is None and w.db.query(GroupScheduleOneOffLesson).filter_by(group_id=w.G1.id).count() == 0
    # the teacher is busy (A teaches G2 on Tuesday 27.10 10:00-11:00)
    assert code(makeup(w, "admin", w.G1, "2026-10-27", start="10:30", end="11:30", expect=409)) == "TEACHER_BUSY"
    # a student who has another lesson then is a warning, never a block: s6 (G3) is also a student of G1; G3 has a lesson on Friday 30.10 12:00-13:00
    from src.models.auth import StudentGroup
    from tests.ops_helpers import open_period

    membership = StudentGroup(student_id=w.s6.id, group_id=w.G1.id)
    open_period(membership, datetime(2026, 9, 1, 5, 0))
    w.db.add(membership)
    w.db.commit()
    make(w, "admin", w.G3, "2026-10-30", start="12:00", end="13:00", purpose="extra", money="none")
    ok = makeup(w, "admin", w.G1, "2026-10-30", start="12:30", end="13:30")
    assert [x["code"] for x in ok.json()["warnings"]] == ["STUDENT_BUSY"]


def test_the_series_end_blocks_a_make_up_after_it(w):
    cancel_origin(w)
    r = client(w, "admin").post(f"{API}/org/schedule/lessons/{ORIGIN}/recurrence/end", json={"effectiveFrom": "2026-11-09", "reason": "Прекращаем"})
    assert r.status_code == 200, r.text
    assert code(makeup(w, "admin", w.G1, "2026-11-10", start="12:00", end="13:00", expect=422)) == "SERIES_ENDED"
    assert makeup(w, "admin", w.G1, "2026-11-04", start="12:00", end="13:00").status_code == 201


# ============================================================================================ Ц6: two at once
def test_c6_two_simultaneous_make_ups_of_one_lesson_give_one_lesson_and_one_409(w):
    cancel_origin(w)
    from src.auth.ops_schemas import CreateOneOffLessonRequestB
    from datetime import date

    def call(day, start, end):
        def run(svc):
            req = CreateOneOffLessonRequestB(groupId=str(w.G1.id), lessonDate=date.fromisoformat(day), startTime=start, endTime=end, purpose="makeup", madeUpLessonId=ORIGIN, madeUpDate=date.fromisoformat(ORIGIN_DAY))
            return svc.create_one_off_lesson(str(w.org.id), req, actor_id=str(w.admin.id), is_org_admin=True)
        return run

    results = run_parallel(w, call("2026-10-29", "12:00", "13:00"), call("2026-10-30", "12:00", "13:00"))
    ok = [r for r, e in results if e is None]
    errors = [e for r, e in results if e is not None]
    assert len(ok) == 1 and [code_of(e) for e in errors] == ["MAKEUP_EXISTS"], results
    w.db.expire_all()
    assert w.db.query(GroupScheduleOneOffLesson).filter_by(group_id=w.G1.id).count() == 1
    assert pointer(w) == ok[0].id


# ============================================================================================ Ц4 / Ц5: what the original may not do while a make-up lives
def test_c4_a_cancelled_lesson_with_a_make_up_cannot_be_returned_until_the_make_up_is_cancelled(w):
    cancel_origin(w)
    mk = makeup(w, "admin", w.G1, "2026-10-29", start="12:00", end="13:00").json()["id"]
    r = restore(w, "admin", w.G1, ORIGIN, ORIGIN_DAY, expect=409)
    assert code(r) == "MAKEUP_EXISTS" and r.json()["detail"]["message"] == "По уроку назначена отработка: сначала отмените её"
    assert row_of(w, w.G1, ORIGIN, ORIGIN_DAY).status == "cancelled"
    sheet(w, "admin", w.G1, mk, "2026-10-29")                                         # the make-up is cancelled: the pointer is free
    assert pointer(w) is None
    restore(w, "admin", w.G1, ORIGIN, ORIGIN_DAY)
    assert row_of(w, w.G1, ORIGIN, ORIGIN_DAY) is None


def test_c5_the_kind_of_the_cancellation_does_not_change_while_a_make_up_lives(w):
    cancel_origin(w)
    makeup(w, "admin", w.G1, "2026-10-29", start="12:00", end="13:00")
    r = client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/attendance/cancellation-kind", json={"lessonDate": ORIGIN_DAY, "lessonId": ORIGIN, "cancellationKind": "holiday", "reason": "Это праздник"})
    assert r.status_code == 409 and code(r) == "MAKEUP_EXISTS"
    assert row_of(w, w.G1, ORIGIN, ORIGIN_DAY).cancellation_kind == "centre"


def test_c5_a_day_off_given_afterwards_leaves_the_row_as_it_is_and_the_preview_says_so(w):
    cancel_origin(w)
    mk = makeup(w, "admin", w.G1, "2026-10-29", start="12:00", end="13:00").json()["id"]
    plan = client(w, "owner").post(f"{API}/org/days-off/preview", json={"dateFrom": ORIGIN_DAY, "dateTo": ORIGIN_DAY, "reason": "Хайит"}).json()
    assert plan["convertible"] == []
    assert [(x["lessonId"], x["date"], x["reason"]) for x in plan["blocked"]] == [(ORIGIN, ORIGIN_DAY, "По уроку назначена отработка: отмена остаётся «по вине центра»")]
    made = client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": ORIGIN_DAY, "dateTo": ORIGIN_DAY, "reason": "Хайит"})
    assert made.status_code == 201 and made.json()["converted"] == []
    row = row_of(w, w.G1, ORIGIN, ORIGIN_DAY)
    assert (row.status, row.cancellation_kind, row.day_off_id, row.made_up_by) == ("cancelled", "centre", None, mk)
    # voiding the day off does not touch a row that was never the day off's
    assert client(w, "owner").post(f"{API}/org/days-off/{made.json()['dayOff']['id']}/void").status_code == 200
    assert pointer(w) == mk


# ============================================================================================ Ц14: «Удалить урок»
def test_c14_the_author_teacher_deletes_his_make_up_before_it_starts_and_the_lesson_is_free_again(w, clock):
    cancel_origin(w)
    mk = makeup(w, "A", w.G1, "2026-10-29", start="12:00", end="13:00").json()["id"]
    gone = sheet(w, "A", w.G1, mk, "2026-10-29", reason="Создал по ошибке")
    assert gone.status_code == 200
    assert pointer(w) is None
    listed = client(w, "A").get(f"{API}/org/schedule/occurrences", params={"date_from": "2026-10-26", "date_to": "2026-10-29"}).json()
    assert next(o for o in listed if o["id"] == ORIGIN and o["occurrenceDate"] == ORIGIN_DAY)["madeUpBy"] is None
    assert makeup(w, "A", w.G1, "2026-10-30", start="12:00", end="13:00").status_code == 201          # a new make-up of the same lesson
    # after the start: 409
    again = pointer(w)
    clock.set("2026-10-30 12:05")
    assert client(w, "A").post(f"{API}/org/groups/{w.G1.id}/attendance/start", json={"lessonDate": "2026-10-30", "lessonId": again}).status_code == 200
    r = sheet(w, "A", w.G1, again, "2026-10-30", expect=409)
    assert code(r) == "LESSON_ALREADY_STARTED"
    assert pointer(w) == again


def test_a_cancelled_make_up_can_be_returned_only_while_its_lesson_is_still_free(w):
    cancel_origin(w)
    first = makeup(w, "admin", w.G1, "2026-10-29", start="12:00", end="13:00").json()["id"]
    sheet(w, "admin", w.G1, first, "2026-10-29")
    restore(w, "admin", w.G1, first, "2026-10-29")                                    # nobody took the lesson meanwhile: the make-up is back and points again
    assert pointer(w) == first
    sheet(w, "admin", w.G1, first, "2026-10-29")
    second = makeup(w, "admin", w.G1, "2026-10-30", start="12:00", end="13:00").json()["id"]
    r = restore(w, "admin", w.G1, first, "2026-10-29", expect=409)
    assert code(r) == "RESTORE_MAKEUP_TARGET" and pointer(w) == second


# ============================================================================================ Ц3 / inv. 5: a move keeps the purpose
def move(w, who, lesson, day, new_day, start="12:00", end="13:00", expect=200):
    r = client(w, who).post(f"{API}/org/groups/{w.G1.id}/lesson-move", json={"lessonId": lesson, "lessonDate": day, "newDate": new_day, "startTime": start, "endTime": end, "reason": "Перенос"})
    assert r.status_code == expect, r.text
    return r


def test_c3_a_moved_make_up_keeps_what_it_is_and_the_pointer_goes_to_the_copy(w):
    cancel_origin(w)
    mk = makeup(w, "A", w.G1, "2026-10-29", start="12:00", end="13:00").json()["id"]
    new = move(w, "admin", mk, "2026-10-29", "2026-10-30").json()["result"]["newLesson"]["id"]
    row = one_off(w, new)
    assert (row.purpose, row.money, row.made_up_lesson_id, row.made_up_date, row.created_by) == ("makeup", None, ORIGIN, ORIGIN_DAY, w.A.id)          # the author stays the teacher
    assert pointer(w) == new                                                          # the cancelled lesson points at the COPY
    old = one_off(w, mk)
    assert old.purpose == "makeup"                                                    # the moved original stays what it was (history)
    # the copy can be moved again: the pointer follows
    newer = move(w, "admin", new, "2026-10-30", "2026-11-02", start="14:00", end="15:00").json()["result"]["newLesson"]["id"]
    assert pointer(w) == newer and one_off(w, newer).purpose == "makeup"
    # and a cancelled copy frees the lesson
    sheet(w, "admin", w.G1, newer, "2026-11-02")
    assert pointer(w) is None


def test_inv5_a_moved_additional_lesson_keeps_its_money_and_author_and_the_copy_of_a_regular_lesson_is_moved(w):
    extra = make(w, "A", w.G1, "2026-10-29", start="12:00", end="13:00", purpose="extra", money="wait").json()["id"]
    copy = move(w, "admin", extra, "2026-10-29", "2026-10-30").json()["result"]["newLesson"]["id"]
    row = one_off(w, copy)
    assert (row.purpose, row.money, row.created_by) == ("extra", "wait", w.A.id)
    regular = move(w, "admin", "lesson-l2", "2026-10-28", "2026-10-29", start="17:00", end="18:30").json()["result"]["newLesson"]["id"]
    row = one_off(w, regular)
    assert (row.purpose, row.money, row.made_up_lesson_id) == ("moved", None, None)
    listed = client(w, "admin").get(f"{API}/org/schedule/occurrences", params={"date_from": "2026-10-29", "date_to": "2026-10-29"}).json()
    item = next(o for o in listed if o["id"] == regular)
    assert (item["purpose"], item["purposeLabel"], item["money"], item["moneyLabel"]) == ("moved", None, None, None)


# ============================================================================================ the list of what can be made up
def test_the_candidates_list_and_its_mask(w):
    empty = client(w, "admin").get(f"{API}/org/groups/{w.G1.id}/makeup-candidates").json()
    assert empty["items"] == [] and empty["emptyReason"].startswith("Нет отменённых уроков")
    cancel_origin(w, reason="Преподаватель заболел")
    cancel_origin(w, lesson="lesson-l2", day="2026-10-28", cancellationKind="holiday", reason="Праздник")
    got = client(w, "admin").get(f"{API}/org/groups/{w.G1.id}/makeup-candidates").json()
    assert got["emptyReason"] is None
    assert got["items"] == [{"lessonId": ORIGIN, "lessonDate": ORIGIN_DAY, "startTime": "10:00", "endTime": "11:30", "label": "пн 26.10, 10:00–11:30", "reason": "Преподаватель заболел"}]
    mine = client(w, "A").get(f"{API}/org/groups/{w.G1.id}/makeup-candidates").json()
    assert mine["items"][0]["lessonId"] == ORIGIN and mine["items"][0]["reason"] == "Преподаватель заболел"      # the group's teacher reads the reason in the journal, so he reads it here (S8)
    assert client(w, "B").get(f"{API}/org/groups/{w.G1.id}/makeup-candidates").status_code == 403    # not his group
    assert client(w, "foreign_admin", org="other").get(f"{API}/org/groups/{w.G1.id}/makeup-candidates").status_code == 404
    makeup(w, "A", w.G1, "2026-10-29", start="12:00", end="13:00")
    done = client(w, "admin").get(f"{API}/org/groups/{w.G1.id}/makeup-candidates").json()
    assert done["items"] == [] and done["emptyReason"]


# ============================================================================================ the mask of the answers (Ц18, LESSONS 51)
def test_c18_the_money_of_a_lesson_is_not_shown_to_a_teacher_of_another_group_nor_to_a_student(w):
    paid = make(w, "admin", w.G1, "2026-10-29", start="12:00", end="13:00", purpose="extra", money="paid").json()["id"]
    params = {"date_from": "2026-10-29", "date_to": "2026-10-29"}
    # the manager and the teacher of the group read the status of the money
    for who in ("owner", "admin", "A"):
        item = next(o for o in client(w, who).get(f"{API}/org/schedule/occurrences", params=params).json() if o["id"] == paid)
        assert item["money"] == "paid", who
    assert next(o for o in client(w, "A").get(f"{API}/org/schedule/occurrences", params=params).json() if o["id"] == paid)["moneyLabel"] == "оплачено"
    assert next(o for o in client(w, "admin").get(f"{API}/org/schedule/occurrences", params=params).json() if o["id"] == paid)["moneyLabel"] == "оплачивается"
    # a teacher of ANOTHER group who sees the lesson at all (he substitutes there) never reads it
    assert client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": "2026-10-29", "lessonId": paid, "teacherId": str(w.B.id), "reason": "Замена"}).status_code == 200
    seen = [o for o in client(w, "B").get(f"{API}/org/schedule/occurrences", params=params).json() if o["id"] == paid]
    assert seen and seen[0]["money"] is None and seen[0]["moneyLabel"] is None and seen[0]["purpose"] == "extra"
    # the dashboard of the teacher: the same function, the same mask
    dash_b = client(w, "B").get(f"{API}/org/teacher/dashboard/summary", params={"business_date": "2026-10-29"}).json()
    assert [(o["money"], o["moneyLabel"]) for o in dash_b["scheduleWindow"] if o["id"] == paid] == [(None, None)]
    dash_a = client(w, "A").get(f"{API}/org/teacher/dashboard/summary", params={"business_date": "2026-10-29"}).json()
    assert [(o["money"], o["moneyLabel"]) for o in dash_a["scheduleWindow"] if o["id"] == paid] == [("paid", "оплачено")]
    # the journal of the group: the same mask
    journal = client(w, "admin").get(f"{API}/org/groups/{w.G1.id}/attendance", params=params).json()
    assert [l["money"] for l in journal["lessons"] if l.get("id") == paid] == ["paid"]
    mine = client(w, "A").get(f"{API}/org/groups/{w.G1.id}/attendance", params=params).json()
    assert [l["moneyLabel"] for l in mine["lessons"] if l.get("id") == paid] == ["оплачено"]
