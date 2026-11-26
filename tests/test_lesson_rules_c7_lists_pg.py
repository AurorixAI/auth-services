"""LESSON-RULES С7: «Ждут решения» with the cancellations that wait, «История» with the entries built from the journal, the block of the teacher, the counters, the number of the queries (PostgreSQL).

World: ``lesson_rules_helpers.world`` (group «Г» of the teacher A «Иванов И.», Mon / Wed 10:00-11:30, nine students).  Today Thu 26.11 12:00 unless a test sets another time.  The teacher cancels Mon
23.11 (3 days ago) -> ``unconfirmed``.  The expectation of each test is in its comment BEFORE the call."""
import json
from datetime import date

import pytest
from sqlalchemy import event

from tests.lesson_rules_helpers import MON, PAST_MON, WED, accounts, kind_in_db, marks_of, reconcile, save, set_kind, teacher_cancels, world, clock, pg_session, needs_pg, client, API  # noqa: F401
from tests.ops_helpers import grant_money_right

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"


def listing(w, who="owner", **params):
    r = client(w, who).get(SUB, params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ============================================================================================ «Ждут решения»
def test_the_waiting_cancellation_is_a_row_of_the_pending_list_with_its_words_and_ages(pg_session, clock):
    """Hand: one row.  Group «Г», 9 students, lesson Mon 23.11 10:00-11:30, teacher «Иванов И.», cancelled by the teacher («Урока не было») today -> waiting 0 days, the lesson is 3 days old.
    The owner may decide (``canDecide``), the administrator without the right reads the same row and may not."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    for who, can in (("owner", True), ("admin", False)):
        got = listing(w, who, status="pending")
        assert got["total"] == 1 and got["unconfirmedTotal"] == 1 and got["pendingTotal"] == 0
        (row,) = got["items"]
        assert (row["source"], row["kind"], row["reason"], row["waitingDays"], row["ageDays"], row["canDecide"]) == ("unconfirmed", "unconfirmed", "Урока не было", 0, 3, can)
        assert row["group"] == {"id": str(g.id), "name": "Г", "studentCount": 9}
        assert row["lesson"] == {"lessonId": MON, "date": PAST_MON, "startTime": "10:00", "endTime": "11:30"}
        assert row["teacher"]["name"] == "Иванов И." and row["cancelledBy"]["role"] == "teacher" and "amount" not in json.dumps(row) and "total" not in row and "per_student" not in row          # no sum in the row


def test_the_counters_the_money_right_counts_it_the_others_do_not(pg_session, clock):
    """Owner: ``unconfirmedTotal`` 1, ``waitingTotal`` 1 (pending requests 0 + the waiting cancellation).  Administrator without the right: ``unconfirmedTotal`` 1 (information) but ``waitingTotal`` 0."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    owner = client(w, "owner").get(f"{SUB}/summary").json()
    admin = client(w, "admin").get(f"{SUB}/summary").json()
    assert (owner["unconfirmedTotal"], owner["waitingTotal"], owner["pendingTotal"]) == (1, 1, 0)
    assert (admin["unconfirmedTotal"], admin["waitingTotal"]) == (1, 0)


def test_the_filters_of_the_list_work_on_the_waiting_rows(pg_session, clock):
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    assert [listing(w, status="pending", **p)["total"] for p in ({"teacherId": str(w.A.id)}, {"teacherId": str(w.B.id)}, {"groupId": str(g.id)}, {"search": "Г"}, {"search": "ЯЯЯ"},
                                                                  {"dateFrom": "2026-11-24"}, {"dateTo": "2026-11-23"})] == [1, 0, 1, 1, 0, 0, 1]


# ============================================================================================ «История»
def test_a_waiting_cancellation_is_not_history_after_the_decision_it_is_with_who_and_when(pg_session, clock):
    """Before: the decided list is EMPTY (the cancellation has no outcome).  After the owner confirms ``centre`` on 27.11 10:00: the pending list is empty, the decided list has the record «Урока не было» with
    ``outcome cancelled_centre`` and who confirmed (the owner) and when; with ``holiday`` the outcome is ``cancelled_holiday``."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    assert listing(w, status="decided")["items"] == []
    clock.set("2026-11-27 10:00")
    set_kind(w, g, "centre")
    assert listing(w, status="pending")["items"] == []
    (rec,) = [i for i in listing(w, status="decided")["items"] if i.get("source") != "audit"]
    assert rec["outcome"] == "cancelled_centre" and rec["confirmedBy"]["role"] == "owner" and rec["confirmedAt"].startswith("2026-11-27")
    w2, g2, p2, m2 = world(pg_session, clock)
    teacher_cancels(w2, g2)
    set_kind(w2, g2, "holiday")
    (rec2,) = [i for i in listing(w2, status="decided")["items"] if i.get("source") != "audit"]
    assert rec2["outcome"] == "cancelled_holiday"


def _held_late(w, g, people, day="2026-11-25"):
    """The teacher «оформил» ``day`` on 26.11 and completed it with two students present: completed one day after its date."""
    reconcile(w, g, day)
    save(w, g, people, day, marks=marks_of(people, first_present=2))


def test_a_lesson_completed_a_day_late_is_an_entry_with_who_and_how_many_days(pg_session, clock):
    """Wed 25.11 completed on Thu 26.11: ONE entry ``backdated``, ``lateDays`` 1, the teacher «Иванов И.» both as the teacher and as who did it, the words of the server; ``backdated_only`` keeps it."""
    w, g, people, members = world(pg_session, clock)
    _held_late(w, g, people)
    for params in ({"status": "decided"}, {"status": "decided", "backdated_only": "true"}):
        items = listing(w, **params)["items"]
        (e,) = [i for i in items if i.get("source") == "audit"]
        assert (e["kind"], e["lateDays"], e["lesson"]["date"], e["group"]["name"], e["teacher"]["name"], e["by"]["name"], e["by"]["role"]) == ("backdated", 1, "2026-11-25", "Г", "Иванов И.", "Иванов И.", "teacher")
        assert e["text"] == "Урок оформлен задним числом: на 1 день позже даты урока"


def test_a_lesson_completed_in_its_own_day_is_not_an_entry(pg_session, clock):
    """Wed 25.11 19:00: «оформил» and completed the same evening - not backdated (0 days): no entry."""
    w, g, people, members = world(pg_session, clock)
    clock.set("2026-11-25 19:00")
    _held_late(w, g, people)
    assert [i for i in listing(w, status="decided")["items"] if i.get("source") == "audit"] == []


def test_a_corrected_lesson_and_an_administrators_action_over_a_teachers_lesson_are_entries(pg_session, clock):
    """The teacher held Wed 25.11 on its day (no entry); on 26.11 the owner corrects it (a reason): ONE entry ``corrected`` by the owner with the reason.  A lesson the teacher left in progress and the
    ADMINISTRATOR completes on 26.11: an entry ``backdated`` by the administrator (``by.role`` admin) over the teacher's lesson."""
    w, g, people, members = world(pg_session, clock, money_right=True)
    clock.set("2026-11-25 19:00")
    _held_late(w, g, people)
    clock.set("2026-11-26 12:00")
    fixed = save(w, g, people, "2026-11-25", who="owner", marks=marks_of(people, first_present=3), correctionReason="Ошибся в отметках")
    assert fixed.status_code == 200
    reconcile(w, g, "2026-11-23", MON)
    save(w, g, people, "2026-11-23", MON, who="admin", marks=marks_of(people, first_present=1))
    entries = {i["kind"]: i for i in listing(w, status="decided")["items"] if i.get("source") == "audit"}
    assert set(entries) == {"corrected", "backdated"}
    assert (entries["corrected"]["by"]["role"], entries["corrected"]["reason"], entries["corrected"]["text"]) == ("owner", "Ошибся в отметках", "Отметки исправлены: Ошибся в отметках")
    assert (entries["backdated"]["by"]["role"], entries["backdated"]["teacher"]["name"], entries["backdated"]["lateDays"]) == ("admin", "Иванов И.", 3)
    only = [i["kind"] for i in listing(w, status="decided", backdated_only="true")["items"]]
    assert sorted(only) == ["backdated", "corrected"]


def test_a_lesson_added_to_the_schedule_by_a_teacher_is_an_entry_by_an_administrator_is_not(pg_session, clock):
    w, g, people, members = world(pg_session, clock)
    body = {"groupId": str(g.id), "dayOfWeek": 5, "startTime": "15:00", "endTime": "16:00", "validFrom": "2026-11-27"}
    assert client(w, "admin").post(f"{API}/org/schedule/lessons", json=body).status_code in (200, 201)
    assert [i for i in listing(w, status="decided")["items"] if i.get("source") == "audit"] == []
    body2 = {**body, "dayOfWeek": 6, "startTime": "16:00", "endTime": "17:00", "validFrom": "2026-11-28"}
    r = client(w, "A").post(f"{API}/org/schedule/lessons", json=body2)
    assert r.status_code in (200, 201), r.text
    (e,) = [i for i in listing(w, status="decided")["items"] if i.get("source") == "audit"]
    assert (e["kind"], e["by"]["name"], e["text"], e["group"]["name"]) == ("lesson_added", "Иванов И.", "Добавил урок в расписание", "Г")


def test_the_history_is_one_list_with_one_order_and_one_page(pg_session, clock):
    """Two entries (26.11 12:00 completion of 25.11, and a request decided before) in ONE list sorted by the time of the event, newest first; ``limit 1`` pages them without losing or doubling one."""
    w, g, people, members = world(pg_session, clock)
    clock.set("2026-11-24 09:00")
    r = client(w, "A").post(SUB, json={"lessons": [{"groupId": str(g.id), "lessonId": MON, "lessonDate": "2026-11-30"}], "reasonCode": "sick"})
    rid = r.json()["created"][0]["id"]
    assert client(w, "owner").post(f"{SUB}/{rid}/decide", json={"decision": "cancel", "reason": "Заболел"}).status_code == 200
    clock.set("2026-11-26 12:00")
    _held_late(w, g, people)
    full = listing(w, status="decided")
    assert full["total"] == 2 and [i.get("source", "request") for i in full["items"]] == ["audit", "request"]
    pages = [listing(w, status="decided", limit=1, offset=o)["items"] for o in (0, 1)]
    assert [len(p) for p in pages] == [1, 1] and [p[0].get("source", "request") for p in pages] == ["audit", "request"]


# ============================================================================================ the block of the teacher
def _mine(w, who="A"):
    return client(w, who).get(f"{SUB}/mine", params={"status": "all"}).json()["items"]


def test_the_block_of_the_teacher_names_what_became_of_his_cancellation(pg_session, clock):
    """«отмена ждёт подтверждения» (waiting 0 days) -> after the confirmation «отменён: по вине центра» -> (other world) «праздник»."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    (item,) = [i for i in _mine(w) if i["lesson"]["date"] == PAST_MON]
    assert (item["outcome"], item["waitingDays"], item["confirmedBy"]) == ("cancel_waiting", 0, None)
    set_kind(w, g, "centre")
    (item,) = [i for i in _mine(w) if i["lesson"]["date"] == PAST_MON]
    assert item["outcome"] == "cancelled_centre" and item["confirmedBy"] is None          # the teacher does not read who decided: the manager's eyes only
    w2, g2, p2, m2 = world(pg_session, clock)
    teacher_cancels(w2, g2)
    set_kind(w2, g2, "holiday")
    assert [i["outcome"] for i in _mine(w2) if i["lesson"]["date"] == PAST_MON] == ["cancelled_holiday"]


def test_the_block_of_the_teacher_after_the_lesson_is_given_back(pg_session, clock):
    """(a) withdrawn by the teacher (age 3, window 7): ``lesson_restored``, ``teacherCanMark`` true.  (b) given back by the owner while the window is 1 day (age 3 > 1): ``teacherCanMark`` false -
    «урок был — отметит администратор».  (c) given back (window 7) and held afterwards by the teacher: ``included_in_payroll``."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    assert client(w, "A").post(f"{API}/org/groups/{g.id}/lessons/withdraw-cancellation", json={"lessonDate": PAST_MON, "lessonId": MON}).status_code == 200
    (a,) = [i for i in _mine(w) if i["lesson"]["date"] == PAST_MON and i["status"] == "cancelled"]
    assert (a["outcome"], a["teacherCanMark"]) == ("lesson_restored", True)
    w2, g2, p2, m2 = world(pg_session, clock)
    teacher_cancels(w2, g2)
    assert client(w2, "owner").put(f"{API}/org/settings/lessons", json={"teacher_lesson_window_days": 1}).status_code == 200
    assert client(w2, "owner").post(f"{API}/org/groups/{g2.id}/lessons/restore", json={"lessonDate": PAST_MON, "lessonId": MON, "reason": "Урок был"}).status_code == 200
    (b,) = [i for i in _mine(w2) if i["lesson"]["date"] == PAST_MON and i["status"] == "cancelled"]
    assert (b["outcome"], b["teacherCanMark"]) == ("lesson_restored", False)
    w3, g3, p3, m3 = world(pg_session, clock)
    teacher_cancels(w3, g3)
    assert client(w3, "owner").post(f"{API}/org/groups/{g3.id}/lessons/restore", json={"lessonDate": PAST_MON, "lessonId": MON, "reason": "Урок был"}).status_code == 200
    reconcile(w3, g3, PAST_MON, MON)
    save(w3, g3, p3, PAST_MON, MON, marks=marks_of(p3, first_present=2))
    (c,) = [i for i in _mine(w3) if i["lesson"]["date"] == PAST_MON and i["status"] == "cancelled"]
    assert c["outcome"] == "included_in_payroll"


# ============================================================================================ the number of the queries
class _Counter:
    def __init__(self, engine):
        self.engine, self.n = engine, 0

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._hook)
        return self

    def __exit__(self, *a):
        event.remove(self.engine, "before_cursor_execute", self._hook)

    def _hook(self, conn, cursor, statement, params, context, executemany):
        if statement.lstrip()[:6].upper() in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            self.n += 1


