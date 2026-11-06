"""Money-1 pack B3 / B9 (PostgreSQL): the windows of a change - price, billing mode, day of the shared month - say «было → станет» for EVERY student in sums and lessons; the journal of the money settings; the second payment.

* ДЫРА-01 (Q1/Q2): a package paid in advance and not begun: the price announced for a later day makes 12 lessons 10 - the window says it BEFORE the confirmation (it said «ничего не меняется»);
* ДЫРА-02 (D1): the mode «месяц» -> «пакет 12 уроков» keeps the number of the price and changes its meaning: 300 000 a month = 23 077 a lesson of 13, 25 000 a lesson of 12;
* ДЫРА-06 (H1): the day of the shared month 1 -> 10: the transitional cell 01.11-09.11 (4 of 13 lessons = 92 000) is told before;
* B4 of the devil: the students with an individual price are listed apart, with the difference from the new group price;
* ДЫРА-12 (M1): two operators, two keys, one sum - the second sees «Такая же оплата внесена N минут назад»; a warning, not a ban."""
from datetime import date, datetime, timedelta

from src.models.auth import OrgMoneyAudit, StudentPayment

from tests.money1b_helpers import account, hold_lessons, set_schedule, slot
from tests.pg_world import give_group_price, give_price, make_client, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, seed_payment, settings_block, stay  # noqa: F401  (fixtures)

pytestmark = needs_pg
D = date
MWF = [slot("mon", 1, "2026-09-01"), slot("wed", 3, "2026-09-01"), slot("fri", 5, "2026-09-01")]


def put_price(w, amount, *, valid_from=None, dry_run=True, expect=200):
    body = {"amount": amount, **({"valid_from": valid_from.isoformat()} if valid_from else {})}
    r = owner(w).put(f"{API}/org/groups/{w.group.id}/price", params={"dry_run": 1} if dry_run else {}, json=body)
    assert r.status_code == expect, r.text
    return r.json()


def put_settings(w, *, dry_run=True, expect=200, **body):
    r = owner(w).put(f"{API}/org/settings/payments", params={"dry_run": 1} if dry_run else {}, json=body)
    assert r.status_code == expect, r.text
    return r.json()


# ---- ДЫРА-01: a package paid in advance ----------------------------------------------------------------------------------------------------------


def package_world(w, fixed_today, held=0):
    fixed_today(D(2026, 11, 20))
    settings_block(w, period_mode="lessons", lessons_per_payment=12, accounting_start_date="2026-09-01", billing_history=[{"from": "min", "period_mode": "lessons", "lessons_per_payment": 12}])
    give_group_price(w.db, w.group, 600_000)
    for student in w.students[1:]:                      # only the first student is the subject
        stay(w, membership(w, student), D(2026, 11, 20))
    m = membership(w)
    stay(w, m, D(2026, 11, 20))
    seed_payment(w, m, 600_000, D(2026, 11, 20))         # paid 12 lessons, nothing held yet
    if held:
        hold_lessons(w, w.students[0], [D(2026, 11, 20)][:held])
    return m


def test_b3_q1_a_package_paid_in_advance_loses_two_lessons_when_the_price_rises_on_01_12_and_the_window_says_so(world, fixed_today):
    w = world
    m = package_world(w, fixed_today)
    assert account(w, m.id).lessons.covered == 12
    dry = put_price(w, 700_000, valid_from=D(2026, 12, 1))
    money = dry["impact"]["money"]
    assert money["as_of"] == "2026-12-01" and money["items_total"] >= 1
    item = next(i for i in money["items"] if i["membership_id"] == str(m.id))
    assert (item["lessons_before"]["covered"], item["lessons_after"]["covered"]) == (12, 10)        # before: changed=False, the student was not even in the list
    assert (item["lessons_before"]["left"], item["lessons_after"]["left"]) == (12, 10)
    assert item["text"].endswith("оплачено уроков 12 → 10; осталось уроков 12 → 10")
    assert "изменится" in money["text"].lower()


def test_b3_q2_one_lesson_held_before_the_rise_freezes_the_package_and_nothing_is_listed(world, fixed_today):
    w = world
    m = package_world(w, fixed_today, held=1)
    money = put_price(w, 700_000, valid_from=D(2026, 12, 1))["impact"]["money"]
    assert not [i for i in money["items"] if i["membership_id"] == str(m.id)]                       # Q2: the price of a package is the price of its first lesson


