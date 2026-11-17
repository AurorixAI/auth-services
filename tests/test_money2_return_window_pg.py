"""Money-2 stage 6 (М11Б, remark 2): the window of adding a student who COMES BACK to the same group tells the truth (PostgreSQL).

Calendar: Tuesday 17.11.2026, B = Tue/Thu/Sat, 450 000 a month, 12 lessons in November (Tue 3,10,17,24 + Thu 5,12,19,26 + Sat 7,14,21,28).
The student is written into B on Tue 17.11, marked present, leaves the same day: the stay charges his one lesson = 450 000 x 1/12 = 37 500 -> 38 000 (whole thousands) = a debt of 38 000.
He comes back on Wed 18.11: the lessons from 19.11 are Thu 19, Sat 21, Tue 24, Thu 26, Sat 28 = 5.  After the return the stay charges Tue 17 .. Sat 28 = 6 of 12 = 225 000.
What THIS adding brings: 225 000 - 38 000 = 187 000 for 5 lessons («ещё»); the debt after = 38 000 + 187 000 = 225 000."""
from datetime import date

from tests.money1b_helpers import add, fresh_student, hold_lessons
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_money2_sameday_pg import TUE, numbers, three
from tests.test_payments_service_pg import pay
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixture)

pytestmark = needs_pg

WED = date(2026, 11, 18)


def _left_with_a_lesson(w, fixed_today, mode="shared", paid=0):
    a, b, c = three(w, fixed_today, mode)
    s = w.students[1]
    add(w, s, group=b, mode="add")
    hold_lessons(w, s, [TUE], group=b)
    if paid:
        pay(w, s, b, amount=paid)
    assert owner(w).post(f"{API}/org/groups/{b.id}/students/{s.id}/leave", json={}).status_code == 200
    fixed_today(WED)
    return b, s


def _window(w, b, s, **kw):
    return add(w, s, group=b, mode="add", dry_run=True, **kw)


def test_the_returning_debtor_sees_his_old_debt_of_this_group_and_what_this_adding_brings(world, fixed_today):
    b, s = _left_with_a_lesson(world, fixed_today)
    assert numbers(world, s, b)[3] == 38_000                       # the debt of the first stay
    dry = _window(world, b, s)
    (row,) = dry["past_balances"]
    assert (row["group_id"], row["group_name"], row["kind"], row["amount"], row["left_on"]) == (str(b.id), b.name, "debt", 38_000, TUE.isoformat())
    assert row["membership_id"] == str(membership(world, s, b).id)
    assert row["text"] == "В этой группе с прошлого раза (ушёл 17.11.2026) остался долг 38\xa0000\xa0сум"
    real = add(world, s, group=b, mode="add")


def test_the_forgiven_debt_of_the_first_stay_is_not_shown_and_the_adding_brings_the_rest(world, fixed_today):
    b, s = _left_with_a_lesson(world, fixed_today)
    r = owner(world).post(f"{API}/org/memberships/{membership(world, s, b).id}/writeoffs", json={"all": True, "reason": "Договорённость"})
    assert r.status_code == 201, r.text
    dry = _window(world, b, s)
    assert dry["past_balances"] == []
    add(world, s, group=b, mode="add")
    assert numbers(world, s, b)[3] == 187_000                      # 225 000 charged - 38 000 forgiven


def test_an_advance_left_by_the_first_stay_is_named_as_an_advance(world, fixed_today):
    # paid 500 000, charged 38 000 -> advance 462 000 (what can be returned is the same number: nothing is spent yet)
    b, s = _left_with_a_lesson(world, fixed_today, paid=500_000)
    (row,) = _window(world, b, s)["past_balances"]
    assert (row["kind"], row["amount"]) == ("advance", 462_000)
    assert row["text"] == "В этой группе с прошлого раза (ушёл 17.11.2026) остался аванс 462\xa0000\xa0сум"


def test_personal_period_the_adding_brings_the_rest_of_the_period(world, fixed_today):
    # «Личный период»: the period of the first payment day, 17.11 - 17.12 = 450 000; the first stay charged 15 000 for its one day (450 000 x 1/30 = 15 000); the return charges the whole period: 450 000 - 15 000 = 435 000
    b, s = _left_with_a_lesson(world, fixed_today, mode="personal")
    assert numbers(world, s, b)[0] == 15_000
    dry = _window(world, b, s)
    (row,) = dry["past_balances"]
    assert (row["kind"], row["amount"]) == ("debt", 15_000)
    add(world, s, group=b, mode="add")
    assert numbers(world, s, b)[3] == 450_000


def test_without_the_money_right_the_row_has_no_sum_and_nothing_is_charged_at_once(world, fixed_today):
    from tests.pg_world import make_client

    b, s = _left_with_a_lesson(world, fixed_today)
    admin = make_client(world, user=world.admin, org_role="admin")
    dry = add(world, s, client=admin, group=b, mode="add", dry_run=True)
    (row,) = dry["past_balances"]
    assert "amount" not in row and row["kind"] == "debt" and row["text"] == "В этой группе с прошлого раза (ушёл 17.11.2026) остался долг"


def test_a_new_student_gets_the_answers_as_before(world, fixed_today):
    a, b, c = three(world, fixed_today)
    s = fresh_student(world, "Совсем новый")
    dry = add(world, s, group=b, mode="add", dry_run=True)
    assert dry["past_balances"] == []


def test_the_bulk_window_says_the_same_for_the_returning_student(world, fixed_today):
    b, s = _left_with_a_lesson(world, fixed_today)
    r = owner(world).post(f"{API}/org/groups/{b.id}/students/bulk", params={"mode": "add", "dry_run": "true"}, json={"student_ids": [str(s.id)]})
    assert r.status_code == 200, r.text
    (item,) = r.json()["students"]
    (row,) = item["past_balances"]
    assert (row["kind"], row["amount"]) == ("debt", 38_000)
