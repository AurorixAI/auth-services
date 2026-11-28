"""LESSON-RULES FIX5 (pack 7): the record of a cancellation / a return / a change of kind belongs to the teacher who LEADS the lesson (the substitute of a lesson whose start has come); the archive
of a group has no exception to the rule «a cancelled lesson that has begun keeps its substitute»; the small rules of the same pack; the tests the review asked for (LESSONS 144).

World: ``lesson_rules_helpers.world`` (group «Г» of the teacher A «Иванов И.»: Mon / Wed 10:00-11:30, nine students, today Thu 26.11.2026 12:00); the teacher B «Петров П.» takes Mon 23.11 (three
days ago) for A (``sub_world`` of FIX4 А2).  A lesson of a student pays 450 000 / 12 * 50 % = 18 750 to the teacher (the rate 50 %); nine present = 168 750; one present = 18 750."""
from datetime import datetime

from src.models.auth import GroupAttendance, GroupAttendanceAudit, LessonTeacherAssignment, StudentGroup
from tests.lesson_rules_helpers import MON, PAST_MON, WED, marks_of, reconcile, save, set_kind, teacher_cancels, world, clock, pg_session, needs_pg, client, API  # noqa: F401
from tests.payf_helpers import D, pay as pay_in
from tests.test_lesson_rules_fix4_a2_pg import FULL_LESSON, held_by, kind_of, mine, pay, standing, sub_world, cleared_events

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"
ONE_STUDENT = 18_750            # 450 000 / 12 * 50 % * 1 present - by hand


