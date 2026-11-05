"""Money-1 (stage walk D-4): the REGISTRY of the closed month - which routes move the pay of a teacher or the accruals of a student, and what each does in a month a saved sheet has closed.

D-4 happened because a new road to the pay of the teacher («Оплатить» an additional lesson) had no check of the closed month of its own: the decision was accepted and the saved sheet of a closed month
drifted.  The rights guard (``test_money1_rights_guard.py``) says WHO may touch money; this one says WHAT happens when the month is closed.  Every non-GET route of ``/org/*`` is in exactly one place:

* ``REFUSES``  - asks the ONE rule (``closed_period.ClosedPeriods.assert_open`` / ``assert_lesson_open``): 409 ``PERIOD_CLOSED`` (422 ``PAUSE_LOCKED`` for a pause) with «Переоткрыть месяц» as the way out.
  Every such route has a SCENARIO below (``SCENARIOS``): a closed month -> the refusal, and the whole database - EVERY table - is the same row by row;
* ``SKIPS``    - acts on the open months only and names the lessons of the closed ones that it left alone (a day off);
* ``ALLOWED``  - deliberately allowed in a closed month, WITH THE REASON (a payment of a student is a fact from outside: it is accepted and the answer says the sheet changed, ДП-07, ...);
* the routes of the rights guard's ``NOT_MONEY`` (it keeps their reasons) that do not touch a held lesson.

A new route without a place fails ``test_every_route_has_a_place_for_the_closed_month``; a route that is not in the registry but whose handler calls a service that moves a held lesson fails too.
Per LESSONS 94 each test first asserts that its walk FOUND what it is about."""
import inspect
import re
from datetime import date

import pytest

from tests.test_money1_rights_guard import (
    CONDITIONAL, INLINE, NOT_MONEY, OWNER_ONLY_PREFIXES, P, PAYMENT_MODULES, ROUTES, SERVICE_CHECKED, WHOLE,
)

R = lambda method, path: (method, f"{P}{path}")  # noqa: E731

# ---------------------------------------------------------------------------------------------------- the registry
REFUSES = {
    R("POST", "/org/attendance/excuses/{excuse_id}/review"): "a decision on an excuse moves the pack of the student and the pay of the teacher",
    R("POST", "/org/attendance/excuses/{excuse_id}/reset"): "taking the decision back",
    R("POST", "/org/attendance/excuses/review-bulk"): "the same for many: the closed one is a skipped item with the code PERIOD_CLOSED, the answer is 200",
    R("POST", "/org/memberships/{membership_id}/pauses"): "a pause takes lessons off the pay of the teacher (422 PAUSE_LOCKED)",
    R("PATCH", "/org/pauses/{pause_id}"): "moving a pause over the days of a closed month",
    R("POST", "/org/pauses/{pause_id}/void"): "cancelling a pause gives the lessons of its days back to the pay",
    R("PUT", "/org/groups/{group_id}/lesson-type"): "the type of lessons from a date sets the rate of the teacher backwards",
    R("PUT", "/org/groups/{group_id}/attendance/cancellation-kind"): "the kind of a cancelled lesson sets the deduction of the students",
    R("POST", "/org/groups/{group_id}/attendance"): "marks of a held lesson / the cancellation of a missed one",
    R("POST", "/org/groups/{group_id}/attendance/return"): "a held lesson goes back to «не проведён»",
    R("POST", "/org/groups/{group_id}/attendance/impact"): "the preview of a correction of a held lesson (it refuses like the correction)",
    R("POST", "/org/groups/{group_id}/attendance/reconcile"): "«Оформить»: a lesson formalised backwards",
    R("POST", "/org/groups/{group_id}/attendance/reconcile/cancel"): "«Урока не было» for a lesson nobody started",
    R("PUT", "/org/groups/{group_id}/lesson-teacher"): "who taught a held lesson",
    R("POST", "/org/groups/{group_id}/lessons/restore"): "the return of a cancelled lesson gives the deductions back",
    R("POST", "/org/one-off-lessons/{lesson_id}/money-decision"): "«Оплатить» / «Не оплачивать» / «Отменить решение» (stage walk D-4)",
    R("POST", "/org/substitutions/{request_id}/decide"): "a decision on a request for a lesson that has begun (a substitute becomes the one who taught / the lesson is cancelled)",
    R("POST", "/org/substitutions/decide-bulk"): "the same for many: a failed item with the code PERIOD_CLOSED",
}

