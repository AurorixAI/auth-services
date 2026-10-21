"""LOGIC-A matrix cells (spec §2) on PostgreSQL: one marked test per cell, ``test_logicA_<item>_<row>``;
``scripts/check_matrix_coverage_a.py`` checks the table (a cell is a test of this name, an existing test it names, or a waiver with a reason)."""
from datetime import date, datetime, timedelta
from uuid import uuid4

import pytest

from src.auth import payment_status as ps
from src.models.auth import (
    GroupAttendance, GroupAudit, GroupLessonTypeHistory, MembershipPause, MembershipPriceOverride, PayrollRate, PayrollSheet, StudentGroup,
    StudentPayment,
)

from tests.pg_world import (  # noqa: F401  (fixtures)
    give_group_price, give_price, make_client, make_group, make_student, payments_block, pg_session, world,
)
from tests.test_pay2_api_pg import (  # noqa: F401
    API, D, TODAY, fixed_today, membership, owner, post_payment, seed_payment, settings_block, stay,
)
from tests.test_logicA_money_pg import centre, enrolled, leave, lp, money_world, new_student, two_students_world  # noqa: F401
from tests.test_logicA_payroll_pg import fixed_day_centre, fp, g_pay_world, month_lessons, payout, save_sheet  # noqa: F401
from tests.test_payroll_pg import (  # noqa: F401
    current_month, lesson, line_of, overview, pay_calendar, pct, preview, previous_month, set_rate, student_group, teacher_row,
)

pytestmark = pytest.mark.skipif(not __import__("os").environ.get("A2_AUTH_TEST_DATABASE_URL"), reason="Requires disposable A2 auth PostgreSQL")


def paid_month_world(w, price=600_000, days=(3, 8)):
    m = previous_month()
    fixed_day_centre(w, m)
    give_group_price(w.db, w.group, price, valid_from=date(2026, 1, 1))
    month_lessons(w, m, [w.students[0]], days=days)
    return m


# ============================================================ F1 =================================================================


def test_logicA_f1_z_a_free_period_pays_nothing_and_a_missing_price_is_no_fee_not_a_silent_zero(world):
    m = paid_month_world(world, price=0)
    payments_block(world.db, world.org, accounting_start_date=m.first.isoformat())
    line = line_of(preview(world, m), world.students[0])
    assert line["amount"] == 0 and line["counted"] == 2 and "UNPAID_LESSONS" not in {w["code"] for w in line["warnings"]}
    give_group_price(world.db, world.group, None)
    line = line_of(preview(world, m), world.students[0])
    assert line["amount"] == 0 and "NO_FEE" in {w["code"] for w in line["warnings"]} and {x["price"] for x in line["marks"]} == {None}


def test_logicA_f1_r_recalculating_without_a_change_of_data_gives_the_same_sheet(world):
    m = paid_month_world(world)
    pay_calendar(world, world.students[0], m.first, m.last, fee=None)
    first, again = preview(world, m), preview(world, m)
    assert first["source_fingerprint"] == again["source_fingerprint"] and first["total_amount"] == again["total_amount"]
    saved = save_sheet(world, m)
    diff = owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}/diff").json()
    assert diff["stale"] is False and diff["lines"] == [] and diff["delta_amount"] == 0


def test_logicA_f1_u_a_voided_payment_makes_the_lessons_unpaid_with_the_same_price_base(world):
    m = paid_month_world(world)
    pay_calendar(world, world.students[0], m.first, m.last, fee=None)
    paid = line_of(preview(world, m), world.students[0])
    assert paid["counted"] == 2
    p = world.db.query(StudentPayment).filter_by(org_id=world.org.id, voided_at=None).one()
    assert owner(world).post(f"{API}/org/payments/{p.id}/void", json={"reason": "ошибка"}).status_code == 200
    line = line_of(preview(world, m), world.students[0])
    assert (line["counted"], line["unpaid"]) == (0, 2) and {x["price"] for x in line["marks"]} == {600_000}


