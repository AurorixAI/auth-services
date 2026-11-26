"""LESSON-RULES С8: the temporary gate of the month (until the pack PAYROLL-MONTH): the sheet that CLOSES a month (saved after its end) is refused while the teacher has waiting cancellations in it.

World: ``lesson_rules_helpers.world`` + the percent of the teacher A.  The teacher cancels Mon 23.11 on 26.11 (``unconfirmed``).  The month of November is over on 02.12 (today then).  Hand: the gate
is on the SERVER, whatever the client sends (``confirm_open_lessons`` true / false / absent, POST and PATCH): 409 ``UNCONFIRMED_CANCELLATIONS``, ``count`` 1, the list of the lessons; after the
decision (any outcome) the sheet is saved."""
from datetime import date

from tests.lesson_rules_helpers import MON, PAST_MON, WED, give_rate, kind_in_db, save_sheet, set_kind, teacher_cancels, world, clock, pg_session, needs_pg, client, API  # noqa: F401
from tests.lesson_rules_helpers import reconcile, save, marks_of

pytestmark = needs_pg


def _waiting_world(pg_session, clock):
    w, g, people, members = world(pg_session, clock)
    give_rate(w)
    teacher_cancels(w, g)
    clock.set("2026-12-02 12:00")
    return w, g, people, members


def test_the_gate_refuses_the_closing_save_whatever_the_client_says(pg_session, clock):
    w, g, people, members = _waiting_world(pg_session, clock)
    for extra in ({}, {"confirm_open_lessons": True}, {"confirm_open_lessons": False}):
        r = save_sheet(w, expect=409, **extra)
        d = r.json()["detail"]
        assert d["code"] == "UNCONFIRMED_CANCELLATIONS" and d["count"] == 1, r.text
        assert [(i["lessonDate"], i["lessonId"], i["groupId"], i["teacherId"]) for i in d["items"]] == [(PAST_MON, MON, str(g.id), str(w.A.id))]
        assert d["teacherId"] == str(w.A.id) and d["month"] == "2026-11" and "ждут подтверждения" in d["message"]
    assert client(w, "owner").get(f"{API}/org/payroll/sheets").json() == []                # nothing was written


def test_the_gate_lets_the_month_close_after_the_decision_of_either_kind(pg_session, clock):
    w, g, people, members = _waiting_world(pg_session, clock)
    save_sheet(w, expect=409)
    clock.set("2026-11-27 12:00")
    set_kind(w, g, "holiday")                                              # any outcome: nothing waits any more
    clock.set("2026-12-02 12:00")
    save_sheet(w)


def test_the_gate_lets_it_close_after_the_lesson_was_given_back(pg_session, clock):
    w, g, people, members = _waiting_world(pg_session, clock)
    clock.set("2026-11-27 12:00")
    assert client(w, "owner").post(f"{API}/org/groups/{g.id}/lessons/restore", json={"lessonDate": PAST_MON, "lessonId": MON, "reason": "Урок был"}).status_code == 200
    clock.set("2026-12-02 12:00")
    # the lesson is back in «без отметки»: the OLD question (open lessons) is asked, not ours - with the answer it saves
    save_sheet(w, confirm_open_lessons=True)


def test_the_gate_is_silent_while_the_month_is_not_over_and_asks_after_it(pg_session, clock):
    """A month that is not over is never closed by a save, so the gate is silent there (26.11: the sheet is only a draft); the same waiting cancellation stops the re-save after the month's end."""
    w, g, people, members = world(pg_session, clock)
    give_rate(w)
    teacher_cancels(w, g)
    save_sheet(w)                                                          # 26.11: the month is not over - a draft, no gate
    clock.set("2026-12-02 12:00")
    save_sheet(w, expect=409, confirm_open_lessons=True, revision=1)


def test_the_patch_of_a_saved_sheet_has_the_same_gate(pg_session, clock):
    """A sheet saved 26.11 (a draft, the month is not over); on 02.12 its re-save (PATCH) would close the month: the same 409."""
    w, g, people, members = world(pg_session, clock)
    give_rate(w)
    teacher_cancels(w, g)
    sid = save_sheet(w).json()["id"]
    clock.set("2026-12-02 12:00")
    r = client(w, "owner").patch(f"{API}/org/payroll/sheets/{sid}", json={"revision": 1, "note": "Проверка", "confirm_open_lessons": True})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "UNCONFIRMED_CANCELLATIONS", r.text


def test_the_sheet_warnings_carry_the_number_next_to_the_other_affairs(pg_session, clock):
    """The month overview / the calculation names the waiting cancellations like it names the open lessons: ``warnings.unconfirmed_cancellations`` = 1 and the list."""
    w, g, people, members = _waiting_world(pg_session, clock)
    r = client(w, "owner").post(f"{API}/org/payroll/months/2026-11/calculate", json={"teacher_ids": [str(w.A.id)]})
    assert r.status_code == 200, r.text
    (sheet,) = r.json()["sheets"]
    assert sheet["warnings"]["unconfirmed_cancellations"] == 1 and sheet["warnings"]["unconfirmed_items"][0]["lessonDate"] == PAST_MON


def test_the_night_check_i4_names_a_closed_month_that_has_a_waiting_cancellation(pg_session, clock):
    """I-4: the sheet of November is closed (saved after the month's end; the fixture writes it directly - the gate would refuse the real save) while the cancellation of 23.11 still waits: ONE finding
    ``closed-unconfirmed:<teacher>:2026-11`` with 1 waiting lesson.  Control: a centre whose month is closed with nothing waiting has no such finding."""
    from src.auth import money_reconcile as mr
    from tests.ops_helpers import closed_sheet

    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    closed_sheet(w, w.A, date(2026, 11, 1), saved="2026-12-02 12:00")
    clock.set("2026-12-05 12:00")
    found = [f for f in mr.reconcile_org(w.db, w.org.id, today=date(2026, 12, 5), snapshots=[]) if f.invariant == "I-4" and f.subject_key.startswith("closed-unconfirmed")]
    assert [(f.subject_key, f.subject_id, f.month, f.actual, f.details["what"]) for f in found] == [(f"closed-unconfirmed:{w.A.id}:2026-11", str(w.A.id), "2026-11", 1, "unconfirmed_cancellations")]
    w2, g2, people2, members2 = world(pg_session, clock)
    closed_sheet(w2, w2.A, date(2026, 11, 1), saved="2026-12-02 12:00")
    assert [f for f in mr.reconcile_org(w2.db, w2.org.id, today=date(2026, 12, 5), snapshots=[]) if f.subject_key.startswith("closed-unconfirmed")] == []