def listing(w, who="owner", **params):
    r = client(w, who).get(SUB, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def rows_of(w, who, day=PAST_MON, lesson=MON):
    """The rows of the lesson in the block «Запросы и изменения уроков» of ``who``: ``[(status, outcome)]``."""
    return sorted((i["status"], i["outcome"]) for i in mine(w, who)["items"] if i["lesson"]["date"] == day and i["lesson"]["lessonId"] == lesson)


def journal_names(w, **params):
    """The teacher named by every row of the lists of the owner for the lesson Mon 23.11: ``{(list, kind): teacher}``."""
    out = {}
    for status in ("pending", "decided"):
        for i in listing(w, status=status, **params)["items"]:
            if i["lesson"]["date"] == PAST_MON and i.get("source") == "request":
                out.setdefault(status, []).append((i["status"], (i["decision"] or {}).get("closedReason"), i["teacher"]["name"]))
    return out


# ============================================================================================ В1: the record is the substitute's
def test_the_report_of_the_substitute_is_his_record_the_main_teacher_has_none_and_the_owner_reads_his_name(pg_session, clock):
    """Hand: B stands on Mon 23.11 (started three days ago).  B reports «Урока не было» (waits).  B's block: ONE row of the lesson, «отмена ждёт» (his report); A's block: only his old request (the
    substitution of B, ``substituted``) - no cancellation of his.  The owner's «Ждут решения» names «Петров П.» as the teacher of the row; after the confirmation «по вине центра» «История» names him too."""
    w, g, people, rid = sub_world(pg_session, clock)
    teacher_cancels(w, g, who="B")
    assert rows_of(w, "B") == [("cancelled", "cancel_waiting")]
    assert rows_of(w, "A") == [("substituted", rows_of(w, "A")[0][1])]
    pending = [i for i in listing(w, status="pending")["items"] if i["lesson"]["date"] == PAST_MON]
    assert [i["teacher"]["name"] for i in pending] == ["Петров П."], pending
    set_kind(w, g, "centre")
    decided = [(i["decision"]["closedReason"] if i["decision"] else None, i["teacher"]["name"]) for i in listing(w, status="decided")["items"]
               if i["lesson"]["date"] == PAST_MON and i.get("source") == "request" and i["status"] == "cancelled"]
    assert decided and all(name == "Петров П." for _r, name in decided), decided
    assert rows_of(w, "B") == [("cancelled", "cancelled_centre")]            # the decision reaches the one whose record it is: B (his block is the only place the teacher is told in this service)
    assert [r for r in rows_of(w, "A") if r[0] == "cancelled"] == []


def test_the_return_of_the_lesson_is_the_substitutes_record_and_he_withdraws_from_his_own_block(pg_session, clock):
    """Hand: B reports, the owner answers «Урок был» (the lesson returns to the mark): B's block has the ONE row «урок возвращён», A's has no cancellation and no return.  The same chain with B
    withdrawing his own report («Отозвать»): the row of B says «урок возвращён», A has nothing of it; the lesson stays B's and he is paid for it (168 750, A 0)."""
    w, g, people, rid = sub_world(pg_session, clock)
    teacher_cancels(w, g, who="B")
    r = client(w, "owner").post(f"{API}/org/groups/{g.id}/lessons/restore", json={"lessonId": MON, "lessonDate": PAST_MON, "reason": "Урок был", "expectedKind": "unconfirmed"})
    assert r.status_code == 200, r.text
    assert rows_of(w, "B") == [("cancelled", "lesson_restored")]
    assert [x for x in rows_of(w, "A") if x[0] in ("cancelled", "closed")] == []
    w2, g2, people2, rid2 = sub_world(pg_session, clock)
    teacher_cancels(w2, g2, who="B")
    assert rows_of(w2, "B") == [("cancelled", "cancel_waiting")]
    r = client(w2, "B").post(f"{API}/org/groups/{g2.id}/lessons/withdraw-cancellation", json={"lessonId": MON, "lessonDate": PAST_MON, "expectedKind": "unconfirmed"})
    assert r.status_code == 200 and r.json()["restored"] is True, r.text
    assert rows_of(w2, "B") == [("cancelled", "lesson_restored")]
    assert [x for x in rows_of(w2, "A") if x[0] in ("cancelled", "closed")] == []
    held_by(w2, g2, people2)
    assert pay(w2) == (0, FULL_LESSON)


def test_a_change_of_kind_of_the_substitutes_lesson_is_a_record_of_the_substitute(pg_session, clock):
    """Hand: B reports, the owner confirms «по вине центра» and then changes it to «праздник» by hand («Ошибка»): the record «Вид отмены изменён» (``KIND_CHANGED``) is B's - the owner's history names him."""
    w, g, people, rid = sub_world(pg_session, clock)
    teacher_cancels(w, g, who="B")
    set_kind(w, g, "centre")
    set_kind(w, g, "holiday", expected="centre", reason="Ошибка")
    changed = [i for i in listing(w, status="decided")["items"] if i.get("source") == "request" and i["decision"] and i["decision"]["closedReason"] == "KIND_CHANGED" and i["lesson"]["date"] == PAST_MON]
    assert [i["teacher"]["name"] for i in changed] == ["Петров П."]


def test_a_lesson_that_is_still_to_come_stays_the_record_of_the_teacher_of_the_group(pg_session, clock):
    """Control (nothing broken): B takes Mon 30.11 (ahead); the administrator cancels it from the lesson sheet: the substitute is released, the record of the cancellation is A's (the teacher of the group)."""
    w, g, people, rid = sub_world(pg_session, clock, day="2026-11-30")
    r = client(w, "admin").post(f"{API}/org/groups/{g.id}/attendance", json={"lessonDate": "2026-11-30", "lessonId": MON, "status": "cancelled", "records": {}, "cancellationReason": "Центр закрыт"})
    assert r.status_code == 200, r.text
    assert standing(w, g, "2026-11-30") == []
    assert [x for x in rows_of(w, "A", "2026-11-30") if x[0] == "cancelled"] and [x for x in rows_of(w, "B", "2026-11-30") if x[0] == "cancelled"] == []


# ============================================================================================ В2: the archive has no exception
def _group_with_a_student_who_left(w, g):
    """One student was in the group from 20.10 and left on 25.11 09:00 (before it can be archived); he was in the class on Mon 23.11."""
    from src.auth.membership_periods import close_period
    from tests.ops_helpers import new_student

    student = new_student(w.db, w.org, w.ids, "gone", "Ушёл", "Тест", g, joined=datetime(2026, 10, 20, 6, 0))
    membership = w.db.query(StudentGroup).filter_by(group_id=g.id, student_id=student.id).one()
    close_period(membership, datetime(2026, 11, 25, 4, 0))
    membership.is_active = False
    w.db.commit()
    from tests.lesson_rules_helpers import account

    owed = account(w, membership)[1]          # what he owes for the lessons he attended - paid to the sum: a group with an open balance cannot be archived
    assert owed > 0
    pay_in(w, membership, owed, D(2026, 11, 25))
    return student


def _assignment_of(w, g, day):
    w.db.expire_all()
    return w.db.query(LessonTeacherAssignment).filter_by(group_id=g.id, lesson_date=day).one()


def test_the_archive_keeps_the_substitute_of_a_lesson_that_has_begun_and_leaves_a_lesson_to_come_to_the_return_check(pg_session, clock):
    """Hand: B stands on Mon 23.11 (started) and on Mon 30.11 (ahead).  The group is archived with «Отменить пропущенные»: B keeps 23.11 (cancelled, a fact); 30.11 is not cancelled by the archive (it stops with the group), so its substitute is untouched and re-checked when the group returns.  The group comes back,
    the owner gives 23.11 back («Урок был»), B arranges and holds it with the one student: B 18 750, A 0."""
    w, g, people, rid = sub_world(pg_session, clock, students=0, money_right=True)
    student = _group_with_a_student_who_left(w, g)
    r = client(w, "admin").put(f"{API}/org/groups/{g.id}/lesson-teacher", json={"lessonDate": "2026-11-30", "lessonId": MON, "teacherId": str(w.B.id), "reason": "Болезнь"})
    assert r.status_code == 200, r.text
    r = client(w, "owner").post(f"{API}/org/groups/{g.id}/archive", json={"reason": "Пауза", "missedLessonsAction": "cancel"})
    assert r.status_code == 200, r.text
    assert kind_of(w, g) == ("cancelled", None)
    assert _assignment_of(w, g, PAST_MON).cancelled_at is None          # a missed lesson that has begun: the substitute stays (the one rule)
    assert _assignment_of(w, g, "2026-11-30").cancelled_at is None      # a lesson to come is not cancelled by the archive at all (it only stops with the group): its substitute is untouched, as before; the return of the group re-checks it
    assert client(w, "owner").post(f"{API}/org/groups/{g.id}/restore", json={}).status_code == 200
    r = client(w, "owner").post(f"{API}/org/groups/{g.id}/lessons/restore", json={"lessonId": MON, "lessonDate": PAST_MON, "reason": "Урок был"})
    assert r.status_code == 200 and r.json()["returnedToReview"] is True, r.text
    assert standing(w, g) == [str(w.B.id)] and pay(w) == (0, 0)
    reconcile(w, g, PAST_MON, MON, who="B")
    save(w, g, [student], PAST_MON, MON, who="B", marks=marks_of([student], first_present=1))
    assert pay(w) == (0, ONE_STUDENT)


# ============================================================================================ the small rules
def test_the_journal_and_the_history_name_who_formalised_the_lesson_the_same_way_and_from_the_first_start(pg_session, clock):
    """Hand: A starts the arrangement of Wed 25.11 on 25.11 12:00 (the lesson's own day), reports «Урока не было» over it, the owner gives it back, A arranges it again on 26.11 and completes it with two
    present.  The journal of the group marks it «задним числом» by A «Иванов И.» (the short form of the history), the history has ONE entry of the lesson, one day late, by «Иванов И.»; a lesson
    arranged on its own day is no entry (control: the arrangement of 25.11 alone)."""
    w, g, people, members = world(pg_session, clock, money_right=True)
    clock.set("2026-11-25 12:00")
    reconcile(w, g, "2026-11-25")
    save(w, g, people, "2026-11-25", WED, who="admin", status="cancelled", marks={}, cancellationReason="Не было")          # the lesson sheet of the administrator over the started row
    r = client(w, "owner").post(f"{API}/org/groups/{g.id}/lessons/restore", json={"lessonId": WED, "lessonDate": "2026-11-25", "reason": "Урок был"})
    assert r.status_code == 200, r.text
    clock.set("2026-11-26 12:00")
    reconcile(w, g, "2026-11-25")
    save(w, g, people, "2026-11-25", marks=marks_of(people, first_present=2))
    journal = client(w, "owner").get(f"{API}/org/groups/{g.id}/attendance", params={"month": "2026-11"}).json()["backdated"]
    (mark,) = [v for k, v in journal.items() if "2026-11-25" in k]
    entries = [i for i in listing(w, status="decided")["items"] if i.get("source") == "audit" and i["kind"] == "backdated" and i["lesson"]["date"] == "2026-11-25"]
    assert len(entries) == 1 and entries[0]["lateDays"] == 1
    assert mark["by_name"] == entries[0]["by"]["name"] == "Иванов И."


def test_a_note_of_the_journal_names_the_substitute_taught_by_in_the_short_form(pg_session, clock):
    """Hand: the administrator (the money right) formalises Mon 23.11 on 26.11 and names B as who taught: the mark has ``by_name`` of the administrator and ``taught_by_name`` «Петров П.» - the form of the history."""
    w, g, people, members = world(pg_session, clock, money_right=True)
    r = client(w, "admin").post(f"{API}/org/groups/{g.id}/attendance/reconcile", json={"lessonDate": PAST_MON, "lessonId": MON, "taughtBy": str(w.B.id)})
    assert r.status_code == 200, r.text
    journal = client(w, "owner").get(f"{API}/org/groups/{g.id}/attendance", params={"month": "2026-11"}).json()["backdated"]
    (mark,) = [v for k, v in journal.items() if PAST_MON in k]
    assert mark["taught_by_name"] == "Петров П." and mark["by_name"] == f"{w.admin.last_name} {w.admin.first_name[0]}."


def _cancelled_row(w, g, day, lesson):
    """The state of OLD data / of a race, made by hand in the test database (LESSONS 135 allows the state, not the way to it): a row ``cancelled`` on the date of the substitution."""
    w.db.add(GroupAttendance(org_id=w.org.id, group_id=g.id, lesson_date=day, lesson_id=lesson, status="cancelled", records={}, roster_snapshot=[], cancellation_kind="centre",
                             cancellation_reason="Центр закрыт", version=2))
    w.db.commit()


def test_a_cancelled_lesson_of_the_future_is_not_a_fact_for_the_releases(pg_session, clock):
    """Hand: B stands on Mon 30.11 10:00 (ahead, now Thu 26.11 12:00); the journal has a row ``cancelled`` on that date (old data / a race: the lesson was cancelled and the substitute stayed).  The row is no
    fact before the start: the owner removes B - the assignment gets ``cancelled_at`` (the lesson has no substitute).  In the other world ``release_substitutions_of`` itself returns ONE (before pack 7: 0).
    Control: the lesson of today (Wed 25.11 10:00, now 10:30) with the same row - the substitute stays (a fact: he taught it)."""
    from src.auth.lesson_teachers import LessonTeachers

    w, g, people, rid = sub_world(pg_session, clock, day="2026-11-30")
    _cancelled_row(w, g, "2026-11-30", MON)
    assert standing(w, g, "2026-11-30") == [str(w.B.id)]
    assert client(w, "owner").delete(f"{API}/org/members/{w.ids('member_B')}").status_code == 200
    assert standing(w, g, "2026-11-30") == []
    w.db.expire_all()
    assert w.db.query(LessonTeacherAssignment).filter_by(group_id=g.id, lesson_date="2026-11-30", teacher_id=w.B.id).one().cancelled_at is not None

    w2, g2, p2, rid2 = sub_world(pg_session, clock, day="2026-11-30")
    _cancelled_row(w2, g2, "2026-11-30", MON)
    assert LessonTeachers(w2.svc).release_substitutions_of(w2.org.id, w2.B.id, w2.owner.id) == 1
    w2.db.commit()
    assert standing(w2, g2, "2026-11-30") == []

    w3, g3, p3, rid3 = sub_world(pg_session, clock, day="2026-11-25", lesson=WED, now="2026-11-25 10:30")
    _cancelled_row(w3, g3, "2026-11-25", WED)
    assert LessonTeachers(w3.svc).release_substitutions_of(w3.org.id, w3.B.id, w3.owner.id) == 0
    assert standing(w3, g3, "2026-11-25", WED) == [str(w3.B.id)]


def test_the_day_off_text_has_the_name_of_the_day_off(pg_session, clock):
    """Hand: a day off «Хайит» on 23.11 turns a waiting cancellation into «праздник»: the line of the history says «выходной день «Хайит» 23.11» (a day off without a name: «выходной день 23.11», as before)."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    r = client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": PAST_MON, "dateTo": PAST_MON, "reason": "Хайит"})
    assert r.status_code == 201, r.text
    lines = [i["decision"]["closedReasonText"] for i in listing(w, status="decided")["items"] if i.get("source") == "request" and i["decision"] and i["decision"]["closedReason"] == "KIND_CHANGED"]
    assert lines and all("Причина: выходной день «Хайит» 23.11." in t for t in lines), lines
    client(w, "owner").post(f"{API}/org/days-off/{r.json()['dayOff']['id']}/void")
    lines = [i["decision"]["closedReasonText"] for i in listing(w, status="decided")["items"] if i.get("source") == "request" and i["decision"] and i["decision"]["closedReason"] == "KIND_CHANGED"]
    assert any("выходной день «Хайит» 23.11 отменён." in t for t in lines), lines


# ============================================================================================ the boundary of А2, old lines, the substitute who left, the move
def _admin_cancels_the_lesson_of_today(w, g):
    return client(w, "admin").post(f"{API}/org/groups/{g.id}/attendance", json={"lessonDate": "2026-11-25", "lessonId": WED, "status": "cancelled", "records": {}, "cancellationReason": "Центр закрыт"})


def test_the_boundary_a_minute_before_the_start_the_substitute_is_taken_off_when_the_lesson_runs_he_stays(pg_session, clock):
    """Hand: B stands on Wed 25.11 10:00-11:30.  The administrator cancels at 09:59 (a minute before): the substitute is off, one ``assignment_cleared``.  In the other world the lesson runs (10:30): the
    cancellation is a lesson that has begun - the substitute stays, nothing cleared."""
    from tests.test_lesson_rules_fix4_a2_pg import cleared_events

    w, g, people, rid = sub_world(pg_session, clock, day="2026-11-25", lesson=WED, now="2026-11-25 09:59")
    assert _admin_cancels_the_lesson_of_today(w, g).status_code == 200
    assert standing(w, g, "2026-11-25", WED) == [] and cleared_events(w) == 1
    w2, g2, p2, rid2 = sub_world(pg_session, clock, day="2026-11-25", lesson=WED, now="2026-11-25 10:30")
    r = _admin_cancels_the_lesson_of_today(w2, g2)
    assert r.status_code == 200, r.text
    assert standing(w2, g2, "2026-11-25", WED) == [str(w2.B.id)] and cleared_events(w2) == 0


def test_the_filters_of_the_dates_find_an_old_line_without_the_date_by_the_date_of_the_lesson_row(pg_session, clock):
    """Hand: Wed 25.11 completed a day late, Mon 23.11 by the administrator 3 days late (and corrected).  EVERY line loses its key (the database before the migration).  dateFrom = dateTo = 25.11 finds
    exactly the entry of 25.11; 23.11-24.11 exactly the two of 23.11; 26.11 and later nothing; 01.11-22.11 nothing."""
    from tests.test_lesson_rules_fix4_a1_pg import _world_with_entries, audit_rows

    w, g = _world_with_entries(pg_session, clock, old=True)
    def keys(**p):
        return set(audit_rows(listing(w, status="decided", **p)["items"]))
    assert keys(dateFrom="2026-11-25", dateTo="2026-11-25") == {("backdated", "2026-11-25")}
    assert keys(dateFrom="2026-11-23", dateTo="2026-11-24") == {("backdated", "2026-11-23"), ("corrected", "2026-11-23")}
    assert keys(dateFrom="2026-11-26") == set() and keys(dateFrom="2026-11-01", dateTo="2026-11-22") == set()


def test_the_substitute_who_left_the_centre_after_a_waiting_cancellation_the_administrator_formalises_and_the_sheet_is_his(pg_session, clock):
    """Hand: B reports (waits) and is removed from the centre; «Урок был»; the administrator (the money right) arranges it for B (``taughtBy`` = B stays the assignment) and completes it with nine present.
    Expected (recorded as it is): the lesson is formalised by the administrator; the sum of the lesson 168 750 is B's sheet, A has 0."""
    w, g, people, rid = sub_world(pg_session, clock, money_right=True)
    teacher_cancels(w, g, who="B")
    assert client(w, "owner").delete(f"{API}/org/members/{w.ids('member_B')}").status_code == 200
    assert standing(w, g) == [str(w.B.id)]
    r = client(w, "owner").post(f"{API}/org/groups/{g.id}/lessons/restore", json={"lessonId": MON, "lessonDate": PAST_MON, "reason": "Урок был", "expectedKind": "unconfirmed"})
    assert r.status_code == 200, r.text
    reconcile(w, g, PAST_MON, MON, who="admin")
    save(w, g, people, PAST_MON, MON, who="admin", marks=marks_of(people, first_present=9))
    w.db.expire_all()
    row = w.db.query(GroupAttendance).filter_by(group_id=g.id, lesson_date=PAST_MON, lesson_id=MON).one()
    assert (row.status, str(row.teacher_id)) == ("completed", str(w.B.id))
    assert pay(w) == (0, FULL_LESSON)


def test_a_lesson_that_has_begun_cannot_be_moved_so_a_substitute_is_not_lost_by_a_move(pg_session, clock):
    """Hand (the check of the review): the move of Mon 23.11 (started three days ago) is refused ``LESSON_ALREADY_STARTED``; B stays on the lesson, nothing cleared.  The path back of a move needs the
    original date to be ahead, so a lesson that began cannot come back to its old date by it either."""
    from tests.test_lesson_rules_fix4_a2_pg import cleared_events

    w, g, people, rid = sub_world(pg_session, clock)
    r = client(w, "admin").post(f"{API}/org/groups/{g.id}/lesson-move", json={"lessonId": MON, "lessonDate": PAST_MON, "newDate": "2026-11-27", "startTime": "10:00", "endTime": "11:30", "reason": "Перенос"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "LESSON_ALREADY_STARTED", r.text
    assert standing(w, g) == [str(w.B.id)] and cleared_events(w) == 0
