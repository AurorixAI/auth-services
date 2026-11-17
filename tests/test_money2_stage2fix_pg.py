"""Money-2 stage fix 2 (У1-У5): the one-day stay rule at every reader (PostgreSQL).

Calendar: Tuesday 17.11.2026.  A = Mon/Wed/Fri (no lesson today), B = Tue/Thu/Sat (lesson today), C = Tue/Fri (lesson today).
November 2026: B = Tue 3,10,17,24 + Thu 5,12,19,26 + Sat 7,14,21,28 = 12 lessons; C = Tue 3,10,17,24 + Fri 6,13,20,27 = 8 lessons.
C from today: Tue 17, 24 + Fri 20, 27 = 4 of 8 = 250 000 of 500 000; from tomorrow: 24, 20, 27 = 3 of 8 = 187 500 -> 188 000 (whole thousands)."""
from datetime import date

import pytest

from tests.money1b_helpers import add, fresh_student, hold_lessons
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_money2_sameday_pg import TUE, numbers, settle, three
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, stay  # noqa: F401  (fixture)

pytestmark = needs_pg


@pytest.mark.parametrize("mode", ["shared", "personal"])
def test_u1_wrong_group_and_moved_the_same_day_the_new_group_charges_todays_lesson(world, fixed_today, mode):
    # written down into B (a lesson today) by mistake and moved the same day into C (a lesson today): B had a stay of no day -> it holds no day -> C charges from TODAY
    a, b, c = three(world, fixed_today, mode)
    s = fresh_student(world, "Ошибся")
    add(world, s, group=b, mode="add")
    add(world, s, group=c, mode="transfer")
    nb, nc = numbers(world, s, b), numbers(world, s, c)
    assert nb[0] == 0 and nb[3] == 0, nb
    assert nc[0] == (250_000 if mode == "shared" else 500_000), nc      # shared: 4 of 8 lessons; a personal period: the whole period of 500 000 from today


def test_u1_a_to_b_to_c_the_same_day_c_charges_todays_lesson_b_nothing_and_the_money_is_whole_in_c(world, fixed_today):
    a, b, c = three(world, fixed_today)
    s0 = world.students[0]
    settle(world, s0, a)
    add(world, s0, group=b, mode="transfer")
    carried_to_b = numbers(world, s0, b)[5]
    add(world, s0, group=c, mode="transfer")
    nb, nc = numbers(world, s0, b), numbers(world, s0, c)
    assert nb[0] == 0 and nb[3] == 0 and nb[5] == 0, nb
    assert nc[0] == 250_000 and nc[5] == carried_to_b > 0, nc


def test_u1_an_ordinary_transfer_from_an_old_group_with_a_lesson_today_keeps_the_day_in_the_old_group(world, fixed_today):
    a, b, c = three(world, fixed_today)
    s0 = fresh_student(world, "Давний")
    add(world, s0, group=b, mode="add")
    settle(world, s0, b, joined=date(2026, 11, 2), paid=450_000)
    add(world, s0, group=c, mode="transfer")
    nb, nc = numbers(world, s0, b), numbers(world, s0, c)
    # B: 7 of 12 lessons up to and including today = 262 500 -> 263 000, the rest of the 450 000 (187 000) went to C; C starts TOMORROW (nothing charged yet): the day is neither paid twice nor lost
    assert nb[0] == 263_000 and nc[0] == 0 and nc[2] == 187_000 and nc[3] == 0, (nb, nc)


# ---- У2: the window and the answer of the leaving count like the record ------------------------------------------------------------------------------------------------


def window(w, student, group, client=None):
    r = (client or owner(w)).get(f"{API}/org/groups/{group.id}/students/{student.id}/leave-preview")
    assert r.status_code == 200, r.text
    return r.json()["memberships"][0]


def leave(w, student, group, body=None, client=None, expect=200):
    r = (client or owner(w)).post(f"{API}/org/groups/{group.id}/students/{student.id}/leave", json=body or {})
    assert r.status_code == expect, r.text
    return r.json()


