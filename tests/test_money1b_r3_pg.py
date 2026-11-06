"""Money-1 pack B, round R3 (PostgreSQL): one test for each finding of ``MONEY1_B_R3_CODE.md`` and ``MONEY1_B_R3_MONEY.md``, each red on the code before the round (the mutations are in the report)."""
from datetime import date

from src.models.auth import OrgMoneyAudit

from tests.money1b_helpers import account, hold_lessons
from tests.pg_world import give_group_price, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_money1b_windows_pg import put_settings
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, seed_payment, settings_block, stay  # noqa: F401  (fixtures)

pytestmark = needs_pg
D = date


# ---- №1 (blocker): a package in progress switches «когда закончится» - the window and the save must not fall over on date.max --------------------------------


def package_in_progress(w, fixed_today, held=5):
    fixed_today(D(2026, 11, 15))
    settings_block(w, period_mode="lessons", lessons_per_payment=12, accounting_start_date="2026-09-01",
                   billing_history=[{"from": "min", "period_mode": "lessons", "lessons_per_payment": 12}])
    give_group_price(w.db, w.group, 600_000)
    for student in w.students:
        stay(w, membership(w, student), D(2026, 11, 2))
    hold_lessons(w, w.students[0], [D(2026, 11, d) for d in (2, 4, 6, 9, 11)][:held])           # 5 of 12: the package is NOT finished
    seed_payment(w, membership(w), 600_000, D(2026, 11, 2))
    return membership(w)


def test_r3_1_from_a_package_in_progress_to_the_calendar_the_window_and_the_save_answer(world, fixed_today):
    w = world
    package_in_progress(w, fixed_today)
    for body in ({"period_mode": "calendar"}, {"period_mode": "calendar", "billing_anchor": "fixed_day", "billing_day": 10}, {"lessons_per_payment": 8}):
        dry = put_settings(w, **body)                                                       # before the round: HTTP 500 (OverflowError on 9999-12-31)
        impact = dry["regime_impact"]
        assert impact["students_affected"] == 3 and impact["pending_package_students"] >= 1
        first = next(i for i in impact["items"] if i["switch_on"] is None)                  # «когда закончится пакет»
        assert first["text"].endswith("начнёт действовать, когда закончится пакет") or "когда закончится пакет" in first["text"]
    before = w.db.query(OrgMoneyAudit).filter_by(org_id=w.org.id).count()
    put_settings(w, dry_run=False, period_mode="calendar")                                  # the real save too
    w.db.expire_all()
    assert w.db.query(OrgMoneyAudit).filter_by(org_id=w.org.id).count() == before + 1


# ---- №2 / money R-1 (major): written down «с 06.11», taken out on 22.10 - he never came, he owes nothing, ever --------------------------------------------------


def test_r3_2_a_stay_that_ended_before_his_pay_from_charges_nothing_before_and_after_the_date(world, fixed_today):
    from src.auth.leave_service import LeaveService
    from tests.money1b_helpers import add, fresh_student, opening_world
    from tests.test_money1b_pay_from_pg import stay_of

    w = world
    opening_world(w, fixed_today)                                                           # today 21.10, the group opens 05.11, the first lesson is Friday 06.11
    s = fresh_student(w, "Призрак")
    add(w, s)
    m, period = stay_of(w, s)
    assert period.billing_from == D(2026, 11, 6)
    fixed_today(D(2026, 10, 22))
    LeaveService(w.db).leave(str(w.org.id), str(w.group.id), str(s.id), str(w.owner.id), "owner", None, money=True)
    w.db.expire_all()
    for day in (D(2026, 10, 26), D(2026, 11, 6), D(2026, 11, 20), D(2026, 12, 20)):
        led = account(w, m.id, day)
        assert (led.charged, led.debt, led.reconciles) == (0, 0, True), day               # before the round: 10 000 from 06.11 for ever (a one-day stay out of an upside-down one)
    fixed_today(D(2026, 12, 20))
    page = owner(w).get(f"{API}/org/payments/status", params={"bucket": "debt", "limit": 100}).json()
    assert str(m.id) not in {i["membership_id"] for i in page["items"]}                  # not in «Должники»