def test_b3_the_calendar_a5_advance_loses_days_and_a_backdated_price_is_read_today(world, fixed_today):
    w = world
    fixed_today(D(2026, 10, 20))
    settings_block(w, accounting_start_date="2026-09-01")
    give_group_price(w.db, w.group, 300_000, valid_from=D(2026, 1, 1))
    for student in w.students:
        stay(w, membership(w, student), D(2026, 10, 5))
    m = membership(w)
    seed_payment(w, m, 600_000, D(2026, 10, 5))                                                      # two months by 300 000
    far = put_price(w, 350_000, valid_from=D(2026, 11, 1))["impact"]["money"]
    item = next(i for i in far["items"] if i["membership_id"] == str(m.id))
    assert (item["paid_until_before"], item["paid_until_after"]) == ("2026-12-04", "2026-11-29")      # A5 of the analyst: the advance buys 5 days less, at once
    near = put_price(w, 350_000, valid_from=D(2026, 10, 15))                                          # a past day: the account is read TODAY and the money of the student blocks it
    assert near["impact"]["money"]["as_of"] == "2026-10-20" and near["impact"]["money"]["backdated"] is True


def test_b3_the_students_with_an_individual_price_are_listed_apart_with_the_difference(world, fixed_today):
    w = world
    fixed_today(D(2026, 11, 20))
    settings_block(w, accounting_start_date="2026-09-01")
    give_group_price(w.db, w.group, 300_000, valid_from=D(2026, 1, 1))
    s0, s1 = w.students[0], w.students[1]
    give_price(w.db, membership(w, s0), 250_000, reason="скидка")
    money = put_price(w, 400_000, valid_from=D(2026, 12, 1))["impact"]["money"]
    (only,) = money["override_students"]
    assert (only["membership_id"], only["price"], only["group_price_before"], only["group_price_after"], only["difference_after"]) == (str(membership(w, s0).id), 250_000, 300_000, 400_000, -150_000)
    assert s1 is not None


# ---- ДЫРА-02: the same number, another meaning -------------------------------------------------------------------------------------------------


def mode_world(w, fixed_today):
    fixed_today(D(2026, 10, 21))
    settings_block(w, accounting_start_date="2026-09-01")
    give_group_price(w.db, w.group, 300_000, valid_from=D(2026, 1, 1))
    set_schedule(w, MWF)
    for student in w.students:
        stay(w, membership(w, student), D(2026, 9, 25))
    seed_payment(w, membership(w), 600_000, D(2026, 9, 25))                                          # the first student: two months


def test_b3_d1_month_to_package_every_student_is_listed_with_the_price_per_lesson(world, fixed_today):
    w = world
    mode_world(w, fixed_today)
    impact = put_settings(w, period_mode="lessons", lessons_per_payment=12)["regime_impact"]
    assert impact["students_affected"] == 3 and impact["items_total"] == 3 and len(impact["items"]) == 3
    for item in impact["items"]:
        assert (item["price_amount"], item["price_unit_before"], item["price_unit_after"]) == (300_000, "за месяц", "за пакет из 12 уроков")
        assert item["per_lesson_after"] == 25_000 and 20_000 <= item["per_lesson_before"] <= 30_000 and item["per_lesson_before"] == (2 * 300_000 + 13) // 26         # 300 000 / 13 lessons of the month = 23 077
        assert "цена 300 000 сум за месяц → за пакет из 12 уроков" in item["text"] and "≈ за урок 23 077 → 25 000 сум" in item["text"]      # R3 R-4: an approximation says so
        assert "(с 25.10, на 23.11)" in item["text"]                                                          # R3 R-5: the day of the figures is told
    assert impact["text"].startswith("Перейдут на новый способ 3 ученика")


def test_b3_d1_a_student_with_an_individual_price_is_told_apart(world, fixed_today):
    w = world
    mode_world(w, fixed_today)
    give_price(w.db, membership(w, w.students[1]), 220_000, reason="скидка")
    impact = put_settings(w, period_mode="lessons", lessons_per_payment=12)["regime_impact"]
    (only,) = impact["override_students"]
    assert only["price"] == 220_000 and "число не меняется, смысл — другой" in only["text"]


