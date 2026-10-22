"""INCOME-2 («Поступления» v2) on PostgreSQL: the overview with the comparison, the month collection, the payments journal and its Excel.

The figures of ``income2_world`` are hand-calculated (see tests/income2_helpers.py); the identities hold for any data (random test below)."""
import io
import random
from datetime import datetime, timedelta

import pytest
from openpyxl import load_workbook

from src.models.auth import StudentGroup, StudentPayment, StudentRefund, StudentWriteoff

from tests.income2_helpers import income2_world, uid
from tests.pg_world import give_group_price, make_client, make_group, make_student, needs_pg, pg_session, world  # noqa: F401
from tests.test_payment_carry_over_pg import engine  # noqa: F401
from tests.test_pay2_api_pg import API, D, TODAY, fixed_today, membership, owner, seed_payment, settings_block, stay  # noqa: F401
from tests.test_pay2_perf_concurrency_pg import count_statements, grow

pytestmark = needs_pg


def income(w, first, last, client=None, expect=200, **params):
    r = (client or owner(w)).get(f"{API}/org/payments/income", params={"from": first, "to": last, **params})
    assert r.status_code == expect, r.text
    return r.json()


def journal(w, first="2026-10-01", last="2026-10-31", client=None, expect=200, **params):
    r = (client or owner(w)).get(f"{API}/org/payments/journal", params={"from": first, "to": last, **params})
    assert r.status_code == expect, r.text
    return r.json()


def receipts(w, first, last, **params):
    r = owner(w).get(f"{API}/org/payments/receipts", params={"from": first, "to": last, "group_by": "month", **params})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def seeded(world, fixed_today):
    return income2_world(world)


# ---- the overview: hand-calculated figures ----------------------------------------------------------------------


def test_october_overview_hand_calculated(world, seeded):
    o = income(world, "2026-10-01", "2026-10-31")
    assert o["period"] == {"kind": "month", "from": "2026-10-01", "to": "2026-10-31", "label": "октябрь", "days": 31, "in_progress": True}
    assert o["previous_period"]["label"] == "1–21 сентября" and o["previous_period"]["to"] == "2026-09-21"
    cur = o["current"]
    # 500+200+100+1250+900+150+350 (the voided 777 and the replaced 400 are not money)
    assert (cur["received"], cur["payments_count"]) == (3_450_000, 7)
    assert (cur["to_past_debt"], cur["to_current"], cur["to_advance"]) == (1_000_000, 1_900_000, 450_000)
    assert (cur["refunded"], cur["refunded_from_received"], cur["transferred"], cur["written_off"], cur["net_received"]) == (
        100_000, 100_000, 0, 100_000, 3_350_000)
    assert cur["to_forward"] == 0 and cur["to_past_debt"] + cur["to_current"] + cur["to_advance"] + cur["refunded_from_received"] == cur["received"]
    assert [(x["kind"], x["label"], x["amount"]) for x in o["strip"]] == [
        ("old_debt", "на старые долги", 1_000_000), ("current", "за текущий период", 1_900_000), ("forward", "вперёд (за будущие периоды)", 0),
        ("advance", "пока не потрачено (аванс)", 450_000), ("refunded", "возвращено", 100_000), ("transferred", "перенесено в другую группу", 0)]
    assert o["previous_label"] == "те же дни сентября" and o["previous_hint"] == "1–21 сентября"
    assert [c["code"] for c in o["checks"] if c["code"].startswith("strip_")] == [
        "strip_old_debt_eq_journal", "strip_current_eq_journal", "strip_advance_eq_journal", "strip_refunded_eq_journal"]
    prev = o["previous"]
    assert (prev["received"], prev["payments_count"], prev["to_current"], prev["to_past_debt"]) == (1_500_000, 4, 1_500_000, 0)
    assert (o["delta_amount"], o["delta_pct"]) == (1_950_000, 130)
    assert o["summary_text"] == "За октябрь (по 21-е) пришло 3 450 000 сум — на 130% больше, чем за те же дни сентября. Собрано 76% стоимости месяца."
    assert [(m["method"], m["amount"], m["count"]) for m in o["by_method"]] == [
        ("click", 1_250_000, 1), ("cash", 950_000, 4), ("bank_transfer", 900_000, 1), ("card", 350_000, 1)]
    assert [m["label"] for m in o["by_method"]] == ["Click", "Наличные", "Перевод", "Карта"]
    assert o["position"] == {"debt_total": 500_000, "past_debt_total": 0, "advance_total": 450_000, "memberships_with_debt": 3,
                             "students_with_debt": 3, "memberships_with_advance": 2, "students_with_advance": 2}
    assert (o["delta_direction"], o["delta_text"]) == ("up", "130% к 1–21 сентября")
    assert o["cash"] is None and o["ok"] and all(c["diff"] == 0 for c in o["checks"])


def test_collection_hand_calculated_and_identities(world, seeded):
    col = income(world, "2026-10-01", "2026-10-31")["collection"]
    assert (col["month"], col["month_label"], col["from"], col["to"]) == ("2026-10", "октябрь 2026", "2026-10-01", "2026-10-31")
    rows = {r["group_name"]: r for r in col["groups"]}
    assert [r["group_name"] for r in col["groups"]] == ["Математика 9А", "Английский 7Б", "Физика 10В"]      # worst first
    a, b, c = rows["Математика 9А"], rows["Английский 7Б"], rows["Физика 10В"]
    assert (a["charged"], a["paid"], a["forgiven"], a["debt"], a["students"], a["students_with_debt"], a["pct"]) == (
        1_500_000, 1_100_000, 100_000, 300_000, 3, 1, 73)
    assert (b["charged"], b["paid"], b["forgiven"], b["debt"], b["students_with_debt"], b["pct"]) == (600_000, 450_000, 0, 150_000, 1, 75)
    assert (c["charged"], c["paid"], c["debt"], c["pct"]) == (400_000, 350_000, 50_000, 87)
    assert (a["teacher_name"], b["teacher_name"]) == ("Пётр Иванов", "Мария Ким")
    t = col["total"]
    assert col["text"] == "Оплачено 1 900 000 из 2 500 000 сум (76%)"
    assert (t["charged"], t["paid"], t["forgiven"], t["debt"], t["students"], t["students_with_debt"], t["pct"]) == (
        2_500_000, 1_900_000, 100_000, 500_000, 6, 3, 76)
    for key in ("charged", "paid", "forgiven", "debt", "students", "students_with_debt"):
        assert t[key] == sum(r[key] for r in col["groups"]), key
    for r in [t, *col["groups"]]:
        assert r["paid"] + r["forgiven"] + r["debt"] == r["charged"]