def test_logicA_f1_c_a_change_of_the_data_during_the_calculation_is_a_409_on_save(world):
    m = paid_month_world(world)
    pay_calendar(world, world.students[0], m.first, m.last, fee=None)
    seen = fp(world, m)
    lesson(world, m.d(12), {world.students[0]: "present"})                       # the data moved after the owner looked
    r = owner(world).post(f"{API}/org/payroll/sheets", json={"month": m.label, "teacher_id": str(world.teacher.id), "expected_fingerprint": seen})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "SHEET_DATA_CHANGED"


# ============================================================ F7 =================================================================


def test_logicA_f7_p_a_divisor_acts_from_a_month_never_from_a_day(world):
    c = owner(world)
    for bad in ("2026-10-15", "2026-13", "october"):
        assert c.put(f"{API}/org/payroll/settings", json={"divisor": 13, "valid_from": bad}).status_code == 422


def test_logicA_f7_z_a_divisor_of_one_is_allowed_and_means_the_whole_price_share(world):
    m = paid_month_world(world, days=(3,))
    pay_calendar(world, world.students[0], m.first, m.last, fee=None)
    assert owner(world).put(f"{API}/org/payroll/settings", json={"divisor": 1, "valid_from": m.label}).status_code == 200
    assert line_of(preview(world, m), world.students[0])["amount"] == 600_000 * 50 // 100


def test_logicA_f7_c_the_last_of_two_changes_of_the_same_month_wins_and_the_answer_has_the_history(world):
    c = owner(world)
    cur = current_month()
    assert c.put(f"{API}/org/payroll/settings", json={"divisor": 13, "valid_from": cur.label}).status_code == 200
    r = c.put(f"{API}/org/payroll/settings", json={"divisor": 14, "valid_from": cur.label})
    assert r.status_code == 200 and r.json()["divisor"] == 14 and r.json()["history"][-1] == {"valid_from": cur.label, "divisor": 14}
    assert c.get(f"{API}/org/payroll/months/{cur.label}").json()["divisor"] == 14


def test_logicA_f7_m_the_divisor_of_a_lesson_is_the_one_of_its_month(world):
    prev, cur = previous_month(), current_month()
    s0 = world.students[0]
    lesson(world, prev.last, {s0: "present"})
    lesson(world, cur.first, {s0: "present"})
    assert owner(world).put(f"{API}/org/payroll/settings", json={"divisor": 13, "valid_from": cur.label}).status_code == 200
    set_rate(world, world.teacher, "group", "50")
    a, b = preview(world, prev), preview(world, cur)
    assert {x["divisor"] for x in line_of(a, s0)["marks"]} == {12} and {x["divisor"] for x in line_of(b, s0)["marks"]} == {13}


# ============================================================ F9 =================================================================


def test_logicA_f9_z_nothing_to_settle_says_so_and_offers_only_keep(world, fixed_today):
    settings_block(world, accounting_start_date="2026-10-01")                       # no price: nothing is charged
    m = membership(world)
    stay(world, m, D(2026, 10, 21))
    mem = lp(world, world.group, world.students[0])["memberships"][0]
    assert mem["has_open_balance"] is False and mem["allowed"] == ["keep"]
    assert (mem["money"]["debt"], mem["money"]["advance"], mem["money"]["refundable"]) == (0, 0, 0)


def test_logicA_f9_u_a_refund_made_at_leaving_can_be_voided_like_any_refund(world, fixed_today):
    m = money_world(world)
    out = leave(world, world.group, world.students[0], {"settle": [{"membership_id": str(m.id), "action": "refund", "expected_refundable": 87_000, "method": "cash"}]})
    refund_id = out["settled"][0]["refund_id"]
    r = owner(world).post(f"{API}/org/refunds/{refund_id}/void", json={"reason": "вернули деньги обратно"})
    assert r.status_code == 200 and r.json()["row"]["advance"] == 87_000


