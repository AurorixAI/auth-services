"""PAY-DUE 10.5 (windows of a leaving and an archive of a student): the sum is named by the state AFTER the action (PostgreSQL, through the routes of the product).

World as in ``test_money2_transfer_a_pg``: Fri 20.11.2026, A = 300 000 a month with a lesson every day, B = 400 000, «Общий месяц» from the 1st, the term 5 days.  A student added to A today has the first charged day 20.11:
the term is 20, 21, 22, 23, 24 -> «к оплате до 24.11»; an open stay charges 20..30.11 = 11 lessons x 10 000 = 110 000 (A); the leaving of today closes it: it keeps only the lesson of today that was held (a stay opened and closed on one day with no mark charges nothing)
= 10 000 (A); B (400 000 a month = 13 333 a lesson) = 13 000."""
from datetime import date

from tests.money1b_helpers import add, fresh_student, hold_lessons
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_money2_transfer_a_pg import groups3
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixture)
from src.auth import payments_texts as tx

pytestmark = needs_pg
TODAY = date(2026, 11, 20)


def preview(w, group, student):
    r = owner(w).get(f"{API}/org/groups/{group.id}/students/{student.id}/leave-preview")
    assert r.status_code == 200, r.text
    return r.json()["memberships"][0]


def archive_preview(w, student):
    r = owner(w).get(f"{API}/org/students/{student.id}/archive-preview")
    assert r.status_code == 200, r.text
    return {m["group_name"]: m for m in r.json()["memberships"]}


def test_the_only_group_leaving_in_the_term_says_the_sum_becomes_a_debt_and_after_it_is_one(world, fixed_today):
    a, b, c = groups3(world, fixed_today)
    s = fresh_student(world, "Единственная")
    add(world, s, group=a, mode="add")
    hold_lessons(world, s, [TODAY], group=a)
    m = preview(world, a, s)
    n = m["money"]["unpaid"]
    assert n == 10_000 and (m["money"]["debt_amount"], m["money"]["due_amount"], m["money"]["becomes_debt"]) == (10_000, 0, 10_000)      # the lesson of today; all of it turns into a debt
    (stay,) = [x for x in m["effects"]["stays"] if x["code"] == "debt"]
    assert stay["text"] == f"Останется долг {tx.money(n)}: это единственная группа ученика" and stay["turns"] == 10_000      # PAY-TAILS Б1.3: the short sentence of the total (was «К оплате …. Это единственная группа ученика: после ухода сумма станет долгом»)
    def mine(bucket):
        rows = owner(world).get(f"{API}/org/payments/status", params={"bucket": bucket, "scope": "all"}).json()["items"]
        return [r for r in rows if r["student_id"] == str(s.id)]

    (now,) = mine("to_pay")
    assert (now["due_amount"], now["debt_amount"]) == (110_000, 0)      # now: «к оплате» (the open stay charges the rest of the month)
    assert owner(world).post(f"{API}/org/groups/{a.id}/students/{s.id}/leave", json={}).status_code == 200
    assert mine("to_pay") == []
    (after,) = mine("debtors")
    assert after["debt_amount"] == 10_000 and not after["overdue_days"] and "просрочено" not in (after["debt_sub"] or "")      # a debt without «просрочено»: K = 0 (the term was 24.11)


def test_a_leaving_from_one_of_two_groups_keeps_the_term_of_the_sum(world, fixed_today):
    a, b, c = groups3(world, fixed_today)
    s = fresh_student(world, "Две группы")
    add(world, s, group=a, mode="add")
    add(world, s, group=b, mode="add")
    hold_lessons(world, s, [TODAY], group=a)
    m = preview(world, a, s)
    assert (m["money"]["debt_amount"], m["money"]["due_amount"], m["money"]["becomes_debt"], m["money"]["due_until"]) == (0, 10_000, 0, "2026-11-24")
    (stay,) = [x for x in m["effects"]["stays"] if x["code"] == "due"]
    assert stay["text"] == f"Останется к оплате {tx.money(10_000)} до 24.11"      # PAY-TAILS Б1.3: the short sentence of the total (the choice under it says what can be done)
    assert not [x for x in m["effects"]["stays"] if x["code"] == "debt"]


def test_the_archive_of_a_student_says_the_whole_unpaid_sum_of_every_group_becomes_a_debt(world, fixed_today):
    # PAY-TAILS Б5.2 (a change of the outcome, LESSONS 143): B was joined BEFORE today, A today (a lesson of both today is marked).  The archive closes both today; the day is charged ONCE (a day is not paid twice): B keeps
    # the lesson of today (10 000), A - written down the same day - charges nothing; the probe now says what the record writes (the probe charged both before and the write charged one)
    a, b, c = groups3(world, fixed_today)
    s = fresh_student(world, "Уходит")
    fixed_today(date(2026, 11, 19))                                                # yesterday: the term of B (5 days) is still running today
    add(world, s, group=b, mode="add")
    fixed_today(TODAY)
    add(world, s, group=a, mode="add")
    hold_lessons(world, s, [TODAY], group=a)
    hold_lessons(world, s, [TODAY], group=b)
    got = archive_preview(world, s)
    (stay,) = [x for x in got[b.name]["effects"]["stays"] if x["code"] == "debt"]
    n = got[b.name]["money"]["unpaid"]
    assert stay["text"] == f"Ученик уходит из центра: вся неоплаченная сумма {tx.money(n)} в группе «{b.name}» станет долгом" and stay["turns"] == n > 0
    assert [x["code"] for x in got[a.name]["effects"]["stays"]] == ["none"] and got[a.name]["money"]["unpaid"] == 0


