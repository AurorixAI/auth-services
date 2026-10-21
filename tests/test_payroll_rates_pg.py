"""UX-2: the rates of the teachers (``payroll_rates``) - lookup by month, the rates page, payroll that reads them, the
"данные изменились" signal and the diff by a rate, the price that is no longer edited on a sheet (PostgreSQL)."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from src.auth.payroll_rates import RateBook, load_book
from src.models.auth import PayrollRate, PayrollSheet, PayrollSheetRevision

from tests.pg_world import (  # noqa: F401  (fixtures)
    business_today, give_group_price, make_client, make_group, make_member, make_user, needs_pg, pg_session, world,
)
from tests.test_payroll_pg import (  # helpers only: no test of that module is imported
    API, FEE, full_month, lesson, line_of, overview, owner, pay_calendar, patch, previous_month, preview,
    save, set_rate, teacher_row,
)

pytestmark = needs_pg


def month_before(m):
    """The calendar month before ``m`` (same shape as ``previous_month``)."""
    from types import SimpleNamespace

    last = m.first - timedelta(days=1)
    first = last.replace(day=1)
    return SimpleNamespace(first=first, last=last, label=first.strftime("%Y-%m"), d=lambda n: first.replace(day=n))


def put(w, teacher, valid_from, percents, expect=200, client=None, **extra):
    r = (client or owner(w)).put(
        f"{API}/org/payroll/rates/{teacher.id}", json={"valid_from": valid_from, "percents": percents, **extra}
    )
    assert r.status_code == expect, r.text
    return r.json()


def rates_of(w, month=None, client=None):
    r = (client or owner(w)).get(f"{API}/org/payroll/rates", params={"month": month} if month else None)
    assert r.status_code == 200, r.text
    return r.json()


def item_of(body, teacher):
    return next(i for i in body["items"] if i["teacher_id"] == str(teacher.id))


# ============================================================================================
# the lookup: the rate of a month
# ============================================================================================


def test_the_rate_of_a_month_is_the_latest_row_not_after_its_first_day(world):
    t = world.teacher
    set_rate(world, t, "group", "50", valid_from=date(2026, 8, 1))
    set_rate(world, t, "group", "45", valid_from=date(2026, 10, 1))
    set_rate(world, t, "individual", "70", valid_from=date(2026, 9, 1))
    book = load_book(world.db, world.org.id, [t.id])
    at = lambda kind, y, mo: (lambda row: row and str(row.percent))(book.at(t.id, kind, date(y, mo, 1)))  # noqa: E731
    assert at("group", 2026, 7) is None                       # before the first rate: none
    assert at("group", 2026, 8) == "50.00" and at("group", 2026, 9) == "50.00"        # the day it begins and after
    assert at("group", 2026, 10) == "45.00" and at("group", 2026, 12) == "45.00"      # the change month and after
    assert at("pair", 2026, 10) is None and at("individual", 2026, 8) is None and at("individual", 2026, 9) == "70.00"
    # the nearest FUTURE change
    assert str(book.after(t.id, "group", date(2026, 9, 1)).percent) == "45.00"
    assert book.after(t.id, "group", date(2026, 10, 1)) is None and book.after(t.id, "group", date(2026, 7, 1)).valid_from == date(2026, 8, 1)
    assert book.percents_at(t.id, date(2026, 9, 1)) == {"group": Decimal("50"), "individual": Decimal("70")}
    assert book.percents_at(world.teacher2.id, date(2026, 9, 1)) == {}
    # a month always gets the row of ITS date, whatever the order of the writes
    set_rate(world, t, "group", "40", valid_from=date(2026, 6, 1))                     # written last, valid earliest
    book = load_book(world.db, world.org.id, [t.id])
    assert at("group", 2026, 6) == "40.00" and at("group", 2026, 7) == "40.00" and at("group", 2026, 8) == "50.00"


def test_a_correction_of_the_same_month_is_a_newer_row_and_wins(world):
    t = world.teacher
    set_rate(world, t, "group", "50", valid_from=date(2026, 9, 1))
    set_rate(world, t, "group", "55", valid_from=date(2026, 9, 1))
    book = load_book(world.db, world.org.id, [t.id])
    assert str(book.at(t.id, "group", date(2026, 9, 1)).percent) == "55.00"
    # the very same created_at: a row a person wrote wins over a migrated one (created_by NULL), whatever the order
    stamp = datetime(2026, 10, 1, 12, 0, 0)
    for by, percent in ((None, "33"), (world.owner.id, "44")):
        world.db.add(PayrollRate(org_id=world.org.id, teacher_id=world.teacher2.id, lesson_type="pair", percent=Decimal(percent),
                                 valid_from=date(2026, 9, 1), created_by=by, created_at=stamp))
    world.db.commit()
    assert str(load_book(world.db, world.org.id, [world.teacher2.id]).at(world.teacher2.id, "pair", date(2026, 9, 1)).percent) == "44.00"
    assert world.db.query(PayrollRate).filter_by(teacher_id=t.id).count() == 2                # nothing was overwritten
    # two future rows for the same month: the newer one is "next"
    set_rate(world, t, "group", "30", valid_from=date(2027, 1, 1))
    set_rate(world, t, "group", "35", valid_from=date(2027, 1, 1))
    book = load_book(world.db, world.org.id, [t.id])
    assert str(book.after(t.id, "group", date(2026, 9, 1)).percent) == "35.00"
    assert RateBook([]).at(t.id, "group", date(2026, 9, 1)) is None


# ============================================================================================
# the API: GET / PUT / history
# ============================================================================================


def test_get_put_history_shapes(world):
    t, t2 = world.teacher, world.teacher2
    body = rates_of(world, "2026-09")
    assert body["month"] == "2026-09"
    ids = {i["teacher_id"] for i in body["items"]}
    assert ids == {str(t.id), str(t2.id)}                      # the teachers of the centre, nobody else
    one = item_of(body, t)
    assert one["teacher_name"] == "Пётр Teacher" and one["lesson_types"] == ["group"]     # he leads the group (kind «group»)
    assert one["rates"] == {"group": None} and one["next"] == {}
    assert item_of(body, t2)["lesson_types"] == [] and item_of(body, t2)["rates"] == {}
    assert [i["teacher_name"] for i in body["items"]] == sorted(i["teacher_name"] for i in body["items"])

    out = put(world, t, "2026-09", {"group": "50", "individual": "40"}, note="С сентября")
    assert set(out) == {"teacher_id", "teacher_name", "lesson_types", "rates", "next"}
    assert out["teacher_id"] == str(t.id)
    body = rates_of(world, "2026-09")
    one = item_of(body, t)
    assert one["lesson_types"] == ["group", "individual"]
    assert one["rates"]["group"] == {"percent": "50", "valid_from": "2026-09-01", "set_by_name": "Иван Owner",
                                     "set_at": one["rates"]["group"]["set_at"]}
    assert one["rates"]["group"]["set_at"] and one["rates"]["individual"]["percent"] == "40"
    assert item_of(rates_of(world, "2026-08"), t)["rates"] == {"group": None, "individual": None}      # before: not set
    # a future change is "next" and does not touch the month asked
    put(world, t, "2026-11", {"group": "45.5"})
    one = item_of(rates_of(world, "2026-10"), t)
    assert one["rates"]["group"]["percent"] == "50" and one["next"] == {"group": {"percent": "45.5", "valid_from": "2026-11-01"}}
    assert item_of(rates_of(world, "2026-11"), t)["rates"]["group"]["percent"] == "45.5"
    assert item_of(rates_of(world, "2026-11"), t)["next"] == {}
    # the default month is the current business month; the answer of PUT is the item for it
    now = business_today().strftime("%Y-%m")
    assert rates_of(world)["month"] == now
    # history: newest first, the author, the note
    r = owner(world).get(f"{API}/org/payroll/rates/{t.id}/history")
    assert r.status_code == 200
    rows = r.json()
    assert [(x["lesson_type"], x["percent"], x["valid_from"]) for x in rows][0] == ("group", "45.5", "2026-11-01")
    assert {(x["lesson_type"], x["percent"], x["valid_from"], x["note"]) for x in rows} == {
        ("group", "50", "2026-09-01", "С сентября"), ("individual", "40", "2026-09-01", "С сентября"),
        ("group", "45.5", "2026-11-01", None)}
    assert all(set(x) == {"lesson_type", "percent", "valid_from", "set_by_name", "set_at", "note"} for x in rows)
    assert rows[0]["set_by_name"] == "Иван Owner" and rows[0]["set_at"]
    assert owner(world).get(f"{API}/org/payroll/rates/{t2.id}/history").json() == []


def test_put_returns_the_item_of_the_current_month_with_the_next_change(world):
    t = world.teacher
    cur = business_today().replace(day=1)
    nxt = (cur + timedelta(days=32)).replace(day=1)
    out = put(world, t, cur.strftime("%Y-%m"), {"group": "50"})
    assert out["rates"]["group"]["percent"] == "50" and out["rates"]["group"]["valid_from"] == cur.isoformat() and out["next"] == {}
    out = put(world, t, nxt.strftime("%Y-%m"), {"group": "42"})
    assert out["rates"]["group"]["percent"] == "50" and out["next"] == {"group": {"percent": "42", "valid_from": nxt.isoformat()}}


def test_put_is_validated_in_russian(world):
    t = world.teacher
    c = owner(world)
    url = f"{API}/org/payroll/rates/{t.id}"
    bad = [
        ({"valid_from": "2026-09", "percents": {"group": "101"}}, "Процент должен быть от 0 до 100"),
        ({"valid_from": "2026-09", "percents": {"group": "-1"}}, "Процент должен быть от 0 до 100"),
        ({"valid_from": "2026-09", "percents": {"group": "12.345"}}, "не более двух знаков"),
        ({"valid_from": "2026-09", "percents": {"group": "abc"}}, "Процент должен быть числом"),
        ({"valid_from": "2026-09", "percents": {"group": None}}, "Процент должен быть числом"),
        ({"valid_from": "2026-09", "percents": {"group": True}}, "Процент должен быть числом"),
        ({"valid_from": "2026-09", "percents": {"nope": "50"}}, "Неизвестный вид занятий"),
        ({"valid_from": "2026-09", "percents": {}}, "хотя бы для одного"),
        ({"valid_from": "2026-09", "percents": "50"}, "хотя бы для одного"),
        ({"valid_from": "2026-09"}, "хотя бы для одного"),
        ({"valid_from": "2026-9", "percents": {"group": "50"}}, "ГГГГ-ММ"),
        ({"valid_from": "2026-09-01", "percents": {"group": "50"}}, "ГГГГ-ММ"),
        ({"valid_from": "2026-13", "percents": {"group": "50"}}, "некорректный месяц"),
        ({"valid_from": "2019-12", "percents": {"group": "50"}}, "некорректный месяц"),
        ({"valid_from": "2999-01", "percents": {"group": "50"}}, "некорректный месяц"),
        ({"valid_from": None, "percents": {"group": "50"}}, "ГГГГ-ММ"),
        ({"valid_from": "2026-09", "percents": {"group": "50"}, "note": "x" * 301}, "Примечание"),
        ({"valid_from": "2026-09", "percents": {"group": "50"}, "note": 5}, "Примечание"),
    ]
    for body, text in bad:
        r = c.put(url, json=body)
        assert r.status_code == 422 and text in str(r.json()["detail"]), (body, r.text)
    # one bad kind refuses the whole request: nothing is written
    assert c.put(url, json={"valid_from": "2026-09", "percents": {"group": "50", "pair": "x"}}).status_code == 422
    assert world.db.query(PayrollRate).filter_by(org_id=world.org.id).count() == 0
    assert c.put(f"{API}/org/payroll/rates/{uuid4()}", json={"valid_from": "2026-09", "percents": {"group": "50"}}).status_code == 404
    assert c.put(url, json={"valid_from": "2026-09", "percents": {"group": 50.5, "pair": "62,5"}}).status_code == 200
    assert c.get(f"{API}/org/payroll/rates", params={"month": "nope"}).status_code == 422
    assert c.get(f"{API}/org/payroll/rates/{uuid4()}/history").status_code == 404


def test_rates_belong_to_the_centre_and_to_the_owner(world):
    t = world.teacher
    put(world, t, "2026-09", {"group": "50"})
    foreign = make_client(world, user=world.foreign_admin, org="other", org_role="owner")
    # another centre sees none of our teachers and none of our rates
    body = rates_of(world, "2026-09", client=foreign)
    assert str(t.id) not in {i["teacher_id"] for i in body["items"]} and str(world.teacher2.id) not in {i["teacher_id"] for i in body["items"]}
    assert foreign.get(f"{API}/org/payroll/rates/{t.id}/history").status_code == 404
    r = foreign.put(f"{API}/org/payroll/rates/{t.id}", json={"valid_from": "2026-09", "percents": {"group": "99"}})
    assert r.status_code == 404
    assert [(x.percent, x.org_id) for x in world.db.query(PayrollRate).filter(PayrollRate.teacher_id == t.id)] == [(Decimal("50"), world.org.id)]
    # ... and a rate of the other centre never reaches our payroll
    other = make_user(world.db, "teacher", "Чужой")
    make_member(world.db, world.other_org, other, "teacher")
    world.db.commit()
    assert foreign.put(f"{API}/org/payroll/rates/{other.id}", json={"valid_from": "2026-09", "percents": {"group": "10"}}).status_code == 200
    assert owner(world).get(f"{API}/org/payroll/rates/{other.id}/history").status_code == 404
    # the owner only: an administrator and a teacher are refused (the whole list is in test_payroll_pg ``endpoints``)
    for who in (world.admin, world.teacher):
        c = make_client(world, user=who)
        assert c.get(f"{API}/org/payroll/rates").status_code == 403
        assert c.put(f"{API}/org/payroll/rates/{t.id}", json={"valid_from": "2026-09", "percents": {"group": "1"}}).status_code == 403
        assert c.get(f"{API}/org/payroll/rates/{t.id}/history").status_code == 403
    assert world.db.query(PayrollRate).filter_by(org_id=world.org.id).count() == 1


# ============================================================================================
# payroll reads the rates
# ============================================================================================


def test_calculate_and_save_use_the_stored_rate_of_the_month_and_ignore_the_body(world):
    m = previous_month()
    full_month(world, m, days=range(1, 7))
    c = owner(world)
    r = c.post(f"{API}/org/payroll/months/{m.label}/calculate", json={})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "PERCENT_REQUIRED"
    assert r.json()["detail"]["message"] == "Не задана ставка преподавателя: укажите её в «Сотрудниках»"
    (missing,) = r.json()["detail"]["missing"]
    assert missing["teacher_id"] == str(world.teacher.id) and missing["lesson_type"] == "group"
    put(world, world.teacher, m.label, {"group": "50"})
    body = {"percents": [{"teacher_id": str(world.teacher.id), "lesson_type": "group", "percent": "99"}]}   # ignored
    sheet = c.post(f"{API}/org/payroll/months/{m.label}/calculate", json=body).json()["sheets"][0]
    assert sheet["percents"] == {"group": "50"} and line_of(sheet, world.students[0])["amount"] == FEE * 50 * 6 // 1200
    saved = c.post(f"{API}/org/payroll/sheets", json={"month": m.label, "teacher_id": str(world.teacher.id), **body,
                                                      "expected_fingerprint": sheet["source_fingerprint"]}).json()
    assert saved["percents"] == {"group": "50"}
    # a rate from the NEXT month does not reach this month
    before = month_before(m)
    for n in range(1, 4):
        lesson(world, before.d(n), {s: "present" for s in world.students})
    for s in world.students:
        pay_calendar(world, s, before.first, before.last)
    r = c.post(f"{API}/org/payroll/months/{before.label}/calculate", json={})
    assert r.status_code == 422                                                       # the rate begins in `m`, not before
    put(world, world.teacher, before.label, {"group": "40"})
    sheet = c.post(f"{API}/org/payroll/months/{before.label}/calculate", json={}).json()["sheets"][0]
    assert sheet["percents"] == {"group": "40"}
    assert c.post(f"{API}/org/payroll/months/{m.label}/calculate", json={}).json()["sheets"][0]["percents"] == {"group": "50"}


def test_the_month_overview_exposes_the_rates_and_the_missing_ones(world):
    m = previous_month()
    full_month(world, m, days=range(1, 4))
    row = teacher_row(overview(world, m), world.teacher)
    assert row["rates"] == {} and row["missing_rates"] == ["group"]
    assert [t["lesson_type"] for t in row["lesson_types"]] == ["group"]
    put(world, world.teacher, m.label, {"group": "50", "pair": "70"})
    row = teacher_row(overview(world, m), world.teacher)
    assert row["rates"] == {"group": "50"} and row["missing_rates"] == []                # only the kinds held in the month
    # a second kind appears: its rate is missing, the first one stays
    second = make_group(world.db, world.org, world.teacher, "Индивидуальная")
    second.lesson_type = "individual"
    from src.models.auth import StudentGroup

    world.db.add(StudentGroup(student_id=world.students[0].id, group_id=second.id, is_active=True, joined_at=datetime(2020, 1, 1)))
    world.db.commit()
    lesson(world, m.d(5), {world.students[0]: "present"}, group=second)
    row = teacher_row(overview(world, m), world.teacher)
    assert row["rates"] == {"group": "50"} and row["missing_rates"] == ["individual"]
    # a rate that begins after the month is not in force for it
    put(world, world.teacher, (m.first + timedelta(days=40)).strftime("%Y-%m"), {"individual": "60"})
    assert teacher_row(overview(world, m), world.teacher)["missing_rates"] == ["individual"]


def test_a_saved_sheet_follows_the_rate_of_its_own_month_only(world):
    m, before = previous_month(), month_before(previous_month())
    for mm in (before, m):
        full_month(world, mm, days=range(1, 5))
    put(world, world.teacher, before.label, {"group": "50"})
    old, new = save(world, before), save(world, m)

    def stale(sheet):
        return owner(world).get(f"{API}/org/payroll/sheets/{sheet['id']}").json()["stale"]

    def in_overview(mm):
        return teacher_row(overview(world, mm), world.teacher)["sheet"]["stale"]

    def in_list(mm):
        return owner(world).get(f"{API}/org/payroll/sheets", params={"month": mm.label}).json()[0]["stale"]

    assert not stale(old) and not stale(new) and not in_overview(before) and not in_overview(m)
    # a rate from the later month: the earlier sheet does not move, the later one says "changed"
    put(world, world.teacher, m.label, {"group": "45"})
    assert not stale(old) and not in_overview(before) and not in_list(before)
    assert stale(new) and in_overview(m) and in_list(m)
    # recalculating takes the rate of ITS month and the sheet is current again
    fresh = patch(world, new, recalculate=True)
    assert fresh["percents"] == {"group": "45"} and fresh["stale"] is False and not in_overview(m)
    assert line_of(fresh, world.students[0])["amount"] == FEE * 45 * 4 // 1200
    # a correction of the EARLIER month (valid from it) is felt by that month and by the later one, which has its own rate
    put(world, world.teacher, before.label, {"group": "40"})
    assert stale(old) and in_overview(before) and not stale(fresh) and not in_overview(m)
    # a rate that begins after both months touches neither
    put(world, world.teacher, (m.first + timedelta(days=40)).strftime("%Y-%m"), {"group": "10"})
    assert not stale(fresh)
    # the saved sheets and their revisions are snapshots: they kept the percent they were calculated with
    world.db.expire_all()
    assert world.db.query(PayrollSheet).filter_by(id=old["id"]).one().percent_inputs == {"group": "50"}
    assert [r.percent_inputs for r in world.db.query(PayrollSheetRevision).filter_by(sheet_id=new["id"]).order_by(PayrollSheetRevision.revision)] == [
        {"group": "50"}, {"group": "45"}]


def test_a_rate_set_for_a_kind_the_sheet_does_not_have_changes_nothing(world):
    m = previous_month()
    full_month(world, m, days=range(1, 4))
    put(world, world.teacher, m.label, {"group": "50"})
    sheet = save(world, m)
    put(world, world.teacher, m.label, {"pair": "10", "individual": "20"})
    assert owner(world).get(f"{API}/org/payroll/sheets/{sheet['id']}").json()["stale"] is False


def test_the_diff_says_a_rate_changed(world):
    m = previous_month()
    full_month(world, m, days=range(1, 7))
    put(world, world.teacher, m.label, {"group": "50"})
    sheet = save(world, m)
    clean = owner(world).get(f"{API}/org/payroll/sheets/{sheet['id']}/diff").json()
    assert clean["reasons"] == [] and clean["rate_changes"] == [] and clean["stale"] is False
    put(world, world.teacher, m.label, {"group": "45"})
    diff = owner(world).get(f"{API}/org/payroll/sheets/{sheet['id']}/diff").json()
    assert diff["stale"] is True
    assert diff["reasons"] == ["Ставка «Группа»: 50% → 45%"]
    assert diff["rate_changes"] == [{"lesson_type": "group", "saved_percent": "50", "fresh_percent": "45", "message": "Ставка «Группа»: 50% → 45%"}]
    per_line = FEE * 50 * 6 // 1200 - FEE * 45 * 6 // 1200
    assert diff["saved_total"] == sheet["total_amount"] and diff["fresh_total"] == sheet["total_amount"] - 3 * per_line
    assert diff["delta_amount"] == -3 * per_line
    assert len(diff["lines"]) == 3 and all("процент" in x["changes"] and x["delta_amount"] == -per_line for x in diff["lines"])
    assert all(x["saved"]["percent"] == "50" and x["fresh"]["percent"] == "45" for x in diff["lines"])
    # the fingerprint to send back is still the data one: the owner saves what he saw
    assert diff["source_fingerprint"] == sheet["source_fingerprint"]
    assert patch(world, sheet, recalculate=True, expected_fingerprint=diff["source_fingerprint"])["percents"] == {"group": "45"}
    # no rate any more is impossible (rows are never removed), but a kind without a rate in force is told as such
    again = owner(world).get(f"{API}/org/payroll/sheets/{sheet['id']}/diff").json()
    assert again["reasons"] == [] and again["lines"] == []


# ============================================================================================
# the price is not edited on a sheet
# ============================================================================================


def test_monthly_fee_is_refused_on_a_sheet(world):
    m = previous_month()
    full_month(world, m, days=range(1, 4))
    put(world, world.teacher, m.label, {"group": "50"})
    base = preview(world, m, value="50")
    mid = line_of(base, world.students[0])["membership_id"]
    message = "Цена меняется в «Оплаты → Цены групп» или в карточке ученика"
    for fee in (0, 300_000, 1):
        r = owner(world).post(f"{API}/org/payroll/sheets", json={
            "month": m.label, "teacher_id": str(world.teacher.id), "line_overrides": [{"membership_id": mid, "monthly_fee": fee}]})
        assert r.status_code == 422 and r.json()["detail"] == {"code": "PRICE_NOT_EDITABLE", "message": message}
    assert world.db.query(PayrollSheet).filter_by(org_id=world.org.id).count() == 0
    sheet = save(world, m)
    for recalc in (False, True):
        r = owner(world).patch(f"{API}/org/payroll/sheets/{sheet['id']}", json={
            "revision": 1, "recalculate": recalc, "line_overrides": [{"membership_id": mid, "monthly_fee": 5}]})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "PRICE_NOT_EDITABLE"
    world.db.expire_all()
    assert world.db.query(PayrollSheet).filter_by(id=sheet["id"]).one().revision == 1
    # "include unpaid" ("начислить") works as before, with or without a null price (no manual price)
    ticked = patch(world, sheet, line_overrides=[{"membership_id": mid, "include_unpaid": True, "monthly_fee": None}])
    assert line_of(ticked, world.students[0])["include_unpaid"] is True and ticked["revision"] == 2


def test_a_manual_price_of_an_old_sheet_is_not_carried_to_a_recalculation_and_the_diff_says_so(world):
    from sqlalchemy.orm.attributes import flag_modified

    from src.auth import payroll_engine as engine

    m = previous_month()
    s0, s1, _ = world.students
    for n in range(1, 7):
        lesson(world, m.d(n), {s: "present" for s in world.students})
    give_group_price(world.db, world.group, FEE)                              # the price of the group
    for s in world.students:
        pay_calendar(world, s, m.first, m.last, fee=None)                      # nobody has a price of his own
    put(world, world.teacher, m.label, {"group": "50"})
    sheet = save(world, m)
    assert line_of(sheet, s0)["price_source"] == "group"
    # an OLD sheet: the owner had overwritten the price of s0 with 300 000 (the code of today cannot do it any more)
    row = world.db.query(PayrollSheet).filter_by(id=sheet["id"]).one()
    lines = []
    for line in row.lines:
        if line["student_id"] == str(s0.id):
            line = engine.recompute_line(line, percent=Decimal("50"), fee_override=300_000)
        lines.append(line)
    row.lines = lines
    row.total_amount = engine.sheet_total(lines)
    flag_modified(row, "lines")
    world.db.commit()
    old = owner(world).get(f"{API}/org/payroll/sheets/{sheet['id']}").json()
    assert line_of(old, s0)["fee_overridden"] is True and line_of(old, s0)["monthly_fee"] == 300_000     # an old sheet reads as it was
    # the data did not move, but a recalculation would drop the manual price: "changed" EVERYWHERE, the same answer
    assert old["stale"] is True
    assert teacher_row(overview(world, m), world.teacher)["sheet"]["stale"] is True
    assert owner(world).get(f"{API}/org/payroll/sheets", params={"month": m.label}).json()[0]["stale"] is True
    diff = owner(world).get(f"{API}/org/payroll/sheets/{sheet['id']}/diff").json()
    assert diff["stale"] is True
    (changed,) = diff["lines"]
    assert changed["student_name"] == "Каримова Алия" and changed["status"] == "changed"
    assert "оплата" in changed["changes"] and "цена из «Цен групп» вместо ручной 300 000 сум" in changed["changes"]
    assert changed["saved"]["monthly_fee"] == 300_000 and changed["fresh"]["monthly_fee"] == FEE
    assert diff["stale"] is True and changed["delta_amount"] == FEE * 50 * 6 // 1200 - 300_000 * 50 * 6 // 1200
    # a recalculation does not carry the manual price: the line is the price of the group again; the ticks stay
    fresh = patch(world, old, recalculate=True)
    assert line_of(fresh, s0)["fee_overridden"] is False and line_of(fresh, s0)["monthly_fee"] == FEE
    assert line_of(fresh, s0)["amount"] == FEE * 50 * 6 // 1200 and fresh["stale"] is False
    assert owner(world).get(f"{API}/org/payroll/sheets/{sheet['id']}/diff").json()["lines"] == []
    assert owner(world).get(f"{API}/org/payroll/sheets/{sheet['id']}").json()["stale"] is False
    assert teacher_row(overview(world, m), world.teacher)["sheet"]["stale"] is False
    assert owner(world).get(f"{API}/org/payroll/sheets", params={"month": m.label}).json()[0]["stale"] is False
    assert owner(world).get(f"{API}/org/payroll/sheets/{sheet['id']}/diff").json()["stale"] is False
    # the history still shows what was: the revision before kept the manual price
    world.db.expire_all()
    first = world.db.query(PayrollSheetRevision).filter_by(sheet_id=sheet["id"], revision=1).one()
    assert next(x for x in first.lines if x["student_id"] == str(s0.id))["monthly_fee"] == FEE        # revision 1 was saved before the edit
    assert owner(world).get(f"{API}/org/payroll/sheets/{sheet['id']}").json()["revision"] == 2


def test_rates_belong_to_members_who_may_teach_and_a_deactivated_teacher_is_still_paid(world):
    from src.models.auth import OrganizationMember

    body = {"valid_from": "2026-09", "percents": {"group": "50"}}
    c = owner(world)
    # an owner and an administrator may teach: they get rates
    for who in (world.owner, world.admin):
        assert c.put(f"{API}/org/payroll/rates/{who.id}", json=body).status_code == 200
        assert c.get(f"{API}/org/payroll/rates/{who.id}/history").status_code == 200
    # a user who is not a member of the centre and an unknown id: 404 «Преподаватель не найден»
    outsider = make_user(world.db, "teacher", "Посторонний")
    world.db.commit()
    for uid in (outsider.id, uuid4()):
        r = c.put(f"{API}/org/payroll/rates/{uid}", json=body)
        assert r.status_code == 404 and r.json()["detail"] == "Преподаватель не найден", uid
        assert c.get(f"{API}/org/payroll/rates/{uid}/history").status_code == 404
    assert world.db.query(PayrollRate).filter_by(teacher_id=outsider.id).count() == 0
    # a teacher let go AFTER the month still has to be paid for it: deactivated, with lessons in the month
    m = previous_month()
    gone = make_user(world.db, "teacher", "Уволенный")
    make_member(world.db, world.org, gone, "teacher")
    world.db.commit()
    full_month(world, m, days=range(1, 4), teacher=gone)
    world.db.query(OrganizationMember).filter_by(org_id=world.org.id, user_id=gone.id).update({"is_active": False})
    world.db.commit()
    listed = {i["teacher_id"] for i in rates_of(world, m.label)["items"]}
    assert str(gone.id) in listed                                     # he held lessons in that month
    assert str(gone.id) not in {i["teacher_id"] for i in rates_of(world, "2020-01")["items"]}   # not in a month he did not teach
    assert item_of(rates_of(world, m.label), gone)["lesson_types"] == ["group"]
    put(world, gone, m.label, {"group": "50"})                         # the rate can be set ...
    assert c.get(f"{API}/org/payroll/rates/{gone.id}/history").status_code == 200
    sheet = c.post(f"{API}/org/payroll/months/{m.label}/calculate", json={"teacher_ids": [str(gone.id)]}).json()["sheets"][0]
    assert sheet["total_amount"] > 0 and sheet["percents"] == {"group": "50"}          # ... and his month is calculated
    r = c.post(f"{API}/org/payroll/sheets", json={"month": m.label, "teacher_id": str(gone.id), "expected_fingerprint": sheet["source_fingerprint"]})
    assert r.status_code == 201 and r.json()["total_amount"] == sheet["total_amount"]    # ... and saved


def test_rate_reason_reads_like_the_interface():
    from decimal import Decimal
    from src.auth.payroll_rates import rate_reason
    assert rate_reason("group", Decimal("62.50"), Decimal("70")) == "Ставка «Группа»: 62,5% → 70%"
    assert rate_reason("group", Decimal("50"), None) == "Ставка «Группа»: 50% → не задана"


def test_unpaid_students_are_counted_fresh_in_the_month_and_from_the_lines_on_a_saved_sheet(world):
    """«9 учеников не оплатили часть уроков (140 посещений)»: ``unpaid`` counts lessons, ``unpaid_students`` the students."""
    from src.models.auth import StudentGroup

    m = previous_month()
    s0, s1, s2 = world.students
    for n in range(1, 7):
        lesson(world, m.d(n), {s: "present" for s in world.students})
    pay_calendar(world, s0, m.first, m.d(4))                     # 2 unpaid lessons each
    pay_calendar(world, s1, m.first, m.d(4))
    pay_calendar(world, s2, m.first, m.last)                     # nothing unpaid
    # s0 also sits in a second group of the same teacher: still ONE student
    second = make_group(world.db, world.org, world.teacher, "Парные")
    second.lesson_type = "pair"
    world.db.add(StudentGroup(student_id=s0.id, group_id=second.id, is_active=True, joined_at=datetime(2020, 1, 1)))
    world.db.commit()
    for n in range(1, 4):
        lesson(world, m.d(n), {s0: "present"}, group=second)
    pay_calendar(world, s0, m.first, m.d(1), group=second)       # 2 more unpaid lessons of s0
    put(world, world.teacher, m.label, {"group": "50", "pair": "50"})

    def month_warnings():
        return teacher_row(overview(world, m), world.teacher)["warnings"]

    w = month_warnings()
    assert (w["unpaid"], w["unpaid_students"], w["no_fee"]) == (6, 2, 0)
    sheet = preview(world, m, value="50")
    assert (sheet["warnings"]["unpaid"], sheet["warnings"]["unpaid_students"]) == (6, 2)
    saved = save(world, m)
    assert (saved["warnings"]["unpaid"], saved["warnings"]["unpaid_students"]) == (6, 2)
    # s1 pays the rest: the month (FRESH data) says 1 student, the saved sheet is a snapshot and still says 2
    pay_calendar(world, s1, m.d(5), m.last)
    w = month_warnings()
    assert (w["unpaid"], w["unpaid_students"]) == (4, 1)
    got = owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}").json()
    assert (got["warnings"]["unpaid"], got["warnings"]["unpaid_students"]) == (6, 2) and got["stale"] is True
    assert preview(world, m, value="50")["warnings"]["unpaid_students"] == 1
    fresh = patch(world, saved, recalculate=True)
    assert (fresh["warnings"]["unpaid"], fresh["warnings"]["unpaid_students"]) == (4, 1)
    # "начислить" does not hide the students: the lessons are still unpaid
    mid = line_of(fresh, s0)["membership_id"]
    ticked = patch(world, fresh, line_overrides=[{"membership_id": mid, "include_unpaid": True}])
    assert ticked["warnings"]["unpaid_students"] == 1