def test_collection_equals_the_statements_of_every_membership(world, seeded):
    """Independent path: the charge rows of each membership's statement whose period begins in the month."""
    col = income(world, "2026-10-01", "2026-10-31")["collection"]
    seen = {}
    for key, m in seeded["memberships"].items():
        st = owner(world).get(f"{API}/org/memberships/{m.id}/statement").json()
        for row in st["rows"]:
            seg = row["details"]["segment"]
            if row["type"] == "charge" and "2026-10-01" <= seg["period_from"] <= "2026-10-31":
                g = seen.setdefault(st["membership"]["group_name"], dict(charged=0, paid=0, forgiven=0, debt=0))
                g["charged"] += seg["charge"]
                g["paid"] += seg["paid"]
                g["forgiven"] += seg["written_off"]
                g["debt"] += seg["remaining"]
    assert seen == {r["group_name"]: {k: r[k] for k in ("charged", "paid", "forgiven", "debt")} for r in col["groups"]}


def test_a_closed_month_is_collected_by_later_payments_and_advances(world, seeded):
    """September was paid partly in October (late money, an advance of September): the share is as of today."""
    o = income(world, "2026-09-01", "2026-09-30")
    assert o["period"]["in_progress"] is False and o["previous_period"]["label"] == "август"
    assert o["current"]["received"] == 1_500_000 and o["previous"]["received"] == 0 and o["delta_pct"] is None
    t = o["collection"]["total"]
    assert (t["charged"], t["paid"], t["debt"], t["pct"], t["students_with_debt"]) == (2_500_000, 2_500_000, 0, 100, 0)
    assert o["summary_text"] == "За сентябрь пришло 1 500 000 сум. Собрано 100% стоимости месяца."
    assert income(world, "2026-08-01", "2026-08-31")["collection"]["groups"] == []        # the accounting starts on 01.09: nothing charged


def test_overview_agrees_with_the_old_receipts_report(world, seeded):
    for first, last in (("2026-10-01", "2026-10-31"), ("2026-09-01", "2026-09-30"), ("2026-10-05", "2026-10-15")):
        old = receipts(world, first, last)["totals"]
        new = income(world, first, last)["current"]
        for key in ("received", "payments_count", "refunded", "refunded_from_received", "to_past_debt", "to_current", "to_advance",
                    "written_off", "net_received"):
            assert new[key] == old[key], (first, key)
        assert new["transferred"] == old["transferred_from_received"]


def test_filters_follow_the_receipts_report(world, seeded):
    gb, ga = seeded["groups"]["B"], seeded["groups"]["A"]
    for params in ({"group_id": str(gb.id)}, {"teacher_id": str(world.teacher.id)}, {"method": "cash"},
                   {"group_id": str(ga.id), "method": "cash"}):
        old = receipts(world, "2026-10-01", "2026-10-31", **params)
        new = income(world, "2026-10-01", "2026-10-31", **params)
        assert new["current"]["received"] == old["totals"]["received"], params
        assert new["current"]["to_current"] == old["totals"]["to_current"], params
        assert {k: new["position"][k] for k in old["position"]} == old["position"], params
        assert new["ok"], params
    only_b = income(world, "2026-10-01", "2026-10-31", group_id=str(gb.id))
    assert only_b["current"]["received"] == 1_050_000 and [g["group_name"] for g in only_b["collection"]["groups"]] == ["Английский 7Б"]
    by_teacher = income(world, "2026-10-01", "2026-10-31", teacher_id=str(world.teacher2.id))
    assert by_teacher["collection"]["total"]["charged"] == 600_000


# ---- periods ---------------------------------------------------------------------------------------------------


def test_day_today_with_the_cash_hint(world, seeded):
    o = income(world, "2026-10-21", "2026-10-21")
    assert o["period"]["kind"] == "day" and o["period"]["label"] == "сегодня" and o["previous_period"]["label"] == "вчера"
    assert (o["current"]["received"], o["current"]["payments_count"]) == (250_000, 2)       # a2 100 000 + b2 150 000, both cash
    assert o["previous"]["received"] == 0                                          # 20.10: only a voided payment
    assert o["summary_text"] == "Сегодня пришло 250 000 сум." and o["delta_direction"] is None and o["delta_text"] is None
    assert o["collection"] is None and [d["bucket"] for d in o["series"]] == ["2026-10-21"]
    assert o["cash"] == {
        "date": "2026-10-21", "label": "Наличные в кассе за сегодня", "cash_received": 250_000, "cash_refunded": 100_000,
        "expected": 150_000, "payments_count": 2,
        "text": "Наличные в кассе за сегодня: 150 000 сум (принято 250 000 сум, возвращено 100 000 сум)",
        "scope": "centre", "filtered_view": False, "scope_text": None}
    # R3 #7: the cash hint is the physical till of the WHOLE centre: the method, group and teacher filters never narrow it
    card = income(world, "2026-10-21", "2026-10-21", method="card")
    assert card["current"]["received"] == 0 and card["cash"]["expected"] == 150_000 and card["cash"]["filtered_view"] is False
    ga, gb = seeded["groups"]["A"], seeded["groups"]["B"]
    only_a = income(world, "2026-10-21", "2026-10-21", group_id=str(ga.id))
    assert only_a["current"]["received"] == 100_000                                       # the page itself IS filtered ...
    assert (only_a["cash"]["cash_received"], only_a["cash"]["cash_refunded"], only_a["cash"]["expected"], only_a["cash"]["payments_count"]) == (
        250_000, 100_000, 150_000, 2)                                                     # ... the till is not
    assert only_a["cash"]["scope"] == "centre" and only_a["cash"]["filtered_view"] is True
    assert only_a["cash"]["scope_text"] == "в кассе центра за сегодня (все группы)"
    assert only_a["cash"]["text"] == o["cash"]["text"] and only_a["cash"]["label"] == o["cash"]["label"]
    only_b = income(world, "2026-10-21", "2026-10-21", group_id=str(gb.id), method="card")
    assert only_b["current"]["received"] == 0 and only_b["cash"]["expected"] == 150_000 and only_b["cash"]["filtered_view"] is True
    by_teacher = income(world, "2026-10-21", "2026-10-21", teacher_id=str(world.teacher2.id))
    assert by_teacher["cash"]["expected"] == 150_000 and by_teacher["cash"]["filtered_view"] is True and by_teacher["cash"]["cash_received"] == 250_000
    past_filtered = income(world, "2026-10-15", "2026-10-15", group_id=str(ga.id))
    assert past_filtered["cash"]["scope_text"] == "в кассе центра за 15 октября (все группы)"
    past = income(world, "2026-10-15", "2026-10-15")
    assert past["cash"]["label"] == "Наличные в кассе за 15 октября" and past["cash"]["expected"] == 0 and past["cash"]["text"].endswith("0 сум")