def test_r3_2_the_same_when_he_is_transferred_to_another_group_before_the_date(world, fixed_today):
    from tests.money1b_helpers import add, fresh_student, opening_world, transfer
    from tests.pg_world import make_group
    from tests.test_money1b_pay_from_pg import stay_of

    w = world
    opening_world(w, fixed_today)
    s = fresh_student(w, "Переведённый")
    add(w, s)
    m, _ = stay_of(w, s)
    other = make_group(w.db, w.org, w.teacher, "Другая")
    give_group_price(w.db, other, 300_000)
    fixed_today(D(2026, 10, 23))
    transfer(w, s, other)
    w.db.expire_all()
    assert (account(w, m.id, D(2026, 11, 20)).charged, account(w, m.id, D(2026, 11, 20)).debt) == (0, 0)


# ---- №3 (major, LESSONS 99): an administrator without the money right cannot give a group a price - nothing is written ------------------------------------------


def test_r3_3_an_administrator_without_the_money_right_cannot_set_the_price_of_a_new_group_and_nothing_is_written(world, fixed_today):
    from src.models.auth import Group
    from tests.money1_helpers import changed_tables, db_fingerprint
    from tests.pg_world import make_client

    w = world
    fixed_today(D(2026, 11, 15))
    admin = make_client(w, user=w.admin)
    groups = w.db.query(Group).filter_by(org_id=w.org.id).count()
    for price in (450_000, 0):                                                              # a free group is a price too
        before = db_fingerprint(w.db)
        r = admin.post(f"{API}/org/groups", json={"name": f"С ценой {price}", "price": price})
        assert r.status_code == 403 and r.json()["detail"]["code"] == "MONEY_RIGHT_REQUIRED", r.text
        assert changed_tables(before, db_fingerprint(w.db)) == []                           # the whole schema: not a group, not an audit row, not a price
    assert w.db.query(Group).filter_by(org_id=w.org.id).count() == groups
    ok = admin.post(f"{API}/org/groups", json={"name": "Без цены"})                         # the same administrator, no price: a group as always
    assert ok.status_code == 201 and w.db.query(Group).filter_by(org_id=w.org.id).count() == groups + 1


def test_r3_3_a_teacher_cannot_give_a_group_a_price_either(world, fixed_today):
    from tests.pg_world import make_client

    w = world
    fixed_today(D(2026, 11, 15))
    r = make_client(w, user=w.teacher).post(f"{API}/org/groups", json={"name": "Учитель", "price": 100_000})
    assert r.status_code == 403


# ---- №6: a repeated request of a first price is not a second row of the history -----------------------------------------------------------------------------------


def test_r3_6_the_same_first_price_twice_is_one_row_and_one_audit(world, fixed_today):
    from src.models.auth import GroupAudit, GroupPrice
    from tests.test_money1b_price_pg import everybody_from, put_price

    w = world
    fixed_today(D(2026, 11, 15))
    settings_block(w, accounting_start_date="2026-09-01")
    everybody_from(w, D(2026, 11, 5))
    put_price(w, 300_000, dry_run=False)                                                     # 1st: the day moves back to 05.11, the row is made
    again = put_price(w, 300_000, dry_run=False)                                             # 2nd, a double click: the same request, «сегодня» is 15.11 now
    assert w.db.query(GroupPrice).filter_by(group_id=w.group.id).count() == 1                # before the round: a second row «15.11: 300 000»
    assert w.db.query(GroupAudit).filter_by(group_id=w.group.id, action="group_price_set").count() == 1
    assert again["current"]["valid_from"] == "2026-11-05"
    put_price(w, 350_000, dry_run=False)                                                     # a real change of the price is a row as always
    assert w.db.query(GroupPrice).filter_by(group_id=w.group.id).count() == 2


