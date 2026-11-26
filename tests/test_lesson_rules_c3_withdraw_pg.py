"""LESSON-RULES С3: the teacher withdraws HIS OWN waiting cancellation (``POST .../lessons/withdraw-cancellation``) while it is ``unconfirmed`` and inside the window of the centre.

World: ``lesson_rules_helpers.world`` (group «Г» of the teacher A, nine students, 450 000, today Thu 26.11 12:00; the teacher cancelled Mon 23.11 = 3 days ago, window 7).  By hand: the withdrawal
moves no account (the cancellation moved none), the lesson is again in the list «без отметки» of the teacher, the trace says «teacher»."""
from datetime import date

from src.models.auth import GroupAudit, LessonSubstitutionRequest as Req

from tests.lesson_rules_helpers import MON, PAST_MON, WED, accounts, kind_in_db, set_kind, teacher_cancels, world, clock, pg_session, needs_pg, client, API  # noqa: F401  (fixtures)
from tests.ops_helpers import closed_sheet
from tests.payf_helpers import cancel

pytestmark = needs_pg
URL = "{api}/org/groups/{g}/lessons/withdraw-cancellation"


def withdraw(w, g, who="A", day=PAST_MON, lesson=MON, expect=200, **extra):
    r = client(w, who).post(URL.format(api=API, g=g.id), json={"lessonDate": day, "lessonId": lesson, **extra})
    assert r.status_code == expect, r.text
    return r


def test_the_teacher_withdraws_his_waiting_cancellation(pg_session, clock):
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    r = withdraw(w, g, expectedKind="unconfirmed")
    assert r.json()["restored"] is True and r.json()["previousKind"] == "unconfirmed"
    assert "moneyEffects" not in r.json() and "deductions" not in r.json()                # the teacher reads no money
    assert kind_in_db(w, g) is None and accounts(w, members) == [(0, 0, 0)] * 9
    open_ = client(w, "A").get(f"{API}/org/attendance/open-actions").json()
    assert (PAST_MON, MON) in {(i["lessonDate"], i["lessonId"]) for i in open_}
    w.db.expire_all()
    audit = [a for a in w.db.query(GroupAudit).filter_by(group_id=g.id, action="lesson_restored")]
    assert len(audit) == 1 and audit[0].actor_role == "teacher" and audit[0].actor_id == w.A.id
    from src.models.auth import LessonSubstitutionEvent

    triggers = [e.snapshot.get("trigger") for e in w.db.query(LessonSubstitutionEvent).filter_by(org_id=w.org.id)]
    assert "teacher_withdraw" in triggers and "manager_restore" not in triggers          # the history says «the teacher», not «an administrator»
    assert {r.decided_via for r in w.db.query(Req).filter_by(org_id=w.org.id) if r.closed_reason == "LESSON_RESTORED"} == {"teacher_withdraw"}


def test_the_owner_who_decides_after_the_withdrawal_is_told_who_withdrew(pg_session, clock):
    """409 ``ALREADY_DECIDED`` for the owner's confirmation: ``kind`` null, outcome ``withdrawn_by_teacher``, «Преподаватель отозвал отмену в 12:00.»; the same on «Урок был»."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    withdraw(w, g)
    d = set_kind(w, g, "centre", expect=409).json()["detail"]
    assert d["code"] == "ALREADY_DECIDED" and d["kind"] is None and d["outcome"] == "withdrawn_by_teacher" and d["decided_by_role"] == "teacher"
    assert d["message"] == "Преподаватель отозвал отмену в 12:00."
    assert accounts(w, members) == [(0, 0, 0)] * 9


def test_after_the_confirmation_the_teacher_cannot_withdraw(pg_session, clock):
    """Confirmed ``centre`` (50 000 each): the teacher's withdrawal is 409 ``ALREADY_DECIDED`` with ``kind: centre``; nothing changes."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    set_kind(w, g, "centre")
    d = withdraw(w, g, expect=409).json()["detail"]
    assert d["code"] == "ALREADY_DECIDED" and d["kind"] == "centre"
    assert kind_in_db(w, g) == ("cancelled", "centre") and accounts(w, members) == [(50_000, 0, 50_000)] * 9