def test_week_and_custom_ranges(world, seeded):
    o = income(world, "2026-10-12", "2026-10-18")
    assert o["period"]["kind"] == "week" and o["previous_period"]["from"] == "2026-10-05" and o["previous_period"]["to"] == "2026-10-11"
    assert o["current"]["received"] == 550_000 and o["previous"]["received"] == 1_750_000     # 200+350 vs 500+1250
    assert o["delta_pct"] == -69 and (o["delta_direction"], o["delta_text"]) == ("down", "69% к прошлой неделе")
    assert o["summary_text"] == "За неделю 12–18 октября пришло 550 000 сум — на 69% меньше, чем за предыдущую неделю."
    assert o["cash"] is None and o["collection"] is None and len(o["series"]) == 7
    c = income(world, "2026-10-02", "2026-10-08")                                        # a Friday to a Thursday: custom, 7 days
    assert c["period"]["kind"] == "custom" and c["previous_period"]["from"] == "2026-09-25" and c["period"]["label"] == "период 02.10–08.10"
    assert c["current"]["received"] == 900_000 + 500_000 + 1_250_000 and c["previous"]["received"] == 0
    assert c["summary_text"] == "За период 02.10–08.10 пришло 2 650 000 сум." and c["previous_label"] == "25 сент. – 1 окт."
    forced = income(world, "2026-10-01", "2026-10-21", period="month")
    assert forced["period"]["kind"] == "month" and forced["previous_period"]["to"] == "2026-09-21" and forced["collection"] is not None
    assert forced["current"]["received"] == 3_450_000


def test_series_has_every_day_and_long_ranges_go_by_month(world, seeded):
    o = income(world, "2026-10-01", "2026-10-31")
    assert o["series_by"] == "day" and [d["bucket"] for d in o["series"]] == [f"2026-10-{i:02d}" for i in range(1, 32)]
    by_day = {d["bucket"]: (d["received"], d["count"]) for d in o["series"]}
    assert by_day["2026-10-21"] == (250_000, 2) and by_day["2026-10-02"] == (900_000, 1) and by_day["2026-10-03"] == (0, 0)
    assert sum(v[0] for v in by_day.values()) == o["current"]["received"] and sum(v[1] for v in by_day.values()) == 7
    long = income(world, "2026-07-01", "2026-10-31")
    assert long["series_by"] == "month" and [d["bucket"] for d in long["series"]] == ["2026-07", "2026-08", "2026-09", "2026-10"]
    assert [d["received"] for d in long["series"]] == [0, 0, 1_500_000, 3_450_000] and long["ok"]


def test_period_errors(world, seeded):
    assert income(world, "2025-10-01", "2026-10-21", expect=422)["detail"]["code"] == "RANGE_TOO_LONG"
    assert income(world, "2026-10-05", "2026-10-04", expect=422)["detail"] == {"code": "INVALID_PERIOD", "message": "Конец периода раньше начала"}
    assert journal(world, "2026-10-05", "2026-10-04", expect=422)["detail"]["code"] == "INVALID_PERIOD"
    assert journal(world, "2025-10-01", "2026-10-21", expect=422)["detail"]["code"] == "RANGE_TOO_LONG"
    assert income(world, "2026-10-01", "2026-10-02", expect=422, period="day")["detail"]["code"] == "INVALID_PERIOD"
    assert income(world, "2026-10-02", "2026-10-31", expect=422, period="month")["detail"]["code"] == "INVALID_PERIOD"
    journal(world, expect=200, size=100)
    journal(world, expect=422, size=101)
    journal(world, expect=422, page=0)


def test_transfers_are_counted_apart(world, fixed_today):
    """A student moved to another group takes his money along: the part of the payment that travelled is «перенесено»."""
    db = world.db
    settings_block(world, accounting_start_date="2026-09-01", billing_anchor="fixed_day", billing_day=1)
    group_b = make_group(db, world.org, world.teacher2, "Группа B")
    group_b.schedule = world.group.schedule = []
    give_group_price(db, world.group, 500_000, valid_from=D(2026, 1, 1))
    give_group_price(db, group_b, 400_000, valid_from=D(2026, 1, 1))
    db.commit()
    a = membership(world)
    stay(world, a, D(2026, 10, 1))
    seed_payment(world, a, 800_000, D(2026, 10, 1), receipt_no=1)
    world.svc.add_student_to_group(str(world.org.id), str(group_b.id), str(world.students[0].id), mode="transfer",
                                   actor_id=str(world.owner.id), payments_detail=True)
    old = receipts(world, "2026-10-01", "2026-10-31")["totals"]
    o = income(world, "2026-10-01", "2026-10-31")
    assert old["transferred_from_received"] > 0
    assert o["current"]["transferred"] == old["transferred_from_received"] and o["current"]["received"] == 800_000
    cur = o["current"]
    assert cur["to_past_debt"] + cur["to_current"] + cur["to_forward"] + cur["to_advance"] + cur["refunded_from_received"] + cur["transferred"] == 800_000
    assert o["ok"]
    row = journal(world)["rows"][0]
    assert row["transferred"] == cur["transferred"] and f"перенесено в другую группу — {tx_num(cur['transferred'])} сум" in row["allocation_text"]


def tx_num(n):
    return f"{n:,}".replace(",", " ")


def test_lessons_mode_collects_by_the_package_that_began_in_the_month(world, fixed_today):
    """Lessons packages: a package «begins» with its first held lesson; the cost of a package that has held lessons is charged then."""
    from src.models.auth import GroupAttendance

    settings_block(world, period_mode="lessons", lessons_per_payment=12, accounting_start_date="2026-09-01",
                   billing_history=[{"from": "min", "period_mode": "lessons", "lessons_per_payment": 12}])
    give_group_price(world.db, world.group, 600_000, valid_from=D(2026, 1, 1))
    m = membership(world)
    stay(world, m, D(2026, 9, 25))
    s1 = world.students[0]
    for d in (D(2026, 9, 25), D(2026, 9, 28), D(2026, 9, 30), D(2026, 10, 2), D(2026, 10, 5), D(2026, 10, 7), D(2026, 10, 9),
              D(2026, 10, 12), D(2026, 10, 14), D(2026, 10, 16), D(2026, 10, 19), D(2026, 10, 21)):
        world.db.add(GroupAttendance(org_id=world.org.id, group_id=world.group.id, lesson_date=d.isoformat(), lesson_id=f"s{d.isoweekday()}",
                                     status="completed", records={str(s1.id): "present"}, roster_snapshot=[{"student_id": str(s1.id)}]))
    world.db.commit()
    seed_payment(world, m, 300_000, D(2026, 10, 1), receipt_no=1)
    sept = income(world, "2026-09-01", "2026-09-30")["collection"]
    assert [(r["group_name"], r["charged"], r["paid"], r["debt"], r["pct"]) for r in sept["groups"]] == [("Группа", 600_000, 300_000, 300_000, 50)]
    assert (sept["total"]["students_with_debt"], sept["total"]["students"]) == (1, 1)
    octo = income(world, "2026-10-01", "2026-10-31")
    assert octo["collection"]["groups"] == [] and octo["collection"]["total"]["pct"] is None
    assert octo["collection"]["text"] == "За этот месяц стоимость не считалась"
    assert "Собрано" not in octo["summary_text"] and octo["current"]["received"] == 300_000 and octo["ok"]