SKIPS = {
    R("POST", "/org/days-off"): "a day off cancels the lessons of its days; a held lesson / a cancellation of a closed month is never touched and the answer names it",
    R("POST", "/org/days-off/preview"): "the plan: the closed lessons are in the list of those that cannot be cancelled",
    R("POST", "/org/days-off/{day_off_id}/apply"): "the same as the creation",
    R("POST", "/org/days-off/{day_off_id}/void"): "«Отменить выходной» gives back only its own untouched future lessons",
}

_PAYMENT = ("ДП-07: a payment of a student is a fact from outside: it is accepted in a closed month, the answer names the sheet (``money_effects.payroll[].state: closed``) and the saved sheet "
            "gets the line «изменилось на ±N» + «Пересохранить»")
_ROSTER = "the roster of a held lesson is frozen in its row; what changes is the money of the student (a fact, like a payment), the saved sheets are not rewritten"
ALLOWED = {
    R("POST", "/org/payroll-periods/{month}/reopen"): "this IS the way out of a closed month (a reason, the journal, the revision of the sheet)",
    R("POST", "/org/memberships/{membership_id}/payments"): _PAYMENT, R("POST", "/org/payments/{payment_id}/void"): _PAYMENT, R("POST", "/org/payments/{payment_id}/replace"): _PAYMENT,
    R("POST", "/org/memberships/{membership_id}/writeoffs"): _PAYMENT, R("POST", "/org/memberships/{membership_id}/refunds"): _PAYMENT,
    R("POST", "/org/writeoffs/{writeoff_id}/void"): _PAYMENT, R("POST", "/org/refunds/{refund_id}/void"): _PAYMENT,
    R("PUT", "/org/groups/{group_id}/price"): "a price of a group is the decision of the centre about the money of the students; the closed sheet is told «изменилось на ±N» (A3), not rewritten",
    R("PUT", "/org/memberships/{membership_id}/price-override"): "an individual price: the same as the price of the group",
    R("PUT", "/org/memberships/{membership_id}/opening-balance"): "an opening balance is the money of the student, like a payment",
    R("POST", "/org/opening-balances/{opening_id}/void"): "the same",
    R("PUT", "/org/groups/{group_id}"): "the type of lessons is changed FROM TODAY there (a backdated one is ``lesson-type``, which refuses); the teacher of the group takes the FUTURE lessons - held ones keep theirs",
    R("POST", "/org/groups/{group_id}/students/{student_id}"): _ROSTER, R("POST", "/org/groups/{group_id}/students/bulk"): _ROSTER,
    R("DELETE", "/org/groups/{group_id}/students/{student_id}"): _ROSTER, R("POST", "/org/groups/{group_id}/students/{student_id}/leave"): _ROSTER,
    R("POST", "/org/students/{student_id}/archive"): _ROSTER, R("POST", "/org/import/preview"): "a read of a file (the opening-balance columns are money of students)",
    R("POST", "/org/import/commit"): "the opening-balance columns are money of students (like a payment); lessons are not touched",
    R("POST", "/org/groups/{group_id}/lesson-move"): "moves a lesson that has NOT begun (409 LESSON_ALREADY_STARTED); a lesson of a closed month is past by definition",
    R("POST", "/org/payroll/months/{month}/calculate"): "a calculation writes nothing",
    R("POST", "/org/payroll/sheets"): "saving the sheet IS the closing; saving it again after a reopening closes the month again",
    R("PATCH", "/org/payroll/sheets/{sheet_id}"): "re-saving a sheet (the owner only): the line «изменилось на ±N» is exactly what it is for",
    R("POST", "/org/payroll/sheets/{sheet_id}/payouts"): "a payout is a fact against the SAVED total, it never changes the sheet",
    R("POST", "/org/payroll/payouts/{payout_id}/void"): "the same",
    R("PUT", "/org/payroll/rates/{teacher_id}"): "a rate from a month: the saved sheets of the closed months are told «изменилось на ±N» (A3), not rewritten (the owner only)",
    R("PUT", "/org/payroll/settings"): "the divisor from a month: the same (the owner only)",
    R("PUT", "/org/settings/payments"): "the settings of payments (the owner only)",
}

