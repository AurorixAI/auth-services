"""Money-1 / Г: holes of the acceptance review MONEY1_G1_REVIEW (N1…N9) — each test carries the numbers of its scenario (PostgreSQL)."""
from datetime import date

import pytest

from src.models.auth import AttendanceExcuse

from tests.money1_helpers import hold_lesson, now_4_nov  # noqa: F401  (fixtures)
from tests.ops_helpers import grant_money_right
from tests.pg_world import make_client, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_money1_attendance_pg import post_save

pytestmark = needs_pg


# ---- Г-1 (N1): the text of a decided excuse does not move the decision --------------------------------------------------------------------------------

@pytest.mark.parametrize("decision", ["confirmed", "rejected"])
def test_g1_the_text_of_a_decided_excuse_keeps_the_decision_for_everybody(world, now_4_nov, decision):
    day, lid = hold_lesson(world, date(2026, 10, 30))
    s1 = world.students[0]
    grant_money_right(world)
    assert post_save(world, world.owner, day, lid, ["excused", "present", "present"], correctionReason="Болел", excuseReasons={str(s1.id): "Болел, написал"}).status_code == 200
    excuse = world.db.query(AttendanceExcuse).filter_by(student_id=s1.id).one()
    excuse.status, excuse.reviewed_by, excuse.review_note = decision, world.owner.id, "Решено"
    world.db.commit()
    grant_money_right(world, False)
    for who in (world.teacher, world.admin):                       # no right to the money: the text is saved, the decision (pack −1 lesson, teacher +20 000) stays
        r = post_save(world, who, day, lid, ["excused", "present", "present"], excuseReasons={str(s1.id): f"Другой текст {who.id}"})
        assert r.status_code == 200, r.text
        world.db.expire_all()
        row = world.db.query(AttendanceExcuse).filter_by(student_id=s1.id).one()
        assert (row.status, row.reviewed_by, row.review_note) == (decision, world.owner.id, "Решено") and row.reason == f"Другой текст {who.id}"


# ---- Г-2 (N2): the first price of a group begins inside the first period of a student who was added before it --------------------------------------------

from tests.money1b_helpers import account, charge_of, set_schedule, slot  # noqa: E402
from tests.pg_world import give_group_price  # noqa: E402
from tests.test_pay2_api_pg import fixed_today, membership, settings_block, stay  # noqa: E402,F401  (fixtures)
from tests.test_money1b_price_pg import everybody_from  # noqa: E402

D = date


def test_g2_the_first_price_from_the_10th_prices_the_period_of_the_student_added_on_the_5th(world, fixed_today):
    w = world
    fixed_today(D(2026, 11, 12))
    settings_block(w, accounting_start_date="2026-09-01")
    give_group_price(w.db, w.group, 500_000, valid_from=D(2026, 11, 10))      # set on 01.11 «с 10.11»; the student was added on 05.11
    everybody_from(w, D(2026, 11, 5))
    led = account(w, membership(w).id)
    assert (led.charged, led.debt) == (500_000, 500_000) and charge_of(led, D(2026, 11, 5)) == 500_000      # e7c44aa: 0 and 0 - the first month for free


def test_r12_two_prices_begin_inside_the_period_and_the_earliest_one_prices_it(world, fixed_today):
    w = world
    fixed_today(D(2026, 11, 25))
    settings_block(w, accounting_start_date="2026-09-01")
    give_group_price(w.db, w.group, 500_000, valid_from=D(2026, 11, 10))      # the first price of the group «с 10.11», raised «с 20.11»; the student was added on 05.11
    give_group_price(w.db, w.group, 600_000, valid_from=D(2026, 11, 20))
    everybody_from(w, D(2026, 11, 5))
    led = account(w, membership(w).id)
    assert (led.charged, led.debt) == (500_000, 500_000) and charge_of(led, D(2026, 11, 5)) == 500_000      # before: 600 000 - the LAST price that began in the period


def test_g2_a_period_that_ended_before_the_first_price_is_still_free_and_the_next_one_is_charged(world, fixed_today):
    w = world
    fixed_today(D(2026, 12, 12))
    settings_block(w, accounting_start_date="2026-09-01")
    give_group_price(w.db, w.group, 500_000, valid_from=D(2026, 11, 10))
    everybody_from(w, D(2026, 10, 1))
    led = account(w, membership(w).id)
    assert charge_of(led, D(2026, 10, 1)) == 0 and charge_of(led, D(2026, 11, 1)) == 500_000       # a month with no price on any day stays unpriced


def test_g2_the_shared_month_cell_of_the_student_added_before_the_first_price_is_priced(world, fixed_today):
    w = world
    fixed_today(D(2026, 11, 12))
    settings_block(w, accounting_start_date="2026-09-01", billing_anchor="fixed_day", billing_day=1)
    set_schedule(w, [slot("mon", 1, "2026-09-01"), slot("wed", 3, "2026-09-01"), slot("fri", 5, "2026-09-01")])
    give_group_price(w.db, w.group, 300_000, valid_from=D(2026, 11, 10))
    everybody_from(w, D(2026, 11, 5))
    assert charge_of(account(w, membership(w).id), D(2026, 11, 1)) == 254_000       # the same 11 of 13 lessons x 300 000 as when the price is typed «с сегодня»