def test_previous_label_is_always_there_and_human(world, seeded, fixed_today):
    running = income(world, "2026-10-01", "2026-10-31")                                  # S2: a running month is compared day for day
    assert (running["previous_label"], running["previous_hint"]) == ("те же дни сентября", "1–21 сентября")
    assert income(world, "2026-10-21", "2026-10-21")["previous_label"] == "вчера"
    assert income(world, "2026-10-15", "2026-10-15")["previous_label"] == "14 окт."
    assert income(world, "2026-10-12", "2026-10-18")["previous_label"] == "5–11 окт."
    assert income(world, "2026-10-02", "2026-10-08")["previous_label"] == "25 сент. – 1 окт."
    sept = income(world, "2026-09-01", "2026-09-30")
    assert sept["previous"]["received"] == 0 and sept["previous_label"] == "август"            # there was nothing in August: the label is still there
    assert income(world, "2026-08-01", "2026-08-31")["previous_label"] == "июль"
    assert income(world, "2026-01-01", "2026-01-31")["previous_label"] == "декабрь 2025"
    fixed_today(D(2026, 10, 25))                                                               # Sunday: the week 19-25.10 is over, the one before is «прошлая»
    assert income(world, "2026-10-19", "2026-10-25")["previous_label"] == "прошлая неделя"


def test_method_unknown_is_the_same_everywhere(world, seeded):
    """«Способ не указан» (method=unknown, also a missing method): Σ journal = the till line of the overview = the filtered overview = the file."""
    from sqlalchemy.dialects import postgresql

    from src.auth.payments_reports import method_clause
    from src.models.auth import StudentPayment as SP

    assert "IS NULL" in str(method_clause(SP.method, "unknown").compile(dialect=postgresql.dialect()))
    assert str(method_clause(SP.method, "cash").compile(dialect=postgresql.dialect())).count("IS NULL") == 0
    m = seeded["memberships"]["a1"]
    seed_payment(world, m, 40_000, D(2026, 10, 8), receipt_no=21, method="unknown")
    seed_payment(world, m, 60_000, D(2026, 10, 9), receipt_no=22, method="unknown")
    o = income(world, "2026-10-01", "2026-10-31")
    till = next(x for x in o["by_method"] if x["method"] == "unknown")
    assert (till["amount"], till["count"], till["label"]) == (100_000, 2, "Способ не указан")
    j = journal(world, method="unknown")
    assert (j["totals"]["count"], j["totals"]["sum"]) == (till["count"], till["amount"]) and {r["method"] for r in j["rows"]} == {"unknown"}
    only = income(world, "2026-10-01", "2026-10-31", method="unknown")
    assert only["current"]["received"] == till["amount"] and [x["method"] for x in only["by_method"]] == ["unknown"] and only["ok"]
    old = receipts(world, "2026-10-01", "2026-10-31", method="unknown")["totals"]
    assert (old["received"], old["payments_count"]) == (100_000, 2)
    r = owner(world).get(f"{API}/org/payments/journal/export", params={"from": "2026-10-01", "to": "2026-10-31", "method": "unknown"})
    rows = [[c.value for c in row] for row in load_workbook(io.BytesIO(r.content)).active.iter_rows()]
    assert rows[1][:4] == ["Оплат", 2, "На сумму", 100_000]
    assert sum(1 for row in rows if len(row) > 8 and row[8] == "Способ не указан") == 2


def test_journal_authors_ignore_created_by_but_follow_the_other_filters(world, seeded):
    """«Кто внёс» keeps all its options after one is chosen; the other filters narrow them."""
    both = {a["name"]: a["count"] for a in journal(world)["authors"]}
    assert both == {"Азиз Хакимов": 4, "Нодира Саидова": 3}
    chosen = journal(world, created_by=str(world.admin.id))
    assert {a["name"]: a["count"] for a in chosen["authors"]} == both and len(chosen["rows"]) == 4        # the rows are narrowed, the options are not
    assert {a["name"]: a["count"] for a in journal(world, method="cash")["authors"]} == {"Нодира Саидова": 3, "Азиз Хакимов": 1}
    assert {a["name"]: a["count"] for a in journal(world, method="click", created_by=str(world.admin.id))["authors"]} == {"Азиз Хакимов": 1}
    assert {a["name"]: a["count"] for a in journal(world, q="алиев")["authors"]} == {"Азиз Хакимов": 1}
    assert {a["name"]: a["count"] for a in journal(world, group_id=str(seeded["groups"]["A"].id))["authors"]} == {"Нодира Саидова": 3, "Азиз Хакимов": 1}
    assert {a["name"]: a["count"] for a in journal(world, status="voided")["authors"]} == both              # the status does not narrow them either


# ---- the journal ------------------------------------------------------------------------------------------------


def test_journal_rows_order_and_texts(world, seeded):
    j = journal(world, size=50)
    assert (j["total"], j["pages"], j["page"], j["size"]) == (9, 1, 1, 50)
    assert j["totals"] == {"count": 7, "sum": 3_450_000, "voided_count": 2}
    assert [r["receipt_no"] for r in j["rows"]] == [13, 12, 10, 11, 9, 8, 7, 6, 5]                  # date desc, then time desc
    by = {r["receipt_no"]: r for r in j["rows"]}
    top = j["rows"][0]
    assert (top["student_name"], top["display_id"], top["group_name"], top["teacher_name"], top["amount"], top["method"],
            top["method_label"], top["created_by_name"], top["paid_on"], top["paid_time"], top["note"]) == (
        "Алиев Тимур", "INC-005", "Английский 7Б", "Мария Ким", 150_000, "cash", "Наличные", "Азиз Хакимов", "2026-10-21", "11:40",
        "=SUM(A1)")                                                                              # 06:40 UTC = 11:40 Tashkent
    assert top["allocation_text"] == "за октябрь — 150 000 сум" and top["status"] == "active"
    assert by[7]["allocation_text"] == "за сентябрь — 500 000 сум · за октябрь — 500 000 сум · аванс — 150 000 сум · возвращено — 100 000 сум"
    assert (by[7]["refunded"], by[7]["transferred"]) == (100_000, 0)
    assert by[5]["allocation_text"] == "за сентябрь — 300 000 сум · за октябрь — 300 000 сум · аванс — 300 000 сум"
    assert by[8]["allocation_text"] == "за сентябрь — 200 000 сум"
    assert by[6]["allocation_text"] == "за октябрь — 500 000 сум"
    assert [a["amount"] for a in by[7]["allocations"]] == [500_000, 500_000, 150_000]
    # corrections: shown, grey, linked
    assert (by[9]["status"], by[9]["replaced_by_receipt_no"], by[9]["allocation_text"], by[9]["allocations"], by[9]["void_reason"]) == (
        "replaced", 11, None, [], "Исправлено")
    assert (by[11]["status"], by[11]["replaces_receipt_no"], by[11]["replaces_amount"], by[11]["allocation_text"]) == (
        "active", 9, 400_000, "за октябрь — 350 000 сум")
    assert (by[10]["status"], by[10]["void_reason"], by[10]["voided_by_name"], by[10]["replaced_by_receipt_no"]) == (
        "voided", "Ошибочный ввод", "Нодира Саидова", None)
    assert [a["name"] for a in j["authors"]] == ["Азиз Хакимов", "Нодира Саидова"] and [a["count"] for a in j["authors"]] == [4, 3]
    assert all(r["entered_at_text"] is None and r["paid_time"] for r in j["rows"])                  # every seeded payment was entered on its own day