# routes that are not money for the RIGHTS guard (it keeps the reasons) but act on a held lesson or on who is paid for it: they must be in the registry above
LESSON_CALLS = re.compile(
    r"reconcile_missed|cancel_missed|save_group_attendance|restore_lesson|return_completed_lesson|change_cancellation_kind|correction_impact|\.review\(|\.reset\(|review_bulk|PauseService|"
    r"LessonTypeService|DaysOffService|OneOffPurpose|_purposes\(db\)|\.decide\(|decide_bulk|move_lesson|cancel_lesson|LessonTeachers\(")

ORG_WRITES = sorted(k for k in ROUTES if k[0] != "GET" and k[1].startswith(f"{P}/org"))
CLASSES = {"REFUSES": REFUSES, "SKIPS": SKIPS, "ALLOWED": ALLOWED}


def _module(key):
    return ROUTES[key][1].endpoint.__module__.rsplit(".", 1)[-1]


def _owner_only(key):
    return key[1].startswith(OWNER_ONLY_PREFIXES)


# ---------------------------------------------------------------------------------------------------- the registry is complete (no database)
def test_the_walk_found_the_routes_this_registry_is_about():
    assert len(ORG_WRITES) >= 85, len(ORG_WRITES)
    assert len(REFUSES) >= 15 and len(SKIPS) == 4 and len(ALLOWED) >= 25
    missing = [k for c in CLASSES.values() for k in sorted(c) if k not in ROUTES]
    assert not missing, f"records of routes that do not exist (renamed? the registry must follow): {missing}"
    assert all(reason.strip() for c in CLASSES.values() for reason in c.values()), "every record carries its reason"


def test_every_route_has_a_place_for_the_closed_month():
    """The ban by default: a new non-GET route of ``/org/*`` is in a class of this file, in the rights guard's ``NOT_MONEY`` (with its reason there), or is a payment / owner-only route."""
    unclassified = [k for k in ORG_WRITES
                    if not any(k in c for c in CLASSES.values()) and k not in NOT_MONEY and _module(k) not in PAYMENT_MODULES and not _owner_only(k)]
    assert not unclassified, ("routes with no place in tests/test_money1_closed_month_guard.py: say what they do in a closed month (REFUSES + a scenario / SKIPS / ALLOWED + the reason), or put "
                              f"them in NOT_MONEY of the rights guard if they touch no lesson and no sum: {unclassified}")
    double = [k for k in ORG_WRITES if sum(k in c for c in CLASSES.values()) > 1]
    assert not double, f"routes in two classes: {double}"


def test_the_money_routes_of_the_rights_guard_are_all_classified_here():
    """Every route the rights guard calls money (WHOLE / CONDITIONAL / INLINE / the service-checked ones) has a closed-month class - the two registries cannot drift apart."""
    money = set(WHOLE) | set(CONDITIONAL) | set(INLINE) | set(SERVICE_CHECKED)
    assert len(money) >= 35, len(money)
    gap = [k for k in sorted(money) if not any(k in c for c in CLASSES.values())]
    assert not gap, f"money routes without a closed-month class: {gap}"


def test_a_route_outside_the_registry_does_not_call_a_service_that_moves_a_held_lesson():
    """The code is the second witness (as in the rights guard): a NOT_MONEY handler whose source calls a service of the lessons / their pay must be in a class of this file."""
    outside = [k for k in ORG_WRITES if not any(k in c for c in CLASSES.values())]
    assert len(outside) >= 25, len(outside)
    wrong = [k for k in outside if LESSON_CALLS.search(inspect.getsource(ROUTES[k][1].endpoint))]
    assert not wrong, f"routes that act on held lessons and have no closed-month class: {wrong}"