# ---- Г-5 (N6, ЗП-15): a reopened month keeps its advance in the chain; a payout locks the advance chain of the teacher ---------------------------------------

import threading  # noqa: E402
import time  # noqa: E402

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.auth.payroll_payouts import OverpayConfirmRequired, PayrollPayoutService  # noqa: E402
from src.auth.payroll_schemas import PayoutCreate  # noqa: E402
from src.models.auth import PayrollPayout  # noqa: E402

from tests.money1_helpers import reopen_now, sheet, tashkent  # noqa: E402


def payout_row(w, sheet_row, amount):
    row = PayrollPayout(org_id=w.org.id, sheet_id=sheet_row.id, teacher_id=sheet_row.teacher_id, period_month=sheet_row.period_month, amount=amount, paid_on=date(2026, 10, 3), method="cash",
                        sheet_revision=sheet_row.revision, sheet_total=sheet_row.total_amount, created_by=w.owner.id)
    return row


def september_and_october(w):
    """September closed at 1 000 000, paid 1 200 000 (a confirmed advance of 200 000); October closed at 800 000."""
    sept = sheet(w, w.teacher, date(2026, 9, 1), tashkent(2026, 10, 3), total=1_000_000)
    octo = sheet(w, w.teacher, date(2026, 10, 1), tashkent(2026, 11, 3), total=800_000)
    w.db.add(payout_row(w, sept, 1_200_000))
    w.db.commit()
    return sept, octo


def test_g5_reopening_september_does_not_move_the_advance_out_of_october(world, now_4_nov):
    w = world
    sept, octo = september_and_october(w)
    payouts = PayrollPayoutService(w.db)
    assert payouts.carry_ins(str(w.org.id), [w.teacher.id], date(2026, 10, 1)) == {w.teacher.id: 200_000}
    reopen_now(w.db, w.org.id, w.teacher.id, "2026-09", w.owner.id, "Поправить урок")
    # September is open again, its LAST saved total (1 000 000) still stands: October is to pay 800 000 - 200 000, not 800 000 (e7c44aa: carry_in 0, «к выплате» +200 000 and a double payout)
    assert payouts.carry_ins(str(w.org.id), [w.teacher.id], date(2026, 10, 1)) == {w.teacher.id: 200_000}
    assert payouts.carry_chain(str(w.org.id), w.teacher.id, [date(2026, 10, 1), date(2026, 11, 1)]) == {date(2026, 10, 1): 200_000, date(2026, 11, 1): 0}
    assert payouts.state_for(str(w.org.id), octo, None).balance == 600_000


def test_g5_a_payout_waits_for_the_advance_that_is_being_written_in_another_tab(world, now_4_nov):
    """The advance of September (+200 000 in October) is written in tab A under the lock of the chain; tab B pays October in full meanwhile: B must wait and then see 600 000 left."""
    w = world
    sept = sheet(w, w.teacher, date(2026, 9, 1), tashkent(2026, 10, 3), total=1_000_000)
    octo = sheet(w, w.teacher, date(2026, 10, 1), tashkent(2026, 11, 3), total=800_000)
    org, teacher, octo_id, owner_id = str(w.org.id), w.teacher.id, octo.id, str(w.owner.id)
    engine = w.db.get_bind()
    make = sessionmaker(engine, autoflush=False)
    tab_a = make()
    tab_a.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"payroll-advance:{org}:{teacher}"})       # tab A is in the middle of its write
    outcome = {}

    def tab_b():
        session = make()
        try:
            outcome["created"] = PayrollPayoutService(session).create(org, octo_id, owner_id, PayoutCreate(amount=800_000, method="cash"), None, lambda s: int(s.total_amount))
        except BaseException as exc:  # noqa: BLE001 - the test inspects it
            outcome["error"] = exc
        finally:
            session.close()

    thread = threading.Thread(target=tab_b)
    thread.start()
    thread.join(timeout=2)
    assert thread.is_alive(), "tab B did not wait for the advance chain of the teacher"
    tab_a.add(payout_row(w, sept, 1_200_000))
    tab_a.commit()                                                                                    # the advance is there, the lock is released
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), OverpayConfirmRequired), outcome       # 800 000 against 600 000 left: the owner is asked, not silently overpaid by 200 000
    assert outcome["error"].extra["balance"] == 600_000 and outcome["error"].extra["overpay"] == 200_000
    tab_a.close()


# ---- Г-6 (N7, N8): a re-save of a month that is over never closes it with stale data -------------------------------------------------------------------------

from src.models.auth import PayrollSheet  # noqa: E402
from tests.test_logicA_payroll_pg import API as PAYROLL_API, owner as payroll_owner  # noqa: E402
from tests.test_money1_changed_months_pg import pay_late, two_teachers_in_a_closed_september  # noqa: E402