def test_logicA_f9_x_a_price_announced_for_later_does_not_change_the_account_after_leaving(world, fixed_today):
    m = money_world(world)
    before = lp(world, world.group, world.students[0])["memberships"][0]["money"]
    assert owner(world).put(f"{API}/org/groups/{world.group.id}/price", json={"amount": 900_000, "valid_from": "2026-12-01"}).status_code == 200
    after = lp(world, world.group, world.students[0])["memberships"][0]["money"]
    assert after == before


def test_logicA_f9_a_the_opening_debt_stays_the_debt_of_the_student_who_left(world, fixed_today):
    centre(world, 600_000)
    m = enrolled(world, D(2026, 10, 21))
    assert owner(world).put(f"{API}/org/memberships/{m.id}/opening-balance", json={"debt_amount": 250_000, "debt_on": "2026-09-30"}).status_code == 200
    fixed_today(D(2026, 10, 25))
    pv = owner(world).get(f"{API}/org/students/{m.student_id}/archive-preview").json()["memberships"][0]
    assert pv["money"]["debt"] == 347_000 and pv["allowed"] == ["keep", "writeoff"]


def test_logicA_f12_c_two_regime_saves_on_one_day_keep_one_history_entry_and_the_last_wins(world, fixed_today):
    settings_block(world, accounting_start_date="2026-09-01")
    c = owner(world)
    for mode in ("lessons", "calendar"):
        assert c.put(f"{API}/org/settings/payments", json={"period_mode": mode, "lessons_per_payment": 12}).status_code == 200
    world.db.expire_all()
    today = [h for h in world.org.settings["payments"]["billing_history"] if h["from"] == "2026-10-21"]
    assert len(today) == 1 and today[0]["period_mode"] == "calendar"


# ============================================================ F13 ================================================================


def override_world(w, amount=400_000, price=600_000):
    centre(w, price)
    settings_block(w, accounting_start_date="2026-09-01")
    m = enrolled(w, D(2026, 9, 1))
    give_price(w.db, m, amount, reason="скидка", valid_from=D(2026, 9, 1))
    return m


def markers(w, m):
    w.db.expire_all()
    return w.db.query(MembershipPriceOverride).filter_by(membership_id=m.id).filter(MembershipPriceOverride.reason.in_(("left_group", "return_group"))).all()


def test_logicA_f13_o_an_advance_left_with_the_exception_is_refunded_by_the_periods_it_was_priced_by(world, fixed_today):
    m = override_world(world)
    seed_payment(world, m, 1_000_000, D(2026, 9, 1))
    mem = lp(world, world.group, world.students[0])["memberships"][0]["money"]
    # September 400 000 + October 1-21 (21/31 of 400 000 = 271 000), not 600 000: 1 000 000 - 671 000
    assert mem["charged_after_leave"] == 671_000 and mem["refundable"] == 329_000 and mem["individual_price_ends"] is True


def test_logicA_f13_z_a_free_exception_ends_too(world, fixed_today):
    m = override_world(world, amount=0)
    leave(world, world.group, world.students[0])
    assert [(r.amount, r.valid_from) for r in markers(world, m)] == [(None, D(2026, 10, 22))]


def test_logicA_f13_r_leave_return_leave_in_one_day_writes_one_marker_per_day(world, fixed_today):
    m = override_world(world)
    leave(world, world.group, world.students[0])
    assert owner(world).post(f"{API}/org/groups/{world.group.id}/students/{world.students[0].id}?mode=add").status_code == 200
    leave(world, world.group, world.students[0])
    rows = markers(world, m)
    assert len({r.valid_from for r in rows}) == len(rows) and {r.reason for r in rows} >= {"left_group"}


def test_logicA_f13_m_returning_on_the_day_of_leaving_is_priced_by_the_group_at_once(world, fixed_today):
    m = override_world(world)
    leave(world, world.group, world.students[0])
    assert owner(world).post(f"{API}/org/groups/{world.group.id}/students/{world.students[0].id}?mode=add").status_code == 200
    row = owner(world).get(f"{API}/org/students/{m.student_id}/memberships").json()[0]
    assert row["price"]["source"] == "group" and row["price"]["amount"] == 600_000


