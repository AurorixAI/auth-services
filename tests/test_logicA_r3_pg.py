"""LOGIC-A, fixes of the lead's R3 review (B1, M1-M3, m1-m13): one test per finding, on PostgreSQL."""
from datetime import date, timedelta

import pytest

from src.models.auth import Group, GroupAudit, GroupLessonTypeHistory, MembershipOpeningBalance

from tests.pg_world import give_group_price, make_client, pg_session, world  # noqa: F401  (fixtures)
from tests.test_payroll_pg import (  # noqa: F401
    API, calc, current_month, lesson, line_of, overview, owner, pay_calendar, preview, previous_month, save, set_rate,
    student_group,
)
from tests.test_logicA_payroll_pg import save_sheet

pytestmark = pytest.mark.skipif(not __import__("os").environ.get("A2_AUTH_TEST_DATABASE_URL"), reason="Requires disposable A2 auth PostgreSQL")


def today():
    from src.auth import payment_status as ps

    return ps.business_today()


# ---- B1: PUT /org/groups/{id} with a new lesson_type goes through the history ------------------------------------------------------------


def test_b1_a_group_update_with_a_new_lesson_type_writes_a_history_row_for_today_and_stales_the_current_sheets(world):
    s0, t = world.students[0], today()
    set_rate(world, world.teacher, "group", "50")
    set_rate(world, world.teacher, "pair", "60")
    cm = current_month()
    give_group_price(world.db, world.group, 600_000, valid_from=date(2026, 1, 1))
    lesson(world, t, {s0: "present"})
    saved = save_sheet(world, cm)
    assert {l["lesson_type"] for l in saved["lines"]} == {"group"}
    r = owner(world).put(f"{API}/org/groups/{world.group.id}", json={"lesson_type": "pair"})
    assert r.status_code == 200, r.text
    world.db.expire_all()
    rows = world.db.query(GroupLessonTypeHistory).filter_by(group_id=world.group.id).order_by(GroupLessonTypeHistory.valid_from).all()
    assert [(x.valid_from, x.lesson_type) for x in rows][-1] == (t, "pair") and rows[0].lesson_type == "group"
    assert world.db.get(Group, world.group.id).lesson_type == "pair"
    audit = world.db.query(GroupAudit).filter_by(group_id=world.group.id, action="group_lesson_type_set").one()
    assert audit.snapshot["valid_from"] == t.isoformat() and audit.snapshot["was"] == "group" and audit.snapshot["lesson_type"] == "pair"
    assert world.db.query(GroupAudit).filter_by(group_id=world.group.id, action="updated").one().snapshot == {"lesson_type": ["group", "pair"]}
    assert owner(world).get(f"{API}/org/groups/{world.group.id}/lesson-types").json()["current"] == "pair"
    assert {l["lesson_type"] for l in preview(world, cm)["lines"]} == {"pair"}               # payroll reads the history
    assert owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}").json()["stale"] is True
    # the same type again writes nothing; a day of the past stays on PUT …/lesson-type
    assert owner(world).put(f"{API}/org/groups/{world.group.id}", json={"lesson_type": "pair", "name": "Новое имя"}).status_code == 200
    world.db.expire_all()
    assert world.db.get(Group, world.group.id).name == "Новое имя"
    assert world.db.query(GroupAudit).filter_by(group_id=world.group.id, action="group_lesson_type_set").count() == 1          # the same type: no new row
    r = owner(world).put(f"{API}/org/groups/{world.group.id}", json={"lesson_type": "individual", "name": "Третье имя"})        # type + name in one save
    assert r.status_code == 200 and world.db.get(Group, world.group.id).name == "Третье имя"
    world.db.expire_all()
    assert world.db.query(GroupAudit).filter_by(group_id=world.group.id, action="group_lesson_type_set").count() == 2
    assert [(x.valid_from, x.lesson_type) for x in world.db.query(GroupLessonTypeHistory).filter_by(group_id=world.group.id).order_by(GroupLessonTypeHistory.valid_from)][-1] == (t, "individual")      # the same day: the row is replaced
    world.db.expire_all()


# ---- M1: the divisor / mode of a lesson come from the ledger segment that covers it --------------------------------------------------------


from tests.test_pay2_api_pg import D, TODAY, fixed_today, membership, seed_payment, settings_block, stay  # noqa: E402,F401


