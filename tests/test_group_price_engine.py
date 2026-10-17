"""ROADMAP #105: the price of the group in the pure parts - the rule of the effective price, the payroll maths per
lesson date, the fingerprint shape, the migration rules.  No database."""
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from src.auth import payroll_engine as pe
from src.auth import pricing
from src.auth.payroll_service import LineSeed, fingerprint_of

D = date
IDENT = {
    "membership_id": "m1", "student_id": "s1", "student_name": "Каримова Алия", "display_id": "N13-001",
    "group_id": "g1", "group_name": "Группа А", "lesson_type": "group", "lesson_time": "10:00–11:00",
}


def priced(day, price, source="group", state=pe.COUNTED, divisor=12):
    return pe.Mark(D(2026, 9, day), f"s{day}", state, divisor, price, True, source)


def line(marks, *, source_fee=None, percent="50", **kwargs):
    return pe.compute_line(IDENT, marks, source_fee=source_fee, percent=Decimal(percent), **kwargs)


# ---- the rule of the effective price -------------------------------------------------------------------


def books(group_steps=(), override_rows=()):
    group_id, membership_id = uuid4(), uuid4()
    return group_id, membership_id, pricing.PriceBooks(
        {group_id: pricing.GroupBook(group_steps)},
        {membership_id: pricing.OverrideBook(override_rows)},
    )


def test_the_price_of_a_group_on_a_day_is_the_row_with_the_greatest_valid_from_not_after_it():
    g, m, b = books([(D(2026, 9, 1), 400_000), (D(2026, 9, 15), 500_000), (D(2026, 11, 1), 600_000)])
    assert b.price(m, g, D(2026, 8, 31)).source == pricing.SOURCE_NONE                    # before the first row: no price
    assert b.price(m, g, D(2026, 8, 31)).amount is None
    assert b.price(m, g, D(2026, 9, 1)).amount == 400_000                                 # valid_from is inclusive
    assert b.price(m, g, D(2026, 9, 14)).amount == 400_000
    assert b.price(m, g, D(2026, 9, 15)).amount == 500_000
    assert b.price(m, g, D(2030, 1, 1)).amount == 600_000
    got = b.price(m, g, D(2026, 9, 20))
    assert (got.source, got.valid_from, got.group_amount, got.reason) == ("group", D(2026, 9, 15), 500_000, None)


def test_an_override_wins_and_a_null_override_returns_the_group_price():
    g, m, b = books(
        [(D(2000, 1, 1), 500_000)],
        [(D(2026, 9, 10), 300_000, "скидка"), (D(2026, 10, 1), None, None)],
    )
    assert b.price(m, g, D(2026, 9, 9)).source == "group" and b.price(m, g, D(2026, 9, 9)).amount == 500_000
    on = b.price(m, g, D(2026, 9, 10))
    assert (on.amount, on.source, on.valid_from, on.group_amount, on.reason) == (300_000, "override", D(2026, 9, 10), 500_000, "скидка")
    assert b.price(m, g, D(2026, 9, 30)).amount == 300_000
    back = b.price(m, g, D(2026, 10, 1))                                                  # "the group price again"
    assert (back.amount, back.source, back.valid_from) == (500_000, "group", D(2000, 1, 1))


def test_an_override_of_zero_is_a_free_student_not_a_missing_price():
    g, m, b = books([(D(2000, 1, 1), 500_000)], [(D(2000, 1, 1), 0, "грант")])
    free = b.price(m, g, D(2026, 9, 1))
    assert free.amount == 0 and free.source == "override" and free.is_set


def test_an_override_without_a_group_price_still_prices_the_student():
    g, m, b = books([], [(D(2000, 1, 1), 250_000, "до введения")])
    got = b.price(m, g, D(2026, 9, 1))
    assert (got.amount, got.source, got.group_amount) == (250_000, "override", None)


def test_the_what_if_book_replaces_a_day_and_never_touches_the_original():
    book = pricing.GroupBook([(D(2000, 1, 1), 100), (D(2026, 9, 15), 200)])
    changed = book.with_step(D(2026, 9, 15), 300).with_step(D(2026, 9, 20), 400)
    assert changed.steps() == [(D(2000, 1, 1), 100), (D(2026, 9, 15), 300), (D(2026, 9, 20), 400)]
    assert book.steps() == [(D(2000, 1, 1), 100), (D(2026, 9, 15), 200)]


# ---- payroll: the price of EACH lesson ---------------------------------------------------------------------