def test_the_lists_ask_the_database_no_more_than_before_plus_one(pg_session, clock):
    """Measured on 8136cf6 (before the pack) in the same world - a waiting cancellation (a decided record then), a pending request, a lesson completed a day late: pending 24, decided 22, all 22,
    summary 12, mine 19 SELECT / INSERT / UPDATE / DELETE statements.  Now each is at most +1 (the union keeps the count and the page; the three counters of the badge are ONE query)."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    clock.set("2026-11-24 12:00")
    assert client(w, "A").post(SUB, json={"lessons": [{"groupId": str(g.id), "lessonId": MON, "lessonDate": "2026-11-30"}], "reasonCode": "sick"}).status_code == 201
    clock.set("2026-11-26 12:00")
    _held_late(w, g, people)
    before = {"pending": 24, "decided": 22, "all": 22, "summary": 12, "mine": 19}
    seen = {}
    for name, who, path, params in (("pending", "owner", "", {"status": "pending"}), ("decided", "owner", "", {"status": "decided"}), ("all", "owner", "", {"status": "all"}),
                                    ("summary", "owner", "/summary", {}), ("mine", "A", "/mine", {})):
        with _Counter(w.db.get_bind()) as c:
            assert client(w, who).get(f"{SUB}{path}", params=params).status_code == 200
        seen[name] = c.n
    room = {"mine": 2}          # С10.2: the block of the teacher reads the window of the centre once (``canWithdraw``): +1 more, a constant (not per row) - measured 21
    assert {k: v <= before[k] + room.get(k, 1) for k, v in seen.items()} == {k: True for k in before}, (seen, before)
