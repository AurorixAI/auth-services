"""LOGIC-A money on PostgreSQL: leave / archive with money (F9, S-5), the individual price that ends on leaving (F13), a backdated price
(decision 4), the deferred change of the billing mode (F12), the opening balance of «Добавить в группу» and of the import (decision 1), a lesson the
centre cancelled (decision 3), the new payment tabs and the summary invariants."""
from datetime import date, datetime, timedelta
from io import BytesIO

import pytest

from src.auth import payment_status as ps
from src.models.auth import (
    GroupAttendance, GroupAudit, MembershipPause, MembershipPriceOverride, Student, StudentGroup, StudentPayment, StudentRefund,
    StudentWriteoff,
)

from tests.pg_world import give_group_price, give_price, make_client, make_group, make_student, pg_session, world  # noqa: F401
from tests.test_pay2_api_pg import (  # noqa: F401
    API, D, TODAY, e1_world, fixed_today, membership, owner, post_payment, preview, seed_payment, settings_block, stay,
)
from tests.test_logicA_opening_pause_pg import centre, enrolled, put as put_opening  # noqa: F401

pytestmark = pytest.mark.skipif(not __import__("os").environ.get("A2_AUTH_TEST_DATABASE_URL"), reason="Requires disposable A2 auth PostgreSQL")