def test_journal_pagination_walks_every_row_once(world, seeded):
    seen, sums = [], 0
    for page in (1, 2, 3):
        j = journal(world, size=4, page=page)
        assert j["total"] == 9 and j["pages"] == 3 and j["totals"]["sum"] == 3_450_000
        seen += [r["id"] for r in j["rows"]]
        sums += sum(r["amount"] for r in j["rows"] if r["status"] == "active")
    assert len(seen) == 9 and len(set(seen)) == 9 and sums == 3_450_000
    assert journal(world, size=4, page=4)["rows"] == []


def test_journal_filters_and_the_total_equals_the_period_received(world, seeded):
    ga, gb = seeded["groups"]["A"], seeded["groups"]["B"]
    cases = [
        ({}, 9), ({"method": "cash"}, 5), ({"method": "click"}, 1), ({"group_id": str(gb.id)}, 3),
        ({"teacher_id": str(world.teacher2.id)}, 3), ({"teacher_id": str(world.teacher.id)}, 6),
        ({"created_by": str(world.admin.id)}, 4), ({"created_by": str(world.owner.id)}, 5), ({"q": "Алиев"}, 2), ({"q": "inc-005"}, 2),
        ({"q": "каримова алия"}, 1), ({"q": "N13"}, 4), ({"q": "7"}, 1), ({"group_id": str(ga.id), "method": "cash"}, 3),
        ({"status": "voided"}, 2), ({"status": "active"}, 7), ({"q": "нет такого"}, 0),
    ]
    for params, rows in cases:
        j = journal(world, **params)
        assert j["total"] == len(j["rows"]) == rows, params
    for params in ({}, {"method": "cash"}, {"group_id": str(gb.id)}, {"teacher_id": str(world.teacher.id)},
                   {"group_id": str(ga.id), "method": "cash"}, {"method": "bank_transfer"}):
        j = journal(world, **params)
        r = receipts(world, "2026-10-01", "2026-10-31", **params)["totals"]
        assert (j["totals"]["count"], j["totals"]["sum"]) == (r["payments_count"], r["received"]), params
        o = income(world, "2026-10-01", "2026-10-31", **params)
        assert o["current"]["received"] == j["totals"]["sum"] and o["ok"], params
    # the status filter does not change the totals (they are the active money of the set)
    assert journal(world, status="voided")["totals"] == {"count": 7, "sum": 3_450_000, "voided_count": 2}
    only_day = journal(world, "2026-10-21", "2026-10-21")
    assert [r["receipt_no"] for r in only_day["rows"]] == [13, 12] and only_day["totals"]["sum"] == 250_000
    assert journal(world, "2026-09-01", "2026-09-30")["totals"] == {"count": 4, "sum": 1_500_000, "voided_count": 0}


def test_journal_is_limited_to_the_callers_organisation_and_to_money_access(world, seeded):
    from src.models.auth import Organization

    for path in ("income", "journal", "journal/export"):
        for user in (world.teacher, world.teacher2, world.admin):          # an admin has no right to payments until the owner gives it
            r = make_client(world, user=user).get(f"{API}/org/payments/{path}", params={"from": "2026-10-01", "to": "2026-10-31"})
            assert r.status_code == 403, (path, user.role, r.status_code)
    settings_block(world, admin_can_manage=True)
    admin = make_client(world, user=world.admin)
    assert journal(world, client=admin)["totals"]["sum"] == 3_450_000
    assert income(world, "2026-10-01", "2026-10-31", client=admin)["current"]["received"] == 3_450_000
    world.db.query(Organization).filter_by(id=world.other_org.id).update({"settings": {"payments": {"admin_can_manage": True}}})
    world.db.commit()
    foreign = make_client(world, user=world.foreign_admin, org="other")
    assert journal(world, client=foreign)["rows"] == [] and journal(world, client=foreign)["totals"]["sum"] == 0
    assert journal(world, client=foreign, group_id=str(seeded["groups"]["A"].id))["rows"] == []
    assert journal(world, client=foreign, created_by=str(world.owner.id))["authors"] == []
    o = income(world, "2026-10-01", "2026-10-31", client=foreign)
    assert o["current"]["received"] == 0 and o["collection"]["groups"] == [] and o["position"]["debt_total"] == 0
    assert o["cash"] is None and o["summary_text"].startswith("За октябрь (по 21-е) оплат не было")
    r = foreign.get(f"{API}/org/payments/journal/export", params={"from": "2026-10-01", "to": "2026-10-31"})
    assert r.status_code == 200 and len([row for row in load_workbook(io.BytesIO(r.content)).active.iter_rows()]) <= 4


# ---- Excel ------------------------------------------------------------------------------------------------------