def tick(w, saved, revision=1):
    mid = saved["lines"][0]["membership_id"]
    return payroll_owner(w).patch(f"{PAYROLL_API}/org/payroll/sheets/{saved['id']}", json={"revision": revision, "line_overrides": [{"membership_id": mid, "include_unpaid": True}]})


def test_g6_a_tick_on_a_reopened_month_whose_data_moved_is_refused_and_a_tick_on_unchanged_data_is_not(world, fixed_today):
    m, s1, s2, first, second = two_teachers_in_a_closed_september(world, fixed_today)       # September 475 000, saved 20.10
    reopen_now(world.db, world.org.id, world.teacher.id, "2026-09", world.owner.id, "Поправить урок")
    fixed_today(date(2026, 11, 20))
    unchanged = tick(world, first)
    assert unchanged.status_code == 200, unchanged.text                                    # nothing moved: the tick is a plain re-save
    pay_late(world, s2)                                                                    # +125 000 for the teacher: the saved sheet is stale
    refused = tick(world, unchanged.json(), revision=unchanged.json()["revision"])
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "SHEET_DATA_CHANGED", refused.text      # e7c44aa: 200 and the month closed again with the old 475 000
    assert world.db.query(PayrollSheet).filter_by(id=first["id"]).one().revision == unchanged.json()["revision"]


def test_g6_a_save_waits_for_a_correction_that_holds_the_sheet_and_then_sees_its_data(world, fixed_today):
    """Tab A corrects the month (it holds the saved sheet ``FOR SHARE``, like every correction of a lesson) and commits a payment; tab B saves the sheet with the fingerprint it saw before.
    B must wait for A and then answer 409 - not save (and close) the month with the figures of a moment before the lock (e7c44aa: it checked the fingerprint BEFORE the lock)."""
    from src.auth.payroll_schemas import SheetCreate
    from src.auth.payroll_service import DataChanged, PayrollService
    from src.models.auth import StudentPayment

    from tests.test_logicA_payroll_pg import student_group

    m, s1, s2, first, second = two_teachers_in_a_closed_september(world, fixed_today)
    fixed_today(date(2026, 11, 20))
    seen = payroll_owner(world).get(f"{PAYROLL_API}/org/payroll/sheets/{first['id']}/diff").json()["source_fingerprint"]
    mem2 = student_group(world, s2)
    org, teacher, owner_id = str(world.org.id), world.teacher.id, str(world.owner.id)
    make = sessionmaker(world.db.get_bind(), autoflush=False)
    tab_a = make()
    tab_a.query(PayrollSheet.id).filter(PayrollSheet.id == first["id"]).with_for_update(read=True).all()
    outcome = {}

    def tab_b():
        session = make()
        try:
            outcome["saved"] = PayrollService(session).save_sheet(org, owner_id, SheetCreate(month="2026-09", teacher_id=teacher, revision=1, expected_fingerprint=seen, confirm_open_lessons=True))
        except BaseException as exc:  # noqa: BLE001 - the test inspects it
            outcome["error"] = exc
        finally:
            session.close()

    thread = threading.Thread(target=tab_b)
    thread.start()
    thread.join(timeout=3)
    assert thread.is_alive(), "the save did not wait for the sheet"
    tab_a.add(StudentPayment(org_id=world.org.id, membership_id=mem2.id, student_id=mem2.student_id, group_id=mem2.group_id, amount=300_000, paid_on=date(2026, 11, 20), created_by=world.owner.id, method="cash"))
    tab_a.commit()
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), DataChanged), outcome
    tab_a.close()


# ---- Г-3 (N4): «кто провёл» of a PAST lesson without a journal row is for the money right too --------------------------------------------------------------

def test_g3_who_taught_yesterdays_lesson_without_a_journal_row_is_for_the_money_right(world, now_4_nov):
    P = "/api/v1/auth/org"
    yesterday = date(2026, 11, 3)                                         # Tuesday; the clock is 04.11 12:00, the lesson is at 10:00 and nobody opened it
    body = {"lessonDate": yesterday.isoformat(), "lessonId": f"s{yesterday.isoweekday()}", "teacherId": str(world.teacher2.id), "reason": "Заменил коллега"}
    admin = make_client(world, user=world.admin)
    refused = admin.put(f"{P}/groups/{world.group.id}/lesson-teacher", json=body)
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "MONEY_RIGHT_REQUIRED"       # e7c44aa: 200, and teacher B then formalises the lesson and is paid
    grant_money_right(world)
    assert admin.put(f"{P}/groups/{world.group.id}/lesson-teacher", json=body).status_code == 200
    coming = date(2026, 11, 9)
    grant_money_right(world, False)
    assert admin.put(f"{P}/groups/{world.group.id}/lesson-teacher", json={**body, "lessonDate": coming.isoformat(), "lessonId": f"s{coming.isoweekday()}"}).status_code == 200


# ---- Г-16 (N3, П2): the kind of a cancellation is money: without the right it is always «по вине центра» --------------------------------------------------------

from tests.ops_helpers import client as staff_client, clock  # noqa: E402,F401  (fixtures)
from tests.test_money1_cancel_preview_pg import WED, post as cancel_post, world as cancel_world  # noqa: E402