def test_u2_written_down_today_taken_out_today_the_window_the_record_and_the_answer_see_no_debt(world, fixed_today):
    # B has a lesson today (38 000 would be the ghost debt of the old rule): the stay is of no day -> no debt anywhere
    import json

    a, b, c = three(world, fixed_today)
    s = fresh_student(world, "Ошибка")
    add(world, s, group=b, mode="add")
    row = window(world, s, b)
    assert row["money"]["debt"] == 0 and row["has_open_balance"] is False and row["allowed"] == ["keep"], row
    done = leave(world, s, b)                                     # no expected_debt, no settle: passes
    assert "долг" not in json.dumps(done["effects"], ensure_ascii=False).lower(), done["effects"]


def test_u2_paid_600000_the_refund_is_the_whole_600000(world, fixed_today):
    from tests.test_payments_service_pg import pay

    a, b, c = three(world, fixed_today)
    s = fresh_student(world, "Оплатил")
    add(world, s, group=b, mode="add")
    pay(world, s, b, amount=600_000)
    row = window(world, s, b)
    assert row["money"]["refundable"] == 600_000 and row["money"]["advance"] == 600_000 and row["money"]["debt"] == 0, row
    done = leave(world, s, b, {"settle": [{"membership_id": row["membership_id"], "action": "refund", "method": "cash", "expected_refundable": 600_000}]})
    assert [(i["action"], i["amount"]) for i in done["settled"]] == [("refund", 600_000)]


def test_u2_an_administrator_without_the_money_right_takes_out_a_student_written_down_today(world, fixed_today):
    from tests.test_money2_wave_b_pg import _admin

    a, b, c = three(world, fixed_today)
    s = fresh_student(world, "Ошибка админа")
    add(world, s, group=b, mode="add")
    done = leave(world, s, b, client=_admin(world))
    assert done["settled"] == []


def test_u2_an_ordinary_old_student_with_a_debt_the_window_equals_the_record(world, fixed_today):
    # in B since 02.11, nothing paid: 7 of 12 lessons up to today = 262 500 -> 263 000, the window and the record agree
    a, b, c = three(world, fixed_today)
    s = fresh_student(world, "Давний")
    add(world, s, group=b, mode="add")
    stay(world, membership(world, s, b), date(2026, 11, 2))
    row = window(world, s, b)
    assert row["money"]["debt"] == 263_000 and row["allowed"] == ["keep", "writeoff"], row
    done = leave(world, s, b, {"settle": [{"membership_id": row["membership_id"], "action": "writeoff", "expected_debt": 263_000, "reason": "Договорились"}]})
    assert [(i["action"], i["amount"]) for i in done["settled"]] == [("writeoff", 263_000)]


# ---- У3: a one-day stay charges its day when the student has a COUNTED mark of a lesson of that day ------------------------------------------------------------------------------


def one_day(w, student, group, marked):
    """Written down today and taken out today; ``marked``: a held lesson of the group today with the student «present»."""
    add(w, student, group=group, mode="add")
    if marked:
        hold_lessons(w, student, [TUE], group=group)
    leave(w, student, group)


@pytest.mark.parametrize("mode,expected", [("shared", 38_000), ("personal", 15_000)])
def test_u3_a_one_day_stay_with_a_mark_charges_its_day_without_one_nothing(world, fixed_today, mode, expected):
    # shared month: B (12 lessons, 450 000) charges the one lesson he was marked at: 37 500 -> 38 000; a personal period is 30 days: one day = 450 000 / 30 = 15 000
    a, b, c = three(world, fixed_today, mode)
    marked, bare = fresh_student(world, "С отметкой"), fresh_student(world, "Без отметки")
    one_day(world, marked, b, True)
    one_day(world, bare, b, False)
    nm, nb = numbers(world, marked, b), numbers(world, bare, b)
    assert nm[0] == expected and nm[3] == expected, nm
    assert nb[0] == 0 and nb[3] == 0, nb


def test_u3_a_mark_that_appears_the_day_after_the_closing_brings_the_charge_and_the_debt_is_seen_in_the_past_balances(world, fixed_today):
    from src.auth.leave_effects import past_balances

    a, b, c = three(world, fixed_today)
    s = fresh_student(world, "Отметка потом")
    one_day(world, s, b, False)
    assert numbers(world, s, b)[0] == 0
    fixed_today(date(2026, 11, 18))
    hold_lessons(world, s, [TUE], group=b)
    assert numbers(world, s, b)[0] == 38_000
    rows = past_balances(world.db, world.org.id, s.id, c.id, money=True)
    assert [(r["group_name"], r["kind"], r["amount"]) for r in rows] == [("Бета", "debt", 38_000)], rows