def test_a_price_raised_from_the_15th_splits_the_month_by_lesson_date():
    marks = [priced(1, 400_000), priced(8, 400_000), priced(15, 500_000), priced(22, 500_000)]
    got = line(marks, source_fee=500_000)
    # 2 lessons at 400 000 + 2 at 500 000, 50 %, D = 12 - one rounding of the whole line
    assert got["amount"] == (2 * 400_000 + 2 * 500_000) * 50 // (100 * 12)
    assert got["amount"] != 4 * 500_000 * 50 // 1200 and got["amount"] != 4 * 400_000 * 50 // 1200
    assert got["monthly_fee"] == 500_000 and got["price_changed"] is True
    assert got["price_steps"] == [{"date": "2026-09-01", "amount": 400_000}, {"date": "2026-09-15", "amount": 500_000}]


def test_a_price_that_did_not_change_is_no_step():
    got = line([priced(1, 400_000), priced(8, 400_000)], source_fee=400_000)
    assert got["price_changed"] is False and got["price_steps"] == []


def test_a_lesson_before_the_first_price_is_not_priced_and_says_so():
    got = line([priced(1, None, "none"), priced(15, 500_000)], source_fee=500_000)
    assert got["amount"] == 500_000 * 50 // 1200 and got["no_price_lessons"] == 1
    assert pe.NO_FEE in [w["code"] for w in got["warnings"]]
    assert got["price_steps"][0] == {"date": "2026-09-01", "amount": None}
    assert pe.sheet_counters([got])["no_fee"] == 1


def test_the_owner_overwrite_on_the_sheet_replaces_the_price_of_every_lesson():
    marks = [priced(1, 400_000), priced(15, 500_000)]
    got = line(marks, source_fee=500_000, fee_override=300_000)
    assert got["amount"] == 2 * 300_000 * 50 // 1200 and got["fee_overridden"] and got["price_changed"] is False
    back = pe.recompute_line(got, percent=Decimal("50"), fee_override=None, reset_fee=True)
    assert back["amount"] == (400_000 + 500_000) * 50 // 1200 and back["price_changed"] is True


def test_a_free_lesson_is_not_unpaid_news_but_a_priced_one_is():
    marks = [priced(1, 0, state=pe.UNPAID), priced(8, 400_000, state=pe.UNPAID), priced(15, 400_000)]
    got = line(marks, source_fee=400_000)
    assert got["unpaid"] == 2 and got["unpaid_due"] == 1
    assert [w["message"] for w in got["warnings"] if w["code"] == pe.UNPAID_LESSONS] == ["Не оплачено уроков: 1"]
    assert pe.sheet_counters([got])["unpaid"] == 1
    ticked = line(marks, source_fee=400_000, include_unpaid=True)
    assert ticked["amount"] == (0 + 400_000 + 400_000) * 50 // 1200


def test_a_sheet_saved_before_the_group_price_is_recomputed_with_its_single_fee():
    """Marks without a price (a legacy line): ``source_fee`` stands for every lesson, exactly as before #105."""
    legacy = [pe.Mark(D(2026, 9, d), f"s{d}", pe.COUNTED, 12) for d in (1, 8, 15)]
    got = line(legacy, source_fee=450_000)
    assert got["amount"] == 3 * 450_000 * 50 // 1200 and "price" not in got["marks"][0]
    again = pe.recompute_line(got, percent=Decimal("60"), fee_override=None)
    assert again["amount"] == 3 * 450_000 * 60 // 1200 and again["monthly_fee"] == 450_000
    assert pe.sheet_counters([{k: v for k, v in again.items() if k not in ("unpaid_due", "no_price_lessons")}])["no_fee"] == 0


def test_marks_round_trip_through_the_saved_json():
    mark = priced(1, 400_000, "override")
    assert pe.Mark.from_dict(mark.as_dict()) == mark
    plain = pe.Mark(D(2026, 9, 1), "s1", pe.COUNTED, 12)
    assert "price" not in plain.as_dict() and pe.Mark.from_dict(plain.as_dict()).priced is False


# ---- the fingerprint ---------------------------------------------------------------------------------------------


def seed(marks, source_fee):
    return LineSeed(identity=dict(IDENT), marks=marks, source_fee=source_fee, pending_excused=0)


MONTH = D(2026, 9, 1)
TEACHER = uuid4()


