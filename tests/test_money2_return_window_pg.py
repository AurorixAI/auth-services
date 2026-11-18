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
    assert row["text"] == "Уже был в этой группе, ушёл 17.11.2026 — остался долг 38\xa0000\xa0сум"
    imm = dry["billing"]["immediate"]
    assert (imm["amount"], imm["lessons"]) == (187_000, 5) and imm["text"] == "К оплате сразу: 187\xa0000\xa0сум (за 5 уроков)"
    assert sum(p["amount"] for p in imm["parts"]) == 187_000 and sum(p["lessons"] for p in imm["parts"]) == 5
    assert "ещё" not in imm["text"] and "начислится" not in imm["text"] and imm["amount"] == 225_000 - 38_000      # stage 10: no «ещё»; the sum is the difference
    real = add(world, s, group=b, mode="add")
    assert real["past_balances"] == [row] and "immediate" not in real["billing"]      # the answer says the same row; no sum in the real answer (as before)
    assert numbers(world, s, b)[3] == 225_000 == 38_000 + imm["amount"]                 # the window = the adding


def test_the_forgiven_debt_of_the_first_stay_is_not_shown_and_the_adding_brings_the_rest(world, fixed_today):
    b, s = _left_with_a_lesson(world, fixed_today)
    r = owner(world).post(f"{API}/org/memberships/{membership(world, s, b).id}/writeoffs", json={"all": True, "reason": "Договорённость"})
    assert r.status_code == 201, r.text
    dry = _window(world, b, s)
    assert dry["past_balances"] == []
    imm = dry["billing"]["immediate"]
    assert (imm["amount"], imm["lessons"], imm["text"]) == (187_000, 5, "К оплате сразу: 187\xa0000\xa0сум (за 5 уроков)")
    add(world, s, group=b, mode="add")
    assert numbers(world, s, b)[3] == 187_000                      # 225 000 charged - 38 000 forgiven


def test_an_advance_left_by_the_first_stay_is_named_as_an_advance(world, fixed_today):
    # paid 500 000, charged 38 000 -> advance 462 000 (what can be returned is the same number: nothing is spent yet)
    b, s = _left_with_a_lesson(world, fixed_today, paid=500_000)
    (row,) = _window(world, b, s)["past_balances"]
    assert (row["kind"], row["amount"]) == ("advance", 462_000)
    assert row["text"] == "Уже был в этой группе, ушёл 17.11.2026 — остался аванс 462\xa0000\xa0сум"


def test_personal_period_the_adding_brings_the_rest_of_the_period(world, fixed_today):
    # «Личный период»: the period of the first payment day, 17.11 - 17.12 = 450 000; the first stay charged 15 000 for its one day (450 000 x 1/30 = 15 000); the return charges the whole period: 450 000 - 15 000 = 435 000
    b, s = _left_with_a_lesson(world, fixed_today, mode="personal")
    assert numbers(world, s, b)[0] == 15_000
    dry = _window(world, b, s)
    imm = dry["billing"]["immediate"]
    assert imm["amount"] == 435_000 and imm["text"].startswith("К оплате сразу: 435\xa0000\xa0сум (за ")
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
    assert "amount" not in row and row["kind"] == "debt" and row["text"] == "Уже был в этой группе, ушёл 17.11.2026 — остался долг"
    assert "immediate" not in dry["billing"]


def test_a_new_student_gets_the_answers_as_before(world, fixed_today):
    a, b, c = three(world, fixed_today)
    s = fresh_student(world, "Совсем новый")
    dry = add(world, s, group=b, mode="add", dry_run=True)
    assert dry["past_balances"] == []
    imm = dry["billing"]["immediate"]
    assert (imm["amount"], imm["lessons"], imm["text"]) == (225_000, 6, "К оплате сразу: 225\xa0000\xa0сум (за 6 уроков)")
    assert "ещё" not in imm["text"]