# ---- №7: the price is set in the transaction of the creation of the group ----------------------------------------------------------------------------------------


def test_r3_7_a_failure_of_the_price_takes_the_new_group_down_with_it(world, fixed_today, monkeypatch):
    from src.auth import pricing
    from src.models.auth import Group

    w = world
    fixed_today(D(2026, 11, 15))
    real = pricing.PricingService.set_group_price

    def fails_after_the_write(self, *args, **kwargs):
        real(self, *args, **kwargs)                                                          # the price IS written (flushed) ...
        raise pricing.PriceConflict("Цену только что изменили. Обновите страницу и повторите.", code="PRICE_CONFLICT")        # ... and then it fails

    monkeypatch.setattr(pricing.PricingService, "set_group_price", fails_after_the_write)
    groups = w.db.query(Group).filter_by(org_id=w.org.id).count()
    r = owner(w).post(f"{API}/org/groups", json={"name": "Не создастся", "price": 450_000})
    assert r.status_code == 400, r.text
    w.db.rollback()
    assert w.db.query(Group).filter_by(org_id=w.org.id).count() == groups and w.db.query(Group).filter_by(name="Не создастся").count() == 0      # before: the group stayed without its price
    monkeypatch.setattr(pricing.PricingService, "set_group_price", real)
    ok = owner(w).post(f"{API}/org/groups", json={"name": "Создастся", "price": 450_000})            # the retry makes ONE group with its price
    assert ok.status_code == 201 and w.db.query(Group).filter_by(name="Создастся").count() == 1


# ---- №8: the day the server suggests is a day it accepts (the boundaries 62 / 63 days and the start of the accounting) ---------------------------------------


def test_r3_8_the_suggested_day_is_accepted_at_62_days_and_at_the_accounting_start(world, fixed_today):
    from tests.money1b_helpers import add, fresh_student, opening_world, set_schedule, slot

    w = world
    opening_world(w, fixed_today, schedule=[slot("tue", 2, "2026-12-22")])                   # today 21.10 + 62 days = Tuesday 22.12: the first lesson is the last allowed day
    got = owner(w).get(f"{API}/org/groups/{w.group.id}/pay-from-default").json()
    assert got == {"today": "2026-10-21", "first_lesson": "2026-12-22", "default_pay_from": "2026-12-22"}
    s = fresh_student(w, "Граница")
    done = add(w, s, pay_from=got["default_pay_from"])                                       # before the round the window searched 70 days: 63..70 was suggested and then refused
    assert done["billing"]["pay_from"] == "2026-12-22"
    set_schedule(w, [slot("wed", 3, "2026-12-23")])                                          # the 63rd day: no lesson in the window - today, never a day outside the range
    got = owner(w).get(f"{API}/org/groups/{w.group.id}/pay-from-default").json()
    assert got == {"today": "2026-10-21", "first_lesson": None, "default_pay_from": "2026-10-21"}
    add(w, fresh_student(w, "После"), pay_from=got["default_pay_from"])
    # the centre moved the start of the accounting 31 days ahead: «сегодня» is before it, the suggestion is the start itself
    settings_block(w, accounting_start_date="2026-11-21")
    set_schedule(w, [slot("mon", 1, "2026-11-02"), slot("fri", 5, "2026-11-02")])           # lessons already from Monday 02.11, before the start of the accounting
    got = owner(w).get(f"{API}/org/groups/{w.group.id}/pay-from-default").json()
    assert got["first_lesson"] == "2026-11-02" and got["default_pay_from"] == "2026-11-21"
    third = add(w, fresh_student(w, "Начало учёта"), pay_from=got["default_pay_from"])
    assert third["billing"]["pay_from"] == "2026-11-21"
    default = add(w, fresh_student(w, "Без даты"))                                           # no date sent: the same day, not a refusal
    assert default["billing"]["pay_from"] == "2026-11-21" and default["billing"]["explicit"] is False


# ---- №13: a bulk adding asks the schedule once ----------------------------------------------------------------------------------------------------------------