def test_logicA_f13_x_the_group_price_of_the_day_of_return_is_the_price_after_the_return(world, fixed_today):
    m = override_world(world)
    leave(world, world.group, world.students[0])
    assert owner(world).put(f"{API}/org/groups/{world.group.id}/price", json={"amount": 700_000, "valid_from": "2026-10-25"}).status_code == 200
    fixed_today(D(2026, 10, 25))
    assert owner(world).post(f"{API}/org/groups/{world.group.id}/students/{world.students[0].id}?mode=add").status_code == 200
    row = owner(world).get(f"{API}/org/students/{m.student_id}/memberships").json()[0]
    assert row["price"]["amount"] == 700_000


def test_logicA_f13_d_leaving_without_an_exception_writes_nothing(world, fixed_today):
    centre(world, 600_000)
    m = enrolled(world, D(2026, 10, 1))
    leave(world, world.group, world.students[0])
    assert markers(world, m) == []


def test_logicA_f13_a_a_transfer_ends_the_exception_of_the_old_group_and_the_new_one_has_the_group_price(world, fixed_today):
    m = override_world(world)
    other = make_group(world.db, world.org, world.teacher2, "Вторая")
    world.db.commit()
    give_group_price(world.db, other, 500_000, valid_from=D(2026, 1, 1))
    r = owner(world).post(f"{API}/org/groups/{other.id}/students/{world.students[0].id}")          # mode = transfer
    assert r.status_code == 200, r.text
    assert [(x.amount, x.valid_from) for x in markers(world, m)] == [(None, D(2026, 10, 22))]
    rows = owner(world).get(f"{API}/org/students/{m.student_id}/memberships").json()
    new = next(x for x in rows if x["group_id"] == str(other.id))
    assert new["price"]["amount"] == 500_000 and new["price"]["source"] == "group"


# ============================================================ F14 ================================================================


def split_world(w, days=(1, 3, 6, 8, 10, 13, 15, 17, 20, 22, 24, 27)):
    m = previous_month()
    fixed_day_centre(w, m)
    give_group_price(w.db, w.group, 600_000, valid_from=date(2026, 1, 1))
    month_lessons(w, m, [w.students[0]], days=days)
    pay_calendar(w, w.students[0], m.first, m.last, fee=None)
    set_rate(w, w.teacher, "group", "50")
    return m


def set_type(w, kind, valid_from):
    r = owner(w).put(f"{API}/org/groups/{w.group.id}/lesson-type", json={"lesson_type": kind, "valid_from": valid_from.isoformat()})
    assert r.status_code == 200, r.text
    return r.json()


def test_logicA_f14_p_a_rate_is_needed_only_for_a_type_the_month_had_lessons_in(world):
    m = split_world(world, days=(1, 3, 6))
    set_type(world, "pair", m.d(15))                                 # no lesson after the 15th: the pair rate is not needed
    assert owner(world).post(f"{API}/org/payroll/months/{m.label}/calculate", json={}).status_code == 200
    lesson(world, m.d(20), {world.students[0]: "present"})
    r = owner(world).post(f"{API}/org/payroll/months/{m.label}/calculate", json={})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "PERCENT_REQUIRED"


def test_logicA_f14_o_several_changes_in_a_month_give_a_line_per_type_and_the_sum_of_the_lessons(world):
    m = split_world(world)
    set_rate(world, world.teacher, "pair", "60")
    set_rate(world, world.teacher, "individual", "70")
    set_type(world, "pair", m.d(10))
    set_type(world, "individual", m.d(20))
    lines = [l for l in preview(world, m)["lines"] if l["student_id"] == str(world.students[0].id)]
    by = {l["lesson_type"]: l for l in lines}
    assert {k: v["counted"] for k, v in by.items()} == {"group": 4, "pair": 4, "individual": 4}              # 1 3 6 8 | 10 13 15 17 | 20 22 24 27
    assert sum(l["counted"] for l in lines) == 12