def test_the_bulk_window_says_the_same_for_the_returning_student(world, fixed_today):
    b, s = _left_with_a_lesson(world, fixed_today)
    r = owner(world).post(f"{API}/org/groups/{b.id}/students/bulk", params={"mode": "add", "dry_run": "true"}, json={"student_ids": [str(s.id)]})
    assert r.status_code == 200, r.text
    (item,) = r.json()["students"]
    assert item["billing"]["immediate"]["text"] == "К оплате сразу: 187\xa0000\xa0сум (за 5 уроков)"
    (row,) = item["past_balances"]
    assert (row["kind"], row["amount"]) == ("debt", 38_000)
    assert r.json()["immediate_total"]["amount"] == 187_000


def test_n1_two_versions_of_the_rules_in_one_month_the_window_gives_one_part_that_adds_up(world, fixed_today):
    """Stage 7 Н1 through the API: the rules of the centre got a new version (same mode) on the day of the return, so November has TWO cells of the ledger with one start (the closed stay under v1, the return under v2).
    The window of the return said the sum 188 000 but a part 150 000 (the old cell taken off twice).  Now: ONE part, the sum of the parts = the sum."""
    from tests.test_pay2_api_pg import settings_block

    b, s = _left_with_a_lesson(world, fixed_today)
    base = {"period_mode": "calendar", "billing_anchor": "fixed_day", "billing_day": 1}
    settings_block(world, billing_history=[{"from": "min", **base}, {"from": WED.isoformat(), **base}])
    imm = _window(world, b, s, pay_from=WED.isoformat())["billing"]["immediate"]      # the return starts charging today: both cells of November have begun
    assert (imm["amount"], imm["lessons"]) == (188_000, 5)                               # 226 000 after - 38 000 before
    assert [p["amount"] for p in imm["parts"]] == [188_000] and [p["lessons"] for p in imm["parts"]] == [5]


# ---- stage 6, step 4: the parts are matched by the START of the period, not by its name (no database: the ledger is a stand-in) -------------------------------------------------------------

def _seg(start, end, amount, days, *, basis="lessons", in_part=None, in_period=None):
    from types import SimpleNamespace

    return SimpleNamespace(kind="period", begun=True, amount=amount, period_start=start, period_end=end, basis=basis, days=days, lessons_in_part=in_part, lessons_in_period=in_period)


def _days(first, last):
    from datetime import timedelta

    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _group_days(first, last):
    return [d for d in _days(first, last) if d.isoweekday() in (2, 4, 6)]            # Tue / Thu / Sat


def test_the_stay_of_a_year_ago_with_the_same_month_name_is_not_taken_off_twice():
    """«Общий месяц» 450 000: November 2025 was charged in full (450 000, 13 lessons of Tue/Thu/Sat: 4 Tue + 4 Thu + 5 Sat = 13: 04.11 .. 29.11), he left on 30.11.2025.  He returns on 18.11.2026: November 2026 from
    19.11 is Thu 19, Sat 21, Tue 24, Thu 26, Sat 28 = 5 lessons of 12 = 450 000 x 5/12 = 187 500 -> 188 000.  Before = 450 000 (November 2025); after = 450 000 + 188 000.  The difference = 188 000 for 5 lessons, ONE part
    (November 2026); the old November must not eat the new one (they have the same name «ноябрь»)."""
    from src.auth.billing_start import immediate_from_ledger

    nov25 = _seg(date(2025, 11, 1), date(2025, 11, 30), 450_000, _days(date(2025, 11, 1), date(2025, 11, 30)), basis="full")
    nov26 = _seg(date(2026, 11, 1), date(2026, 11, 30), 188_000, _days(date(2026, 11, 19), date(2026, 11, 30)), in_part=5, in_period=12)

    class Ledger:
        def __init__(self, *segments):
            self.segments = list(segments)

    lesson_days = _group_days
    before = immediate_from_ledger(Ledger(nov25), lesson_days)
    assert before["amount"] == 450_000 and before["parts"][0]["period_start"] == "2025-11-01"
    after = immediate_from_ledger(Ledger(nov25, nov26), lesson_days, before)
    assert (after["amount"], after["lessons"]) == (188_000, 5)
    assert [p["period_start"] for p in after["parts"]] == ["2026-11-01"]
    assert sum(p["amount"] for p in after["parts"]) == after["amount"] and sum(p["lessons"] for p in after["parts"]) == after["lessons"]
    assert all(p["lessons"] >= 0 for p in after["parts"])
    assert [(p["from"], p["whole"]) for p in after["parts"]] == [("2026-11-19", False)]      # no part before for this period: «from» stays as it was