def test_b3_h1_the_day_of_the_shared_month_1_to_10_tells_the_transitional_cell(world, fixed_today):
    w = world
    fixed_today(D(2026, 10, 15))
    settings_block(w, accounting_start_date="2026-09-01", billing_anchor="fixed_day", billing_day=1)
    give_group_price(w.db, w.group, 300_000, valid_from=D(2026, 1, 1))
    set_schedule(w, MWF)
    for student in w.students:
        stay(w, membership(w, student), D(2026, 9, 1))
    impact = put_settings(w, billing_day=10)["regime_impact"]
    assert impact["students_affected"] == 3 and impact["items_total"] == 3
    one = impact["items"][0]
    assert one["switch_on"] == "2026-11-01" and one["as_of"] == "2026-11-30"
    assert (one["charged_before"], one["charged_after"]) == (300_000, 392_000)            # H1: IN the first 30 days (R3 money R-2; not the history since the accounting began): the cell 01.11-30.11 = 300 000
    assert one["charged_after"] - one["charged_before"] == 92_000                         # ... against the cell 01.11-09.11 = 4 of 13 lessons = 92 000 and the cell from 10.11 = 300 000
    assert (impact["charged_before_total"], impact["charged_after_total"]) == (900_000, 1_176_000)
    assert impact["text"] == "Перейдут на новый способ 3 ученика: за первые 30 дней после перехода начислено 900 000 сум → 1 176 000 сум"
    assert "(с 01.11, на 30.11)" in one["text"]


def test_b3_a_change_that_moves_nobody_has_an_empty_list(world, fixed_today):
    w = world
    mode_world(w, fixed_today)
    impact = put_settings(w, lessons_per_payment=10)["regime_impact"]                   # the package size of a centre that counts by months moves nothing
    assert impact is None or impact["items_total"] == 0


# ---- B9: the journal of the money settings ----------------------------------------------------------------------------------------------------


def test_b9_a_change_of_the_mode_the_day_and_the_recalculation_go_to_the_journal_with_the_sums(world, fixed_today):
    w = world
    mode_world(w, fixed_today)
    assert put_settings(w, period_mode="lessons", lessons_per_payment=12)["dry_run"] is True
    assert w.db.query(OrgMoneyAudit).filter_by(org_id=w.org.id).count() == 0                  # a dry run leaves nothing
    put_settings(w, dry_run=False, period_mode="lessons", lessons_per_payment=12)
    put_settings(w, dry_run=False, centre_cancel_recalc=True)
    w.db.expire_all()
    rows = w.db.query(OrgMoneyAudit).filter_by(org_id=w.org.id).order_by(OrgMoneyAudit.occurred_at).all()
    assert [r.action for r in rows] == ["billing_regime_changed", "centre_cancel_recalc_changed"]
    first = rows[0].details
    assert first["students_affected"] == 3 and first["items_total"] == 3 and first["before"]["period_mode"] == "calendar" and first["after"]["period_mode"] == "lessons"
    assert first["text"].startswith("Изменён способ оплаты: способ «Свой месяц у каждого ученика» → «Пакет уроков»")
    assert "Перейдут на новый способ 3 ученика" in first["text"] and rows[0].actor_id == w.owner.id
    assert first["sums_available"] is True and (first["charged_before_total"], first["charged_after_total"]) == (900_000, 0)           # the first 30 days: 3 x a month of 300 000 now, a package charges lessons as they are held (R3 R-2: not 1 640 000, the history)
    assert "начислено 900 000 сум → 0 сум" in first["text"]
    assert rows[1].details["text"].startswith("Перерасчёт за уроки, отменённые центром: выключен → включён; действует на уроки с 21.10.2026")
    shown = owner(w).get(f"{API}/org/settings/payments/history").json()["items"]
    assert [i["action"] for i in shown] == ["centre_cancel_recalc_changed", "billing_regime_changed"] and shown[1]["details"]["items"][0]["text"]
    assert make_client(w, user=w.admin).get(f"{API}/org/settings/payments/history").status_code == 403         # the journal is the owner's, like the settings