def test_logicA_f14_z_a_zero_percent_of_one_type_is_a_line_of_zero_without_a_warning(world):
    m = split_world(world)
    set_rate(world, world.teacher, "pair", "0")
    set_type(world, "pair", m.d(15))
    pair = next(l for l in preview(world, m)["lines"] if l["lesson_type"] == "pair")
    assert pair["amount"] == 0 and pair["percent"] == "0" and "PERCENT_REQUIRED" not in {w["code"] for w in pair["warnings"]}


def test_logicA_f14_u_taking_a_change_back_with_a_new_row_returns_the_old_type_and_marks_saved_sheets(world):
    m = split_world(world)
    set_rate(world, world.teacher, "pair", "60")
    saved = save_sheet(world, m)
    set_type(world, "pair", m.d(15))
    got = owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}").json()
    assert got["stale"] is True
    set_type(world, "group", m.d(15))                                # the same day again: the row is replaced
    assert {l["lesson_type"] for l in preview(world, m)["lines"]} == {"group"}
    assert owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}").json()["stale"] is False


def test_logicA_f14_x_the_percent_of_a_lesson_comes_from_the_rates_of_the_month_by_its_type(world):
    m = split_world(world)
    set_rate(world, world.teacher, "pair", "60")
    set_rate(world, world.teacher, "group", "80", valid_from=current_month().first)                    # a later rate does not reach the previous month
    set_type(world, "pair", m.d(15))
    assert {(l["lesson_type"], l["percent"]) for l in preview(world, m)["lines"]} == {("group", "50"), ("pair", "60")}


def test_logicA_f14_d_a_group_without_a_history_is_read_by_its_own_type(world):
    m = split_world(world, days=(1, 3))
    world.db.query(GroupLessonTypeHistory).filter_by(group_id=world.group.id).delete()
    world.group.lesson_type = "individual"
    world.db.commit()
    set_rate(world, world.teacher, "individual", "40")
    assert {l["lesson_type"] for l in preview(world, m)["lines"]} == {"individual"}


def test_logicA_f14_a_a_saved_sheet_keeps_its_lines_after_the_type_changes(world):
    m = split_world(world)
    set_rate(world, world.teacher, "pair", "60")
    saved = save_sheet(world, m)
    set_type(world, "pair", m.d(15))
    got = owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}").json()
    assert got["total_amount"] == saved["total_amount"] and {l["lesson_type"] for l in got["lines"]} == {"group"} and got["stale"] is True


# ============================================================ D3 =================================================================


def cancel_lesson(w, m, n):
    row = w.db.query(GroupAttendance).filter_by(group_id=w.group.id, lesson_date=m.d(n).isoformat()).one()
    row.status, row.records, row.teacher_id = "cancelled", {}, None
    w.db.commit()


def test_logicA_d3_n_a_month_costs_the_same_but_the_teacher_is_paid_for_the_lessons_held(world):
    m = paid_month_world(world, days=(3, 8, 10, 13))
    pay_calendar(world, world.students[0], m.first, m.last, fee=None)
    row_before = owner(world).get(f"{API}/org/students/{world.students[0].id}/memberships").json()[0]
    cancel_lesson(world, m, 13)
    assert line_of(preview(world, m), world.students[0])["counted"] == 3
    assert owner(world).get(f"{API}/org/students/{world.students[0].id}/memberships").json()[0]["debt"] == row_before["debt"]


def test_logicA_d3_o_more_than_half_of_the_lessons_cancelled_changes_no_charge(world):
    m = paid_month_world(world, days=(3, 8, 10, 13))
    before = owner(world).get(f"{API}/org/students/{world.students[0].id}/memberships").json()[0]
    for n in (3, 8, 10):
        cancel_lesson(world, m, n)
    after = owner(world).get(f"{API}/org/students/{world.students[0].id}/memberships").json()[0]
    assert (after["debt"], after["advance"]) == (before["debt"], before["advance"])