def test_r3_13_a_bulk_adding_reads_the_schedule_and_the_settings_once_for_the_whole_group(world, fixed_today, monkeypatch):
    from src.auth.service import AuthService
    from tests.money1b_helpers import fresh_student, opening_world

    w = world
    opening_world(w, fixed_today)
    students = [fresh_student(w, f"Массовый {i}") for i in range(6)]
    calls = []
    real = AuthService._resolved_occurrences_for_group

    def counted(self, group, **kwargs):
        calls.append(group.id)
        return real(self, group, **kwargs)

    monkeypatch.setattr(AuthService, "_resolved_occurrences_for_group", counted)
    r = owner(w).post(f"{API}/org/groups/{w.group.id}/students/bulk", params={"mode": "add"}, json={"student_ids": [str(s.id) for s in students]})
    assert r.status_code == 200 and r.json()["added"] == 6
    assert len([g for g in calls if g == w.group.id]) <= 2                                  # one search of the first lesson for the group (before the round: one for each of the six students)


# ---- lead 11: «сразу начислится N сум (K уроков)» in the add window --------------------------------------------------------------------------------------


def test_r3_lead11_the_dry_run_of_adding_with_a_past_pay_from_says_what_is_charged_at_once(world, fixed_today):
    from src.models.auth import StudentGroup
    from tests.money1b_helpers import add, fresh_student, opening_world, set_schedule, slot

    w = world
    opening_world(w, fixed_today, schedule=[slot("mon", 1, "2026-09-01"), slot("wed", 3, "2026-09-01"), slot("fri", 5, "2026-09-01")])
    s = fresh_student(w, "Давно ходит")
    lessons = sum(1 for k in range(31) if (D(2026, 10, 7).toordinal() + k - D(2026, 10, 5).toordinal()) % 7 in (0, 2, 4))      # 07.10 .. 06.11 by Mon / Wed / Fri (05.10 is a Monday)
    dry = add(w, s, pay_from="2026-10-07", dry_run=True)
    assert dry["billing"]["immediate"] == {"amount": 300_000, "lessons": lessons, "text": f"Сразу начислится 300 000 сум (за {lessons} уроков)"}
    assert w.db.query(StudentGroup).filter_by(student_id=s.id).count() == 0                  # a dry run writes nothing
    today_lesson = add(w, fresh_student(w, "Урок сегодня"), dry_run=True)                    # the default: Wednesday 21.10 is a lesson - the period begins today, the charge is at once too
    assert today_lesson["billing"]["immediate"]["amount"] == 300_000
    set_schedule(w, [slot("mon", 1, "2026-11-05"), slot("wed", 3, "2026-11-05"), slot("fri", 5, "2026-11-05")])
    future = add(w, fresh_student(w, "Придёт"), dry_run=True)                                # the first lesson is in the future (06.11) - nothing at once
    assert future["billing"]["immediate"] is None
    real = add(w, s, pay_from="2026-10-07")
    assert real["billing"]["immediate"]["amount"] == 300_000


# ---- №4 (major) / lead 4: the student who pays in time is not «вне списка» - his NEXT period costs another sum --------------------------------------------------------