def _coverage(w, m, first=D(2026, 9, 1)):
    from src.auth.payments_service import PaymentService

    w.db.expire_all()
    got = PaymentService(w.db).lesson_coverage(str(w.org.id), [m.id], first, TODAY)
    return {day: (c.mode, c.package_size, c.cover_source) for (_mid, day, _lid), c in sorted(got.items(), key=lambda kv: kv[0][1])}


def test_m1_deferred_lessons_to_calendar_the_running_package_keeps_its_divisor(world, fixed_today):
    """Package of 12 lessons begun 15.09; the centre switches to calendar on 10.10 (deferred): the package is finished first (its lessons
    on 10.10-14.10 keep the divisor 12, mode lessons), the lessons after it belong to the calendar period (no package size)."""
    s0 = world.students[0]
    settings_block(world, accounting_start_date="2026-09-01", period_mode="calendar", billing_history=[
        {"from": "min", "period_mode": "lessons", "lessons_per_payment": 12, "billing_anchor": "payment_date"},
        {"from": "2026-10-10", "defer": True, "period_mode": "calendar", "lessons_per_payment": 12, "billing_anchor": "payment_date"}])
    give_group_price(world.db, world.group, 600_000, valid_from=D(2026, 1, 1))
    m = membership(world, s0)
    stay(world, m, D(2026, 9, 15))
    days = [D(2026, 9, 15) + timedelta(days=2 * i) for i in range(8)] + [D(2026, 10, 11), D(2026, 10, 12), D(2026, 10, 13), D(2026, 10, 14), D(2026, 10, 16), D(2026, 10, 18)]
    for d in days:
        lesson(world, d, {s0: "present"})
    seed_payment(world, m, 600_000, D(2026, 9, 15))
    got = _coverage(world, m)
    pkg, tail = days[:12], days[12:]
    assert {got[d][:2] for d in pkg} == {("lessons", 12)}, got
    assert {got[d][:2] for d in tail} == {("calendar", None)}, got


def test_m1_deferred_calendar_to_lessons_the_running_period_keeps_calendar_and_the_first_package_follows(world, fixed_today):
    s0 = world.students[0]
    settings_block(world, accounting_start_date="2026-09-01", period_mode="lessons", lessons_per_payment=4, billing_history=[
        {"from": "min", "period_mode": "calendar", "billing_anchor": "payment_date"},
        {"from": "2026-10-10", "defer": True, "period_mode": "lessons", "lessons_per_payment": 4, "billing_anchor": "payment_date"}])
    give_group_price(world.db, world.group, 600_000, valid_from=D(2026, 1, 1))
    m = membership(world, s0)
    stay(world, m, D(2026, 9, 15))
    days = [D(2026, 9, 20), D(2026, 10, 12), D(2026, 10, 13), D(2026, 10, 16), D(2026, 10, 17), D(2026, 10, 18)]        # the period 15.09-14.10, then packages
    for d in days:
        lesson(world, d, {s0: "present"})
    seed_payment(world, m, 1_200_000, D(2026, 9, 15))
    got = _coverage(world, m)
    assert {got[d][:2] for d in days[:3]} == {("calendar", None)}, got                 # 12.10 and 13.10 lie in the period that was running when the regime changed
    assert {got[d][:2] for d in days[3:]} == {("lessons", 4)}, got


# ---- M3: pause PATCH - every day that enters the pause is recent, a change of money has a reason -----------------------------------------------


from src.models.auth import MembershipPause  # noqa: E402
from tests.test_logicA_opening_pause_pg import chain1_world, pause  # noqa: E402,F401