def test_the_fingerprint_of_a_constant_price_is_the_old_one():
    """A sheet saved before #105 hashed ``[membership, group, kind, fee, pending, marks]``; a line whose price did not
    change must hash to the same value, or every saved sheet would turn "данные изменились" at the release."""
    import hashlib
    import json

    plain = [pe.Mark(D(2026, 9, 1), "s1", pe.COUNTED, 12)]
    old = hashlib.sha256(json.dumps(
        {"v": 1, "month": MONTH.isoformat(), "teacher": str(TEACHER), "lines": [
            ["m1", "g1", "group", 450_000, 0, [["2026-09-01", "s1", "counted", 12]]]]},
        sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()
    assert fingerprint_of(MONTH, TEACHER, [seed(plain, 450_000)]) == old
    # the same data with the price on the mark - the same hash
    assert fingerprint_of(MONTH, TEACHER, [seed([priced(1, 450_000)], 450_000)]) == old


def test_the_fingerprint_sees_a_price_that_changed_inside_the_month_and_one_that_moved():
    a = fingerprint_of(MONTH, TEACHER, [seed([priced(1, 400_000), priced(15, 500_000)], 500_000)])
    b = fingerprint_of(MONTH, TEACHER, [seed([priced(1, 400_000), priced(15, 600_000)], 600_000)])
    c = fingerprint_of(MONTH, TEACHER, [seed([priced(1, 450_000), priced(15, 500_000)], 500_000)])   # same last price, other first
    d = fingerprint_of(MONTH, TEACHER, [seed([priced(1, 400_000), priced(15, 500_000)], 500_000)])
    assert a == d and len({a, b, c}) == 3


# ---- the migration rules (pure) -----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def migration():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "b8c9d0e1f2a_group_prices.py"
    spec = importlib.util.spec_from_file_location("group_prices_migration_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_group_price_is_the_most_frequent_fee_then_the_later_then_the_smaller(migration):
    choose = migration.choose_group_price
    old, new = datetime(2026, 1, 1), datetime(2026, 6, 1)
    assert choose([(400, old), (500, old), (500, old)]) == (500, 2, False)                 # the most frequent
    assert choose([(400, old), (500, new)]) == (500, 1, True)                              # a tie: the later one
    assert choose([(400, new), (500, old)]) == (400, 1, True)
    assert choose([(500, None), (400, old)]) == (400, 1, True)                             # NULL date is the oldest
    assert choose([(500, old), (400, old)]) == (400, 1, True)                              # same date: the smaller sum
    assert choose([(500, old), (500, new), (400, new)]) == (500, 2, False)
    assert choose([(0, old), (0, old), (300, new)]) == (0, 2, False)                       # free is a price too
    assert choose([]) == (None, 0, False)


def row(group, fee, *, active=True, updated=None, member=None, org="ЦО", name="Группа"):
    return {"membership_id": member or uuid4(), "group_id": group, "org_id": uuid4(), "org_name": org,
            "group_name": name, "monthly_fee": fee, "fee_updated_at": updated, "active": active}


def test_the_plan_counts_followers_exceptions_and_the_members_that_start_to_follow(migration):
    g = uuid4()
    rows = [
        row(g, 500_000), row(g, 500_000), row(g, 450_000),                                 # price 500 000, one exception
        row(g, None),                                                                      # starts to follow
        row(g, 300_000, active=False),                                                     # inactive exception
        row(g, None, active=False), row(g, 500_000, active=False),
    ]
    (plan,) = migration.plan_backfill(rows)
    assert (plan.price, plan.votes, plan.tie) == (500_000, 2, False)
    assert (plan.memberships, plan.active) == (7, 4)
    assert (plan.follow_active, plan.follow_inactive) == (3, 2)                            # 2 + the NULL / the NULL + the equal
    assert (plan.null_active, plan.null_inactive) == (1, 1)
    assert sorted(fee for _, fee, _ in plan.overrides) == [300_000, 450_000] and plan.override_active == 1


def test_inactive_fees_never_vote_but_are_kept_as_exceptions(migration):
    g = uuid4()
    (plan,) = migration.plan_backfill([row(g, 400_000, active=False), row(g, 400_000, active=False), row(g, None)])
    assert plan.price is None and plan.votes == 0
    assert len(plan.overrides) == 2 and plan.follow_active == 0 and plan.null_active == 0   # a group without a price: nothing to follow


def test_a_group_without_any_fee_stays_without_a_price(migration):
    g = uuid4()
    (plan,) = migration.plan_backfill([row(g, None), row(g, None)])
    assert plan.price is None and plan.overrides == [] and plan.null_active == 0