def test_r3_4_a_price_from_01_12_names_the_student_who_paid_in_time_and_the_cost_of_his_next_period(world, fixed_today):
    from tests.test_money1b_price_pg import everybody_from, put_price

    w = world
    fixed_today(D(2026, 11, 20))
    settings_block(w, accounting_start_date="2026-09-01")
    give_group_price(w.db, w.group, 300_000, valid_from=D(2026, 1, 1))
    everybody_from(w, D(2026, 11, 5))
    for student in w.students:
        seed_payment(w, membership(w, student), 300_000, D(2026, 11, 5))                   # everybody paid 05.11 - 04.12 in time: no debt, no advance
    money = put_price(w, 400_000, valid_from=D(2026, 12, 1), dry_run=True)["impact"]["money"]
    assert money["as_of"] == "2026-12-01" and money["items_total"] == 3                    # before the round: «Деньги учеников не изменятся», nobody in the list
    assert money["next_period_students"] == 3 and money["next_period_delta_total"] == 300_000
    one = money["items"][0]
    assert (one["debt_before"], one["debt_after"], one["advance_before"], one["advance_after"]) == (0, 0, 0, 0)         # the account on 01.12 is the same ...
    assert one["next_period"] == {"period_from": "2026-12-05", "period_to": "2027-01-04", "amount_before": 300_000, "amount_after": 400_000, "difference": 100_000}      # ... the next period is not
    assert one["text"].endswith("(на 01.12): следующий период с 05.12: 300 000 → 400 000 сум (+100 000)")
    assert money["text"] == "Изменится у 3 учеников: следующий период дороже на 300 000 сум у 3 учеников"


def test_r3_4_a_cheaper_price_and_a_student_whose_next_period_costs_the_same(world, fixed_today):
    from tests.test_money1b_price_pg import everybody_from, put_price

    w = world
    fixed_today(D(2026, 11, 20))
    settings_block(w, accounting_start_date="2026-09-01")
    give_group_price(w.db, w.group, 300_000, valid_from=D(2026, 1, 1))
    everybody_from(w, D(2026, 11, 5))
    for student in w.students:
        seed_payment(w, membership(w, student), 300_000, D(2026, 11, 5))
    from tests.pg_world import give_price

    give_price(w.db, membership(w, w.students[2]), 250_000, reason="скидка")                # an individual price does not follow the group: the same next period
    money = put_price(w, 200_000, valid_from=D(2026, 12, 1), dry_run=True)["impact"]["money"]
    assert money["items_total"] == 2 and money["next_period_delta_total"] == -200_000 and "дешевле на 200 000 сум у 2 учеников" in money["text"]
    assert [o["student_name"] for o in money["override_students"]] == ["Орлова Дана"] and money["override_total"] == 1 and money["override_truncated"] is False


# ---- №9: the owner corrects the opening of the OLD group after a transfer - the nightly reconciliation does not write to the team ------------------------------------


def test_r3_9_a_correction_of_the_source_opening_after_a_transfer_explains_the_moved_paid_until(world):
    """(the real clock: the records of the database are stamped by its own ``now()``)"""
    from datetime import timedelta

    from src.auth import money_reconcile as mr
    from src.models.auth import MembershipOpeningBalance, MoneyReconcileSnapshot, StudentGroup
    from tests.money1b_helpers import opening_until, transfer
    from tests.pg_world import business_today, ensure_billing, make_group

    w = world
    ensure_billing(w)
    today = business_today()
    other = make_group(w.db, w.org, w.teacher, "Вторая")
    give_group_price(w.db, other, 450_000)
    s = w.students[0]
    m = membership(w, s)
    opening_until(w, m, today + timedelta(days=20))                                        # the notebook: paid until today + 20, no money
    transfer(w, s, other)
    first = mr.run(w.db, apply=True, only=[w.org.id])                                      # night 1: the snapshots (the transfer is older than the next snapshot)
    assert [i for i in first["items"] if i["invariant"] == "I-2" and i["org"] == str(w.org.id)] == []
    w.db.expire_all()
    target = w.db.query(StudentGroup).filter_by(student_id=s.id, group_id=other.id).one()
    assert w.db.query(MoneyReconcileSnapshot).filter_by(membership_id=target.id).count() == 1
    old = w.db.query(MembershipOpeningBalance).filter_by(membership_id=m.id).one()          # the owner corrects the opening of the OLD membership: 20 days -> 10 days
    old.voided_at, old.voided_by, old.void_reason = datetime_now(), w.owner.id, "ошибка в тетради"
    w.db.add(MembershipOpeningBalance(org_id=w.org.id, membership_id=m.id, student_id=m.student_id, group_id=m.group_id, paid_until=today + timedelta(days=10), source="manual", created_by=w.owner.id))
    w.db.commit()
    second = mr.run(w.db, apply=True, only=[w.org.id])
    ours = [i for i in second["items"] if i["invariant"] == "I-2" and i["org"] == str(w.org.id) and str(target.id) in i["key"]]
    assert ours == []                                                                      # before the round: «Оплачено до изменилось без записи в журнале» for the NEW membership