def test_m3_a_patch_cannot_lengthen_an_old_pause_over_old_days_and_a_money_change_needs_a_reason(world, fixed_today):
    m = chain1_world(world)
    c = owner(world)
    old = MembershipPause(org_id=world.org.id, membership_id=m.id, student_id=m.student_id, group_id=m.group_id,
                          first_day=D(2026, 8, 1), last_day=D(2026, 8, 10), created_by=world.owner.id)
    world.db.add(old)
    world.db.commit()
    for body in ({"last_day": "2026-08-20", "reason": "продлил"}, {"first_day": "2026-07-20", "reason": "раньше"}):       # the days 11-20.08 / 20-31.07 are older than 30 days
        r = c.patch(f"{API}/org/pauses/{old.id}", json=body)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "PAUSE_RANGE", (body, r.text)
    p = pause(world, m, "2026-10-20", "2026-10-24")["pause"]
    r = c.patch(f"{API}/org/pauses/{p['id']}", json={"last_day": "2026-10-22"})                                     # 2 days are paid again: money moves
    assert r.status_code == 422 and r.json()["detail"]["code"] == "REASON_REQUIRED", r.text
    r = c.patch(f"{API}/org/pauses/{p['id']}", params={"dry_run": True}, json={"last_day": "2026-10-22"})           # the preview needs none
    assert r.status_code == 200 and r.json()["impact"]["charged_delta"] > 0
    world.db.expire_all()
    assert world.db.get(MembershipPause, p["id"]).last_day == D(2026, 10, 24)
    r = c.patch(f"{API}/org/pauses/{p['id']}", json={"last_day": "2026-10-22", "reason": "вернулся 23-го"})
    assert r.status_code == 200 and r.json()["pause"]["days"] == 3, r.text
    world.db.expire_all()
    assert [a.snapshot["reason"] for a in world.db.query(GroupAudit).filter_by(org_id=world.org.id, action="membership_pause_set").order_by(GroupAudit.created_at)][-1] == "вернулся 23-го"


# ---- m1: «просрочено на N дней» counts from the first charged unpaid day ----------------------------------------------------------------------


def test_m1_overdue_days_do_not_count_the_paused_days(world, fixed_today):
    fixed_today(D(2026, 11, 16))
    settings_block(world, accounting_start_date="2026-09-01", billing_anchor="fixed_day", billing_day=1)
    give_group_price(world.db, world.group, 600_000, valid_from=D(2026, 1, 1))
    world.group.schedule = []                                    # no schedule: the periods are charged by days
    world.db.commit()
    m = membership(world, world.students[0])
    stay(world, m, D(2026, 10, 1))
    seed_payment(world, m, 387_000, D(2026, 10, 1))
    r = owner(world).post(f"{API}/org/memberships/{m.id}/pauses", json={"first_day": "2026-10-20", "last_day": "2026-11-15"})
    assert r.status_code == 201, r.text
    rows = owner(world).get(f"{API}/org/payments/status", params={"bucket": "debt"}).json()["items"]
    row = next(x for x in rows if x["membership_id"] == str(m.id))
    # paid through 19.10 (informational), paused 20.10-15.11: the first charged unpaid day is 16.11 = today - overdue for one day, not 28
    assert row["paid_until"] == "2026-10-19" and row["debt"] == 281_000
    assert row["days_overdue"] == 1 and row["debt_reason"] == "overdue" and row["reason"] == "Просрочено на 1 день", row


# ---- m3: a deadlock on «убрать из группы» is a 409 «повторите», not a 500 --------------------------------------------------------------------


def test_m3_leave_answers_409_when_postgresql_finds_a_deadlock(world, monkeypatch):
    from types import SimpleNamespace

    from sqlalchemy.exc import OperationalError

    from src.models.auth import StudentGroup

    s0 = world.students[0]
    original = world.db.commit

    def dead():
        world.db.rollback()
        raise OperationalError("UPDATE", {}, SimpleNamespace(pgcode="40P01"))

    monkeypatch.setattr(world.db, "commit", dead)
    r = owner(world).post(f"{API}/org/groups/{world.group.id}/students/{s0.id}/leave", json={})
    assert r.status_code == 409 and "Повторите" in str(r.json()["detail"]), r.text
    monkeypatch.setattr(world.db, "commit", original)
    world.db.expire_all()
    assert world.db.query(StudentGroup).filter_by(student_id=s0.id, group_id=world.group.id, is_active=True).count() == 1       # nothing was written
    assert owner(world).post(f"{API}/org/groups/{world.group.id}/students/{s0.id}/leave", json={}).status_code == 200            # the retry works


# ---- m4: a group price takes the family locks (memberships of the group, ascending id) before the money what-if -----------------------------


def test_m4_group_price_locks_the_memberships_in_id_order_before_the_what_if(world, fixed_today):
    from sqlalchemy import event

    give_group_price(world.db, world.group, 500_000, valid_from=D(2026, 1, 1))
    for s in world.students[:2]:
        m = membership(world, s)
        stay(world, m, D(2026, 10, 1))
        seed_payment(world, m, 100_000, D(2026, 10, 1))
    statements = []
    engine = world.db.get_bind()

    def spy(conn, cursor, statement, params, context, executemany):
        statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", spy)
    try:
        r = owner(world).put(f"{API}/org/groups/{world.group.id}/price", json={"amount": 600_000, "valid_from": "2026-10-21"})
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    assert r.status_code == 200, r.text
    lock = next(i for i, q in enumerate(statements) if "FROM student_groups" in q and "ORDER BY student_groups.id FOR NO KEY UPDATE" in q)
    ledger = next(i for i, q in enumerate(statements) if q.startswith("SELECT student_payments."))
    assert lock < ledger, "the locks come before the ledger is read for the what-if"