def test_two_groups_written_down_and_closed_on_one_day_with_lessons_both_charge_nothing_as_the_record_does(world, fixed_today):
    # PAY-TAILS Б5.2, the known edge «как есть»: each group holds the day for the other, a stay of one day that the other holds is nothing - so both charge 0, in the window and in the record
    a, b, c = groups3(world, fixed_today)
    s = fresh_student(world, "Оба сегодня")
    add(world, s, group=a, mode="add")
    add(world, s, group=b, mode="add")
    hold_lessons(world, s, [TODAY], group=a)
    hold_lessons(world, s, [TODAY], group=b)
    got = archive_preview(world, s)
    assert [got[g.name]["money"]["unpaid"] for g in (a, b)] == [0, 0]
    assert owner(world).post(f"{API}/org/students/{s.id}/archive", json={}).status_code == 200
    assert rows_of(world, s, "debtors") == [] and rows_of(world, s, "to_pay") == []                          # and the record owes nothing either


def rows_of(w, student, bucket="all"):
    r = owner(w).get(f"{API}/org/payments/status", params={"bucket": bucket, "scope": "all", "limit": 100})
    assert r.status_code == 200, r.text
    return [x for x in r.json()["items"] if x["student_id"] == str(student.id)]


def test_a_transfer_keeps_the_term_of_the_periods_of_the_old_group(world, fixed_today):
    a, b, c = groups3(world, fixed_today)
    s = fresh_student(world, "Переходит")
    add(world, s, group=a, mode="add")
    hold_lessons(world, s, [TODAY], group=a)
    add(world, s, group=b, mode="transfer", from_group_id=str(a.id))
    (old,) = [r for r in rows_of(world, s, "to_pay") if r["group_id"] == str(a.id)]
    # the student stays in the centre: the part of the old group goes on to its term (20, 21, 22, 23, 24.11) - «к оплате», not a debt
    assert (old["state"], old["due_amount"], old["debt_amount"], old["due_until"]) == ("to_pay", 10_000, 0, "2026-11-24")


def test_a_return_on_the_day_of_leaving_gives_back_the_sum_to_pay_with_the_same_term(world, fixed_today):
    a, b, c = groups3(world, fixed_today)
    s = fresh_student(world, "Вернулся")
    add(world, s, group=a, mode="add")
    hold_lessons(world, s, [TODAY], group=a)
    (before,) = rows_of(world, s, "to_pay")
    assert (before["state"], before["due_until"]) == ("to_pay", "2026-11-24")
    assert owner(world).post(f"{API}/org/groups/{a.id}/students/{s.id}/leave", json={}).status_code == 200
    (gone,) = rows_of(world, s, "debtors")
    assert (gone["state"], gone["debt_amount"]) == ("debt", 10_000)                     # the only group: a debt at once
    add(world, s, group=a, mode="add")                                                  # the same day: the leaving is cancelled
    (back,) = rows_of(world, s, "to_pay")
    assert (back["state"], back["due_until"], back["debt_amount"]) == ("to_pay", "2026-11-24", 0) and back["due_amount"] == before["due_amount"]


def test_17_9_when_only_a_part_becomes_a_debt_the_text_says_the_two_names_apart():
    """PAY-DUE 17.9: «вся сумма станет долгом» only when the WHOLE sum becomes a debt; a package part turns into a debt while the periods stay «к оплате» - two names, two sums."""
    from uuid import uuid4
    from src.auth import leave_effects as le

    whole = le.Snap(membership_id=uuid4(), group_id=uuid4(), group_name="Альфа", debt=500_000, named_debt=500_000, before_debt=200_000, turns=300_000)
    assert le.debt_text(whole, True).endswith("После ухода вся сумма 500\xa0000\xa0сум станет долгом")
    part = le.Snap(membership_id=uuid4(), group_id=uuid4(), group_name="Альфа", debt=700_000, named_debt=500_000, named_due=200_000, before_debt=200_000, turns=300_000)
    text = le.debt_text(part, True)
    assert text == "Долг 200\xa0000\xa0сум и к оплате 500\xa0000\xa0сум. После ухода 300\xa0000\xa0сум станет долгом, к оплате останется 200\xa0000\xa0сум" and "вся сумма" not in text
    assert "вся" not in le.debt_text(part, False) and "часть" in le.debt_text(part, False)