def test_journal_excel_is_complete_and_formula_safe(world, seeded):
    r = owner(world).get(f"{API}/org/payments/journal/export", params={"from": "2026-10-01", "to": "2026-10-31"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert 'filename="payments-2026-10-01-2026-10-31.xlsx"' in r.headers["content-disposition"]
    ws = load_workbook(io.BytesIO(r.content)).active
    rows = [[c.value for c in row] for row in ws.iter_rows()]
    assert rows[0][:2] == ["Журнал оплат", "с 01.10.2026 по 31.10.2026"]
    assert rows[1][:4] == ["Оплат", 7, "На сумму", 3_450_000]
    head = rows.index(["№ квитанции", "Дата", "Время", "Ученик", "ID ученика", "Группа", "Преподаватель", "Сумма", "Способ", "Кто внёс",
                       "Куда пошли", "Статус", "Примечание", "Внесена"])
    body = rows[head + 1:]
    assert len(body) == 9 and [r[0] for r in body] == [13, 12, 10, 11, 9, 8, 7, 6, 5]
    assert body[0][1:] == ["21.10.2026", "11:40", "Алиев Тимур", "INC-005", "Английский 7Б", "Мария Ким", 150_000, "Наличные",
                           "Азиз Хакимов", "за октябрь — 150 000 сум", "Действует", "=SUM(A1)", None]       # an empty cell reads back as None
    assert body[2][11] == "Отменена" and body[3][11] == "Действует (исправляет № 9)" and body[4][11] == "Исправлена (№ 11)"
    cells = [c for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=")]
    assert cells and all(c.data_type == "s" for c in cells)                       # the note starts with "=": text, never a formula
    csv = owner(world).get(f"{API}/org/payments/journal/export", params={"from": "2026-10-01", "to": "2026-10-31", "format": "csv"})
    assert "'=SUM(A1)" in csv.text and not any(p.strip().startswith("=") for line in csv.text.splitlines() for p in line.split(";"))
    filtered = owner(world).get(f"{API}/org/payments/journal/export", params={"from": "2026-10-01", "to": "2026-10-31", "method": "click"})
    ws = load_workbook(io.BytesIO(filtered.content)).active
    assert [c.value for c in ws[2]][:4] == ["Оплат", 1, "На сумму", 1_250_000]


# ---- the budget of queries --------------------------------------------------------------------------------------


def test_queries_do_not_grow_with_the_number_of_payments_and_memberships(world, fixed_today):
    settings_block(world, accounting_start_date="2026-09-01", billing_anchor="fixed_day", billing_day=1)
    give_group_price(world.db, world.group, 500_000)
    world.group.schedule = []
    world.db.commit()
    c = owner(world)
    probes = {
        "income_month": lambda: c.get(f"{API}/org/payments/income", params={"from": "2026-10-01", "to": "2026-10-31"}).json(),
        "income_day": lambda: c.get(f"{API}/org/payments/income", params={"from": "2026-10-21", "to": "2026-10-21"}).json(),
        "income_filtered": lambda: c.get(f"{API}/org/payments/income", params={"from": "2026-09-01", "to": "2026-10-31", "method": "cash"}).json(),
        "journal": lambda: c.get(f"{API}/org/payments/journal", params={"from": "2026-09-01", "to": "2026-10-31", "size": 100}).json(),
        "journal_q": lambda: c.get(f"{API}/org/payments/journal", params={"from": "2026-09-01", "to": "2026-10-31", "q": "Имя", "size": 100}).json(),
        "export": lambda: c.get(f"{API}/org/payments/journal/export", params={"from": "2026-09-01", "to": "2026-10-31"}).content,
    }
    grow(world, 12)
    small = {name: count_statements(world, fn)[1] for name, fn in probes.items()}
    grow(world, 190, start=12)
    big = {name: count_statements(world, fn)[1] for name, fn in probes.items()}
    print("QUERIES small/big:", small, big)
    assert all(n <= 32 for n in big.values()), big
    for name in big:
        assert big[name] <= small[name] + 2, (name, small, big)
    full = c.get(f"{API}/org/payments/journal", params={"from": "2026-09-01", "to": "2026-10-31", "size": 100}).json()
    assert full["total"] >= 200 and len(full["rows"]) == 100 and full["totals"]["count"] == full["total"]


# ---- identities on random data ----------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
def test_identities_hold_on_random_data(world, fixed_today, seed):
    rnd = random.Random(seed)
    db = world.db
    settings_block(world, accounting_start_date="2026-08-01", billing_anchor=rnd.choice(["fixed_day", "payment_date"]), billing_day=rnd.choice([1, 5, 15]))
    groups = [world.group] + [make_group(db, world.org, rnd.choice([world.teacher, world.teacher2]), f"Г{i}") for i in range(3)]
    for g in groups:
        g.schedule = []
    db.commit()
    for g in groups:
        give_group_price(db, g, rnd.choice([200_000, 350_000, 500_000]), valid_from=D(2026, 1, 1))
    members = []
    for i in range(14):
        g = rnd.choice(groups)
        s = make_student(db, world.org, g, f"Имя{i}", f"Фам{i}", f"R-{seed}-{i:02d}")
        db.commit()
        m = membership(world, s, g)
        first = D(2026, 8, 1) + timedelta(days=rnd.randint(0, 70))
        stay(world, m, first, None if rnd.random() < 0.8 else first + timedelta(days=rnd.randint(10, 40)))
        members.append(m)
    receipt = 0
    for m in members:
        for _ in range(rnd.randint(0, 4)):
            receipt += 1
            day = D(2026, 8, 1) + timedelta(days=rnd.randint(0, 80))
            p = seed_payment(world, m, rnd.choice([50_000, 120_000, 200_000, 350_000, 500_000, 900_000]), day, receipt_no=receipt,
                             method=rnd.choice(["cash", "card", "click", "bank_transfer", "payme", "other", "unknown"]))
            if rnd.random() < 0.12:
                p.voided_at, p.voided_by, p.void_reason = datetime(2026, 10, 1, 8), world.owner.id, "Ошибка"
        if rnd.random() < 0.3:
            db.add(StudentWriteoff(org_id=world.org.id, membership_id=m.id, student_id=m.student_id, group_id=m.group_id,
                                   amount=rnd.choice([10_000, 50_000]), reason="льгота", effective_on=D(2026, 10, rnd.randint(1, 20)),
                                   created_by=world.owner.id))
    db.commit()
    for first, last in (("2026-10-01", "2026-10-31"), ("2026-09-01", "2026-09-30"), ("2026-08-01", "2026-10-21"), ("2026-10-10", "2026-10-21")):
        for params in ({}, {"method": "cash"}, {"group_id": str(groups[1].id)}, {"teacher_id": str(world.teacher2.id)}):
            o = income(world, first, last, **params)
            j = journal(world, first, last, size=100, **params)
            old = receipts(world, first, last, **params)["totals"]
            assert o["ok"], (seed, first, last, params, o["checks"])
            assert o["current"]["received"] == j["totals"]["sum"] == old["received"], (seed, first, last, params)
            assert j["totals"]["count"] == old["payments_count"]
            assert sum(m["amount"] for m in o["by_method"]) == o["current"]["received"]
            col = o["collection"]
            if col:
                for r in [col["total"], *col["groups"]]:
                    assert r["paid"] + r["forgiven"] + r["debt"] == r["charged"], (seed, r)
                    assert 0 <= (r["pct"] or 0) <= 100
                for key in ("charged", "paid", "forgiven", "debt"):
                    assert col["total"][key] == sum(r[key] for r in col["groups"])
                assert col["total"]["students_with_debt"] <= sum(r["students_with_debt"] for r in col["groups"])
                pcts = [r["paid"] / r["charged"] for r in col["groups"]]
                assert pcts == sorted(pcts)


# ---- R3 fixes -----------------------------------------------------------------------------------------------------


def add_payments(world, seeded, *rows):
    """``(key, paid_on, created_at UTC, amount, note)`` for the membership a1; returns ``{key: payment id string}``."""
    m = seeded["memberships"]["a1"]
    out = {}
    for n, (key, paid_on, created_at, amount, note) in enumerate(rows):
        p = StudentPayment(id=uid(world.org, "r3:" + key), org_id=world.org.id, membership_id=m.id, student_id=m.student_id, group_id=m.group_id, amount=amount,
                           paid_on=paid_on, method="cash", receipt_no=900 + n, created_by=world.owner.id, created_at=created_at, note=note)
        world.db.add(p)
        out[key] = str(p.id)
    world.db.commit()
    return out


def test_paid_time_is_the_entry_time_only_for_a_payment_entered_on_its_own_day(world, seeded):
    """R3 #2: a back-dated payment has no time of its own (null) and says when it was entered; the Tashkent midnight (19:30 UTC = 00:30 next day) decides, not UTC."""
    ids = add_payments(
        world, seeded,
        ("backdated", D(2026, 10, 15), datetime(2026, 10, 21, 6, 40), 10_000, None),             # entered 21.10 11:40 Tashkent for 15.10
        ("after-midnight-own-day", D(2026, 10, 21), datetime(2026, 10, 20, 19, 30), 20_000, None),   # 00:30 on 21.10 Tashkent, for 21.10
        ("after-midnight-utc-day", D(2026, 10, 20), datetime(2026, 10, 20, 19, 30), 30_000, None))   # 00:30 on 21.10 Tashkent, but for 20.10: back-dated
    rows = {r["id"]: r for r in journal(world, "2026-10-01", "2026-10-31", size=100)["rows"]}
    back = rows[ids["backdated"]]
    assert (back["paid_time"], back["entered_at_text"], back["created_at"]) == (None, "внесена 21.10 в 11:40", "2026-10-21T06:40:00")
    own = rows[ids["after-midnight-own-day"]]
    assert (own["paid_time"], own["entered_at_text"]) == ("00:30", None)
    utc_day = rows[ids["after-midnight-utc-day"]]
    assert (utc_day["paid_time"], utc_day["entered_at_text"], utc_day["created_at"]) == (None, "внесена 21.10 в 00:30", "2026-10-20T19:30:00")
    # Excel / CSV: the «Время» cell follows the same rule; «Внесена» is filled only for a back-dated payment
    r = owner(world).get(f"{API}/org/payments/journal/export", params={"from": "2026-10-01", "to": "2026-10-31"})
    sheet = [[c.value for c in row] for row in load_workbook(io.BytesIO(r.content)).active.iter_rows()]
    head = next(i for i, row in enumerate(sheet) if row[0] == "№ квитанции")
    assert sheet[head][2] == "Время" and sheet[head][13] == "Внесена"
    by_receipt = {row[0]: [c if c is not None else "" for c in row] for row in sheet[head + 1:]}      # an empty cell reads back as None
    assert (by_receipt[900][1], by_receipt[900][2], by_receipt[900][13]) == ("15.10.2026", "", "21.10 11:40")
    assert (by_receipt[901][1], by_receipt[901][2], by_receipt[901][13]) == ("21.10.2026", "00:30", "")
    assert (by_receipt[902][1], by_receipt[902][2], by_receipt[902][13]) == ("20.10.2026", "", "21.10 00:30")
    assert by_receipt[13][2] == "11:40" and by_receipt[13][13] == ""


def test_a_non_ascii_digit_in_the_search_is_text_not_a_receipt_number(world, seeded):
    """R3 #3: ``"²".isdigit()`` is True but ``int("²")`` raises - the receipt number is looked up for ASCII digits only; any other glyph is plain text (200, no rows)."""
    for q in ("²", "٣", "①", "１２"):                                                   # superscript, Arabic-Indic, circled, full-width
        j = journal(world, q=q)
        assert j["total"] == 0 and j["rows"] == [], q
    assert journal(world, q="7")["total"] == 1                                       # the receipt number still works for ASCII digits


def test_csv_cells_starting_with_tab_or_cr_are_neutralised(world, seeded):
    """R3 #4 (OWASP): not only = + - @ but a leading TAB / CR makes a spreadsheet read a formula."""
    add_payments(world, seeded, ("tab", D(2026, 10, 21), datetime(2026, 10, 21, 7, 0), 1_000, "\t=HYPERLINK(1)"),
                 ("cr", D(2026, 10, 21), datetime(2026, 10, 21, 7, 1), 2_000, "\r=HYPERLINK(2)"),
                 ("minus", D(2026, 10, 21), datetime(2026, 10, 21, 7, 2), 3_000, "-1+1"))
    r = owner(world).get(f"{API}/org/payments/journal/export", params={"from": "2026-10-21", "to": "2026-10-21", "format": "csv"})
    import csv as csv_module

    cells = [c for line in csv_module.reader(io.StringIO(r.text.lstrip("\ufeff")), delimiter=";") for c in line]
    assert "'\t=HYPERLINK(1)" in cells and "'\r=HYPERLINK(2)" in cells and "'-1+1" in cells and "'=SUM(A1)" in cells
    assert not [c for c in cells if c[:1] in ("=", "+", "-", "@", "\t", "\r")]


def test_export_too_long_cap_and_the_voided_export(world, seeded, monkeypatch):
    """R3 #10: the 20 000-row cap (monkeypatched low) answers 422 EXPORT_TOO_LONG for both formats and counts the SELECTED status; a voided-only export holds only the voided rows."""
    from src.auth import payments_income

    params = {"from": "2026-10-01", "to": "2026-10-31"}
    monkeypatch.setattr(payments_income, "MAX_EXPORT_ROWS", 3)
    for fmt in ("xlsx", "csv"):
        r = owner(world).get(f"{API}/org/payments/journal/export", params={**params, "format": fmt})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "EXPORT_TOO_LONG" and "Слишком много оплат" in r.json()["detail"]["message"], fmt
    assert owner(world).get(f"{API}/org/payments/journal/export", params={**params, "status": "active"}).status_code == 422      # 7 live rows > 3
    ok = owner(world).get(f"{API}/org/payments/journal/export", params={**params, "status": "voided"})                       # 2 voided rows <= 3
    assert ok.status_code == 200
    monkeypatch.setattr(payments_income, "MAX_EXPORT_ROWS", 2)
    assert owner(world).get(f"{API}/org/payments/journal/export", params={**params, "status": "voided"}).status_code == 200       # exactly the cap passes
    monkeypatch.setattr(payments_income, "MAX_EXPORT_ROWS", 1)
    assert owner(world).get(f"{API}/org/payments/journal/export", params={**params, "status": "voided"}).status_code == 422
    monkeypatch.setattr(payments_income, "MAX_EXPORT_ROWS", 20_000)
    voided = journal(world, status="voided", size=100)
    assert voided["total"] == 2 and sorted(r["receipt_no"] for r in voided["rows"]) == [9, 10]
    for fmt in ("xlsx", "csv"):
        r = owner(world).get(f"{API}/org/payments/journal/export", params={**params, "status": "voided", "format": fmt})
        assert r.status_code == 200
        if fmt == "xlsx":
            sheet = [[c.value for c in row] for row in load_workbook(io.BytesIO(r.content)).active.iter_rows()]
        else:
            import csv as csv_module

            sheet = [line for line in csv_module.reader(io.StringIO(r.text.lstrip("\ufeff")), delimiter=";")]
        head = next(i for i, row in enumerate(sheet) if row and row[0] == "№ квитанции")
        body = [row for row in sheet[head + 1:] if row]
        assert sorted(str(row[0]) for row in body) == ["10", "9"], fmt
        assert {str(row[0]): row[11] for row in body} == {"9": "Исправлена (№ 11)", "10": "Отменена"}, fmt



# ---- S1: «Куда ушли деньги» (the current allocation, each piece relative to the payment's own date) -------------------------


def split_identities(world, first, last, **filters):
    """The strip sums to ``received``; the rows of the SAME filters (their ``split``) sum to the strip kind by kind; every row sums to its amount (allocations +
    refunded + transferred); the advance part never exceeds the position's advance."""
    o = income(world, first, last, **filters)
    strip = {x["kind"]: x["amount"] for x in o["strip"]}
    assert list(strip) == ["old_debt", "current", "forward", "advance", "refunded", "transferred"]
    cur = o["current"]
    assert strip == {"old_debt": cur["to_past_debt"], "current": cur["to_current"], "forward": cur["to_forward"], "advance": cur["to_advance"],
                     "refunded": cur["refunded_from_received"], "transferred": cur["transferred"]}
    assert sum(strip.values()) == cur["received"], (filters, strip)
    rows = [r for r in journal(world, first, last, size=100, status="active", **filters)["rows"]]
    by_kind = {}
    for r in rows:
        assert sum(x["amount"] for x in r["split"]) == r["amount"], r["receipt_no"]
        assert sum(a["amount"] for a in r["allocations"]) + r["refunded"] + r["transferred"] == r["amount"], r["receipt_no"]
        for x in r["split"]:
            by_kind[x["kind"]] = by_kind.get(x["kind"], 0) + x["amount"]
    assert by_kind == {k: v for k, v in strip.items() if v}, (filters, by_kind, strip)
    assert strip["advance"] <= o["position"]["advance_total"], (filters, strip["advance"], o["position"]["advance_total"])
    return o


def test_where_the_money_went_identities(world, seeded):
    ga, gb = seeded["groups"]["A"], seeded["groups"]["B"]
    for first, last in (("2026-10-01", "2026-10-31"), ("2026-09-01", "2026-09-30"), ("2026-09-01", "2026-10-31"), ("2026-10-12", "2026-10-18"),
                        ("2026-10-21", "2026-10-21")):
        for filters in ({}, {"method": "cash"}, {"group_id": str(ga.id)}, {"group_id": str(gb.id), "method": "card"}, {"teacher_id": str(world.teacher2.id)}):
            split_identities(world, first, last, **filters)


def test_a_back_dated_payment_is_forward_once_its_period_has_begun(world, seeded, fixed_today):
    """S1: a1 has paid September and October; a payment entered on 21.10 for 13.10 pays the NOVEMBER period.  While November has not begun the money is «аванс» (not consumed);
    on 5.11 the period has begun: it is «вперёд (за будущие периоды)», NOT advance - the period starts after the payment's date but the money is spent."""
    ids = add_payments(world, seeded, ("back", D(2026, 10, 13), datetime(2026, 10, 21, 6, 0), 500_000, None))
    row = {r["id"]: r for r in journal(world, "2026-10-01", "2026-10-31", size=100)["rows"]}[ids["back"]]
    assert [(x["kind"], x["amount"]) for x in row["split"]] == [("advance", 500_000)] and row["allocation_text"] == "аванс — 500 000 сум"
    before = split_identities(world, "2026-10-01", "2026-10-31")
    assert {x["kind"]: x["amount"] for x in before["strip"]}["forward"] == 0
    fixed_today(D(2026, 11, 5))
    after = split_identities(world, "2026-10-01", "2026-10-31")
    row = {r["id"]: r for r in journal(world, "2026-10-01", "2026-10-31", size=100)["rows"]}[ids["back"]]
    assert [(x["kind"], x["label"], x["amount"]) for x in row["split"]] == [("forward", "вперёд (за будущие периоды)", 500_000)]
    assert row["allocation_text"] == "за ноябрь — 500 000 сум" and row["allocations"][0]["bucket"] == "advance"          # relative to ITS date: it is the future period
    strip = {x["kind"]: x["amount"] for x in after["strip"]}
    assert strip["forward"] >= 500_000
    split_identities(world, "2026-10-12", "2026-10-18")                                                                   # the week of the payment
    split_identities(world, "2026-09-01", "2026-11-30")


# ---- S2: a running week ---------------------------------------------------------------------------------------------


def test_a_running_week_is_worded_and_compared_day_for_day(world, seeded):
    o = income(world, "2026-10-19", "2026-10-25")                                      # Mon 19 - Sun 25, today Wed 21
    assert o["period"]["kind"] == "week" and o["period"]["in_progress"] is True
    assert (o["previous_label"], o["previous_hint"]) == ("те же дни прошлой недели", "тем же дням прошлой недели (12–14 окт.)")
    assert o["current"]["received"] == 250_000 and o["previous"]["received"] == 200_000                     # 12.10: a2 200 000
    assert o["summary_text"] == "За эту неделю (пн–ср) пришло 250 000 сум — на 25% больше, чем за те же дни прошлой недели."
    month = income(world, "2026-10-01", "2026-10-31")
    assert month["summary_text"].startswith("За октябрь (по 21-е) пришло 3 450 000 сум — на 130% больше, чем за те же дни сентября.")
    closed = income(world, "2026-09-01", "2026-09-30")
    assert closed["previous_hint"] == "августу" and income(world, "2026-10-21", "2026-10-21")["previous_hint"] == "вчерашнему дню"
    assert o["delta_text"] == "25% к тем же дням прошлой недели (12–14 окт.)"