# ---- m5: the card of a student shows a closed membership that still owes (an opening debt alone) ------------------------------------------------


from tests.test_logicA_opening_pause_pg import centre, enrolled, put as put_opening  # noqa: E402,F401


def test_m5_the_card_keeps_a_closed_membership_with_only_an_opening_debt(world, fixed_today):
    centre(world)
    debtor = world.students[0]
    enrolled(world, D(2026, 10, 1), debtor)
    m = membership(world, debtor)
    put_opening(world, m, {"debt_amount": 250_000, "debt_on": "2026-09-30"})
    assert owner(world).post(f"{API}/org/groups/{world.group.id}/students/{debtor.id}/leave", json={}).status_code == 200
    got = owner(world).get(f"{API}/org/students/{debtor.id}/memberships").json()
    assert [r["membership_id"] for r in got] == [str(m.id)] and got[0]["debt"] >= 250_000      # no payment, no write-off: only the opening debt


# ---- m7: money cells of the import - inf / nan / overflow are a row error, never a 500 -----------------------------------------------------------


def test_m7_import_money_cells_inf_nan_overflow_are_row_errors(world, fixed_today):
    from tests.test_logicA_money_pg import import_workbook

    centre(world, 600_000)
    c = owner(world)
    for cell in ("inf", "-inf", "nan", "1e999", "1e400", "∞", "12,5", "9" * 400):
        data = import_workbook([["Один", "Ученик", 7, "", "Импорт-группа", "", None, cell, None]])
        for route in ("preview", "commit"):
            r = c.post(f"{API}/org/import/{route}", files={"file": ("x.xlsx", data)})
            assert r.status_code == 200, (cell, route, r.status_code)
            body = r.json()
            assert body["ok"] is False and any(i["level"] == "error" and i["row"] == 2 for i in body["issues"]), (cell, body["issues"])
    data = import_workbook([["Два", "Ученик", "inf", "", "Импорт-группа", "", None, None, None]])
    assert c.post(f"{API}/org/import/preview", files={"file": ("x.xlsx", data)}).status_code == 200


# ---- m8: query counts of the payroll reads -----------------------------------------------------------------------------------------------------