def test_every_refusing_route_has_a_scenario_and_every_scenario_a_refusing_route():
    covered = {s.route for s in SCENARIOS.values()}
    assert set(REFUSES) <= covered, f"REFUSES routes without a scenario: {sorted(set(REFUSES) - covered)}"
    assert covered <= set(REFUSES), f"scenarios of routes that are not in REFUSES: {sorted(covered - set(REFUSES))}"
    assert len(SCENARIOS) >= 20, len(SCENARIOS)


# ---------------------------------------------------------------------------------------------------- the scenarios (PostgreSQL)
from tests.money1_helpers import changed_tables, db_fingerprint, reopen_now  # noqa: E402
from tests.ops_helpers import clock, closed_sheet, needs_pg, pg_session  # noqa: E402,F401  (fixtures)
from tests.payf_helpers import API, client, pause  # noqa: E402
from tests.test_p154_money_pg import build_org, decide, extra_world, hold, make  # noqa: E402

OCT = date(2026, 10, 1)


class Scenario:
    """``route`` is the key of the registry; ``who`` the caller; ``call(ctx)`` -> the response; ``world`` is ``money`` (an October of five students with held lessons, an additional lesson, an excuse,
    a pause, a cancelled lesson, two lessons nobody started) or ``subs`` (the world of the substitutions); ``expect`` the refusal."""

    def __init__(self, route, who, call, *, world="money", status=409, code="PERIOD_CLOSED", skipped=False):
        self.route, self.who, self.call, self.world, self.status, self.code, self.skipped = route, who, call, world, status, code, skipped


def _rest(w, who, method, path, **kw):
    return client(w, who).request(method, f"{API}{path}", **kw)


GROUP = "/org/groups/{g}"
SCENARIOS = {}


def scenario(name, route, who="owner", **options):
    def register(fn):
        SCENARIOS[name] = Scenario(route, who, fn, **options)
        return fn

    return register


@scenario("excuse-review", R("POST", "/org/attendance/excuses/{excuse_id}/review"))
def _(c):
    return _rest(c.w, "owner", "POST", f"/org/attendance/excuses/{c.pending_excuse}/review", json={"decision": "confirmed"})


@scenario("excuse-reset", R("POST", "/org/attendance/excuses/{excuse_id}/reset"))
def _(c):
    return _rest(c.w, "owner", "POST", f"/org/attendance/excuses/{c.confirmed_excuse}/reset", json={})


@scenario("excuse-review-bulk", R("POST", "/org/attendance/excuses/review-bulk"), status=200, skipped=True)
def _(c):
    return _rest(c.w, "owner", "POST", "/org/attendance/excuses/review-bulk", json={"ids": [str(c.pending_excuse)], "decision": "confirmed"})


@scenario("pause-create", R("POST", "/org/memberships/{membership_id}/pauses"), code="PAUSE_LOCKED", status=422)
def _(c):
    return _rest(c.w, "owner", "POST", f"/org/memberships/{c.ms[1].id}/pauses", json={"first_day": "2026-10-19", "last_day": "2026-10-20"})


@scenario("pause-patch", R("PATCH", "/org/pauses/{pause_id}"), code="PAUSE_LOCKED", status=422)
def _(c):
    return _rest(c.w, "owner", "PATCH", f"/org/pauses/{c.pause.id}", json={"first_day": "2026-10-12", "last_day": "2026-10-16"})


@scenario("pause-void", R("POST", "/org/pauses/{pause_id}/void"), code="PAUSE_LOCKED", status=422)
def _(c):
    return _rest(c.w, "owner", "POST", f"/org/pauses/{c.pause.id}/void", json={"reason": "Ошибка"})


@scenario("lesson-type", R("PUT", "/org/groups/{group_id}/lesson-type"))
def _(c):
    return _rest(c.w, "owner", "PUT", f"/org/groups/{c.g.id}/lesson-type", json={"lesson_type": "pair", "valid_from": "2026-10-10"})


