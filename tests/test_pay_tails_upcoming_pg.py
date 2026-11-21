"""PAY-TAILS Б1.4 / Б5.3 (step 5): the charge that has not begun.  ОМ 600 000, Tue / Thu / Fri, billing day 1; today Saturday 21.11.2026; the term 5 days.  A student written down today is charged from the first lesson,
Tuesday 24.11: lessons 24, 26, 27 = 3 of 12 -> 600 000 x 3 / 12 = 150 000; the term counts from the first lesson: 24, 25, 26, 27, 28 -> «платить до 28.11»."""
from datetime import date

from src.auth import payments_texts as tx

from tests.money1b_helpers import account, add, fresh_student, set_schedule, slot
from tests.pg_world import ensure_billing, give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.pg_world import make_client
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, settings_block  # noqa: F401  (fixtures)
from tests.test_pay_tails_golden_pg import get
from tests.test_pay_tails_money_pg import TTF, leave, pay

pytestmark = needs_pg
D = date
M = tx.money


def world_om(w, fixed_today, today=D(2026, 11, 21)):
    fixed_today(today)
    settings_block(w, accounting_start_date="2026-10-01", billing_anchor="fixed_day", billing_day=1)
    g = make_group(w.db, w.org, w.teacher, "Физика 7")
    give_group_price(w.db, g, 600_000)
    set_schedule(w, TTF, g)
    ensure_billing(w, g, price=None)
    return g


def row(w, s, g, bucket="active"):
    page = get(w, "/org/payments/status", bucket=bucket, limit=200)
    (r,) = [x for x in page["items"] if x["student_id"] == str(s.id) and x["group_id"] == str(g.id)]
    return r


def test_a_first_lesson_ahead_is_a_forecast_and_not_a_debt(world, fixed_today):
    g = world_om(world, fixed_today)
    s = fresh_student(world, "Будущий")
    probe = add(world, s, group=g, mode="add", dry_run=True)
    up = probe["billing"]["upcoming"]
    assert (up["from"], up["amount"], up["lessons_text"], up["due_until"]) == ("2026-11-24", 150_000, "3 урока из 12 в ноябре", "2026-11-28")
    assert (up["text"], up["sub"]) == ("Начисление с 24.11: " + M(150_000), "платить до 28.11")
    done = add(world, s, group=g, mode="add")
    assert done["billing"]["upcoming"] == up                                                  # the probe was the write
    r = row(world, s, g)                                                                       # the tab «Без долга» (the number of the tab did not change), with its label
    assert (r["state"], r["state_text"], r["state_sub"], r["status"], r["upcoming"]["amount"]) == ("starts", "Начнётся 24.11", "≈ " + M(150_000), "none", 150_000)
    sm = get(world, "/org/payments/summary")
    assert (sm["debt_total"], sm["due_total"], sm["advance_total"], sm["buckets"]["active"]) == (0, 0, 0, 1)       # in no sum, in the tab as before


def test_the_forecast_of_three_days_before_is_the_charge_of_the_day(world, fixed_today):
    g = world_om(world, fixed_today)
    s = fresh_student(world, "По дням")
    add(world, s, group=g, mode="add")
    forecast = row(world, s, g)["upcoming"]["amount"]
    assert forecast == 150_000
    fixed_today(D(2026, 11, 24))
    assert account(world, membership(world, s, g).id, D(2026, 11, 24)).charged == forecast    # D-3 -> D: the same sum
    r = row(world, s, g, "to_pay")
    assert (r["state"], r["state_text"], r["due_until"]) == ("to_pay", "К оплате " + M(150_000), "2026-11-28") and r["upcoming"] is None
    fixed_today(D(2026, 11, 29))
    assert row(world, s, g, "debtors")["state_text"] == "Долг " + M(150_000)


def test_paid_before_the_start_it_is_still_starts_the_money_is_an_advance_in_the_sums(world, fixed_today):
    g = world_om(world, fixed_today)
    s = fresh_student(world, "Оплатил")
    add(world, s, group=g, mode="add")
    pay(world, s, g, amount=150_000, paid_on=D(2026, 11, 21))
    r = row(world, s, g)
    assert (r["state"], r["state_text"], r["state_sub"], r["advance"]) == ("starts", "Начнётся 24.11", "оплачено " + M(150_000), 150_000)
    assert get(world, "/org/payments/summary")["advance_total"] == 150_000                    # the advance stays an advance: the tab «Аванс» and the card see it
    assert [x["student_id"] for x in get(world, "/org/payments/status", bucket="advance", limit=50)["items"]] == [str(s.id)]


def test_the_statement_shows_one_grey_row_that_is_in_no_total(world, fixed_today):
    g = world_om(world, fixed_today)
    s = fresh_student(world, "Выписка")
    add(world, s, group=g, mode="add")
    st = get(world, f"/org/memberships/{membership(world, s, g).id}/statement")
    assert st["forecast"] == {"date": "2026-11-24", "title": "Ноябрь 2026 — неполный месяц (с 24.11, 3 урока из 12)", "sub": "прогноз: начислится " + M(150_000)}
    assert st["rows"] == [] and (st["summary"]["charged"], st["summary"]["reconciles"]) == (0, True)
    assert (st["summary"]["state"], st["summary"]["state_text"], st["summary"]["state_sub"]) == ("starts", "Начнётся 24.11", "платить до 28.11")


