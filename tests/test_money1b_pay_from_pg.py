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
    assert got == {"today": "2026-10-21", "first_lesson": "2026-11-09", "default_pay_from": "2026-11-09", "text": "Оплата с 09.11.2026 — первое занятие группы с сегодня"}      # Friday 06.11 is cancelled: Monday 09.11


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


def test_b1_the_default_of_the_dialog_carries_the_ready_phrase_of_every_case(world, fixed_today):
    """LESSONS 104: the sentence under «Оплата с» is the server's, the front does not build it."""
    w = world
    opening_world(w, fixed_today, schedule=[])
    got = owner(w).get(f"{API}/org/groups/{w.group.id}/pay-from-default").json()
    assert got["first_lesson"] is None and got["text"] == "Оплата с 21.10.2026 — у группы нет занятий в ближайшие дни"
    opening_world(w, fixed_today)                                                       # Monday .. Friday lessons: the first one from 21.10
    got = owner(w).get(f"{API}/org/groups/{w.group.id}/pay-from-default").json()
    assert got["text"] == f"Оплата с {D.fromisoformat(got['default_pay_from']).strftime('%d.%m.%Y')} — первое занятие группы с сегодня"
    assert got["text"] == make_client(w, user=w.admin).get(f"{API}/org/groups/{w.group.id}/pay-from-default").json()["text"]       # a phrase without a sum: the same for the administrator


def test_b1_the_phrase_of_the_default_names_the_start_of_the_accounting_when_it_moves_the_day(world, fixed_today):
    w = world
    opening_world(w, fixed_today, schedule=[])
    settings_block(w, accounting_start_date="2026-10-30")
    got = owner(w).get(f"{API}/org/groups/{w.group.id}/pay-from-default").json()
    assert got["default_pay_from"] == "2026-10-30" and got["text"] == "Оплата с 30.10.2026 — начало учёта центра"


def _bulk(w, students, *, client=None, dry_run=True, **body):
    r = (client or owner(w)).post(f"{API}/org/groups/{w.group.id}/students/bulk", params={"mode": "add", **({"dry_run": "true"} if dry_run else {})},
                                  json={"student_ids": [str(x.id) for x in students], **body})
    assert r.status_code == 200, r.text
    return r.json()


def test_b1_the_preview_of_a_bulk_adding_says_what_is_charged_at_once_per_student_and_in_total(world, fixed_today):
    w = world
    opening_world(w, fixed_today, schedule=[slot("mon", 1, "2026-09-01"), slot("wed", 3, "2026-09-01"), slot("fri", 5, "2026-09-01")])
    a, b = fresh_student(w, "Первый"), fresh_student(w, "Второй")
    got = _bulk(w, [a, b], pay_from="2026-10-07")
    lessons = got["students"][0]["billing"]["immediate"]["lessons"]
    assert lessons > 0 and [x["student_id"] for x in got["students"]] == [str(a.id), str(b.id)]
    word = "уроков" if lessons % 10 not in (2, 3, 4) or lessons // 10 == 1 else "урока"
    one = f"Сразу начислится 300\u00a0000\u00a0сум (за {lessons} {word})"
    assert [x["billing"]["immediate"]["text"] for x in got["students"]] == [one, one]                      # the same phrase as the single adding
    assert got["immediate_total"] == {"amount": 600_000, "lessons": 2 * lessons, "students": 2,
                                      "text": f"Всего сразу начислится 600\u00a0000\u00a0сум (за {2 * lessons} {'уроков' if (2 * lessons) % 10 not in (2, 3, 4) or (2 * lessons) // 10 == 1 else 'урока'})"}
    assert w.db.query(StudentGroup).filter(StudentGroup.student_id.in_([a.id, b.id])).count() == 0          # a dry run writes nothing
    single = add(w, fresh_student(w, "Одиночный"), pay_from="2026-10-07", dry_run=True)
    assert single["billing"]["immediate"]["text"] == one                                                    # one dictionary for both windows
    for item in (*(x["billing"]["immediate"] for x in got["students"]), single["billing"]["immediate"]):        # Money-1 V0: the sum is told by its periods, the same numbers
        assert item["parts"] and sum(p["amount"] for p in item["parts"]) == item["amount"] == 300_000 and sum(p["lessons"] for p in item["parts"]) == item["lessons"]
    assert "parts" not in got["immediate_total"]                                                            # the total under a bulk adding has no breakdown