@scenario("cancellation-kind", R("PUT", "/org/groups/{group_id}/attendance/cancellation-kind"))
def _(c):
    return _rest(c.w, "owner", "PUT", f"/org/groups/{c.g.id}/attendance/cancellation-kind",
                 json={"lessonDate": "2026-10-26", "lessonId": "lesson-mon", "cancellationKind": "holiday", "reason": "Это был праздник"})


@scenario("attendance-save", R("POST", "/org/groups/{group_id}/attendance"))
def _(c):
    marks = {str(s.id): ("absent" if i == 0 else "present") for i, s in enumerate(c.students)}
    return _rest(c.w, "owner", "POST", f"/org/groups/{c.g.id}/attendance",
                 json={"lessonDate": "2026-10-21", "lessonId": "lesson-wed", "status": "completed", "records": marks, "correctionReason": "Ошибся в отметке"})


@scenario("attendance-return", R("POST", "/org/groups/{group_id}/attendance/return"))
def _(c):
    return _rest(c.w, "owner", "POST", f"/org/groups/{c.g.id}/attendance/return", json={"lessonDate": "2026-10-21", "lessonId": "lesson-wed", "reason": "Урока не было", "expectedVersion": 1})


@scenario("attendance-impact", R("POST", "/org/groups/{group_id}/attendance/impact"))
def _(c):
    return _rest(c.w, "owner", "POST", f"/org/groups/{c.g.id}/attendance/impact",
                 json={"lessonDate": "2026-10-21", "lessonId": "lesson-wed", "records": {str(s.id): "absent" for s in c.students}})


@scenario("attendance-reconcile", R("POST", "/org/groups/{group_id}/attendance/reconcile"))
def _(c):
    return _rest(c.w, "owner", "POST", f"/org/groups/{c.g.id}/attendance/reconcile", json={"lessonDate": "2026-10-28", "lessonId": "lesson-wed"})


@scenario("attendance-reconcile-cancel", R("POST", "/org/groups/{group_id}/attendance/reconcile/cancel"))
def _(c):
    return _rest(c.w, "owner", "POST", f"/org/groups/{c.g.id}/attendance/reconcile/cancel",
                 json={"lessonDate": "2026-10-23", "lessonId": "lesson-fri", "cancellationReason": "Урока не было", "cancellationKind": "centre"})


@scenario("lesson-teacher", R("PUT", "/org/groups/{group_id}/lesson-teacher"))
def _(c):
    return _rest(c.w, "owner", "PUT", f"/org/groups/{c.g.id}/lesson-teacher",
                 json={"lessonDate": "2026-10-21", "lessonId": "lesson-wed", "teacherId": str(c.w.B.id), "reason": "Провёл другой преподаватель"})


@scenario("lesson-restore", R("POST", "/org/groups/{group_id}/lessons/restore"))
def _(c):
    return _rest(c.w, "owner", "POST", f"/org/groups/{c.g.id}/lessons/restore", json={"lessonDate": "2026-10-26", "lessonId": "lesson-mon", "reason": "Урок был"})


def _decision(decision):
    return lambda c: _rest(c.w, "owner", "POST", f"/org/one-off-lessons/{c.undecided_extra if decision != 'undo' else c.decided_extra}/money-decision", json={"decision": decision})


for _d in ("pay", "skip", "undo"):
    SCENARIOS[f"money-decision-{_d}"] = Scenario(R("POST", "/org/one-off-lessons/{lesson_id}/money-decision"), "owner", _decision(_d))
    SCENARIOS[f"money-decision-{_d}-admin-preview"] = Scenario(R("POST", "/org/one-off-lessons/{lesson_id}/money-decision"), "admin", lambda c, d=_d: _rest(
        c.w, "admin", "POST", f"/org/one-off-lessons/{c.undecided_extra if d != 'undo' else c.decided_extra}/money-decision", params={"dry_run": "true"}, json={"decision": d}))


def _substitution(decision_body):
    def call(c):
        return _rest(c.w, "admin", "POST", f"/org/substitutions/{c.request_id}/decide", json=decision_body(c))

    return call


