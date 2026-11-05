"""Money-1 pack B1 / ДЫРА-03 (PostgreSQL): «Оплата с» - the day a student who is added to a group is charged from.

The complaint of the partner: written down on 21.10 for a group that starts on 05.11, a student of «свой месяц» owed 300 000 from 21.10 (period 21.10-20.11, 7 lessons of 13) while the screen said «с
первого занятия».  Now the default is the first lesson of the group from today; the owner can choose another day (money right only); a transfer has its own day."""
from datetime import date

from src.models.auth import StudentGroup, StudentGroupPeriod

from tests.money1b_helpers import OPENS, account, add, fresh_student, opening_world, set_schedule, slot
from tests.pg_world import give_group_price, make_client, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, settings_block, stay  # noqa: F401  (fixtures)

pytestmark = needs_pg
D = date


def stay_of(w, student):
    w.db.expire_all()
    (m,) = w.db.query(StudentGroup).filter_by(student_id=student.id, group_id=w.group.id).all()
    return m, m.periods[-1]


def test_b1_d3_written_down_on_21_10_for_a_group_that_starts_on_06_11_he_owes_nothing_before_the_first_lesson(world, fixed_today):
    w = world
    opening_world(w, fixed_today)
    s = fresh_student(w)
    done = add(w, s, client=make_client(w, user=w.admin))               # an administrator WITHOUT the money right: the default is not money
    assert done["billing"]["pay_from"] == "2026-11-06" and done["billing"]["explicit"] is False and done["billing"]["first_lesson"] == "2026-11-06"
    assert done["billing"]["text"] == "Оплата считается с 06.11.2026 — с первого занятия группы"
    m, period = stay_of(w, s)
    assert period.billing_from == D(2026, 11, 6)                         # the roster still follows the day he joined (the moment is naive UTC: 07:00 UTC of 21.10)
    assert (period.joined_at.year, period.joined_at.month, period.joined_at.day) == (2026, 10, 21)
    before = account(w, m.id, D(2026, 10, 21))
    assert before.charged == 0 and before.debt == 0                      # before: 300 000 from 21.10 for the period 21.10-20.11
    on_first = account(w, m.id, D(2026, 11, 6))
    (seg,) = [x for x in on_first.segments if x.kind == "period"]
    assert (seg.period_start, seg.period_end, seg.amount) == (D(2026, 11, 6), D(2026, 12, 5), 300_000) and on_first.debt == 300_000     # the month he really studies: 06.11 - 05.12


def test_b1_a_chosen_day_is_the_money_right_and_is_written_as_chosen(world, fixed_today):
    w = world
    opening_world(w, fixed_today)
    s = fresh_student(w)
    denied = add(w, s, client=make_client(w, user=w.admin), expect=403, pay_from="2026-10-25")
    assert w.db.query(StudentGroup).filter_by(student_id=s.id).count() == 0, denied                       # nothing is written for a refusal
    done = add(w, s, pay_from="2026-10-25")
    assert done["billing"] == {**done["billing"], "pay_from": "2026-10-25", "explicit": True, "default_pay_from": "2026-11-06"}
    m, period = stay_of(w, s)
    assert period.billing_from == D(2026, 10, 25)
    assert account(w, m.id, D(2026, 10, 26)).charged == 300_000


def test_b1_the_chosen_day_is_in_the_range_of_the_accounting_and_two_months_ahead(world, fixed_today):
    w = world
    opening_world(w, fixed_today)
    s = fresh_student(w)
    for bad in ("2026-08-31", "2027-01-01"):
        r = add(w, s, expect=422, pay_from=bad)
        assert r["detail"]["code"] == "PAY_FROM_RANGE" and "не раньше начала учёта центра (01.09.2026)" in r["detail"]["message"]
    assert w.db.query(StudentGroup).filter_by(student_id=s.id).count() == 0
    add(w, s, pay_from="2026-09-01")                                     # the first day of the accounting is allowed


def test_b1_a_lesson_today_means_from_today_and_nothing_is_stored(world, fixed_today):
    w = world
    opening_world(w, fixed_today, today=D(2026, 11, 6))                   # Friday 06.11: the first lesson IS today
    s = fresh_student(w)
    done = add(w, s)
    assert done["billing"]["pay_from"] == "2026-11-06" and done["billing"]["text"] == "Оплата считается с 06.11.2026 — с первого занятия группы"
    _, period = stay_of(w, s)
    assert period.billing_from is None                                    # the same day he joined: the stay is what it was before the pack


def test_b1_a_group_without_a_lesson_in_sight_charges_from_today(world, fixed_today):
    w = world
    opening_world(w, fixed_today, schedule=[])
    s = fresh_student(w)
    done = add(w, s)
    assert done["billing"]["pay_from"] == "2026-10-21" and done["billing"]["first_lesson"] is None
    assert done["billing"]["text"] == "Оплата считается с 21.10.2026: у группы нет занятий в ближайшие дни"


def test_b1_a_lesson_cancelled_by_the_centre_is_not_the_first_lesson(world, fixed_today):
    from src.models.auth import GroupAttendance

    w = world
    opening_world(w, fixed_today)
    w.db.add(GroupAttendance(org_id=w.org.id, group_id=w.group.id, lesson_date="2026-11-06", lesson_id="fri", status="cancelled", cancellation_kind="centre", records={}))
    w.db.commit()
    got = owner(w).get(f"{API}/org/groups/{w.group.id}/pay-from-default").json()
    assert got == {"today": "2026-10-21", "first_lesson": "2026-11-09", "default_pay_from": "2026-11-09"}      # Friday 06.11 is cancelled: Monday 09.11