def test_m8_payroll_reads_collect_the_month_once_and_the_overview_does_not_get_every_sheet(world, monkeypatch):
    from sqlalchemy import event

    from src.auth.payroll_service import PayrollService

    m = previous_month()
    teachers = [world.teacher]
    s0 = world.students[0]
    give_group_price(world.db, world.group, 600_000, valid_from=date(2026, 1, 1))
    for n in (3, 6, 10):
        lesson(world, m.d(n), {s0: "present"})
    pay_calendar(world, s0, m.first, m.last, fee=None)
    saved = save_sheet(world, m)
    calls = []
    original = PayrollService._collect

    def counted(self, *args, **kwargs):
        calls.append(kwargs.get("teacher_ids"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PayrollService, "_collect", counted)
    c = owner(world)
    assert c.get(f"{API}/org/payroll/sheets/{saved['id']}").status_code == 200
    assert len(calls) == 1, calls                                                   # the stale flag and the payout state share one collection
    calls.clear()
    assert c.get(f"{API}/org/payroll/sheets/{saved['id']}/export.xlsx").status_code == 200
    assert len(calls) == 1, calls
    calls.clear()
    assert c.get(f"{API}/org/payroll/months/{m.label}/export.xlsx").status_code == 200
    assert len(calls) == 1, calls                                                   # one collection for the month, whatever the number of sheets
    calls.clear()
    statements = []
    engine = world.db.get_bind()

    def spy(conn, cursor, statement, params, context, executemany):
        statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", spy)
    try:
        assert c.get(f"{API}/org/payroll/months/{m.label}").status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    assert not [q for q in statements if "FROM payroll_sheets WHERE payroll_sheets.id =" in q], "no db.get per sheet"


# ---- m12: «include unpaid» is keyed by the line of the sheet (membership:type) -------------------------------------------------------------------


def test_m12_include_unpaid_is_set_per_line_when_the_type_changed_in_the_month(world):
    from tests.test_logicA_payroll_pg import fixed_day_centre, month_lessons
    from tests.test_payroll_pg import patch as patch_sheet

    m = previous_month()
    s0 = world.students[0]
    fixed_day_centre(world, m)
    give_group_price(world.db, world.group, 600_000, valid_from=date(2026, 1, 1))
    month_lessons(world, m, [s0])
    pay_calendar(world, s0, m.first, m.d(8), fee=None)                           # lessons of the 1st-8th are paid, the rest are not
    set_rate(world, world.teacher, "group", "50")
    set_rate(world, world.teacher, "pair", "60")
    owner(world).put(f"{API}/org/groups/{world.group.id}/lesson-type", json={"lesson_type": "pair", "valid_from": m.d(15).isoformat()})
    mid = str(student_group(world, s0).id)
    saved = save_sheet(world, m)
    by_key = {l["line_key"]: l for l in saved["lines"]}
    assert set(by_key) == {f"{mid}:group", f"{mid}:pair"} and not any(l["include_unpaid"] for l in saved["lines"])
    out = patch_sheet(world, saved, line_overrides=[{"membership_id": mid, "line_key": f"{mid}:pair", "include_unpaid": True}])
    got = {l["line_key"]: l for l in out["lines"]}
    assert got[f"{mid}:pair"]["include_unpaid"] is True and got[f"{mid}:group"]["include_unpaid"] is False
    assert got[f"{mid}:pair"]["amount"] > by_key[f"{mid}:pair"]["amount"] and got[f"{mid}:group"]["amount"] == by_key[f"{mid}:group"]["amount"]
    # the tick survives a recalculation (carried by the line key), and the other line stays off
    again = patch_sheet(world, out, recalculate=True)
    assert {l["line_key"]: l["include_unpaid"] for l in again["lines"]} == {f"{mid}:group": False, f"{mid}:pair": True}
    # an override without a line key is for every line of the membership (an older front)
    both = patch_sheet(world, again, line_overrides=[{"membership_id": mid, "include_unpaid": True}])
    assert {l["line_key"]: l["include_unpaid"] for l in both["lines"]} == {f"{mid}:group": True, f"{mid}:pair": True}
    bad = owner(world).patch(f"{API}/org/payroll/sheets/{both['id']}", json={
        "revision": both["revision"], "line_overrides": [{"membership_id": mid, "line_key": f"{mid}:individual", "include_unpaid": True}]})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "LINE_NOT_FOUND", bad.text


# ---- m13: replacing an opening on a membership with money needs a reason - also with «Добавить в группу» (a return) ---------------------------------


def test_m13_a_return_that_replaces_an_opening_of_a_membership_with_money_needs_a_reason(world, fixed_today):
    centre(world)
    s0 = world.students[0]
    enrolled(world, D(2026, 10, 1), s0)
    m = membership(world, s0)
    put_opening(world, m, {"paid_until": "2026-10-10"})
    seed_payment(world, m, 100_000, D(2026, 10, 11))
    assert owner(world).post(f"{API}/org/groups/{world.group.id}/students/{s0.id}/leave", json={}).status_code == 200
    url = f"{API}/org/groups/{world.group.id}/students/{s0.id}"
    body = {"opening_balance": {"debt_amount": 100_000, "debt_on": "2026-09-30"}}
    r = owner(world).post(url, params={"mode": "add"}, json=body)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "REASON_REQUIRED", r.text
    world.db.expire_all()
    live = world.db.query(MembershipOpeningBalance).filter_by(membership_id=m.id, voided_at=None).all()
    assert [(o.paid_until, o.debt_amount) for o in live] == [(D(2026, 10, 10), None)]            # nothing was replaced (and the return was rolled back)
    assert world.db.get(type(m), m.id).is_active is False
    body["opening_balance"]["reason"] = "ошибка в таблице Excel"
    r = owner(world).post(url, params={"mode": "add"}, json=body)
    assert r.status_code == 200, r.text
    world.db.expire_all()
    rows = world.db.query(MembershipOpeningBalance).filter_by(membership_id=m.id).order_by(MembershipOpeningBalance.created_at).all()
    assert [(o.debt_amount, o.voided_at is None) for o in rows] == [(None, False), (100_000, True)] and rows[0].void_reason == "ошибка в таблице Excel"