def datetime_now():
    from datetime import datetime

    return datetime.utcnow()


# ---- №11 / №18: the original schedule typed over two days; no warning of a missing validFrom while the pattern is counted ----------------------------------------------


def test_r3_11_monday_and_wednesday_typed_on_two_following_days_are_one_original_schedule(world, fixed_today):
    from tests.money1b_helpers import account, charge_of, set_schedule, slot

    w = world
    fixed_today(D(2026, 12, 2))
    settings_block(w, accounting_start_date="2026-09-01", billing_anchor="fixed_day", billing_day=1)
    give_group_price(w.db, w.group, 300_000)
    set_schedule(w, [slot("mon", 1, "2026-10-15"), slot("wed", 3, "2026-10-16"), slot("fri", 5, "2026-10-16")])         # the group opened on 15.10: the schedule was typed over two days
    stay(w, membership(w), D(2026, 10, 20))
    seg = [x for x in account(w, membership(w).id).segments if x.period_start == D(2026, 10, 1)][0]
    assert (seg.lessons_in_part, seg.lessons_in_period) == (5, 13) and seg.amount == 115_000                          # before the round: 5 of 10 = 150 000 (Wednesday and Friday counted from 16.10)
    set_schedule(w, [slot("mon", 1, "2026-10-15"), slot("wed", 3, "2026-10-17")])                                      # two days later: a NEW day of the week
    seg = [x for x in account(w, membership(w).id).segments if x.period_start == D(2026, 10, 1)][0]
    assert (seg.lessons_in_part, seg.lessons_in_period) == (3, 6) and seg.amount == 150_000                            # Mondays 5, 12, 19, 26 (original) + Wednesdays 21, 28 (from 17.10); his: Mon 26, Wed 21, 28


def test_r3_18_a_slot_without_valid_from_is_a_warning_in_the_real_schedule_only(caplog):
    import logging
    from types import SimpleNamespace

    from src.auth import schedule_rules

    group = SimpleNamespace(id="g1", schedule=[{"id": "a", "dayOfWeek": 1}], created_at=None)
    with caplog.at_level(logging.WARNING, logger="src.auth.schedule_rules"):
        with schedule_rules.ignoring_valid_from():
            schedule_rules.slot_valid_from(group, group.schedule[0])
        assert "schedule_slot_without_valid_from" not in caplog.text                       # the denominator is worked out at every run: no line in the log each time
        schedule_rules.slot_valid_from(group, group.schedule[0])
        assert "schedule_slot_without_valid_from" in caplog.text


# ---- money R-6: the period that a return made longer says so in the statement -------------------------------------------------------------------------------------


def test_r3_r6_the_statement_says_the_period_was_made_longer_by_the_days_he_was_out(world, fixed_today):
    from datetime import datetime

    from src.models.auth import StudentGroupPeriod

    w = world
    fixed_today(D(2026, 10, 20))
    settings_block(w, accounting_start_date="2026-09-01")
    give_group_price(w.db, w.group, 300_000, valid_from=D(2026, 1, 1))
    m = membership(w)
    stay(w, m, D(2026, 10, 5), D(2026, 10, 14))
    m.is_active, m.left_at, m.joined_at = True, None, datetime(2026, 10, 16, 6)
    w.db.add(StudentGroupPeriod(membership_id=m.id, joined_at=datetime(2026, 10, 16, 6)))
    w.db.commit()
    seed_payment(w, m, 300_000, D(2026, 10, 5))
    rows = owner(w).get(f"{API}/org/memberships/{m.id}/statement").json()["rows"]
    pauses = [r for r in rows if r["type"] == "pause"]
    assert [r["title"] for r in pauses] == ["Период продлён на 1 день: ученик возвращался в группу (15.10)"]
    assert [r for r in rows if r["type"] == "charge"][0]["title"] == "Стоимость 05.10–05.11"                 # the 32 calendar days of the month are explained by the row above