NBSP = " "


@pytest.mark.parametrize("who", ["admin", "A"], ids=["admin-without-right", "teacher"])
def test_g16_holiday_is_refused_without_the_money_right_on_the_road_of_a_missed_lesson(pg_session, clock, who):
    w, g, students, ms, url = cancel_world(pg_session, clock)
    grant_money_right(w, False)
    refused = cancel_post(w, who, url, {**WED, "cancellationKind": "holiday"})
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "MONEY_RIGHT_REQUIRED", refused.text      # e7c44aa: 200 and no deduction (46 000 x 5 = 0 instead)
    assert cancel_post(w, "owner", url, {**WED, "cancellationKind": "holiday"}, dry=True).status_code == 200


def test_g16_without_the_right_the_cancellation_is_by_the_centre_and_the_preview_says_what_the_students_get(pg_session, clock):
    w, g, students, ms, url = cancel_world(pg_session, clock)
    grant_money_right(w, False)
    dry = cancel_post(w, "admin", url, WED, dry=True)
    assert dry.status_code == 200, dry.text
    assert dry.json()["consequence"] == f"Ученикам зачтётся по 46{NBSP}000{NBSP}сум"
    assert "deductions" not in dry.json() and "46" not in str(dry.json()["moneyEffects"])      # the students' sums of the whole group stay with the money right
    assert cancel_post(w, "admin", url, WED).status_code == 200


def test_g16_the_preview_of_a_holiday_says_it_does_not_touch_the_payment(pg_session, clock):
    w, g, students, ms, url = cancel_world(pg_session, clock)
    dry = cancel_post(w, "owner", url, {**WED, "cancellationKind": "holiday"}, dry=True)
    assert dry.json()["consequence"] == "На оплату не влияет"



def _subs_world(pg_session, clock):
    from tests.b2_helpers import build_d2
    from tests.test_ops_b2_decide_pg import one

    clock.set("2026-10-22 11:00")
    w = build_d2(pg_session)
    grant_money_right(w, False)
    return w, one(w, "A", "L2", "2026-10-28")["id"]


def test_g16_a_decision_to_cancel_with_the_kind_holiday_is_refused_without_the_right_one_and_many(pg_session, clock):
    from tests.payf_helpers import API, client as payf_client

    w, rid = _subs_world(pg_session, clock)
    body = {"decision": "cancel", "reason": "Урока не было", "cancellationKind": "holiday"}
    one = payf_client(w, "admin").post(f"{API}/org/substitutions/{rid}/decide", json=body)
    assert one.status_code == 403 and one.json()["detail"]["code"] == "MONEY_RIGHT_REQUIRED", one.text
    many = payf_client(w, "admin").post(f"{API}/org/substitutions/decide-bulk", json={**body, "ids": [rid]})
    assert many.status_code == 403 and many.json()["detail"]["code"] == "MONEY_RIGHT_REQUIRED", many.text
    ok = payf_client(w, "admin").post(f"{API}/org/substitutions/{rid}/decide", json={"decision": "cancel", "reason": "Урока не было"})      # no kind = by the centre
    assert ok.status_code == 200, ok.text


def test_g16_the_save_of_attendance_as_a_cancellation_holiday_is_refused_without_the_right(pg_session, clock):
    w, g, students, ms, url = cancel_world(pg_session, clock)
    grant_money_right(w, False)
    body = {"lessonDate": "2026-10-28", "lessonId": "lesson-wed", "status": "cancelled", "records": {}, "cancellationReason": "Праздник", "cancellationKind": "holiday"}
    refused = cancel_post(w, "admin", url.replace("/reconcile/cancel", ""), body)
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "MONEY_RIGHT_REQUIRED", refused.text


# ---- Г-4 (N5, ДП-04): the cancellation of a payment of the old group after a transfer says what appears in the new one -----------------------------------------

from tests.test_payment_carry_over_pg import billing, clock as at_day, day, mid, owner, pay, statement, transfer, two  # noqa: E402,F401  (fixtures)

from tests.test_payments_service_pg import API  # noqa: E402

PAY_URL = f"{API}/org/payments"


def _transferred_world(two, monkeypatch):
    billing(two)
    p = pay(two, paid_on=day(-10), amount=3_000_000)["payment"]       # 3 000 000 in the group A, the student is moved to B three days ago: the money went with him
    with at_day(monkeypatch, day(-3), 12):
        transfer(two, two.second)
    return p, statement(two, two.second)["transferred_in"]


