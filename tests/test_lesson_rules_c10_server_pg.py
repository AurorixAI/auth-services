"""LESSON-RULES С10 (the screens' needs after the first acceptance): the window marks of every lesson, the marks of the block of the teacher, the time of the entries of the history, the student of
``EXCUSE_DECIDED``, the words of the approved mock, the number of the tab of the administrator without the right (PostgreSQL).

World: ``lesson_rules_helpers.world`` (group «Г» of the teacher A «Иванов И.», Mon / Wed 10:00-11:30, nine students «Ученик1 Тест» ... «Ученик9 Тест», 450 000, today Thu 26.11.2026 12:00, window 7).
The lessons of November 2026: Mon 02, 09, 16, 23, 30 and Wed 04, 11, 18, 25 (9).  The expectation of each test is in its comment BEFORE the call."""
from datetime import date

import pytest

from tests.lesson_rules_helpers import MON, PAST_MON, WED, marks_of, reconcile, save, teacher_cancels, world, clock, pg_session, needs_pg, client, API  # noqa: F401  (fixtures)
from tests.ops_helpers import closed_sheet
from tests.test_lesson_rules_c4_c6_teacher_rules_pg import _decide, _excused_lesson

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"
RULES = f"{API}/org/settings/lessons"
NOV = ["2026-11-02", "2026-11-04", "2026-11-09", "2026-11-11", "2026-11-16", "2026-11-18", "2026-11-23", "2026-11-25", "2026-11-30"]


def journal(w, g, who):
    r = client(w, who).get(f"{API}/org/groups/{g.id}/attendance", params={"month": "2026-11"})
    assert r.status_code == 200, r.text
    return {lesson["date"]: lesson for lesson in r.json()["lessons"]}


def schedule(w, who):
    r = client(w, who).get(f"{API}/org/schedule/occurrences", params={"date_from": "2026-11-01", "date_to": "2026-11-30"})
    assert r.status_code == 200, r.text
    return {i["occurrenceDate"]: i for i in r.json()}


def marks(lessons):
    return {d: (x["canActSelf"], x["monthClosed"], x["windowDays"]) for d, x in lessons.items()}


# ============================================================================================ 1. the window marks of every lesson
def test_every_lesson_of_the_journal_and_of_the_schedule_says_the_window_to_the_teacher(pg_session, clock):
    """Hand, window 7, today 26.11: a teacher acts on lessons with date >= 26.11 - 7 = 19.11 -> 23.11, 25.11, 30.11 (the future) are True; 02, 04, 09, 11, 16, 18.11 are False.  No month is closed.  The
    owner and the administrator: True everywhere.  Window 14 (>= 12.11): 16.11 and 18.11 turn True, 02, 04, 09, 11 stay False.  Journal and schedule say the same thing."""
    w, g, people, members = world(pg_session, clock)
    seven = {d: (d >= "2026-11-19", False, 7) for d in NOV}
    for lessons in (journal(w, g, "A"), schedule(w, "A")):
        assert marks(lessons) == seven
    for who in ("owner", "admin"):
        assert marks(journal(w, g, who)) == {d: (True, False, 7) for d in NOV} and marks(schedule(w, who)) == {d: (True, False, 7) for d in NOV}
    assert client(w, "owner").put(RULES, json={"teacher_lesson_window_days": 14}).status_code == 200
    fourteen = {d: (d >= "2026-11-12", False, 14) for d in NOV}
    assert marks(journal(w, g, "A")) == fourteen and marks(schedule(w, "A")) == fourteen
    assert [d for d, v in fourteen.items() if not v[0]] == ["2026-11-02", "2026-11-04", "2026-11-09", "2026-11-11"]


def test_a_closed_month_is_said_apart_from_the_window(pg_session, clock):
    """Hand: the sheet of the teacher A for November is saved on 02.12 (the month is closed for him).  Every lesson of November of A is ``monthClosed`` True - for the teacher, the owner and the administrator -
    while ``canActSelf`` of the recent lessons (>= 19.11) stays True for the teacher: the window and the closed month are two different words."""
    w, g, people, members = world(pg_session, clock)
    closed_sheet(w, w.A, date(2026, 11, 1), saved="2026-12-02 12:00")
    for who in ("A", "owner", "admin"):
        got = marks(journal(w, g, who))
        assert {d: v[1] for d, v in got.items()} == {d: True for d in NOV}
    assert {d: v[0] for d, v in marks(journal(w, g, "A")).items()} == {d: d >= "2026-11-19" for d in NOV}


