"""PAY-TAILS Б1.3 / Б5.1 (step 3): «что было -> что станет» of a leaving and the protection of the shown number.  PostgreSQL, through the routes of the product; every number is worked out by hand.

ОМ 600 000, Tue / Thu / Fri: November lessons 3, 5, 6, 10, 12, 13, 17, 19, 20, 24, 26, 27 = 12 (50 000 each); leaving on 11.11 after four lessons (3, 5, 6, 10) = 200 000.  ЛП 420 000 for 30 days: leaving on
the 21st day = 420 000 x 21 / 30 = 294 000."""
from datetime import date

from src.auth import payments_texts as tx

from tests.pg_world import make_client, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixtures)
from tests.test_pay_tails_money_pg import forgive, lp, om, pay

pytestmark = needs_pg
D = date
M, N = tx.money, tx.num


def preview(w, g, s, client=None):
    r = (client or owner(w)).get(f"{API}/org/groups/{g.id}/students/{s.id}/leave-preview")
    assert r.status_code == 200, r.text
    (m,) = r.json()["memberships"]
    return r.json(), m


def leave(w, g, s, body=None, expect=200, client=None):
    r = (client or owner(w)).post(f"{API}/org/groups/{g.id}/students/{s.id}/leave", json=body or {})
    assert r.status_code == expect, r.text
    return r.json()


def stays(m):
    return {x["code"]: x for x in m["effects"]["stays"]}


def test_om_a_debtor_leaves_the_month_is_cut_to_the_four_lessons_and_the_total_is_a_sentence(world, fixed_today):
    g, s = om(world, fixed_today)
    fixed_today(D(2026, 11, 11))
    body, m = preview(world, g, s)
    assert m["recalc"]["more"] == 0 and m["recalc"]["lines"] == [{"label": "Ноябрь", "before": 600_000, "after": 200_000, "basis_text": "пересчёт по день ухода · 4 урока из 12", "text": f"Ноябрь: {N(600_000)} → {M(200_000)}", "done": False}]
    assert stays(m)["debt"]["text"] == "Останется долг " + M(200_000) and m["money"]["unpaid"] == 200_000 and body["as_of"] == "2026-11-11"
    done = leave(world, g, s)
    assert done["recalc"]["lines"][0]["text"] == f"Ноябрь: {N(600_000)} → {M(200_000)}" and done["recalc"]["lines"][0]["done"] is True
    assert done["message"] == "Ученик убран из группы" and [x["text"] for x in done["effects"]["stays"]] == ["Долг " + M(200_000) + " остался за учеником"]


def test_om_paid_ahead_two_periods_the_next_one_never_begins_and_the_money_on_it_stays_an_advance(world, fixed_today):
    # paid 600 000 (November) + 300 000 (half of December): leaving 11.11 -> November 200 000; the money 900 000 - 200 000 = 700 000 is an advance; December «не начнётся: 300 000» (the MONEY laid on it, not its price)
    g, s = om(world, fixed_today)
    pay(world, s, g, amount=600_000, paid_on=D(2026, 11, 3))
    pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 3))
    fixed_today(D(2026, 11, 11))
    _, m = preview(world, g, s)
    assert [x["text"] for x in m["recalc"]["lines"]] == [f"Ноябрь: {N(600_000)} → {M(200_000)}", f"Декабрь не начнётся: {M(300_000)} останутся авансом"]
    assert stays(m)["advance"]["text"] == "Останется аванс " + M(700_000) and m["money"]["advance"] == 700_000
    done = leave(world, g, s)
    assert done["recalc"]["lines"][1]["text"] == f"Декабрь не начался: {M(300_000)} остались авансом"


def test_om_money_and_forgiveness_the_forgiveness_falls_with_the_charge_the_money_stays(world, fixed_today):
    # С3: paid 300 000 + forgiven 300 000 on a month of 600 000; leaving after four lessons: charged 200 000 < money 300 000 -> the forgiveness is used 300 000 -> 0, advance 100 000 (money)
    g, s = om(world, fixed_today)
    pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 3))
    forgive(world, s, g, 300_000)
    fixed_today(D(2026, 11, 11))
    _, m = preview(world, g, s)
    assert [x["text"] for x in m["recalc"]["lines"]] == [f"Ноябрь: {N(600_000)} → {M(200_000)}", f"Прощено: {N(300_000)} → {M(0)}"]
    assert stays(m)["advance"]["text"] == "Останется аванс " + M(100_000)


