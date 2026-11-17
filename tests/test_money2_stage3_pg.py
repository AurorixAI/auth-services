"""Money-2 stage fix 3 (Ф1-Ф3): the one-day stay rule, the last touches (PostgreSQL).

Calendar: Tuesday 17.11.2026.  B = Tue/Thu/Sat, 450 000 a month, 12 lessons in November (37 500 -> 38 000 a lesson).
B from 02.11 to 10.11 (the old stay): lessons 03, 05, 07, 10 = 4 x 37 500 = 150 000."""
from datetime import date

from tests.money1b_helpers import add, fresh_student
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_money2_sameday_pg import numbers, three
from tests.test_money2_stage2fix_pg import forgive, leave, window, writeoffs_of
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, stay  # noqa: F401  (fixture)

pytestmark = needs_pg


def old_debtor(world, fixed_today):
    """In B 02.11 - 10.11 with a debt, back in B today (a new stay of today).  Returns (student, group, membership, the old debt)."""
    a, b, c = three(world, fixed_today)
    s = fresh_student(world, "Вернулся с долгом")
    add(world, s, group=b, mode="add")
    m = membership(world, s, b)
    stay(world, m, date(2026, 11, 2), date(2026, 11, 10))
    debt = numbers(world, s, b)[3]
    assert debt == 150_000, debt                                     # 4 lessons x 37 500
    add(world, s, group=b, mode="add")
    return s, b, m, debt


def auto_lines(world):
    from src.models.auth import GroupAudit

    return [l for l in world.db.query(GroupAudit).filter(GroupAudit.org_id == world.org.id, GroupAudit.action == "payment_writeoff_void").all() if (l.snapshot or {}).get("auto")]


def test_f1_a_forgiveness_of_the_real_old_debt_of_a_returned_student_is_not_cancelled_by_the_closing_of_the_empty_stay(world, fixed_today):
    # the old debt 150 000 is forgiven today (the new stay of today charges its own 38 000 on top, so the forgiveness is allowed); the new stay is taken out without a mark: it is of no day,
    # the real debt 150 000 stays and the forgiveness of 150 000 covers it -> debt 0, the forgiveness LIVE, no automatic cancel in the journal.  The window says the same as the record.
    s, b, m, debt = old_debtor(world, fixed_today)
    forgive(world, s, b, amount=debt)
    row = window(world, s, b)
    assert row["money"]["debt"] == 0 and row["money"]["advance"] == 0, row
    leave(world, s, b)
    assert [r.voided_at is None for r in writeoffs_of(world, m)] == [True]
    n = numbers(world, s, b)
    assert (n[0], n[2], n[3]) == (150_000, 0, 0), n                    # charged 150 000 (the old stay), no advance, no debt
    assert auto_lines(world) == []


def test_f1_partial_only_the_forgiveness_that_would_stand_as_an_advance_is_cancelled_the_latest_first(world, fixed_today):
    # forgiven 150 000 (the old debt) and then 5 000 more (covers today's lesson of the open stay); the stay of today is taken out: the real debt is 150 000 -> the LATEST forgiveness (5 000) would be an advance
    # and is cancelled, the 150 000 one stays (without it the debt would be positive).
    s, b, m, debt = old_debtor(world, fixed_today)
    _, first = forgive(world, s, b, amount=debt)
    _, second = forgive(world, s, b, amount=5_000)
    row = window(world, s, b)
    assert row["money"]["debt"] == 0 and row["money"]["advance"] == 0, row
    leave(world, s, b)
    alive = {str(r.id): r.voided_at is None for r in writeoffs_of(world, m)}
    assert alive == {first: True, second: False}, alive
    n = numbers(world, s, b)
    assert (n[2], n[3]) == (0, 0), n
    assert [l.snapshot["writeoff_id"] for l in auto_lines(world)] == [second]


def test_f1_a_forgiveness_given_to_the_empty_stay_with_no_old_debt_is_still_cancelled(world, fixed_today):
    # recorded -> forgiven 38 000 -> taken out without a mark: nothing is owed without it -> cancelled (as before)
    a, b, c = three(world, fixed_today)
    s = fresh_student(world, "Простили")
    add(world, s, group=b, mode="add")
    m, wid = forgive(world, s, b)
    assert window(world, s, b)["money"]["debt"] == 0
    leave(world, s, b)
    (r,) = writeoffs_of(world, m)
    assert r.voided_at is not None
    assert [l.snapshot["writeoff_id"] for l in auto_lines(world)] == [wid]
    assert numbers(world, s, b)[2:4] == (0, 0)