def test_a_lesson_of_another_teacher_is_not_for_the_teacher_himself(pg_session, clock):
    """Hand: the teacher B leads no lesson of the group; the schedule of the OWNER shows the lessons of A.  For a teacher the lesson is not his, so ``canActSelf`` of B would be False; B's own schedule has
    no lesson of this group at all (empty) - nothing of the group's is offered to him.  The owner reads the lessons of A as True."""
    w, g, people, members = world(pg_session, clock)
    assert schedule(w, "B") == {}
    assert {d: v[0] for d, v in marks(schedule(w, "owner")).items()} == {d: True for d in NOV}


# ============================================================================================ 2. the block of the teacher: «Отозвать»
def mine_of(w, who="A"):
    r = client(w, who).get(f"{SUB}/mine", params={"status": "all"})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def test_a_waiting_cancellation_says_whether_the_teacher_may_withdraw_it_and_every_row_has_its_time(pg_session, clock):
    """Hand: the teacher cancels Mon 23.11 (3 days old, window 7): ONE row, outcome ``cancel_waiting``, ``canWithdraw`` True, the time 10:00-11:30.  Window 1 (3 > 1): False.  Window back to 7 and the
    month of the teacher closed: False.  The owner confirms (the lesson has an outcome): the row is ``cancelled_centre`` and ``canWithdraw`` is null (nothing to give back)."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    (row,) = mine_of(w)
    assert (row["outcome"], row["canWithdraw"], row["lesson"]["startTime"], row["lesson"]["endTime"]) == ("cancel_waiting", True, "10:00", "11:30")
    assert client(w, "owner").put(RULES, json={"teacher_lesson_window_days": 1}).status_code == 200
    assert mine_of(w)[0]["canWithdraw"] is False
    assert client(w, "owner").put(RULES, json={"teacher_lesson_window_days": 7}).status_code == 200
    assert mine_of(w)[0]["canWithdraw"] is True
    closed_sheet(w, w.A, date(2026, 11, 1), saved="2026-12-02 12:00")
    assert mine_of(w)[0]["canWithdraw"] is False


def test_the_withdraw_flag_agrees_with_the_road(pg_session, clock):
    """The flag and the refusal are one rule: with ``canWithdraw`` True the road gives 200; with False (the window 1) the road gives 403 ``LESSON_TOO_OLD``."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    body = {"lessonDate": PAST_MON, "lessonId": MON}
    assert client(w, "owner").put(RULES, json={"teacher_lesson_window_days": 1}).status_code == 200
    assert mine_of(w)[0]["canWithdraw"] is False
    r = client(w, "A").post(f"{API}/org/groups/{g.id}/lessons/withdraw-cancellation", json=body)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "LESSON_TOO_OLD"
    assert client(w, "owner").put(RULES, json={"teacher_lesson_window_days": 7}).status_code == 200
    assert mine_of(w)[0]["canWithdraw"] is True
    assert client(w, "A").post(f"{API}/org/groups/{g.id}/lessons/withdraw-cancellation", json=body).status_code == 200


def test_the_owner_decides_a_waiting_cancellation_it_is_no_more_to_withdraw(pg_session, clock):
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    r = client(w, "owner").put(f"{API}/org/groups/{g.id}/attendance/cancellation-kind", json={"lessonDate": PAST_MON, "lessonId": MON, "cancellationKind": "centre", "expectedKind": "unconfirmed"})
    assert r.status_code == 200, r.text
    (row,) = mine_of(w)
    assert (row["outcome"], row["canWithdraw"]) == ("cancelled_centre", None)
    assert (row["lesson"]["startTime"], row["lesson"]["endTime"]) == ("10:00", "11:30")