def test_lp_the_basis_is_the_days_and_the_label_is_the_period(world, fixed_today):
    g, s = lp(world, fixed_today, joined=D(2026, 11, 5))                      # the period 05.11 - 04.12
    fixed_today(D(2026, 11, 25))                                              # the 21st day
    _, m = preview(world, g, s)
    (line,) = m["recalc"]["lines"]
    assert (line["label"], line["before"], line["after"], line["basis_text"]) == ("05.11–04.12", 420_000, 294_000, "пересчёт по день ухода · 21 день из 30")


def test_no_change_of_the_charge_no_block_and_a_closed_account_says_so(world, fixed_today):
    # the last day of the personal period: charged 420 000 either way; paid in full: nothing to settle
    g, s = lp(world, fixed_today)
    pay(world, s, g, amount=420_000, paid_on=D(2026, 11, 1))
    fixed_today(D(2026, 11, 30))
    _, m = preview(world, g, s)
    assert m["recalc"] is None and [x["text"] for x in m["effects"]["stays"]] == ["Расчётов нет — можно убирать"] and m["others"] == []


def test_the_shown_numbers_are_checked_for_every_membership_even_for_keep(world, fixed_today):
    g, s = om(world, fixed_today)
    fixed_today(D(2026, 11, 11))
    _, m = preview(world, g, s)
    mid = m["membership_id"]
    ok = {"membership_id": mid, "action": "keep", "expected_unpaid": 200_000, "expected_advance": 0, "expected_as_of": "2026-11-11"}
    for bad in ({"expected_unpaid": 150_000}, {"expected_advance": 5}, {"expected_as_of": "2026-11-10"}):          # a sum moved / the midnight passed between the window and the click
        r = owner(world).post(f"{API}/org/groups/{g.id}/students/{s.id}/leave", json={"settle": [{**ok, **bad}]})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "BALANCE_CHANGED" and r.json()["detail"]["message"] == "Сумма изменилась, откройте окно заново", r.text
        assert membership(world, s, g).is_active                                                                # nothing was done
    leave(world, g, s, {"settle": [ok]})
    assert not membership(world, s, g).is_active


def test_a_caller_without_the_right_to_money_is_never_refused_by_the_check(world, fixed_today):
    # a student with nothing on the account can be removed by an administrator without the right; the numbers he sends (he cannot see them) are not checked
    g, s = om(world, fixed_today, joined=D(2026, 11, 2))
    fixed_today(D(2026, 11, 3))
    pay(world, s, g, amount=600_000, paid_on=D(2026, 11, 3))
    fixed_today(D(2026, 11, 27))                                                                               # the last lesson of November: the month is paid, nothing to give back, nothing owed
    admin = make_client(world, user=world.admin)
    r = admin.get(f"{API}/org/groups/{g.id}/students/{s.id}/leave-preview")
    assert r.status_code == 200 and r.json()["memberships"][0]["recalc"] is None and r.json()["memberships"][0]["money"] is None
    leave(world, g, s, {"settle": [{"membership_id": str(membership(world, s, g).id), "action": "keep", "expected_unpaid": 999, "expected_as_of": "2020-01-01"}]}, client=admin)


def test_a_pause_inside_the_current_period_the_charge_before_has_no_paused_days(world, fixed_today):
    # a pause 17.11 - 30.11 entered on 03.11: before the leaving November charges the lessons outside it (3, 5, 6, 10, 12, 13 = 6 x 50 000 = 300 000); leaving 11.11 keeps four (3, 5, 6, 10) = 200 000
    g, s = om(world, fixed_today)
    r = owner(world).post(f"{API}/org/memberships/{membership(world, s, g).id}/pauses", json={"first_day": "2026-11-17", "last_day": "2026-11-30", "reason": "Болезнь"})
    assert r.status_code == 201, r.text
    fixed_today(D(2026, 11, 11))
    _, m = preview(world, g, s)
    assert [(x["before"], x["after"], x["basis_text"]) for x in m["recalc"]["lines"]] == [(300_000, 200_000, "пересчёт по день ухода · 4 урока из 12")]