def test_b1_the_bulk_preview_has_no_sum_without_the_money_right_and_the_real_answer_has_none(world, fixed_today):
    w = world
    opening_world(w, fixed_today, schedule=[slot("mon", 1, "2026-09-01"), slot("wed", 3, "2026-09-01"), slot("fri", 5, "2026-09-01")])
    a, b, c = fresh_student(w, "Первый"), fresh_student(w, "Второй"), fresh_student(w, "Третий")
    admin = _bulk(w, [a], client=make_client(w, user=w.admin))                                              # an administrator without the money right: no pay_from, so the default day
    assert admin["immediate_total"] is None and all("immediate" not in (x["billing"] or {}) for x in admin["students"])
    assert "начислится" not in str(admin)
    real = _bulk(w, [b, c], dry_run=False, pay_from="2026-10-07")
    assert real["immediate_total"] is None and all("immediate" not in x["billing"] for x in real["students"]) and "начислится" not in str(real)
    empty = _bulk(w, [fresh_student(w, "Позже")], pay_from="2026-11-30")                                    # nothing is charged at once: no total line
    assert empty["immediate_total"] is None


# ---- G3M С2: an individual price given with a past «Оплата с» begins on that day (before: today - the period from «Оплата с» was charged by the group price) ----

def _preview_suggested(w, m):
    r = owner(w).get(f"{API}/org/memberships/{m.id}/payments/preview")
    assert r.status_code == 200, r.text
    return r.json()["suggested_amount"]


def _discount_world(w, fixed_today):
    opening_world(w, fixed_today)
    give_group_price(w.db, w.group, 500_000)            # the card of the group says 500 000, the student's own price is 300 000
    return fresh_student(w, "Со скидкой")


def test_g3m_c2_the_individual_price_of_an_adding_with_a_past_pay_from_prices_the_whole_period(world, fixed_today):
    from tests.money1b_helpers import add

    w = world
    s = _discount_world(w, fixed_today)
    body = {"pay_from": "2026-10-07", "price_override": {"amount": 300_000, "reason": "Скидка по договорённости"}}
    dry = add(w, s, dry_run=True, **body)
    assert dry["billing"]["immediate"]["amount"] == 300_000                         # before: 500 000
    add(w, s, **body)
    m, _ = stay_of(w, s)
    assert account(w, m.id, D(2026, 10, 21)).charged == 300_000
    assert _preview_suggested(w, m) == 300_000
    from src.models.auth import MembershipPriceOverride
    assert [(r.valid_from, r.amount) for r in w.db.query(MembershipPriceOverride).filter_by(membership_id=m.id).all()] == [(D(2026, 10, 7), 300_000)]


def test_g3m_c2_without_pay_from_the_individual_price_begins_today_as_before(world, fixed_today):
    from src.models.auth import MembershipPriceOverride
    from tests.money1b_helpers import add

    w = world
    s = _discount_world(w, fixed_today)
    add(w, s, price_override={"amount": 300_000, "reason": "Скидка по договорённости"})
    m, _ = stay_of(w, s)
    rows = w.db.query(MembershipPriceOverride).filter_by(membership_id=m.id).all()
    assert [(r.valid_from, r.amount) for r in rows] == [(D(2026, 10, 21), 300_000)]


def test_g3m_c2_the_old_rows_of_a_returning_student_stay_in_force_after_the_return(world, fixed_today):
    """A student who left (real leave) and comes back the day after: his own earlier price stays for the earlier stay, the new price begins on the day of the return (Р3-1: «Оплата с» is never inside the earlier stay)."""
    from src.models.auth import MembershipPriceOverride
    from tests.test_logicA_money_pg import leave

    w = world
    s = _discount_world(w, fixed_today)
    add(w, s, pay_from="2026-10-01", price_override={"amount": 300_000, "reason": "Скидка по договорённости"})
    m, _ = stay_of(w, s)
    leave(w, w.group, s)
    fixed_today(D(2026, 10, 22))
    add(w, s, pay_from="2026-10-22", price_override={"amount": 250_000, "reason": "Новая скидка при возврате"})
    w.db.expire_all()
    rows = sorted((r.valid_from, r.amount) for r in w.db.query(MembershipPriceOverride).filter_by(membership_id=m.id).all() if r.amount is not None)
    assert rows == [(D(2026, 10, 1), 300_000), (D(2026, 10, 22), 250_000)]


def test_g3m_c2_a_package_centre_prices_the_package_of_the_student_from_the_first_lesson_of_the_period(world, fixed_today):
    from src.auth import pricing
    from tests.money1b_helpers import add

    w = world
    s = _discount_world(w, fixed_today)
    settings_block(w, period_mode="lessons", lessons_per_payment=12)
    add(w, s, pay_from="2026-10-07", price_override={"amount": 300_000, "reason": "Скидка по договорённости"})
    m, _ = stay_of(w, s)
    price = pricing.load_books(w.db, [w.group.id], [m.id]).price(m.id, w.group.id, D(2026, 10, 7))
    assert (price.amount, price.source) == (300_000, "override")                    # before: the group price (500 000) up to the day of adding
    assert _preview_suggested(w, m) == 300_000


# ---- G3M Р3-1: «Оплата с» of a returning student is not before the day after the earlier stay ----