def test_g4_the_confirmation_and_the_answer_name_the_debt_that_appears_in_the_new_group(two, monkeypatch):
    p, carried = _transferred_world(two, monkeypatch)
    in_b, in_a = statement(two, two.second), statement(two, two.group)
    assert carried > 0 and in_b["debt"] == 0 and in_a["debt"] == 0
    dry = owner(two).post(f"{PAY_URL}/{p['id']}/void", params={"dry_run": "true"}, json={"reason": "ошибка"})
    assert dry.status_code == 200, dry.text
    assert statement(two, two.second) == in_b                                              # the preview writes nothing
    real = owner(two).post(f"{PAY_URL}/{p['id']}/void", json={"reason": "ошибка"})
    assert real.status_code == 200, real.text
    after_b, after_a = statement(two, two.second), statement(two, two.group)
    assert after_b["transferred_in"] == 0 and after_b["debt"] > 0
    expected = [{"membership_id": str(mid(two, two.student, two.second)), "group_id": str(two.second.id), "group_name": "Вторая", "amount": after_b["debt"], "total": after_b["debt"]}]
    assert dry.json()["new_debts"] == expected == real.json()["new_debts"]                  # the numbers of the preview are the numbers after (I-5)
    sentence = f"В группе «Вторая» появится долг {after_b['debt']:,}".replace(",", NBSP) + f"{NBSP}сум"
    assert sentence in dry.json()["money_effects"]["text"], dry.json()["money_effects"]["text"]
    assert sentence.replace("появится", "появился") in real.json()["money_effects"]["text"]
    (row,) = [r for r in owner(two).get(f"{PAY_URL}/decisions", params={"from": day(-1).isoformat(), "to": day(1).isoformat()}).json()["rows"] if r["kind"] == "payment_void"]
    assert row["debt_before"] == in_a["debt"] + in_b["debt"] and row["debt_after"] == after_a["debt"] + after_b["debt"]       # the debt of ALL the groups the payment touches
    assert row["consequence"] == sentence.replace("появится", "появился")


def test_g4_a_payment_nobody_took_over_names_no_other_group(two, monkeypatch):
    billing(two)
    p = pay(two, paid_on=day(-10), amount=3_000_000)["payment"]                             # no transfer: nothing appears elsewhere
    dry = owner(two).post(f"{PAY_URL}/{p['id']}/void", params={"dry_run": "true"}, json={"reason": "ошибка"})
    assert dry.status_code == 200 and dry.json()["new_debts"] == [] and "появится" not in (dry.json()["money_effects"] or {}).get("text", "")


# ---- Г-4б: a correction of a payment to a SMALLER sum is the same rule as the cancellation (``_moved_by_correction``) ---------------------------------------

def test_g4b_a_correction_to_a_smaller_sum_names_the_debt_that_appears_in_the_new_group(two, monkeypatch):
    p, carried = _transferred_world(two, monkeypatch)
    in_b, in_a = statement(two, two.second), statement(two, two.group)
    assert carried > 0 and in_b["debt"] == 0 and in_a["debt"] == 0
    body = {"amount": 100_000, "paid_on": p["paid_on"], "method": "cash", "reason": "Опечатка в сумме"}
    dry = owner(two).post(f"{PAY_URL}/{p['id']}/replace", params={"dry_run": "true"}, json=body)
    assert dry.status_code == 201, dry.text
    assert statement(two, two.second) == in_b                                              # the preview writes nothing
    real = owner(two).post(f"{PAY_URL}/{p['id']}/replace", json=body)
    assert real.status_code == 201, real.text
    after_b, after_a = statement(two, two.second), statement(two, two.group)
    assert after_b["transferred_in"] < carried and after_b["debt"] > 0
    expected = [{"membership_id": str(mid(two, two.student, two.second)), "group_id": str(two.second.id), "group_name": "Вторая", "amount": after_b["debt"], "total": after_b["debt"]}]
    assert dry.json()["new_debts"] == expected == real.json()["new_debts"]
    sentence = f"В группе «Вторая» появится долг {after_b['debt']:,}".replace(",", NBSP) + f"{NBSP}сум"
    assert sentence in dry.json()["money_effects"]["text"], dry.json()["money_effects"]["text"]
    assert sentence.replace("появится", "появился") in real.json()["money_effects"]["text"]
    (row,) = [r for r in owner(two).get(f"{PAY_URL}/decisions", params={"from": day(-1).isoformat(), "to": day(1).isoformat()}).json()["rows"] if r["kind"] == "payment_replace"]
    assert row["debt_before"] == in_a["debt"] + in_b["debt"] and row["debt_after"] == after_a["debt"] + after_b["debt"]
    assert row["consequence"] == sentence.replace("появится", "появился")


def test_g4b_a_correction_to_the_same_or_a_bigger_sum_names_no_other_group(two):
    billing(two)
    p = pay(two, paid_on=day(-10), amount=3_000_000)["payment"]                             # no transfer: the student is still in A, nothing appears elsewhere
    r = owner(two).post(f"{PAY_URL}/{p['id']}/replace", params={"dry_run": "true"}, json={"amount": 3_000_000, "paid_on": p["paid_on"], "method": "cash", "reason": "Способ"})
    assert r.status_code == 201, r.text                                                     # POST /replace answers 201 also with dry_run
    assert r.json()["new_debts"] == [] and "появится" not in (r.json()["money_effects"] or {}).get("text", "")


