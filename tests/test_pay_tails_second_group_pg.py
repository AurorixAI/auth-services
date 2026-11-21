"""PAY-TAILS Б5.2 (step 4): the rule «a day is not paid twice» seen by the windows.  The closing of A keeps today for A, so a group B of the same student that BEGAN today starts a day later - and the probe says it with the
money, in the words of the mode.  The probe and the write agree (they did not for the archive of a student).  Friday 20.11.2026.

ОМ: A = Tue / Thu / Fri, 600 000 (Nov lessons 3, 5, 6, 10, 12, 13, 17, 19, 20, 24, 26, 27 = 12); the student is in A since 02.11 and leaves it today after nine lessons (3 ... 20) = 450 000.
B = Mon / Wed / Fri, 1 300 000 (13 lessons = 100 000 each: 2, 4, 6, 9, 11, 13, 16, 18, 20, 23, 25, 27, 30); added to B today: lessons 20, 23, 25, 27, 30 = 500 000; after the leaving of A the lesson of today is A's: 23, 25, 27, 30 = 400 000."""
from datetime import date

from src.auth import payments_texts as tx

from tests.money1b_helpers import account, add, fresh_student, set_schedule, slot
from tests.pg_world import ensure_billing, give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, settings_block  # noqa: F401  (fixtures)
from tests.test_pay_tails_money_pg import TTF

pytestmark = needs_pg
D = date
M, N = tx.money, tx.num
MWF = [slot("m1", 1, "2026-01-01"), slot("m3", 3, "2026-01-01"), slot("m5", 5, "2026-01-01")]


def om_pair(w, fixed_today):
    fixed_today(D(2026, 11, 2))
    settings_block(w, accounting_start_date="2026-10-01", billing_anchor="fixed_day", billing_day=1)
    a, b = make_group(w.db, w.org, w.teacher, "Математика 7"), make_group(w.db, w.org, w.teacher, "Английский 7")
    give_group_price(w.db, a, 600_000)
    give_group_price(w.db, b, 1_300_000)
    set_schedule(w, TTF, a)
    set_schedule(w, MWF, b)
    for g in (a, b):
        ensure_billing(w, g, price=None)
    s = fresh_student(w, "Двое")
    add(w, s, group=a, mode="add")
    fixed_today(D(2026, 11, 20))
    add(w, s, group=b, mode="add")                                              # begins today
    return a, b, s


def probe(w, g, s):
    r = owner(w).get(f"{API}/org/groups/{g.id}/students/{s.id}/leave-preview")
    assert r.status_code == 200, r.text
    (m,) = r.json()["memberships"]
    return m


def test_om_the_probe_says_the_money_of_the_second_group_and_the_write_agrees(world, fixed_today):
    a, b, s = om_pair(world, fixed_today)
    assert account(world, membership(world, s, b).id, D(2026, 11, 30)).charged == 500_000            # before: B begins today (read when the month has begun)
    m = probe(world, a, s)
    assert m["others"] == [f"В группе «Английский 7»: {N(500_000)} → {M(400_000)} — сегодняшний урок оплачен в группе «Математика 7»"]
    assert [x["text"] for x in m["recalc"]] == [f"Ноябрь: {N(600_000)} → {M(450_000)}"] and m["recalc"][0]["basis_text"] == "пересчёт по день ухода · 9 уроков из 12"
    done = owner(world).post(f"{API}/org/groups/{a.id}/students/{s.id}/leave", json={}).json()
    assert done["others"] == m["others"]
    assert account(world, membership(world, s, b).id, D(2026, 11, 30)).charged == 400_000            # the probe was the write: B begins on Monday 23.11 (the lesson of Friday 20.11 is A's)


def test_om_a_return_to_a_the_same_day_takes_the_line_away_and_b_charges_from_today_again(world, fixed_today):
    a, b, s = om_pair(world, fixed_today)
    assert owner(world).post(f"{API}/org/groups/{a.id}/students/{s.id}/leave", json={}).status_code == 200
    add(world, s, group=a, mode="add")                                          # back the same day: the leaving is cancelled
    assert account(world, membership(world, s, b).id, D(2026, 11, 30)).charged == 500_000
    assert probe(world, a, s)["others"] == [f"В группе «Английский 7»: {N(500_000)} → {M(400_000)} — сегодняшний урок оплачен в группе «Математика 7»"]


def test_a_group_that_began_earlier_is_not_moved(world, fixed_today):
    a, b, s = om_pair(world, fixed_today)
    fixed_today(D(2026, 11, 21))
    assert probe(world, a, s)["others"] == []


def test_lp_the_period_of_the_second_group_begins_the_next_day_the_sum_is_the_same(world, fixed_today):
    fixed_today(D(2026, 11, 1))
    settings_block(world, accounting_start_date="2026-10-01", billing_anchor="payment_date")
    a, b = make_group(world.db, world.org, world.teacher, "Личная А"), make_group(world.db, world.org, world.teacher, "Личная Б")
    for g in (a, b):
        give_group_price(world.db, g, 420_000)
        ensure_billing(world, g, price=None)
    s = fresh_student(world, "Двое ЛП")
    add(world, s, group=a, mode="add")
    fixed_today(D(2026, 11, 20))
    add(world, s, group=b, mode="add")
    assert account(world, membership(world, s, b).id, D(2026, 11, 20)).first_billable == D(2026, 11, 20)
    m = probe(world, a, s)
    assert m["others"] == ["В группе «Личная Б» период начнётся с 21.11: сегодняшний день оплачен в группе «Личная А»"]


def test_the_archive_probe_and_the_write_agree_for_a_group_added_today(world, fixed_today):
    a, b, s = om_pair(world, fixed_today)
    r = owner(world).get(f"{API}/org/students/{s.id}/archive-preview")
    assert r.status_code == 200, r.text
    shown = {m["group_name"]: m["money"]["charged_after_leave"] for m in r.json()["memberships"]}
    assert owner(world).post(f"{API}/org/students/{s.id}/archive", json={}).status_code == 200
    got = {g.name: account(world, membership(world, s, g).id, D(2026, 11, 20)).charged for g in (a, b)}
    assert shown == got                                                         # before the fix the probe charged B 500 000 and the write 0 or 400 000
    assert got["Математика 7"] == 450_000


def test_a_transfer_says_when_the_new_group_begins_to_charge(world, fixed_today):
    # a student only in A (since 02.11) moves to B on Friday 20.11: A keeps the day (it has a lesson), B begins to charge on Monday 23.11 (the first lesson after today: «с 21.11» is the first BILLABLE day, a Saturday)
    a, b, s = om_pair(world, fixed_today)
    t = fresh_student(world, "Один")
    fixed_today(D(2026, 11, 2))
    add(world, t, group=a, mode="add")
    fixed_today(D(2026, 11, 20))
    r = owner(world).post(f"{API}/org/groups/{b.id}/students/{t.id}", params={"mode": "transfer", "dry_run": "true"}, json={"from_group_id": str(a.id)})
    assert r.status_code == 200, r.text
    assert r.json()["others"] == ["Начисление в группе «Английский 7» начнётся с 21.11"] and r.json()["recalc"][0]["text"] == f"Ноябрь: {N(600_000)} → {M(450_000)}"