def test_only_his_own_cancellation_only_the_teacher_of_the_lesson(pg_session, clock):
    """A cancellation of the administrator (without the right -> ``unconfirmed``) is not the teacher's own: 403.  Another teacher B: 403.  The teacher's own is withdrawn."""
    w, g, people, members = world(pg_session, clock)
    cancel(w, g, MON, PAST_MON, who="admin")
    assert kind_in_db(w, g) == ("cancelled", "unconfirmed")
    assert withdraw(w, g, who="A", expect=403).json()["detail"]["code"] == "NOT_YOUR_CANCELLATION"
    teacher_cancels(w, g, "2026-11-25", WED)
    assert withdraw(w, g, who="B", day="2026-11-25", lesson=WED, expect=403).status_code == 403
    withdraw(w, g, day="2026-11-25", lesson=WED)
    assert kind_in_db(w, g, "2026-11-25", WED) is None and kind_in_db(w, g) == ("cancelled", "unconfirmed")


def test_the_window_of_the_centre_decides_not_seven(pg_session, clock):
    """Mon 16.11 is 10 days old: the teacher cancelled it as ... he could not (window 7).  So the owner sets the window to 14, the teacher cancels it (``unconfirmed``), the window goes back to 3:
    the withdrawal of an age-10 lesson is 403 ``LESSON_TOO_OLD`` (3 days); with 14 it works."""
    w, g, people, members = world(pg_session, clock)
    assert client(w, "owner").put(f"{API}/org/settings/lessons", json={"teacher_lesson_window_days": 14}).status_code == 200
    teacher_cancels(w, g, "2026-11-16", MON)
    assert client(w, "owner").put(f"{API}/org/settings/lessons", json={"teacher_lesson_window_days": 3}).status_code == 200
    d = withdraw(w, g, day="2026-11-16", expect=403).json()["detail"]
    assert d["code"] == "LESSON_TOO_OLD" and d["message"] == "Урок старше 3 дн. — его закрывает администратор или владелец"
    assert client(w, "owner").put(f"{API}/org/settings/lessons", json={"teacher_lesson_window_days": 14}).status_code == 200
    withdraw(w, g, day="2026-11-16")
    assert kind_in_db(w, g, "2026-11-16", MON) is None


def test_a_closed_month_refuses_the_withdrawal(pg_session, clock):
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    closed_sheet(w, w.A, date(2026, 11, 1), saved="2026-12-02 12:00")
    assert withdraw(w, g, expect=409).json()["detail"]["code"] == "PERIOD_CLOSED"
    assert kind_in_db(w, g) == ("cancelled", "unconfirmed")


def test_an_archived_group_the_owner_still_confirms_but_nobody_gives_the_lesson_back(pg_session, clock):
    """SPEC 6.5: the group is archived while the cancellation waits (a group with no students: a group with balances cannot be archived).  The teacher's withdrawal and the owner's «Урок был» are
    409 ``GROUP_ARCHIVED``; the dry run of the confirmation says ``restore_allowed: false`` (the window shows two outcomes); the confirmation itself works (200) and the kind is ``centre``."""
    w, g, people, members = world(pg_session, clock, students=0)
    teacher_cancels(w, g)
    r = client(w, "owner").post(f"{API}/org/groups/{g.id}/archive", json={"missedLessonsAction": "cancel", "reason": "Группа закрыта"})
    assert r.status_code == 200, r.text
    assert withdraw(w, g, expect=409).json()["detail"]["code"] == "GROUP_ARCHIVED"
    assert client(w, "owner").post(f"{API}/org/groups/{g.id}/lessons/restore", json={"lessonDate": PAST_MON, "lessonId": MON, "reason": "Урок был"}).status_code == 409
    d = set_kind(w, g, "centre", dry_run=True).json()
    assert d["restore_allowed"] is False and (d["students"], d["total"]) == (0, 0)
    set_kind(w, g, "centre")
    assert kind_in_db(w, g) == ("cancelled", "centre")