# ============================================================================================ 3. the entries of the history: the time and the group
def test_the_entries_of_the_history_carry_the_time_of_the_lesson_and_the_group(pg_session, clock):
    """Hand: Wed 25.11 10:00-11:30 completed on 26.11 (a day late): ONE entry ``backdated``, ``lesson`` has ``startTime`` 10:00 and ``endTime`` 11:30, the group is «Г» with 9 students (the same block as the
    entries of the requests)."""
    w, g, people, members = world(pg_session, clock)
    reconcile(w, g, "2026-11-25")
    save(w, g, people, "2026-11-25", marks=marks_of(people, first_present=2))
    got = client(w, "owner").get(SUB, params={"status": "decided"}).json()["items"]
    (e,) = [i for i in got if i.get("source") == "audit"]
    assert e["kind"] == "backdated" and e["lesson"] == {"lessonId": WED, "date": "2026-11-25", "startTime": "10:00", "endTime": "11:30"}
    assert e["group"] == {"id": str(g.id), "name": "Г", "studentCount": 9}


# ============================================================================================ 4. EXCUSE_DECIDED names the student
@pytest.mark.parametrize("decision", ["confirmed", "rejected"])
def test_the_refusal_of_a_decided_excuse_names_the_student(pg_session, clock, decision):
    """Hand: the student 0 («Ученик1 Тест») has a decided excuse; the teacher rewrites its text: 409 ``EXCUSE_DECIDED`` with ``studentId`` = his id and ``studentName`` «Ученик1 Тест»."""
    w, g, people, members = world(pg_session, clock)
    eid, marks_ = _excused_lesson(w, g, people)
    _decide(w, eid, decision)
    r = save(w, g, people, "2026-11-25", status="in_progress", marks=marks_, excuseReasons={str(people[0].id): "Другая причина"}, expect=409)
    d = r.json()["detail"]
    assert d["code"] == "EXCUSE_DECIDED" and d["studentId"] == str(people[0].id) and d["studentName"] == "Ученик1 Тест"


def test_the_refusal_of_removing_an_approved_excuse_names_the_student(pg_session, clock):
    w, g, people, members = world(pg_session, clock)
    eid, marks_ = _excused_lesson(w, g, people)
    _decide(w, eid, "confirmed")
    r = save(w, g, people, "2026-11-25", status="in_progress", marks={**marks_, str(people[0].id): "absent"}, expect=409)
    d = r.json()["detail"]
    assert d["code"] == "EXCUSE_DECIDED" and d["studentId"] == str(people[0].id) and d["studentName"] == "Ученик1 Тест"


# ============================================================================================ 5. the words of the approved mock
def test_the_words_of_the_rules_of_the_lessons_are_the_mock(pg_session, clock):
    """The captions and the hints of «Настройки → Уроки» are the frame N2u of the owner's mock, word for word."""
    w, g, people, members = world(pg_session, clock)
    items = {i["key"]: i for i in client(w, "owner").get(RULES).json()["items"]}
    window, confirm = items["teacher_lesson_window_days"], items["past_cancel_needs_confirmation"]
    assert window["title"] == "Преподаватель сам оформляет прошедший урок в течение"
    assert window["hint"] == "Позже этого срока урок оформляет администратор или владелец. Оформленное задним числом видно в «Замены → История»."
    assert confirm["title"] == "Отмену прошедшего урока преподавателем или администратором без доступа к деньгам подтверждает владелец или администратор с доступом к деньгам"
    assert confirm["hint"] == "Пока отмена не подтверждена, перерасчёта ученикам нет."


def test_the_sentence_after_the_teacher_cancels_is_the_mock(pg_session, clock):
    """Hand: the teacher cancels Mon 23.11 (waits): the answer says ``waiting`` and the mock's sentence.  The administrator without the right keeps his own sentence (the owner confirms)."""
    w, g, people, members = world(pg_session, clock)
    r = teacher_cancels(w, g)
    assert r.json()["waiting"] is True and r.json()["consequence"] == "Отмену подтвердит администратор. За отменённый урок оплаты нет."


# ============================================================================================ 6. the number of the tab
def test_the_number_of_the_tab_is_what_the_caller_may_decide(pg_session, clock):
    """Hand: one waiting cancellation, no requests.  The list answer says ``waitingTotal`` 1 to the owner and 0 to the administrator without the right (``total`` stays 1: he reads the row as «решает
    владелец»); the summary says the same numbers."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    for who, number in (("owner", 1), ("admin", 0)):
        got = client(w, who).get(SUB, params={"status": "pending"}).json()
        assert (got["total"], got["unconfirmedTotal"], got["waitingTotal"]) == (1, 1, number)
        assert client(w, who).get(f"{SUB}/summary").json()["waitingTotal"] == number