def test_logicA_d3_z_a_month_of_only_cancelled_lessons_pays_the_teacher_nothing_without_warnings(world):
    m = paid_month_world(world, days=(3, 8))
    pay_calendar(world, world.students[0], m.first, m.last, fee=None)
    for n in (3, 8):
        cancel_lesson(world, m, n)
    sheet = preview(world, m)
    assert sheet["total_amount"] == 0 and sheet["lines"] == []


# ============================================================ D4 =================================================================


def test_logicA_d4_o_a_big_backdated_rise_shows_the_total_new_debt_and_needs_no_payments_to_pass(world, fixed_today):
    centre(world, 500_000)
    enrolled(world, D(2026, 10, 1))
    r = owner(world).put(f"{API}/org/groups/{world.group.id}/price", params={"dry_run": True}, json={"amount": 5_000_000, "valid_from": "2026-10-01"})
    assert r.status_code == 200 and r.json()["impact"]["money"]["debt_delta"] >= 4_500_000 and r.json()["impact"]["money"]["backdated"] is True


def test_logicA_d4_z_a_zero_price_from_today_is_allowed_with_a_window(world, fixed_today):
    centre(world, 500_000)
    enrolled(world, D(2026, 10, 1))
    r = owner(world).put(f"{API}/org/groups/{world.group.id}/price", json={"amount": 0, "valid_from": TODAY.isoformat()})
    assert r.status_code == 200 and r.json()["impact"]["money"]["backdated"] is False


def test_logicA_d4_m_a_price_from_the_first_day_of_a_period_changes_it_from_the_second_day_the_next(world, fixed_today):
    a, b = two_students_world(world)
    c = owner(world)
    r = c.put(f"{API}/org/groups/{world.group.id}/price", json={"amount": 600_000, "valid_from": "2026-10-01"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PRICE_BACKDATE_WITH_PAYMENTS"
    assert c.put(f"{API}/org/groups/{world.group.id}/price", json={"amount": 600_000, "valid_from": "2026-10-02"}).status_code == 200


def test_logicA_d4_d_a_group_without_a_price_may_get_one_for_the_past_when_nobody_has_money(world, fixed_today):
    settings_block(world, accounting_start_date="2026-10-01", billing_anchor="fixed_day", billing_day=1)
    enrolled(world, D(2026, 10, 1))
    r = owner(world).put(f"{API}/org/groups/{world.group.id}/price", json={"amount": 500_000, "valid_from": "2026-10-01"})
    assert r.status_code == 200


def test_logicA_d4_a_a_price_before_the_accounting_that_is_shadowed_blocks_nothing(world, fixed_today):
    a, b = two_students_world(world)
    give_group_price(world.db, world.group, 500_000, valid_from=D(2026, 9, 1))
    r = owner(world).put(f"{API}/org/groups/{world.group.id}/price", json={"amount": 999_000, "valid_from": "2026-08-01"})
    assert r.status_code == 200 and r.json()["impact"]["money"]["items"] == []


# ============================================================ D5 =================================================================


def writeoff_world(w):
    m = paid_month_world(w, price=620_000, days=(5, 12, 26, 28))
    pay_calendar(w, w.students[0], m.first, m.d(20), fee=None)
    return m, student_group(w, w.students[0])


def test_logicA_d5_r_a_second_writeoff_of_the_whole_debt_finds_no_debt(world):
    m, mem = writeoff_world(world)
    body = {"all": True, "reason": "болезнь", "effective_on": m.last.isoformat()}
    assert owner(world).post(f"{API}/org/memberships/{mem.id}/writeoffs", json=body).status_code == 201
    r = owner(world).post(f"{API}/org/memberships/{mem.id}/writeoffs", json=body)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "WRITEOFF_EXCEEDS_DEBT"


def test_logicA_d5_z_a_zero_writeoff_is_refused_with_the_amount_range_code(world):
    m, mem = writeoff_world(world)
    r = owner(world).post(f"{API}/org/memberships/{mem.id}/writeoffs", json={"amount": 0, "reason": "болезнь", "effective_on": m.last.isoformat()})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "AMOUNT_RANGE", r.text


def test_logicA_d5_u_a_voided_writeoff_makes_the_days_unpaid_and_the_sheet_stale(world):
    m, mem = writeoff_world(world)
    w = owner(world).post(f"{API}/org/memberships/{mem.id}/writeoffs", json={"all": True, "reason": "болезнь", "effective_on": m.last.isoformat()}).json()
    saved = save_sheet(world, m)
    assert line_of(saved, world.students[0])["written_off"] == 2
    assert owner(world).post(f"{API}/org/writeoffs/{w['writeoff']['id']}/void", json={"reason": "отменили прощение"}).status_code == 200
    got = owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}").json()
    assert got["stale"] is True and line_of(preview(world, m), world.students[0])["unpaid"] == 2


def test_logicA_d5_b_a_writeoff_dated_in_the_past_marks_the_saved_sheet_stale(world):
    m, mem = writeoff_world(world)
    saved = save_sheet(world, m)
    assert line_of(saved, world.students[0])["unpaid"] == 2
    assert owner(world).post(f"{API}/org/memberships/{mem.id}/writeoffs",
                             json={"all": True, "reason": "договорённость", "effective_on": m.d(25).isoformat()}).status_code == 201
    assert owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}").json()["stale"] is True