# ---- money R-5: «оплачено до: — → 24.10» for a student who never paid; the lost lessons of the packages are in the line over the list --------------------------------


def test_r3_r5_a_student_who_never_paid_has_a_dash_not_the_word_нет(world, fixed_today):
    from tests.test_money1b_price_pg import everybody_from, put_price

    w = world
    fixed_today(D(2026, 11, 15))
    settings_block(w, accounting_start_date="2026-09-01")
    everybody_from(w, D(2026, 11, 5))
    items = put_price(w, 300_000, dry_run=True)["impact"]["money"]["items"]
    assert items and all("оплачено до — → 04.11" in i["text"] and "нет" not in i["text"] for i in items if not i["has_money"])


def test_r3_r5_the_line_over_the_list_names_the_lessons_the_packages_lose(world, fixed_today):
    from tests.test_money1b_windows_pg import package_world, put_price

    w = world
    package_world(w, fixed_today)
    money = put_price(w, 700_000, valid_from=D(2026, 12, 1), dry_run=True)["impact"]["money"]
    assert money["lessons_lost_total"] == 2 and money["text"] == "Изменится у 1 ученика: оплаченных уроков станет меньше на 2"


# ---- №16: a list cut at the limit says so (the students with an individual price) ----------------------------------------------------------------------------------


def test_r3_16_the_individual_price_list_is_cut_with_a_flag_in_both_windows(world, fixed_today, monkeypatch):
    from src.auth import payments_service, pricing
    from tests.pg_world import give_price
    from tests.test_money1b_price_pg import everybody_from, put_price
    from tests.test_money1b_windows_pg import put_settings

    w = world
    fixed_today(D(2026, 11, 15))
    settings_block(w, accounting_start_date="2026-09-01")
    give_group_price(w.db, w.group, 300_000, valid_from=D(2026, 1, 1))
    everybody_from(w, D(2026, 11, 5))
    for student in w.students:
        give_price(w.db, membership(w, student), 250_000, reason="скидка")
    monkeypatch.setattr(pricing, "IMPACT_ITEMS", 2)
    money = put_price(w, 400_000, valid_from=D(2026, 12, 1), dry_run=True)["impact"]["money"]
    assert (len(money["override_students"]), money["override_total"], money["override_truncated"]) == (2, 3, True)         # before: three students, the third was silently not shown
    monkeypatch.setattr(payments_service.PaymentService, "REGIME_ITEMS", 2)
    impact = put_settings(w, period_mode="lessons", lessons_per_payment=12)["regime_impact"]
    assert (len(impact["override_students"]), impact["override_total"], impact["override_truncated"]) == (2, 3, True)


def test_r3_4_a_period_that_begins_on_the_day_of_the_price_is_in_the_account_of_that_day_not_a_second_time_in_the_next_period(world, fixed_today):
    from tests.test_money1b_price_pg import everybody_from, put_price

    w = world
    fixed_today(D(2026, 11, 20))
    settings_block(w, accounting_start_date="2026-09-01")
    give_group_price(w.db, w.group, 300_000, valid_from=D(2026, 1, 1))
    everybody_from(w, D(2026, 11, 5))
    for student in w.students:
        seed_payment(w, membership(w, student), 300_000, D(2026, 11, 5))
    money = put_price(w, 400_000, valid_from=D(2026, 12, 5), dry_run=True)["impact"]["money"]                   # the price begins on the first day of the next period
    one = money["items"][0]
    assert (one["debt_before"], one["debt_after"]) == (300_000, 400_000) and one["next_period"] is None             # the period of 05.12 is in the debt of 05.12 already
    assert one["text"] == f"{one['student_name']} (на 05.12): долг 300 000 сум → 400 000 сум"