STUDY = [slot("mon", 1, "2026-09-01"), slot("wed", 3, "2026-09-01"), slot("fri", 5, "2026-09-01")]


def _left_on_20_10(w, fixed_today, *, mode_package=False):
    """The group costs 500 000; the student studied 01.09-20.10 by the group price and left; the clock now stands at 14.11."""
    from tests.test_logicA_money_pg import leave

    opening_world(w, fixed_today, today=D(2026, 10, 20), schedule=STUDY)
    give_group_price(w.db, w.group, 500_000)
    if mode_package:
        settings_block(w, period_mode="lessons", lessons_per_payment=12)
    s = fresh_student(w, "Вернувшийся")
    add(w, s, pay_from="2026-09-01")
    leave(w, w.group, s)
    fixed_today(D(2026, 11, 14))
    m, _ = stay_of(w, s)
    return s, m


def _past_stay_numbers(w, m):
    led = account(w, m.id, D(2026, 11, 14))
    return led.charged, led.debt, [(x.kind, x.period_start, x.period_end, x.amount) for x in led.segments]


def test_g3m_r31_a_return_with_pay_from_inside_the_earlier_stay_is_refused_and_the_old_stay_is_untouched(world, fixed_today):
    w = world
    s, m = _left_on_20_10(w, fixed_today)
    before = _past_stay_numbers(w, m)
    body = {"pay_from": "2026-10-10", "price_override": {"amount": 300_000, "reason": "Скидка по договорённости"}}
    for dry in (True, False):
        r = add(w, s, expect=422, dry_run=dry, **body)
        assert r["detail"]["code"] == "PAY_FROM_RANGE" and "не раньше 21.10.2026: до этого дня ученик уже учился в группе" in r["detail"]["message"]
    from src.models.auth import MembershipPriceOverride
    assert w.db.query(MembershipPriceOverride).filter_by(membership_id=m.id, amount=300_000).count() == 0       # nothing is written for a refusal
    assert _past_stay_numbers(w, m) == before                                                                     # the statement of the earlier stay: not a sum changed


def test_g3m_r31_pay_from_the_day_after_the_leave_is_allowed_the_discount_acts_from_it_the_old_stay_keeps_the_old_price(world, fixed_today):
    from src.models.auth import MembershipPriceOverride

    w = world
    s, m = _left_on_20_10(w, fixed_today)
    add(w, s, pay_from="2026-10-21", price_override={"amount": 300_000, "reason": "Скидка по договорённости"})
    w.db.expire_all()
    rows = sorted((r.valid_from, r.amount) for r in w.db.query(MembershipPriceOverride).filter_by(membership_id=m.id).all() if r.amount is not None)
    assert rows == [(D(2026, 10, 21), 300_000)]
    after = account(w, m.id, D(2026, 11, 14))
    cells = {x.period_start: x.amount for x in account(w, m.id, D(2026, 11, 14)).segments if x.kind == "period"}
    assert cells == {D(2026, 9, 1): 500_000, D(2026, 10, 1): 500_000, D(2026, 11, 1): 300_000}      # the days of the earlier stay: the old price; the discount from 21.10 prices the cell that begins after it


def test_g3m_r31_a_return_without_pay_from_gets_the_discount_from_today(world, fixed_today):
    from src.models.auth import MembershipPriceOverride

    w = world
    s, m = _left_on_20_10(w, fixed_today)
    add(w, s, price_override={"amount": 300_000, "reason": "Скидка по договорённости"})
    w.db.expire_all()
    assert [(r.valid_from, r.amount) for r in w.db.query(MembershipPriceOverride).filter_by(membership_id=m.id).all() if r.amount is not None] == [(D(2026, 11, 14), 300_000)]


def test_g3m_r31_a_new_student_with_a_past_pay_from_is_as_in_c2(world, fixed_today):
    from src.models.auth import MembershipPriceOverride

    w = world
    opening_world(w, fixed_today, today=D(2026, 10, 21), schedule=STUDY)
    give_group_price(w.db, w.group, 500_000)
    s = fresh_student(w, "Новый")
    add(w, s, pay_from="2026-10-07", price_override={"amount": 300_000, "reason": "Скидка по договорённости"})
    m, _ = stay_of(w, s)
    assert [(r.valid_from, r.amount) for r in w.db.query(MembershipPriceOverride).filter_by(membership_id=m.id).all()] == [(D(2026, 10, 7), 300_000)]


def test_g3m_r31_a_package_centre_refuses_the_same_way(world, fixed_today):
    w = world
    s, m = _left_on_20_10(w, fixed_today, mode_package=True)
    r = add(w, s, expect=422, pay_from="2026-10-10", price_override={"amount": 300_000, "reason": "Скидка по договорённости"})
    assert r["detail"]["code"] == "PAY_FROM_RANGE"