def test_b1_the_default_of_the_dialog_is_readable_by_an_administrator_and_not_across_centres(world, fixed_today):
    w = world
    opening_world(w, fixed_today)
    assert make_client(w, user=w.admin).get(f"{API}/org/groups/{w.group.id}/pay-from-default").status_code == 200
    assert make_client(w, user=w.foreign_admin, org="other").get(f"{API}/org/groups/{w.group.id}/pay-from-default").status_code == 404
    assert make_client(w, user=w.teacher).get(f"{API}/org/groups/{w.group.id}/pay-from-default").status_code == 403


def test_b1_dry_run_says_the_day_and_writes_nothing(world, fixed_today):
    w = world
    opening_world(w, fixed_today)
    s = fresh_student(w)
    done = add(w, s, dry_run=True)
    assert done["dry_run"] is True and done["billing"]["pay_from"] == "2026-11-06"
    assert w.db.query(StudentGroup).filter_by(student_id=s.id).count() == 0


def test_b1_a_transfer_has_its_own_day_and_refuses_a_chosen_one(world, fixed_today):
    from tests.pg_world import make_group

    w = world
    opening_world(w, fixed_today)
    other = make_group(w.db, w.org, w.teacher, "Другая")
    give_group_price(w.db, other, 300_000)
    s = w.students[0]
    r = add(w, s, mode="transfer", group=other, expect=422, pay_from="2026-11-01")
    assert r["detail"]["code"] == "PAY_FROM_NOT_APPLICABLE"
    w.db.expire_all()
    assert w.db.query(StudentGroup).filter_by(student_id=s.id, group_id=w.group.id, is_active=True).count() == 1          # nothing was closed for a refusal
    done = add(w, s, mode="transfer", group=other)
    assert done["billing"] is None                                       # the new group is charged from the day after the transfer (first_billable_floor), as before


def test_b1_a_student_who_comes_back_to_the_group_gets_the_day_of_the_new_stay(world, fixed_today):
    from src.auth.leave_service import LeaveService

    w = world
    opening_world(w, fixed_today)
    s = fresh_student(w)
    add(w, s, pay_from="2026-10-25")
    fixed_today(D(2026, 10, 28))
    LeaveService(w.db).leave(str(w.org.id), str(w.group.id), str(s.id), str(w.owner.id), "owner", None, money=True)
    w.db.expire_all()
    fixed_today(D(2026, 10, 30))
    done = add(w, s)
    assert done["billing"]["pay_from"] == "2026-11-06"
    m = w.db.query(StudentGroup).filter_by(student_id=s.id, group_id=w.group.id).one()
    periods = sorted(w.db.query(StudentGroupPeriod).filter_by(membership_id=m.id).all(), key=lambda p: p.joined_at)
    assert [p.billing_from for p in periods] == [D(2026, 10, 25), D(2026, 11, 6)]


def test_b1_bulk_add_takes_the_default_for_everybody_and_a_chosen_day_is_the_money_right(world, fixed_today):
    w = world
    opening_world(w, fixed_today)
    a, b = fresh_student(w, "Первый"), fresh_student(w, "Второй")
    url = f"{API}/org/groups/{w.group.id}/students/bulk"
    done = make_client(w, user=w.admin).post(url, params={"mode": "add"}, json={"student_ids": [str(a.id)]})
    assert done.status_code == 200 and done.json()["added"] == 1
    assert stay_of(w, a)[1].billing_from == D(2026, 11, 6)
    assert make_client(w, user=w.admin).post(url, params={"mode": "add"}, json={"student_ids": [str(b.id)], "pay_from": "2026-10-25"}).status_code == 403
    both = owner(w).post(url, params={"mode": "add"}, json={"student_ids": [str(b.id)], "pay_from": "2026-10-25"})
    assert both.status_code == 200 and stay_of(w, b)[1].billing_from == D(2026, 10, 25)


def test_b5_in_the_real_flow_taken_out_on_14_10_and_back_on_16_10_is_one_period(world, fixed_today):
    """B2 of the devil (d8) through the services: «Убрать из группы» and «Добавить в группу» two days later - the period he left in goes on, 300 000, not 97 000 + 300 000."""
    from src.auth.leave_service import LeaveService
    from tests.pg_world import make_group
    from tests.test_pay2_api_pg import seed_payment

    w = world
    fixed_today(D(2026, 10, 5))
    settings_block(w, accounting_start_date="2026-09-01")
    give_group_price(w.db, w.group, 300_000)
    s = fresh_student(w, "Вернувшийся")
    add(w, s)                                                                       # the test group meets every day: the first lesson is today, he is charged from 05.10
    m, _ = stay_of(w, s)
    seed_payment(w, m, 300_000, D(2026, 10, 5))
    fixed_today(D(2026, 10, 14))
    LeaveService(w.db).leave(str(w.org.id), str(w.group.id), str(s.id), str(w.owner.id), "owner", None, money=True)
    w.db.expire_all()
    assert account(w, m.id, D(2026, 10, 14)).charged == 97_000                     # 10 of 31 days: he is out
    fixed_today(D(2026, 10, 16))
    add(w, s)
    led = account(w, m.id, D(2026, 10, 20))
    assert led.charged == 300_000 and led.debt == 0 and led.reconciles              # before the pack: 397 000 and a debt of 97 000 for a second full period from 16.10
    assert len([x for x in led.segments if x.kind == "period"]) == 1