def test_g4b_a_student_who_left_takes_no_more_than_his_debt_even_in_a_correction(two, monkeypatch):
    p, _ = _transferred_world(two, monkeypatch)                                             # S-5 (older rule): a BIGGER sum after a transfer is refused, G-4b adds nothing to it
    r = owner(two).post(f"{PAY_URL}/{p['id']}/replace", params={"dry_run": "true"}, json={"amount": 3_000_001, "paid_on": p["paid_on"], "method": "cash", "reason": "Способ"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "PAYMENT_EXCEEDS_DEBT"


def test_r13_only_the_method_of_a_payment_of_the_old_group_can_be_corrected_after_a_transfer(two, monkeypatch):
    p, _ = _transferred_world(two, monkeypatch)                                             # the sum and the date stay: the debt is not looked at (R3 M11)
    before = statement(two, two.second)
    body = {"amount": p["amount"], "paid_on": p["paid_on"], "method": "card", "reason": "Оплатили картой"}
    r = owner(two).post(f"{PAY_URL}/{p['id']}/replace", json=body)
    assert r.status_code == 201, r.text                                                     # before: 422 PAYMENT_EXCEEDS_DEBT
    assert r.json()["payment"]["method"] == "card" and r.json()["new_debts"] == [] and statement(two, two.second) == before


# ---- Г-17 (N9): the cash of a day is counted by the day the record was ENTERED (the till of the centre), not by the date of the payment ---------------------------

from datetime import datetime  # noqa: E402

from tests.test_income2_pg import add_payments, income, journal, seeded  # noqa: E402,F401  (fixtures)
from tests.test_pay2_api_pg import D, fixed_today  # noqa: E402,F401  (fixtures)


def _till(w, day):
    c = income(w, day, day)["cash"]
    return c["cash_received"], c["cash_refunded"], c["expected"]


def _cancelled(w, day):
    return income(w, day, day)["cash"]["cash_cancelled"]


def test_g17_cash_follows_the_day_of_entry_and_a_closed_day_does_not_move(world, seeded):
    base21, base22 = _till(world, "2026-10-03"), _till(world, "2026-10-04")
    ids = add_payments(
        world, seeded,
        ("closed-day", D(2026, 10, 3), datetime(2026, 10, 3, 5, 0), 1_000_000, None),            # 03.10 10:00 Tashkent: 1 000 000 in the till, the day is over
        ("late", D(2026, 10, 3), datetime(2026, 10, 4, 6, 0), 200_000, None))                    # 04.10 11:00: entered a day later FOR 03.10
    assert _till(world, "2026-10-03") == (base21[0] + 1_000_000, base21[1], base21[2] + 1_000_000)      # e7c44aa: 1 200 000 (by paid_on) - the closed day moved
    assert _till(world, "2026-10-04") == (base22[0] + 200_000, base22[1], base22[2] + 200_000)           # e7c44aa: nothing - the money is in the box on 04.10
    late = next(r for r in journal(world, "2026-10-01", "2026-10-31", size=100)["rows"] if r["id"] == ids["late"])
    assert late["paid_on"] == "2026-10-03" and late["dates_text"] == "внесена 04.10 · дата оплаты 03.10"
    assert next(r for r in journal(world, "2026-10-01", "2026-10-31", size=100)["rows"] if r["id"] == ids["closed-day"])["dates_text"] is None


def test_g17_the_cancellation_on_another_day_leaves_the_closed_day_alone_and_leaves_the_till_of_its_own_day(world, seeded):
    from src.models.auth import StudentPayment

    base = {d: _till(world, d) for d in ("2026-10-03", "2026-10-05")}
    ids = add_payments(world, seeded, ("p", D(2026, 10, 3), datetime(2026, 10, 3, 5, 0), 300_000, None))
    p = world.db.query(StudentPayment).get(ids["p"])
    p.voided_at, p.voided_by, p.void_reason = datetime(2026, 10, 5, 6, 0), world.owner.id, "ошибка"          # cancelled on 05.10 11:00
    world.db.commit()
    assert _till(world, "2026-10-03") == (base["2026-10-03"][0] + 300_000, base["2026-10-03"][1], base["2026-10-03"][2] + 300_000)      # e7c44aa: the cancelled payment vanished from 03.10
    assert _till(world, "2026-10-05") == (base["2026-10-05"][0], base["2026-10-05"][1], base["2026-10-05"][2] - 300_000)       # and the till of 05.10 says it ...
    assert _cancelled(world, "2026-10-05") == 300_000 and income(world, "2026-10-05", "2026-10-05")["cash"]["text"].endswith("отменено 300\u00a0000\u00a0сум)")   # ... as «отменено», not «возвращено» (Г-17b)


def test_r11_a_cash_correction_on_another_day_is_one_part_with_the_difference_and_a_real_cancellation_stays_a_cancellation(world, seeded):
    from src.models.auth import StudentPayment

    base = _till(world, "2026-10-05")
    ids = add_payments(world, seeded, ("old", D(2026, 10, 3), datetime(2026, 10, 3, 5, 0), 300_000, None), ("new", D(2026, 10, 3), datetime(2026, 10, 5, 6, 0), 250_000, None))
    old, new = world.db.query(StudentPayment).get(ids["old"]), world.db.query(StudentPayment).get(ids["new"])
    old.voided_at, old.voided_by, old.void_reason = datetime(2026, 10, 5, 6, 0), world.owner.id, "Опечатка"       # 300 000 corrected to 250 000 on 05.10 11:00 Tashkent
    new.replaces_payment_id = old.id
    world.db.commit()
    cash = income(world, "2026-10-05", "2026-10-05")["cash"]
    assert (cash["cash_received"], cash["cash_cancelled"], cash["cash_corrected"]) == (base[0], 0, -50_000)       # before: received +250 000, cancelled 300 000
    assert cash["expected"] == base[2] - 50_000 and "исправлено −50 000 сум)" in cash["text"] and "отменено" not in cash["text"]      # the till moved by the difference only


# ---- Р-1 (R3 Б1, М3): the substitution desk is the second way to Г-3 -------------------------------------------------------------------------------------------

def test_r1_a_substitute_of_a_past_lesson_without_a_journal_row_is_for_the_money_right_on_the_substitution_desk(pg_session, clock):
    from tests.payf_helpers import API, client as payf_client

    w, rid = _subs_world(pg_session, clock)                              # request of A for L2 28.10 (Wed), made on 22.10
    clock.set("2026-10-29 12:00")                                        # the lesson (28.10) is over, nobody opened it: no journal row, the request is still open
    body = {"decision": "substitute", "teacherId": str(w.B.id)}
    admin = payf_client(w, "admin")
    one = admin.post(f"{API}/org/substitutions/{rid}/decide", json=body)
    assert one.status_code == 403 and one.json()["detail"]["code"] == "MONEY_RIGHT_REQUIRED", one.text      # e7c44aa..4f76113: 200, B formalises the lesson and is paid
    many = admin.post(f"{API}/org/substitutions/decide-bulk", json={**body, "ids": [rid]})
    assert many.status_code == 200 and many.json()["updated"] == 0 and many.json()["skipped"][0]["code"] == "MONEY_RIGHT_REQUIRED", many.text      # the bulk answers per request
    grant_money_right(w)
    assert admin.post(f"{API}/org/substitutions/{rid}/decide", json=body).status_code == 200


# ---- Р-5 (R3 В4): a tick on a reopened month also refuses when only the RATE moved --------------------------------------------------------------------------

def test_r5_a_tick_on_a_reopened_month_whose_rate_changed_is_refused_and_the_month_stays_open(world, fixed_today):
    from tests.test_payroll_pg import set_rate

    m, s1, s2, first, second = two_teachers_in_a_closed_september(world, fixed_today)       # September of the first teacher: 475 000 at 50 %
    reopen_now(world.db, world.org.id, world.teacher.id, "2026-09", world.owner.id, "Поправить ставку")
    fixed_today(date(2026, 11, 20))
    set_rate(world, world.teacher, "group", "60", valid_from=date(2026, 9, 1))             # the owner raises the rate from 01.09: the data of the month are the same, the rate is not
    refused = tick(world, first)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "SHEET_DATA_CHANGED", refused.text      # before: 200, the month closed again with 475 000 at the old rate
    assert world.db.query(PayrollSheet).filter_by(id=first["id"]).one().revision == first["revision"]


# ---- Р-2 (R3 В1): «появится долг» is the GROWTH of the debt of the new group, not all of its debt ---------------------------------------------------------

def _transferred_into_a_group_that_already_owes(two, monkeypatch):
    billing(two, price_b=2_000_000)                                   # the money that went with the student (3 000 000 less the use in A) does not cover the dearer group B
    p = pay(two, paid_on=day(-10), amount=3_000_000)["payment"]
    with at_day(monkeypatch, day(-3), 12):
        transfer(two, two.second)
    return p


def test_r2_the_new_debt_is_the_growth_and_names_the_whole_debt_when_the_group_owed_before(two, monkeypatch):
    p = _transferred_into_a_group_that_already_owes(two, monkeypatch)
    was = statement(two, two.second)["debt"]
    assert was > 0 and statement(two, two.second)["transferred_in"] > 0                 # B owes before the cancellation (the numbers of the scenario: 200 000 there)
    dry = owner(two).post(f"{PAY_URL}/{p['id']}/void", params={"dry_run": "true"}, json={"reason": "ошибка"})
    assert dry.status_code == 200, dry.text
    real = owner(two).post(f"{PAY_URL}/{p['id']}/void", json={"reason": "ошибка"})
    assert real.status_code == 200, real.text
    now = statement(two, two.second)["debt"]
    growth = now - was
    assert growth > 0 and now == 2_000_000
    expected = [{"membership_id": str(mid(two, two.student, two.second)), "group_id": str(two.second.id), "group_name": "Вторая", "amount": growth, "total": now}]
    assert dry.json()["new_debts"] == expected == real.json()["new_debts"]            # before: amount = the whole debt (2 000 000)
    money = lambda n: f"{n:,}".replace(",", NBSP) + f"{NBSP}сум"      # noqa: E731
    assert f"В группе «Вторая» долг вырастет на {money(growth)} — до {money(now)}" in dry.json()["money_effects"]["text"]
    assert f"В группе «Вторая» долг вырос на {money(growth)} — до {money(now)}" in real.json()["money_effects"]["text"]
    (row,) = [r for r in owner(two).get(f"{PAY_URL}/decisions", params={"from": day(-1).isoformat(), "to": day(1).isoformat()}).json()["rows"] if r["kind"] == "payment_void"]
    assert row["consequence"] == f"В группе «Вторая» долг вырос на {money(growth)} — до {money(now)}"


# ---- Р-3 (R3 В2): the confirmation of a cancellation names the saved sheet of the teacher of the OTHER group the money went to ----------------------------------

def test_r3_the_cancellation_of_a_payment_of_the_old_group_names_the_sheet_of_the_teacher_of_the_new_group(two, monkeypatch):
    from tests.money1_helpers import sheet as saved_sheet, tashkent
    from tests.test_payroll_pg import lesson as held, set_rate

    p, carried = _transferred_world(two, monkeypatch)                                   # 3 000 000 in A, moved to B three days ago: the money went with him
    set_rate(two, two.teacher2, "group", "50")
    lesson_day = day(-1)
    set_schedule(two, [slot(f"s{lesson_day.isoweekday()}", lesson_day.isoweekday(), "2026-01-01")], two.second)
    held(two, lesson_day, {two.student: "present"}, group=two.second, teacher=two.teacher2)     # a lesson of B, paid out of the money that came from A
    saved_sheet(two, two.teacher2, lesson_day.replace(day=1), tashkent(lesson_day.year, lesson_day.month, lesson_day.day), total=100_000)
    dry = owner(two).post(f"{PAY_URL}/{p['id']}/void", params={"dry_run": "true"}, json={"reason": "ошибка"})
    assert dry.status_code == 200, dry.text
    months = [line["month"] for line in dry.json()["money_effects"]["payroll"]]
    assert months == [lesson_day.strftime("%Y-%m")], dry.json()["money_effects"]          # before: [] - only the sheets of the teacher of A were looked at
    replace = owner(two).post(f"{PAY_URL}/{p['id']}/replace", params={"dry_run": "true"}, json={"amount": 100_000, "paid_on": p["paid_on"], "method": "cash", "reason": "Опечатка"})
    assert [line["month"] for line in replace.json()["money_effects"]["payroll"]] == months      # the correction is the same rule


# ---- Р-4 (R3 В3): the consequence of a cancellation is one sentence on EVERY road, asked before the cancellation (GET, nothing is written) -----------------------

def _consequence(w, who, group, lesson="lesson-wed", day="2026-10-28", expect=200):
    from tests.payf_helpers import API as PAYF_API, client as payf_client

    r = payf_client(w, who).get(f"{PAYF_API}/org/groups/{group.id}/lessons/cancel-consequence", params={"lessonDate": day, "lessonId": lesson})
    assert r.status_code == expect, r.text
    return r.json()


def test_r4_the_own_month_with_the_setting_on_says_what_each_student_gets_and_the_money_right_changes_nothing(pg_session, clock):
    from tests.money1_helpers import changed_tables, db_fingerprint

    w, g, students, ms, url = cancel_world(pg_session, clock)               # 46 000 deducted from each of the five for the lesson of 26.10 already
    sentence = f"Ученикам зачтётся по 46{NBSP}000{NBSP}сум"
    before = db_fingerprint(w.db)
    assert _consequence(w, "owner", g) == {"consequence": sentence}
    assert changed_tables(before, db_fingerprint(w.db)) == []                 # nothing is written
    assert cancel_post(w, "owner", url, WED, dry=True).json()["consequence"] == sentence       # «Урока не было» says the same sentence (ONE function)
    grant_money_right(w, False)
    assert _consequence(w, "admin", g) == {"consequence": sentence}           # without the right: the sentence only, the sums of the group are not in the answer


def test_r4_the_setting_off_says_the_payment_is_not_touched(pg_session, clock):
    from tests.test_p154_money_pg import build_org
    from tests.test_payf_recalc_pg import month_world

    w = build_org(pg_session)
    g, s, m = month_world(w, clock, switch_on=None)                          # own month, the setting «вычитать уроки, отменённые центром» was never switched on
    clock.set("2026-10-30 12:00")
    assert _consequence(w, "owner", g) == {"consequence": "На оплату не влияет"}


def test_r4_a_package_group_says_the_payment_is_not_touched(pg_session, clock):
    from tests.test_p154_money_pg import build_org, package_world

    w = build_org(pg_session)
    g, s, m = package_world(w, clock)
    clock.set("2026-10-30 12:00")
    assert _consequence(w, "owner", g) == {"consequence": "На оплату не влияет"}               # a package spends nothing on a cancelled lesson


def test_r4_an_unknown_group_is_not_found(pg_session, clock):
    import uuid

    w, g, students, ms, url = cancel_world(pg_session, clock)
    assert _consequence(w, "owner", type("G", (), {"id": uuid.uuid4()}), expect=404)["detail"]["code"] == "GROUP_NOT_FOUND"