def test_a_future_lesson_cancelled_by_the_centre_vanishes_after_the_leaving_and_the_total_is_read_from_the_account(world, fixed_today):
    # paid 600 000; the centre cancelled Tuesday 24.11 (a deduction of 50 000 on the account); leaving on 11.11: charged 200 000, the cancelled lesson is outside the stay -> no deduction, advance 600 000 - 200 000 = 400 000
    g, s = om(world, fixed_today)
    assert owner(world).put(f"{API}/org/settings/payments", json={"centre_cancel_recalc": True}).status_code == 200
    pay(world, s, g, amount=600_000, paid_on=D(2026, 11, 3))
    r = owner(world).post(f"{API}/org/groups/{g.id}/attendance", json={"lessonDate": "2026-11-24", "lessonId": "s2", "status": "cancelled", "records": {}, "cancellationReason": "Преподаватель заболел", "cancellationKind": "centre"})
    assert r.status_code == 200, r.text
    fixed_today(D(2026, 11, 11))
    _, m = preview(world, g, s)
    # R4: the deduction takes its place INSIDE November (money 550 000 + deduction 50 000) and the 50 000 of the payment it pushed out lie on December as money: the closing sees December paid ahead and that it will not
    # begin - one more line (before R4 the 50 000 were a "deduction" credit on December, not money: no line).  The total below is the same: 550 000 - 200 000 + 50 000 = 400 000 advance.
    assert [x["text"] for x in m["recalc"]["lines"]] == [f"Ноябрь: {N(550_000)} → {M(200_000)}", f"Декабрь не начнётся: {M(50_000)} останутся авансом"]      # Б11.21 п. 4: the NET cost before the closing: 600 000 - the deduction 50 000 (was «600 000 → …»)
    assert stays(m)["advance"]["text"] == "Останется аванс " + M(400_000) and m["money"]["advance"] == 400_000       # the total comes from the account after the closing, not from the lines


def test_b11_21_p4_the_line_of_the_month_is_the_net_cost_and_a_quiet_note_names_the_recalculation(world, fixed_today):
    # Б11.21 п. 4.  paid 1 100 000 (03.11); the centre cancelled Tuesday 24.11 (a deduction of 50 000); leaving on 26.11 (lessons 3 5 6 10 12 13 17 19 20 24 26 = 11 x 50 000 = 550 000).  The NET cost of November: before the
    # closing 600 000 - 50 000 = 550 000, after it 550 000 - 50 000 = 500 000 (what the debt and the advance are made of: advance = 1 100 000 - 500 000 = 600 000); the line says «550 000 → 500 000 сум» and under it, quietly,
    # «с учётом перерасчёта за отменённые уроки: −50 000 сум» (it said «600 000 → 550 000»: the deduction was not seen and the lines did not meet the total).
    g, s = om(world, fixed_today)
    assert owner(world).put(f"{API}/org/settings/payments", json={"centre_cancel_recalc": True}).status_code == 200
    pay(world, s, g, amount=1_100_000, paid_on=D(2026, 11, 3))
    r = owner(world).post(f"{API}/org/groups/{g.id}/attendance", json={"lessonDate": "2026-11-24", "lessonId": "s2", "status": "cancelled", "records": {}, "cancellationReason": "Преподаватель заболел", "cancellationKind": "centre"})
    assert r.status_code == 200, r.text
    fixed_today(D(2026, 11, 26))
    _, m = preview(world, g, s)
    first = m["recalc"]["lines"][0]
    assert (first["text"], first["before"], first["after"]) == (f"Ноябрь: {N(550_000)} → {M(500_000)}", 550_000, 500_000), first
    assert first["basis_text"].endswith(f"с учётом перерасчёта за отменённые уроки: −{M(50_000)}"), first["basis_text"]
    assert m["money"]["advance"] == 1_100_000 - 500_000
    done = leave(world, g, s)["recalc"]["lines"][0]
    assert (done["before"], done["after"]) == (550_000, 500_000) and done["basis_text"] == first["basis_text"]