def leave(w, group, student, body=None, client=None, expect=200):
    r = (client or owner(w)).post(f"{API}/org/groups/{group.id}/students/{student.id}/leave", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def lp(w, group, student, client=None):
    r = (client or owner(w)).get(f"{API}/org/groups/{group.id}/students/{student.id}/leave-preview")
    assert r.status_code == 200, r.text
    return r.json()


def money_world(w, *, paid=300_000, price=600_000, first=D(2026, 10, 11)):
    """fixed_day=1, price 600 000, the student joined on ``first`` and paid ``paid`` on the 16th."""
    centre(w, price)
    m = enrolled(w, first)
    if paid:
        seed_payment(w, m, paid, D(2026, 10, 16))
    return m


# ---- F9: leave with money -----------------------------------------------------------------------------------------------------------------------


def test_leave_preview_and_leave_with_a_refund(world, fixed_today):
    m = money_world(world)                                       # joined 11.10, 300 000 paid; today 21.10: charged 11..21 = 11 days
    pv = lp(world, world.group, world.students[0])
    mem = pv["memberships"][0]
    assert pv["as_of"] == "2026-10-21" and mem["membership_id"] == str(m.id) and mem["has_open_balance"] is True
    assert mem["money"]["advance"] == 300_000 - 213_000 and mem["money"]["refundable"] == 87_000 and mem["money"]["debt"] == 0
    assert mem["allowed"] == ["keep", "refund"] and mem["money"]["charged_after_leave"] == 213_000
    body = {"settle": [{"membership_id": str(m.id), "action": "refund", "expected_refundable": 87_000, "method": "cash"}]}
    out = leave(world, world.group, world.students[0], body)
    assert out["settled"][0]["action"] == "refund" and out["settled"][0]["amount"] == 87_000 and out["settled"][0]["refund_id"]
    world.db.expire_all()
    assert not world.db.get(StudentGroup, m.id).is_active
    assert world.db.query(StudentRefund).filter_by(membership_id=m.id, voided_at=None).one().amount == 87_000
    row = owner(world).get(f"{API}/org/students/{m.student_id}/memberships").json()[0]
    assert (row["advance"], row["debt"], row["refundable"]) == (0, 0, 0)
    assert world.db.query(GroupAudit).filter_by(org_id=world.org.id, action="student_left_group").count() == 1
    # R: a second click finds him gone: nothing is refunded twice
    r = owner(world).post(f"{API}/org/groups/{world.group.id}/students/{world.students[0].id}/leave", json=body)
    assert r.status_code == 409 and world.db.query(StudentRefund).filter_by(membership_id=m.id).count() == 1


def test_leave_with_a_writeoff_and_the_balance_changed_answer(world, fixed_today):
    m = money_world(world, paid=100_000)                          # debt: 213 000 - 100 000 = 113 000
    mem = lp(world, world.group, world.students[0])["memberships"][0]
    assert mem["money"]["debt"] == 113_000 and mem["allowed"] == ["keep", "writeoff"]
    stale = {"settle": [{"membership_id": str(m.id), "action": "writeoff", "expected_debt": 90_000, "reason": "договорённость"}]}
    r = owner(world).post(f"{API}/org/groups/{world.group.id}/students/{world.students[0].id}/leave", json=stale)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "BALANCE_CHANGED"
    assert r.json()["detail"]["fresh"] == [{"membership_id": str(m.id), "debt": 113_000, "advance": 0, "refundable": 0}]
    world.db.expire_all()
    assert world.db.get(StudentGroup, m.id).is_active and world.db.query(StudentWriteoff).filter_by(membership_id=m.id).count() == 0   # C: nothing done
    bad = {"settle": [{"membership_id": str(m.id), "action": "writeoff", "expected_debt": 113_000, "reason": "а"}]}
    r = owner(world).post(f"{API}/org/groups/{world.group.id}/students/{world.students[0].id}/leave", json=bad)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "REASON_REQUIRED"
    world.db.expire_all()
    assert world.db.get(StudentGroup, m.id).is_active                                              # atomic: the leaving rolled back too
    ok = {"settle": [{"membership_id": str(m.id), "action": "writeoff", "expected_debt": 113_000, "reason": "договорённость"}]}
    out = leave(world, world.group, world.students[0], ok)
    assert out["settled"][0]["amount"] == 113_000
    assert owner(world).get(f"{API}/org/students/{m.student_id}/memberships").json()[0]["debt"] == 0
    # refund without a method
    m2 = membership(world, world.students[1]); stay(world, m2, D(2026, 10, 11)); seed_payment(world, m2, 400_000, D(2026, 10, 16))
    r = owner(world).post(f"{API}/org/groups/{world.group.id}/students/{world.students[1].id}/leave",
                          json={"settle": [{"membership_id": str(m2.id), "action": "refund", "expected_refundable": 187_000}]})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "METHOD_REQUIRED"


def test_leave_rights_and_the_plain_delete(world, fixed_today):
    m = money_world(world)
    adm = make_client(world, user=world.admin)                    # an admin without the money switch: no sums, no settlement
    pv = lp(world, world.group, world.students[0], client=adm)["memberships"][0]
    assert pv["money"] is None and pv["has_open_balance"] is True and pv["allowed"] == ["keep"]
    r = adm.post(f"{API}/org/groups/{world.group.id}/students/{world.students[0].id}/leave",
                 json={"settle": [{"membership_id": str(m.id), "action": "refund", "method": "cash"}]})
    assert r.status_code == 403
    assert make_client(world, user=world.teacher).get(f"{API}/org/groups/{world.group.id}/students/{world.students[0].id}/leave-preview").status_code == 403
    r = adm.delete(f"{API}/org/groups/{world.group.id}/students/{world.students[0].id}")          # the old route = leave with «keep»
    assert r.status_code == 200 and r.json()["message"]
    world.db.expire_all()
    assert not world.db.get(StudentGroup, m.id).is_active and world.db.query(StudentRefund).filter_by(membership_id=m.id).count() == 0
    assert adm.delete(f"{API}/org/groups/{world.group.id}/students/{world.students[0].id}").status_code == 400     # the old answer: not in the group


def test_archive_preview_and_archive_with_settle(world, fixed_today):
    m = money_world(world)
    pv = owner(world).get(f"{API}/org/students/{m.student_id}/archive-preview").json()
    assert [x["membership_id"] for x in pv["memberships"]] == [str(m.id)] and pv["memberships"][0]["money"]["refundable"] == 87_000
    r = owner(world).post(f"{API}/org/students/{m.student_id}/archive", json={"reason": "переехал", "settle": [
        {"membership_id": str(m.id), "action": "refund", "expected_refundable": 1, "method": "cash"}]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "BALANCE_CHANGED"
    world.db.expire_all()
    assert world.db.get(Student, m.student_id).status != "archived"                                  # nothing archived
    r = owner(world).post(f"{API}/org/students/{m.student_id}/archive", json={"reason": "переехал", "settle": [
        {"membership_id": str(m.id), "action": "refund", "expected_refundable": 87_000, "method": "cash"}]})
    assert r.status_code == 200, r.text
    assert r.json()["settled"][0]["amount"] == 87_000 and r.json()["closed_groups"]
    world.db.expire_all()
    assert world.db.get(Student, m.student_id).status == "archived"


# ---- S-5: a student who left pays his debt only ---------------------------------------------------------------------------------------------------


def test_a_left_student_pays_the_debt_and_nothing_more(world, fixed_today):
    m = money_world(world, paid=100_000)                          # debt 113 000 today
    leave(world, world.group, world.students[0])                    # keep
    pv = preview(world, m)
    assert pv["suggested_amount"] == 113_000 and [w["code"] for w in pv["warnings"]] == ["left_debt_only"]
    r = owner(world).post(f"{API}/org/memberships/{m.id}/payments", json={"amount": 400_000, "paid_on": TODAY.isoformat(), "method": "cash"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "PAYMENT_EXCEEDS_DEBT" and r.json()["detail"]["debt"] == 113_000
    assert r.json()["detail"]["message"] == "Ученик ушёл: принять можно не больше долга 113 000 сум"
    created = post_payment(world, m, amount=50_000)               # P: partial is fine
    assert created["row"]["debt"] == 63_000 and created["row"]["advance"] == 0
    post_payment(world, m, amount=63_000)
    r = owner(world).post(f"{API}/org/memberships/{m.id}/payments", json={"amount": 1, "paid_on": TODAY.isoformat(), "method": "cash"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "NOTHING_TO_PAY"                  # Z: no debt
    # an archived student is the same
    m2 = membership(world, world.students[1]); stay(world, m2, D(2026, 10, 11))
    world.students[1].status = "archived"
    m2.is_active = False
    world.db.commit()
    r = owner(world).post(f"{API}/org/memberships/{m2.id}/payments", json={"amount": 999_000, "paid_on": TODAY.isoformat(), "method": "cash"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "PAYMENT_EXCEEDS_DEBT"


# ---- F13 ----------------------------------------------------------------------------------------------------------------------------------------


def test_f13_the_individual_price_ends_when_the_student_leaves_and_a_return_is_priced_by_the_group(world, fixed_today):
    m = money_world(world, paid=0, price=600_000, first=D(2026, 9, 1))
    settings_block(world, accounting_start_date="2026-09-01")
    give_price(world.db, m, 400_000, reason="скидка", valid_from=D(2026, 9, 1))
    r = owner(world).get(f"{API}/org/students/{m.student_id}/memberships").json()[0]
    assert r["price"]["amount"] == 400_000 and r["price_ends_on_leave"] is False
    leave(world, world.group, world.students[0])                    # leaves today (21.10)
    world.db.expire_all()
    marker = world.db.query(MembershipPriceOverride).filter_by(membership_id=m.id, reason="left_group").one()
    assert marker.amount is None and marker.valid_from == D(2026, 10, 22)
    assert world.db.query(GroupAudit).filter_by(org_id=world.org.id, action="membership_price_set").count() >= 1
    r = owner(world).delete(f"{API}/org/memberships/{m.id}/price-override/{marker.id}")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PRICE_MARKER"
    # he comes back on 20.11: the group price (600 000), the past stays at 400 000
    fixed_today(D(2026, 11, 20))
    r = owner(world).post(f"{API}/org/groups/{world.group.id}/students/{world.students[0].id}?mode=add")
    assert r.status_code == 200, r.text
    world.db.expire_all()
    back = world.db.get(StudentGroup, m.id)
    back.joined_at = datetime(2026, 11, 19, 19, 0)                   # the service takes the real clock: put the stay on 20.11 (Tashkent)
    back.periods[-1].joined_at = back.joined_at
    world.db.commit()
    row = owner(world).get(f"{API}/org/students/{m.student_id}/memberships").json()[0]
    assert row["price"]["amount"] == 600_000 and row["price"]["source"] == "group"
    st = owner(world).get(f"{API}/org/memberships/{m.id}/statement").json()
    charges = {c["details"]["segment"]["period_from"]: c["details"]["segment"] for c in st["rows"] if c["type"] == "charge"}
    assert charges["2026-09-01"]["price"]["amount"] == 400_000 and charges["2026-10-01"]["price"]["amount"] == 400_000
    assert charges["2026-11-01"]["charge"] == round(600_000 * 11 / 30, -3) == 220_000                       # 11 of 30 days at the group price


# ---- decision 4: a backdated price ------------------------------------------------------------------------------------------------------------------


def two_students_world(w, price=500_000):
    centre(w, price)
    a, b = enrolled(w, D(2026, 10, 1)), enrolled(w, D(2026, 10, 1), w.students[1])
    seed_payment(w, a, 500_000, D(2026, 10, 1))
    return a, b


def test_backdated_price_with_payments_is_refused_and_without_payments_it_shows_the_debt(world, fixed_today):
    a, b = two_students_world(world)
    c = owner(world)
    r = c.put(f"{API}/org/groups/{world.group.id}/price", json={"amount": 600_000, "valid_from": "2026-10-01"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PRICE_BACKDATE_WITH_PAYMENTS"
    assert [s["membership_id"] for s in r.json()["detail"]["students"]] == [str(a.id)]
    assert "Нельзя изменить цену задним числом" in r.json()["detail"]["message"] and "прощением долга" in r.json()["detail"]["message"]
    for dry in (True, False):                                      # the refusal is the same for the dry run; nothing was written
        assert c.put(f"{API}/org/groups/{world.group.id}/price", params={"dry_run": dry}, json={"amount": 600_000, "valid_from": "2026-10-01"}).status_code == 409
    # only B (no money): allowed; the impact says «появится долг»
    world.db.query(StudentPayment).filter_by(membership_id=a.id).delete()
    world.db.commit()
    r = c.put(f"{API}/org/groups/{world.group.id}/price", params={"dry_run": True}, json={"amount": 600_000, "valid_from": "2026-10-01"})
    assert r.status_code == 200
    money = r.json()["impact"]["money"]
    assert money["backdated"] is True and money["memberships_with_new_debt"] == 3 and money["debt_delta"] == 3 * 100_000
    assert {i["student_name"] for i in money["items"]} and all(i["debt_after"] - i["debt_before"] == 100_000 for i in money["items"])
    assert c.put(f"{API}/org/groups/{world.group.id}/price", json={"amount": 600_000, "valid_from": "2026-10-01"}).status_code == 200


def test_a_future_price_is_always_allowed_and_the_impact_counts_the_days_an_advance_buys(world, fixed_today):
    centre(world, 500_000)
    a = enrolled(world, D(2026, 10, 1))
    seed_payment(world, a, 700_000, D(2026, 10, 20))               # October 500 000 + 200 000 advance: 12 days of November at 500 000
    c = owner(world)
    r = c.put(f"{API}/org/groups/{world.group.id}/price", params={"dry_run": True}, json={"amount": 600_000, "valid_from": "2026-11-01"})
    assert r.status_code == 200, r.text
    money = r.json()["impact"]["money"]
    item = money["items"][0]
    assert (item["paid_until_before"], item["paid_until_after"], item["advance_after"], item["has_money"]) == ("2026-11-12", "2026-11-10", 200_000, True)
    assert (money["memberships_with_less_advance"], money["days_lost_total"], money["backdated"]) == (1, 2, False)
    assert c.put(f"{API}/org/groups/{world.group.id}/price", json={"amount": 600_000, "valid_from": "2026-11-01"}).status_code == 200
    # the individual price: the same refusal for a backdated one (the membership has money)
    r = c.put(f"{API}/org/memberships/{a.id}/price-override", json={"amount": 100_000, "valid_from": "2026-10-01", "reason": "скидка"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PRICE_BACKDATE_WITH_PAYMENTS"
    r = c.put(f"{API}/org/memberships/{a.id}/price-override", params={"dry_run": True}, json={"amount": 100_000, "valid_from": "2026-11-15", "reason": "скидка"})
    assert r.status_code == 200 and r.json()["dry_run"] is True and r.json()["impact"]["money"]["backdated"] is False
    assert r.json()["impact"]["students_with_override"] == 1 and r.json()["impact"]["students_following"] == 0


# ---- F12: the deferred change of the billing mode -----------------------------------------------------------------------------------------------------


def test_f12_the_running_period_is_not_cut_and_the_dry_run_says_who_switches(world, fixed_today):
    settings_block(world, accounting_start_date="2026-09-01")                  # payment_date
    give_group_price(world.db, world.group, 600_000, valid_from=D(2026, 1, 1))
    m = membership(world); stay(world, m, D(2026, 10, 15)); seed_payment(world, m, 600_000, D(2026, 10, 15))
    c = owner(world)
    r = c.put(f"{API}/org/settings/payments", params={"dry_run": True}, json={"period_mode": "lessons", "lessons_per_payment": 12})
    assert r.status_code == 200, r.text
    ri = r.json()["regime_impact"]
    assert ri["defer"] is True and ri["students_affected"] == 3 and (ri["first_switch"], ri["latest_switch"]) == ("2026-11-01", "2026-11-15")
    world.db.expire_all()
    assert (world.org.settings or {}).get("payments", {}).get("billing_history") is None                # the dry run wrote nothing
    assert c.put(f"{API}/org/settings/payments", json={"period_mode": "lessons", "lessons_per_payment": 12}).status_code == 200
    world.db.expire_all()
    hist = world.org.settings["payments"]["billing_history"]
    assert hist[-1]["defer"] is True and hist[-1]["from"] == "2026-10-21" and hist[-1]["period_mode"] == "lessons" and "defer" not in hist[0]
    row = c.get(f"{API}/org/students/{m.student_id}/memberships").json()[0]
    assert (row["debt"], row["advance"], row["paid_until"], row["period_mode"]) == (0, 0, "2026-11-14", "calendar")     # the running period goes on
    # two identical saves on one day replace the entry of the day
    assert c.put(f"{API}/org/settings/payments", json={"period_mode": "calendar"}).status_code == 200
    world.db.expire_all()
    assert len([h for h in world.org.settings["payments"]["billing_history"] if h["from"] == "2026-10-21"]) == 1


# ---- decision 1: the opening balance with «Добавить в группу» and with the import -----------------------------------------------------------------------


def new_student(w, first):
    s = Student(org_id=w.org.id, first_name=first, last_name="Ученик", class_level=9, status="new")
    w.db.add(s)
    w.db.commit()
    return s


def test_add_student_with_an_opening_balance(world, fixed_today):
    centre(world, 600_000)
    s = new_student(world, "Новый")
    c = owner(world)
    r = c.post(f"{API}/org/groups/{world.group.id}/students/{s.id}?mode=add", json={"opening_balance": {"debt_amount": 250_000, "debt_on": "2026-09-30"}})
    assert r.status_code == 200, r.text
    mid_ = r.json()["membership_id"]
    row = c.get(f"{API}/org/memberships/{mid_}/opening-balance").json()
    assert row["debt_amount"] == 250_000 and row["source"] == "add"
    assert c.get(f"{API}/org/students/{s.id}/memberships").json()[0]["debt"] == 463_000
    # the range is checked: «оплачено до» before the enrolment is refused and nothing is added
    s2 = new_student(world, "Второй")
    r = c.post(f"{API}/org/groups/{world.group.id}/students/{s2.id}?mode=add", json={"opening_balance": {"paid_until": "2026-10-01"}})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "OPENING_RANGE"
    world.db.expire_all()
    assert world.db.query(StudentGroup).filter_by(student_id=s2.id).count() == 0
    # a teacher cannot; an admin without the money switch cannot
    adm = make_client(world, user=world.admin)
    assert adm.post(f"{API}/org/groups/{world.group.id}/students/{s2.id}?mode=add", json={"opening_balance": {"paid_until": "2026-11-05"}}).status_code == 403


def import_workbook(rows, extra_headers=True):
    from openpyxl import Workbook

    from src.auth import import_service as imp

    wb = Workbook()
    wg = wb.active
    wg.title = imp.SHEET_GROUPS
    wg.append(["Название группы*"])
    wg.append(["Импорт-группа"])
    ws = wb.create_sheet(imp.SHEET_STUDENTS)
    ws.append(imp.COLS_STUDENTS)
    for r in rows:
        ws.append(r)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_import_creates_opening_balances_in_one_transaction_and_reports_what_was_skipped(world, fixed_today):
    centre(world, 600_000)
    data = import_workbook([
        ["Один", "Ученик", 7, "", "Импорт-группа", "", date(2026, 10, 31), None, None],             # paid until 31.10
        ["Два", "Ученик", 7, "", "Импорт-группа", "", None, "250 000", "30.09.2026"],               # a debt
        ["Три", "Ученик", 7, "", "Импорт-группа", "", date(2026, 10, 10), None, None],              # before the enrolment: skipped with a warning
        ["Четыре", "Ученик", 7, "", "Импорт-группа", "", None, "100000", None],                      # the date defaults to the day before
        ["Пять", "Ученик", 7, "", "Импорт-группа", "", None, None, None],                            # no opening
    ])
    c = owner(world)
    pv = c.post(f"{API}/org/import/preview", files={"file": ("x.xlsx", data)}).json()
    assert pv["ok"] and pv["counts"]["openings"] == 3
    assert any("раньше даты зачисления" in i["message"] and i["level"] == "warning" and i["row"] == 4 for i in pv["issues"])
    done = c.post(f"{API}/org/import/commit", files={"file": ("x.xlsx", data)}).json()
    assert done["ok"] and done["counts"]["openings"] == 3
    world.db.expire_all()
    from src.models.auth import MembershipOpeningBalance

    got = {(o.paid_until, o.debt_amount, o.debt_on, o.source) for o in world.db.query(MembershipOpeningBalance).filter_by(org_id=world.org.id)}
    assert got == {(D(2026, 10, 31), None, None, "import"), (None, 250_000, D(2026, 9, 30), "import"), (None, 100_000, D(2026, 10, 20), "import")}
    # the owner of the centre without the right to money imports without the openings
    adm = make_client(world, user=world.admin)
    data2 = import_workbook([["Шесть", "Ученик", 7, "", "Импорт-группа", "", date(2026, 10, 31), None, None]])
    pv2 = adm.post(f"{API}/org/import/preview", files={"file": ("x.xlsx", data2)}).json()
    assert pv2["ok"] and pv2["counts"]["openings"] == 0 and any("Нет прав вносить остатки" in i["message"] for i in pv2["issues"])
    # a broken cell is an error and nothing is written (all or nothing)
    bad = import_workbook([["Семь", "Ученик", 7, "", "Импорт-группа", "", "не дата", None, None]])
    r = c.post(f"{API}/org/import/commit", files={"file": ("x.xlsx", bad)}).json()
    assert r["ok"] is False and not world.db.query(Student).filter_by(org_id=world.org.id, last_name="Ученик", first_name="Семь").count()


# ---- decision 3: a lesson the centre cancelled -------------------------------------------------------------------------------------------------------


def test_a_cancelled_lesson_does_not_change_the_charge_of_an_incomplete_first_period(world, fixed_today):
    settings_block(world, accounting_start_date="2026-10-01", billing_anchor="fixed_day", billing_day=1)
    give_group_price(world.db, world.group, 600_000, valid_from=D(2026, 1, 1))
    world.group.schedule = [{"id": f"s{d}", "dayOfWeek": d, "startTime": "10:00", "endTime": "11:00", "format": "offline"} for d in (1, 3, 5)]
    world.db.commit()
    m = membership(world); stay(world, m, D(2026, 10, 16))
    before = owner(world).get(f"{API}/org/students/{m.student_id}/memberships").json()[0]
    assert before["debt"] == 323_000                                           # 7 of 13 lessons (E2)
    world.db.add(GroupAttendance(org_id=world.org.id, group_id=world.group.id, lesson_date="2026-10-19", lesson_id="s1", status="cancelled",
                                 cancellation_reason="учитель заболел", records={}, roster_snapshot=[]))
    world.db.commit()
    after = owner(world).get(f"{API}/org/students/{m.student_id}/memberships").json()[0]
    assert after["debt"] == before["debt"] == 323_000                         # the month costs the same


# ---- the payment tabs (owner's rule of 21.10) -------------------------------------------------------------------------------------------------------------


def test_the_tabs_debt_due_soon_paid_and_the_summary_invariants(world, fixed_today):
    centre(world, 600_000)
    ms = [enrolled(world, D(2026, 10, 1), s) for s in world.students]
    a, b, c3 = ms
    seed_payment(world, a, 450_000, D(2026, 10, 1))              # A: paid 450 000 of 600 000: partial, debt 150 000, paid until 22.10 (covered_through 22)
    seed_payment(world, b, 1_300_000, D(2026, 10, 1))            # B: paid ahead: an advance
    # C: never paid: debt 600 000
    c = owner(world)
    summary = c.get(f"{API}/org/payments/summary").json()
    pages = {k: c.get(f"{API}/org/payments/status", params={"bucket": k, "limit": 100}).json() for k in ("debt", "expiring", "active", "paused")}
    by = {k: {i["membership_id"]: i for i in v["items"]} for k, v in pages.items()}
    assert set(by["debt"]) == {str(a.id), str(c3.id)} and set(by["active"]) == {str(b.id)} and by["expiring"] == {} and by["paused"] == {}
    assert by["debt"][str(a.id)]["debt_reason"] == "partial" or by["debt"][str(a.id)]["debt_reason"] == "overdue"
    assert by["debt"][str(c3.id)]["debt_reason"] == "never_paid" and by["debt"][str(c3.id)]["reason"] == "Ещё не платил"
    assert by["debt"][str(a.id)]["bucket"] == "debt" and by["active"][str(b.id)]["bucket"] == "active"
    # the invariants of the summary
    assert summary["buckets"] == {"debt": 2, "expiring": 0, "active": 1, "paused": 0} and summary["debtors"] == summary["students_with_debt"] == 2
    assert summary["memberships_with_debt"] == pages["debt"]["total"] == summary["buckets"]["debt"] and summary["students_with_debt"] == 2
    assert sum(i["debt"] for i in pages["debt"]["items"]) == summary["debt_total"]
    assert summary["students_with_advance"] == 1 and summary["advance_total"] == sum(i["advance"] for i in c.get(f"{API}/org/payments/status", params={"limit": 100}).json()["items"])
    assert {"free", "price_missing"} <= set(summary)