def test_logicA_d5_d_only_the_owner_and_an_admin_with_the_money_switch_forgive_a_debt(world):
    m, mem = writeoff_world(world)
    body = {"all": True, "reason": "болезнь"}
    assert make_client(world, user=world.teacher).post(f"{API}/org/memberships/{mem.id}/writeoffs", json=body).status_code == 403
    assert make_client(world, user=world.admin).post(f"{API}/org/memberships/{mem.id}/writeoffs", json=body).status_code == 403
    settings_block(world, admin_can_manage=True)
    assert make_client(world, user=world.admin).post(f"{API}/org/memberships/{mem.id}/writeoffs", json=body).status_code == 201


# ============================================================ D6 =================================================================


def test_logicA_d6_z_a_sheet_of_zero_is_paid_in_full_without_a_payout_and_zero_is_not_a_payout(world):
    m = paid_month_world(world, days=(3,))
    set_rate(world, world.teacher, "group", "50")
    sheet = save_sheet(world, m)                                       # no payment: the lesson is unpaid, the sheet is 0
    assert sheet["total_amount"] == 0 and sheet["payout"]["state"] == "settled" and sheet["payout"]["text"] == "Выплачено полностью"
    r = owner(world).post(f"{API}/org/payroll/sheets/{sheet['id']}/payouts", json={"amount": 0, "method": "cash"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "AMOUNT_RANGE"


def test_logicA_d6_x_a_rate_changed_after_a_payout_shows_the_new_target_and_the_old_payout_stays(world):
    m, s1, s2 = g_pay_world(world)
    sheet = save_sheet(world, m)
    payout(world, sheet, 475_000)
    set_rate(world, world.teacher, "group", "60", valid_from=m.first)                                   # a later row of the same month wins
    st = owner(world).get(f"{API}/org/payroll/sheets/{sheet['id']}").json()["payout"]
    assert st["fresh_total"] == 570_000 and st["paid"] == 475_000 and st["balance"] == 95_000 and st["state"] == "due"


def test_logicA_d6_d_without_a_rate_or_with_a_manual_price_the_target_is_the_saved_sum(world):
    m, s1, s2 = g_pay_world(world)
    sheet = save_sheet(world, m)
    world.db.query(PayrollRate).filter_by(org_id=world.org.id).delete()
    world.db.commit()
    st = owner(world).get(f"{API}/org/payroll/sheets/{sheet['id']}").json()["payout"]
    assert st["fresh_total"] is None and st["basis"] == "saved" and st["target"] == 475_000