SCENARIOS["substitution-assign"] = Scenario(R("POST", "/org/substitutions/{request_id}/decide"), "admin",
                                            _substitution(lambda c: {"decision": "substitute", "teacherId": str(c.w.D.id)}), world="subs")
SCENARIOS["substitution-cancel"] = Scenario(R("POST", "/org/substitutions/{request_id}/decide"), "admin",
                                            _substitution(lambda c: {"decision": "cancel", "reason": "Урока не было"}), world="subs")
SCENARIOS["substitution-decide-bulk"] = Scenario(R("POST", "/org/substitutions/decide-bulk"), "admin", lambda c: _rest(
    c.w, "admin", "POST", "/org/substitutions/decide-bulk",
    json={"ids": [str(c.request_id)], "decision": "substitute", "teacherId": str(c.w.D.id)}), world="subs", status=200, skipped=True)


class Ctx:
    pass


def money_world(pg_session, clock):
    """October of teacher A: five students paid; held lessons (21.10 with a confirmed excuse of the second student, 29.10 and 30.10 additional ones), the cancellation of 26.10 by the centre, two lessons
    nobody started (23.10, 28.10), a pending excuse, a pause; October is CLOSED by a sheet saved on 02.11 and today is 04.11."""
    from src.models.auth import AttendanceExcuse, GroupAttendance

    c = Ctx()
    c.w = w = build_org(pg_session)
    g, c.students, c.ms, extra = extra_world(w, clock, "wait", "A")
    c.g = g
    a, b, cc, d, e = c.students
    clock.set("2026-10-29 17:30")
    c.undecided_extra = make(w, "A", g, "2026-10-30", start="15:00", end="16:00", purpose="extra", money="wait").json()["id"]
    clock.set("2026-10-30 17:00")
    hold(w, g, c.undecided_extra, "2026-10-30", {a: "present"}, start="15:00", end="16:00")
    decide(w, "owner", extra, "pay")
    c.decided_extra = extra
    hold(w, g, "lesson-wed", "2026-10-21", {a: "present", b: "excused", cc: "present", d: "present", e: "present"}, start="10:00", end="11:00", confirmed=(b,))
    held = w.db.query(GroupAttendance).filter_by(group_id=g.id, lesson_id=extra).one()
    pending = AttendanceExcuse(org_id=w.org.id, attendance_id=held.id, student_id=e.id, status="pending", reason="Справка", reason_by=w.owner.id, reason_at=held.created_at)
    w.db.add(pending)
    w.db.commit()
    c.pending_excuse = pending.id
    c.confirmed_excuse = w.db.query(AttendanceExcuse).filter_by(org_id=w.org.id, status="confirmed").one().id
    c.pause = pause(w, c.ms[2], date(2026, 10, 12), date(2026, 10, 13))
    closed_sheet(w, w.A, OCT, saved="2026-11-02 12:00")
    clock.set("2026-11-04 12:00")
    return c


def substitutions_world(pg_session, clock):
    """The world of the substitutions: a request for a lesson of 28.10 asked on 22.10; on 05.12 October of teacher A is closed by his saved sheet."""
    from tests.b2_helpers import build_d2, req

    c = Ctx()
    clock.set("2026-10-22 11:00")
    c.w = w = build_d2(pg_session)
    from tests.test_ops_b2_decide_pg import one
    from tests.ops_helpers import grant_money_right

    grant_money_right(w)
    c.request_id = one(w, "A", "L2", "2026-10-28")["id"]
    clock.set("2026-12-05 10:00")
    closed_sheet(w, w.A, OCT)
    return c


WORLDS = {"money": money_world, "subs": substitutions_world}