def test_u3_payroll_a_marked_one_day_stay_is_a_lesson_covered_by_the_money_and_an_unmarked_one_is_no_lesson_at_all(world, fixed_today):
    from src.auth.payments_service import PaymentService
    from tests.test_payments_service_pg import pay

    a, b, c = three(world, fixed_today)
    marked, bare = fresh_student(world, "Оплачен с отметкой"), fresh_student(world, "Нет отметки")
    add(world, marked, group=b, mode="add")
    pay(world, marked, b, amount=450_000)
    hold_lessons(world, marked, [TUE], group=b)
    leave(world, marked, b)
    one_day(world, bare, b, False)
    cover = PaymentService(world.db).lesson_coverage(str(world.org.id), [membership(world, marked, b).id, membership(world, bare, b).id], TUE, TUE)
    # the marked one: counted and covered by the MONEY of the student (before У3 the ledger had no stay there: «before_start», the teacher was paid for a lesson the student was not charged); the bare one: not in the sheet at all
    assert {(k[0] == membership(world, marked, b).id, v.paid, v.cover_source) for k, v in cover.items()} == {(True, True, "money")}, cover


# ---- У4: a forgiveness of a debt that is gone after the closing is cancelled by the closing itself -----------------------------------------------------------------------------


def forgive(w, student, group, amount=38_000):
    m = membership(w, student, group)
    r = owner(w).post(f"{API}/org/memberships/{m.id}/writeoffs", json={"amount": amount, "reason": "Договорённость с родителем"})
    assert r.status_code in (200, 201), r.text
    return m, r.json()["writeoff"]["id"]


def writeoffs_of(w, m):
    from src.models.auth import StudentWriteoff

    w.db.expire_all()
    return w.db.query(StudentWriteoff).filter(StudentWriteoff.membership_id == m.id).all()


def test_u4_forgiven_while_in_the_group_taken_out_the_same_day_without_a_mark_the_forgiveness_is_cancelled_with_a_journal_line(world, fixed_today):
    from src.auth import money_reconcile as mr
    from src.models.auth import GroupAudit

    a, b, c = three(world, fixed_today)
    s = fresh_student(world, "Простили")
    add(world, s, group=b, mode="add")
    m, wid = forgive(world, s, b)
    row = window(world, s, b)
    assert row["money"]["advance"] == 0 and row["money"]["debt"] == 0 and row["has_open_balance"] is False, row      # the window reads the account as the closing will leave it
    leave(world, s, b)
    (row,) = writeoffs_of(world, m)
    assert row.voided_at is not None and row.voided_by == world.owner.id and "день записи" in row.void_reason, row
    lines = world.db.query(GroupAudit).filter(GroupAudit.org_id == world.org.id, GroupAudit.action == "payment_writeoff_void").all()
    assert [(l.snapshot["writeoff_id"], str(l.actor_id)) for l in lines] == [(wid, str(world.owner.id))]
    n = numbers(world, s, b)
    assert (n[0], n[2], n[3]) == (0, 0, 0), n                          # charged 0, advance 0, debt 0
    result = mr.run(world.db, apply=True, only=[world.org.id])
    assert result["failed"] == [] and [i for i in result["items"] if i["org"] == str(world.org.id)] == []


def test_u4_with_a_counted_mark_the_forgiveness_stays(world, fixed_today):
    a, b, c = three(world, fixed_today)
    s = fresh_student(world, "С отметкой")
    add(world, s, group=b, mode="add")
    m, _ = forgive(world, s, b)
    hold_lessons(world, s, [TUE], group=b)
    leave(world, s, b)
    (row,) = writeoffs_of(world, m)
    assert row.voided_at is None
    n = numbers(world, s, b)
    assert (n[0], n[3]) == (38_000, 0), n                              # the lesson he got is charged and forgiven