def test_b9_the_day_of_the_shared_month_is_in_the_journal(world, fixed_today):
    w = world
    fixed_today(D(2026, 10, 15))
    settings_block(w, accounting_start_date="2026-09-01", billing_anchor="fixed_day", billing_day=1)
    give_group_price(w.db, w.group, 300_000, valid_from=D(2026, 1, 1))
    put_settings(w, dry_run=False, billing_day=10)
    (row,) = w.db.query(OrgMoneyAudit).filter_by(org_id=w.org.id).all()
    assert "день общего месяца 1-е → 10-е" in row.details["text"]


# ---- ДЫРА-12 (M1): the second payment ----------------------------------------------------------------------------------------------------------


def paid_before(w, minutes_ago, amount=300_000):
    from src.auth import business_clock as bc

    fixed_now = bc.utc_now()                                  # the frozen clock itself (R3 code №14: not a literal «12:00 Ташкента»: the load run sets ALGO_TEST_CLOCK)
    p = seed_payment(w, membership(w), amount, D(2026, 10, 21))
    p.created_at = fixed_now - timedelta(minutes=minutes_ago)
    w.db.commit()
    return p


def fixed_now_utc():
    from src.auth import business_clock as bc

    return bc.utc_now()


def repeat_world(w, fixed_today):
    fixed_today(D(2026, 10, 21))
    settings_block(w, accounting_start_date="2026-09-01")
    give_group_price(w.db, w.group, 300_000, valid_from=D(2026, 1, 1))
    for student in w.students:
        stay(w, membership(w, student), D(2026, 10, 5))


def codes(response):
    return [x["code"] for x in response["warnings"]]


def test_b9_m1_the_same_sum_for_the_same_student_a_few_minutes_later_is_a_warning_in_the_window_and_in_the_answer_not_a_ban(world, fixed_today):
    w = world
    repeat_world(w, fixed_today)
    m = membership(w)
    paid_before(w, 5)
    preview = owner(w).get(f"{API}/org/memberships/{m.id}/payments/preview", params={"amount": 300_000}).json()
    (warning,) = [x for x in preview["warnings"] if x["code"] == "payment_repeated"]
    assert warning["message"].startswith("Такая же оплата внесена 5 минут назад — ")
    made = owner(w).post(f"{API}/org/memberships/{m.id}/payments", json={"amount": 300_000, "paid_on": "2026-10-21", "method": "cash"}, headers={"Idempotency-Key": "3f2504e0-4f89-41d3-9a0c-0305e82c3301"})
    assert made.status_code == 201 and "payment_repeated" in codes(made.json())                  # never a ban: the payment is made
    assert w.db.query(StudentPayment).filter_by(membership_id=m.id, voided_at=None).count() == 2


def test_b9_another_sum_or_an_old_payment_or_a_voided_one_is_not_a_repeat(world, fixed_today):
    w = world
    repeat_world(w, fixed_today)
    m = membership(w)
    p = paid_before(w, 5)
    other = owner(w).get(f"{API}/org/memberships/{m.id}/payments/preview", params={"amount": 250_000}).json()
    assert "payment_repeated" not in [x["code"] for x in other["warnings"]]
    p.created_at = fixed_now_utc() - timedelta(hours=25)                                          # 25 hours ago
    w.db.commit()
    old = owner(w).get(f"{API}/org/memberships/{m.id}/payments/preview", params={"amount": 300_000}).json()
    assert "payment_repeated" not in [x["code"] for x in old["warnings"]]
    p.created_at = fixed_now_utc() - timedelta(minutes=5)
    p.voided_at = fixed_now_utc() - timedelta(minutes=4)
    p.voided_by, p.void_reason = w.owner.id, "ошибка"
    w.db.commit()
    voided = owner(w).get(f"{API}/org/memberships/{m.id}/payments/preview", params={"amount": 300_000}).json()
    assert "payment_repeated" not in [x["code"] for x in voided["warnings"]]


def test_b9_hours_are_told_in_hours(world, fixed_today):
    w = world
    repeat_world(w, fixed_today)
    m = membership(w)
    paid_before(w, 130)
    got = owner(w).get(f"{API}/org/memberships/{m.id}/payments/preview", params={"amount": 300_000}).json()
    assert [x["message"] for x in got["warnings"] if x["code"] == "payment_repeated"][0].startswith("Такая же оплата внесена 2 часа назад")