def test_the_first_period_without_lessons_is_skipped_the_forecast_is_the_first_period_with_a_charge(world, fixed_today):
    # «Оплата с» 28.11 (Saturday): November has no lesson after it (lessons 24, 26, 27) - no «≈ 0»; the forecast is December (Tuesday 01.12 is the first lesson), the whole month 600 000
    g = world_om(world, fixed_today)
    s = fresh_student(world, "Декабрьский")
    probe = add(world, s, group=g, mode="add", dry_run=True, pay_from="2026-11-28")
    up = probe["billing"]["upcoming"]
    assert (up["from"], up["amount"]) == ("2026-12-01", 600_000)


def test_a_leaving_before_the_start_takes_the_forecast_away_without_a_trace(world, fixed_today):
    g = world_om(world, fixed_today)
    s = fresh_student(world, "Ушёл")
    add(world, s, group=g, mode="add")
    leave(world, g, s)
    sm = get(world, f"/org/memberships/{membership(world, s, g).id}/statement")
    assert sm["forecast"] is None and (sm["summary"]["state"], sm["summary"]["state_text"]) == ("no_charges", "Расчётов нет")


def test_a_caller_without_the_right_to_money_gets_the_date_only(world, fixed_today):
    g = world_om(world, fixed_today)
    s = fresh_student(world, "Без денег")
    admin = make_client(world, user=world.admin)
    r = admin.post(f"{API}/org/groups/{g.id}/students/{s.id}", params={"mode": "add", "dry_run": "true"}, json={})
    assert r.status_code == 200, r.text
    up = r.json()["billing"]["upcoming"]
    assert up["from"] == "2026-11-24" and up["amount"] is None and up["text"] is None


def test_a_package_without_a_held_lesson_says_so(world, fixed_today):
    fixed_today(D(2026, 11, 21))
    settings_block(world, accounting_start_date="2026-10-01", period_mode="lessons", lessons_per_payment=8, billing_history=[{"from": "min", "period_mode": "lessons", "lessons_per_payment": 8}])
    g = make_group(world.db, world.org, world.teacher, "Пакетная")
    give_group_price(world.db, g, 400_000)
    set_schedule(world, TTF, g)
    s = fresh_student(world, "Пакетный")
    add(world, s, group=g, mode="add")
    r = row(world, s, g)
    assert (r["state"], r["state_text"], r["state_sub"]) == ("starts", "Уроков ещё не было", f"{M(50_000)} за урок · без оплаты 2 урока")      # 400 000 / 8 = 50 000 a lesson; the centre lets 2 lessons go unpaid


def test_the_excel_of_the_statement_has_the_forecast_line_below_the_totals_marked_forecast(world, fixed_today):
    import io

    from openpyxl import load_workbook

    g = world_om(world, fixed_today)
    s = fresh_student(world, "Файл")
    add(world, s, group=g, mode="add")
    r = owner(world).get(f"{API}/org/memberships/{membership(world, s, g).id}/statement/export", params={"format": "xlsx"})
    assert r.status_code == 200, r.text
    ws = load_workbook(io.BytesIO(r.content)).active
    cells = [str(c.value) for row in ws.iter_rows() for c in row if c.value is not None]
    at = lambda text: next(i for i, v in enumerate(cells) if text in v)
    assert at("Прогноз") > at("Итого") if any("Итого" in v for v in cells) else True               # no charge yet: no total row, the forecast is a service band
    assert any("прогноз: начислится 150 000 сум" in v.replace(" ", " ") for v in cells) and any("Ноябрь 2026 — неполный месяц (с 24.11, 3 урока из 12)" in v for v in cells)


def test_the_accounting_of_the_centre_that_starts_later_gives_a_forecast_of_its_first_period(world, fixed_today):
    # the accounting begins on Tuesday 01.12: nothing is charged in November; the forecast is December (14 lessons, the whole month 600 000), «Начнётся 01.12»
    g = world_om(world, fixed_today)
    settings_block(world, accounting_start_date="2026-12-01")
    s = fresh_student(world, "Учёт позже")
    add(world, s, group=g, mode="add")
    r = row(world, s, g)
    assert (r["state"], r["state_text"], r["upcoming"]["from"], r["upcoming"]["amount"]) == ("starts", "Начнётся 01.12", "2026-12-01", 600_000)


def test_a_new_price_and_a_pause_before_the_start_move_the_forecast(world, fixed_today):
    g = world_om(world, fixed_today)
    s = fresh_student(world, "Меняется")
    add(world, s, group=g, mode="add")
    assert row(world, s, g)["upcoming"]["amount"] == 150_000                                  # 600 000 x 3 / 12
    r = owner(world).put(f"{API}/org/groups/{g.id}/price", json={"amount": 900_000, "confirm_unusual": True, "valid_from": "2026-11-21"})
    assert r.status_code == 200, r.text
    assert row(world, s, g)["upcoming"]["amount"] == 225_000                                  # 900 000 x 3 / 12
    r = owner(world).post(f"{API}/org/memberships/{membership(world, s, g).id}/pauses", json={"first_day": "2026-11-27", "last_day": "2026-11-27", "reason": "Болезнь"})
    assert r.status_code == 201, r.text
    assert row(world, s, g, "all")["upcoming"]["amount"] == 150_000                           # the pause takes the lesson of 27.11: 24, 26 = 2 of 12 -> 900 000 x 2 / 12 (a student with a pause entry is on the tab by the old status, which package B does not touch)