def test_u4_a_forgiveness_of_an_earlier_stay_is_not_touched_by_the_one_day_return(world, fixed_today):
    # in B since 02.11 with a forgiveness dated 10.11, left, came back today and was taken out today: the old forgiveness belongs to the old stay
    from src.models.auth import StudentWriteoff

    a, b, c = three(world, fixed_today)
    s = fresh_student(world, "Вернулся")
    add(world, s, group=b, mode="add")
    m = membership(world, s, b)
    stay(world, m, date(2026, 11, 2), date(2026, 11, 12))
    world.db.add(StudentWriteoff(org_id=world.org.id, membership_id=m.id, student_id=s.id, group_id=b.id, amount=1_000, reason="Старое", effective_on=date(2026, 11, 10), created_by=world.owner.id))
    world.db.commit()
    add(world, s, group=b, mode="add")
    leave(world, s, b)
    assert [r.voided_at is None for r in writeoffs_of(world, m)] == [True]


def test_u1b_a_wrong_group_with_a_mark_holds_the_day_the_new_group_starts_tomorrow(world, fixed_today):
    a, b, c = three(world, fixed_today)
    s = fresh_student(world, "Был на уроке")
    add(world, s, group=b, mode="add")
    hold_lessons(world, s, [TUE], group=b)
    add(world, s, group=c, mode="transfer")
    nb, nc = numbers(world, s, b), numbers(world, s, c)
    assert nb[0] == 38_000 and nc[0] == 0, (nb, nc)      # the lesson he got is B's; C starts tomorrow


# ---- У5: the small ones --------------------------------------------------------------------------------------------------------------------------------------------------------


def test_u5a_the_table_of_openings_takes_the_first_day_from_the_stays_of_the_book_not_from_a_stay_of_no_day(world, fixed_today):
    # the accounting starts 01.10; the student was written down on 20.09 and taken out the same day (no mark), and is in the group since 02.11: his first day is 02.11 -> no «не хватает остатка»
    from datetime import datetime

    from src.models.auth import StudentGroupPeriod
    from tests.test_money2_fix_r3c_pg import BASE

    a, b, c = three(world, fixed_today)
    s = fresh_student(world, "Ошибся в сентябре")
    add(world, s, group=b, mode="add")
    m = membership(world, s, b)
    stay(world, m, date(2026, 11, 2))
    world.db.add(StudentGroupPeriod(membership_id=m.id, joined_at=datetime(2026, 9, 20, 6), left_at=datetime(2026, 9, 20, 6)))
    world.db.commit()
    rows = [r for g in owner(world).get(BASE).json()["groups"] for r in g["memberships"] if r["membership_id"] == str(m.id)]
    assert [(r["enrolled_on"], r["missing"]) for r in rows] == [("2026-11-02", False)], rows


@pytest.mark.parametrize("state", ["done", "skipped"])
def test_u5b_a_row_that_is_not_editable_because_the_launch_is_closed_says_why(world, fixed_today, state):
    from src.auth.payments_service import OpeningOwnerOnly
    from tests.test_money2_fix_r3c_pg import BASE, world_g1

    a, s0, admin = world_g1(world, fixed_today, state)
    rows = [r for g in admin.get(BASE).json()["groups"] for r in g["memberships"]]
    assert rows and all(r["editable"] is False and r["reason_not_editable"] == OpeningOwnerOnly.TEXT for r in rows), rows


def test_u5c_pay_from_is_not_limited_by_a_stay_that_charged_nothing(world, fixed_today):
    a, b, c = three(world, fixed_today)
    bare, marked = fresh_student(world, "Без отметки"), fresh_student(world, "С отметкой")
    one_day(world, bare, b, False)
    one_day(world, marked, b, True)
    fixed_today(date(2026, 11, 18))                                                         # (the same day it would be a cancelled leaving, not a return)
    add(world, bare, group=b, mode="add", pay_from=TUE.isoformat())                       # the earlier stay charged nothing: 17.11 is allowed
    r = owner(world).post(f"{API}/org/groups/{b.id}/students/{marked.id}", params={"mode": "add"}, json={"pay_from": TUE.isoformat()})
    assert r.status_code == 422 and "PAY_FROM_RANGE" in r.text, r.text                   # the earlier stay charged its lesson: «Оплата с» is after it