@needs_pg
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_a_closed_month_refuses_and_nothing_is_written(name, pg_session, clock):
    """For EVERY route of the class REFUSES: the month of the teacher is closed -> the refusal of the one rule (the code, the month, the teacher, the day of the closing and the way out), and the
    database - every table, row by row - is the same afterwards (nothing written: not a row of the action, not an audit row)."""
    s = SCENARIOS[name]
    c = WORLDS[s.world](pg_session, clock)
    before = db_fingerprint(c.w.db)
    r = s.call(c)
    assert r.status_code == s.status, (name, r.status_code, r.text[:400])
    body = r.json()
    if s.skipped:                                             # a bulk: the closed one is an item that was skipped / failed, the answer itself is 200
        items = body.get("skipped") or body.get("failed") or [x for x in body.get("items", []) if not x.get("ok", True)] or body.get("results", [])
        assert items and "PERIOD_CLOSED" in str(items), (name, body)
        assert body.get("updated", 0) == 0
    else:
        detail = body["detail"]
        assert detail["code"] == s.code, (name, detail)
        assert "2026-10" in str(detail) or "октябр" in str(detail).lower(), (name, detail)
        if s.code == "PERIOD_CLOSED":
            assert detail["month"] == "2026-10" and detail["closedOn"] and "переоткрывает месяц" in detail["message"], (name, detail)
    assert changed_tables(before, db_fingerprint(c.w.db)) == [], (name, changed_tables(before, db_fingerprint(c.w.db)))


@needs_pg
def test_after_the_reopening_the_decision_on_an_additional_lesson_goes_through(pg_session, clock):
    """The way out is real (not just the refusal): «Переоткрыть месяц» -> the same call is accepted."""
    c = money_world(pg_session, clock)
    r = _rest(c.w, "owner", "POST", f"/org/one-off-lessons/{c.undecided_extra}/money-decision", json={"decision": "pay"})
    assert r.status_code == 409
    reopen_now(c.w.db, c.w.org.id, c.w.A.id, "2026-10", c.w.owner.id, "Решение по доп. уроку")
    clock.set("2026-11-04 13:00")
    assert _rest(c.w, "owner", "POST", f"/org/one-off-lessons/{c.undecided_extra}/money-decision", json={"decision": "pay"}).status_code == 200


@needs_pg
def test_a_day_off_leaves_the_lessons_of_a_closed_month_alone_and_names_them(pg_session, clock):
    """The class SKIPS: a day off on 26.10 (the lesson cancelled by the centre in the closed October) does not turn it into a holiday and does not touch a held lesson."""
    c = money_world(pg_session, clock)
    before = db_fingerprint(c.w.db)
    r = _rest(c.w, "owner", "POST", "/org/days-off", json={"dateFrom": "2026-10-21", "dateTo": "2026-10-26", "reason": "Праздник"})
    assert r.status_code == 201, r.text
    changed = changed_tables(before, db_fingerprint(c.w.db))
    assert changed and all(t.startswith("org_days_off") for t in changed), changed            # the day off itself is written (a decision of the centre) - not one lesson, not one sum
    assert "group_attendance" not in changed and "group_attendance_audit" not in changed and "payroll_sheets" not in changed
    body = r.json()
    reasons = {item["date"]: item["reason"] for item in body["blocked"]}
    assert body["cancelled"] == [] and body["converted"] == [], body                   # nothing of the closed October was cancelled or turned into a holiday
    assert reasons["2026-10-23"] == "Месяц преподавателя закрыт ведомостью: урок не меняется"        # a lesson nobody started
    assert reasons["2026-10-26"].startswith("Отмена «по вине центра» в закрытом месяце") and reasons["2026-10-21"] == "Урок уже проведён"


@needs_pg
def test_a_lesson_of_a_closed_month_cannot_be_moved_because_it_has_begun_and_nothing_is_written(pg_session, clock):
    """The record in ALLOWED for ``lesson-move`` says why it needs no check of its own: a lesson of a closed month is past, and a lesson that has begun is not moved (409 LESSON_ALREADY_STARTED)."""
    c = money_world(pg_session, clock)
    before = db_fingerprint(c.w.db)
    r = _rest(c.w, "owner", "POST", f"/org/groups/{c.g.id}/lesson-move",
              json={"lessonId": "lesson-wed", "lessonDate": "2026-10-28", "newDate": "2026-11-11", "startTime": "10:00", "endTime": "11:00", "reason": "Перенос"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "LESSON_ALREADY_STARTED", r.text
    assert changed_tables(before, db_fingerprint(c.w.db)) == []