def test_the_part_of_a_period_reduced_by_the_return_has_no_from_and_is_not_whole():
    """Stage 8 (М12): a period charged before is reduced by the part before: the date «from» of the whole stay does not describe the difference (it argued with «Оплата с»), the part is not whole."""
    from src.auth.billing_start import immediate_from_ledger

    class Ledger:
        def __init__(self, *segments):
            self.segments = list(segments)

    before_seg = _seg(date(2026, 11, 1), date(2026, 11, 30), 262_500, _days(date(2026, 11, 17), date(2026, 11, 30)), in_part=7, in_period=12)
    after_seg = _seg(date(2026, 11, 1), date(2026, 11, 30), 450_000, _days(date(2026, 11, 17), date(2026, 11, 30)), in_part=12, in_period=12)
    before = immediate_from_ledger(Ledger(before_seg), _group_days)
    assert before["parts"][0]["from"] == "2026-11-17"
    after = immediate_from_ledger(Ledger(after_seg), _group_days, before)
    assert after["amount"] == 187_500 and len(after["parts"]) == 1
    assert (after["parts"][0]["from"], after["parts"][0]["whole"]) == (None, False)


def test_a_period_charged_less_after_the_return_keeps_the_parts_adding_up():
    """The price of the return is lower than the price of the first stay (an individual price ended): the period is charged 450 000 now and was 500 000 -> the part is -50 000, the new period 188 000: the total 138 000 is
    the sum of the parts, the lessons never go below zero.  There is no «rest goes on the last part»: with the start as the key every period of the first stay is in the account after the return (a charge of a closed stay
    never disappears), so the parts always sum up by themselves."""
    from src.auth.billing_start import immediate_from_ledger

    class Ledger:
        def __init__(self, *segments):
            self.segments = list(segments)

    old_before = _seg(date(2026, 10, 1), date(2026, 10, 31), 500_000, _days(date(2026, 10, 1), date(2026, 10, 31)), basis="full")
    old_after = _seg(date(2026, 10, 1), date(2026, 10, 31), 450_000, _days(date(2026, 10, 1), date(2026, 10, 31)), basis="full")
    new = _seg(date(2026, 11, 1), date(2026, 11, 30), 188_000, _days(date(2026, 11, 19), date(2026, 11, 30)), in_part=5, in_period=12)
    before = immediate_from_ledger(Ledger(old_before), _group_days)
    after = immediate_from_ledger(Ledger(old_after, new), _group_days, before)
    assert after["amount"] == 138_000
    assert after["parts"] == []         # stage 7 Н1 lock: a part of -50 000 is not a part - no breakdown, the sum and the text stay
    assert after["lessons"] >= 0


# ---- stage 6, step 5: nothing is counted that is thrown away ------------------------------------------------------------------------------------------------------------------------------

def test_a_return_on_the_day_of_leaving_does_not_read_the_balance_of_the_closed_stay(world, fixed_today, monkeypatch):
    """The leaving of the same day is cancelled: the row «with the last time» is thrown away, so it is not counted (a spy on ``returned_balance``: 0 calls); an ordinary return the next day reads it ONCE."""
    from src.auth import leave_effects

    calls = []
    real = leave_effects.returned_balance
    monkeypatch.setattr(leave_effects, "returned_balance", lambda *a, **k: calls.append(1) or real(*a, **k))
    a, b, c = three(world, fixed_today)
    s = world.students[1]
    add(world, s, group=b, mode="add")
    hold_lessons(world, s, [TUE], group=b)
    assert owner(world).post(f"{API}/org/groups/{b.id}/students/{s.id}/leave", json={}).status_code == 200
    out = add(world, s, group=b, mode="add")                                              # the same day: the leaving is cancelled
    assert out["leave_undo"]["undone"] is True and out["past_balances"] == [] and calls == []
    assert owner(world).post(f"{API}/org/groups/{b.id}/students/{s.id}/leave", json={}).status_code == 200
    fixed_today(WED)
    add(world, s, group=b, mode="add", dry_run=True)                                       # the next day: an ordinary return
    assert calls == [1]
