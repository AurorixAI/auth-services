"""Money-2 / wave B (the server): T10..T21 (PostgreSQL).  Every money number is worked out by hand in the comment of its test."""
from datetime import date, datetime, timedelta

import pytest

from src.models.auth import (
    GroupAudit, MembershipPause, MembershipTransfer, OrgMoneyAudit, Organization, OrganizationMember, Student, StudentGroup, StudentPayment,
)

from src.auth import payments_texts as tx
from tests.money1b_helpers import account, add, fresh_student, set_schedule, slot
from tests.pg_world import business_today, ensure_billing, give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.ops_helpers import exams  # noqa: F401  (fixture: exam-service stub)
from tests.test_money2_transfer_a_pg import DAILY, DAY, groups3, snap
from tests.test_payments_service_pg import mid, pay
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, settings_block, stay  # noqa: F401  (fixture)

pytestmark = needs_pg


# ---------------------------------------------------------------------------------------------------- T10 a bulk write does not break off


def test_t10_one_refusal_of_a_bulk_adding_does_not_break_off_the_rest_and_every_student_has_his_line(world, fixed_today):
    # Four students are added to B with «Оплата с» = today (20.11).  The third was in B before and left TODAY: «Оплата с» must be after his last stay (21.11 at the earliest) -> PAY_FROM_RANGE for him alone.
    # The other three are added: 3 of 4.  Before the fix the 422 of the third broke the whole request off (the first two already written, the fourth not even tried).
    a, b, c = groups3(world, fixed_today)
    s0, s1, s2 = world.students
    s3 = fresh_student(world, "Четвёртый")
    add(world, s2, group=b, mode="add")
    assert owner(world).post(f"{API}/org/groups/{b.id}/students/{s2.id}/leave", json={}).status_code == 200
    r = owner(world).post(f"{API}/org/groups/{b.id}/students/bulk", params={"mode": "add"}, json={"student_ids": [str(s.id) for s in (s0, s1, s2, s3)], "pay_from": DAY.isoformat()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == 3 and body["skipped"] == 1
    assert [(x["student_id"], x["status"]) for x in body["results"]] == [(str(s0.id), "added"), (str(s1.id), "added"), (str(s2.id), "skipped"), (str(s3.id), "added")]
    (skip,) = body["skipped_details"]
    assert skip["student_id"] == str(s2.id) and skip["code"] == "PAY_FROM_RANGE" and "21.11.2026" in skip["reason"]
    assert body["results"][2]["code"] == "PAY_FROM_RANGE" and body["results"][2]["reason"] == skip["reason"]
    for s in (s0, s1, s3):
        assert membership(world, s, b).is_active is True
    assert membership(world, s2, b).is_active is False


# ---------------------------------------------------------------------------------------------------- T11 the archive of a student = the rules of the leaving


NEUTRAL = "Убрать ученика с оплатой вперёд или долгом может владелец или администратор с доступом к деньгам"


def _admin(world):
    from tests.pg_world import make_client

    return make_client(world, user=world.admin, org_role="admin")


def test_t11_the_archive_and_the_leaving_refuse_an_admin_without_the_money_right_with_one_neutral_text_when_there_is_a_balance(world, fixed_today):
    # A: 300 000 a month, 30 lessons in November.  In A since 02.11, today 20.11: days 02..20 = 19 lessons = 19 x 10 000 = 190 000.
    # s0 paid nothing -> debt 190 000; s1 paid 600 000 -> advance 410 000; s2 paid exactly 190 000 -> no debt, no advance.
    a, b, c = groups3(world, fixed_today)
    s0, s1, s2 = world.students
    for s in (s0, s1, s2):
        stay(world, membership(world, s, a), date(2026, 11, 2))
    pay(world, s1, a, amount=600_000)
    pay(world, s2, a, amount=190_000)
    # (the leaving and the archive close the stay TODAY: the numbers that count are of the stay that ends on 20.11, not of the whole month)
    admin = _admin(world)
    for s in (s0, s1):
        left = admin.post(f"{API}/org/groups/{a.id}/students/{s.id}/leave", json={})
        archived = admin.post(f"{API}/org/students/{s.id}/archive", json={})
        for r in (left, archived):
            assert r.status_code == 403, r.text
            assert r.json()["detail"] == NEUTRAL or r.json()["detail"].get("message") == NEUTRAL
        assert not any(ch.isdigit() for ch in NEUTRAL)                                        # a fact, not a sum
        world.db.expire_all()
        assert membership(world, s, a).is_active is True and world.db.get(Student, s.id).status != "archived"
    # no balance: an admin without the right archives (and the leaving, for s2 after that, is the ordinary one)
    assert admin.post(f"{API}/org/students/{s2.id}/archive", json={}).status_code == 200


def test_t11_the_owner_archives_a_student_with_an_advance_and_the_refund_and_the_forgiveness_stay_allowed_for_the_archived(world, fixed_today):
    # s1: advance 410 000 (600 000 paid - 190 000 charged until 20.11); s0: debt 190 000.  The owner archives both (no settlement: both stay as they are); then he refunds 100 000 of the advance
    # and forgives 190 000 of the debt of the ARCHIVED students: both allowed.
    a, b, c = groups3(world, fixed_today)
    s0, s1, s2 = world.students
    for s in (s0, s1):
        stay(world, membership(world, s, a), date(2026, 11, 2))
    pay(world, s1, a, amount=600_000)
    for s in (s0, s1):
        assert owner(world).post(f"{API}/org/students/{s.id}/archive", json={}).status_code == 200
    r = owner(world).post(f"{API}/org/memberships/{membership(world, s1, a).id}/refunds", json={"amount": 100_000, "method": "cash", "reason": "Возврат"})
    assert r.status_code == 201, r.text
    r = owner(world).post(f"{API}/org/memberships/{membership(world, s0, a).id}/writeoffs", json={"amount": 190_000, "reason": "Договорённость"})
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------------------------------- T15 one way to leave a group


def test_t15_there_is_one_way_to_remove_a_student_from_a_group_the_leaving():
    from src.auth.service import AuthService

    assert not hasattr(AuthService, "remove_student_from_group")                  # was a second, unaudited closing used only by tests


# ---------------------------------------------------------------------------------------------------- T12 the archive of a group with balances


def _a_group_with_two_leavers(world, fixed_today):
    # A: 300 000 a month, 30 lessons a month (10 000 a lesson).  In A since 02.11, today 20.11, the stay closes on 20.11: days 02..20 = 19 lessons = 190 000.
    # s0 paid nothing: debt 190 000.  s1 paid 600 000: advance 600 000 - 190 000 = 410 000.  s2 paid 190 000: no debt, no advance.
    a, b, c = groups3(world, fixed_today)
    s0, s1, s2 = world.students
    for s in (s0, s1, s2):
        stay(world, membership(world, s, a), date(2026, 11, 2))
    pay(world, s1, a, amount=600_000)
    pay(world, s2, a, amount=190_000)
    for s in (s0, s1, s2):
        assert owner(world).post(f"{API}/org/groups/{a.id}/students/{s.id}/leave", json={}).status_code == 200
    return a, (s0, s1, s2)


def test_t12_the_archive_of_a_group_is_refused_while_a_leaver_has_a_debt_or_an_advance_and_passes_after_the_forgiveness_and_the_refund(world, fixed_today, exams):
    a, (s0, s1, s2) = _a_group_with_two_leavers(world, fixed_today)
    r = owner(world).post(f"{API}/org/groups/{a.id}/archive", json={})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "GROUP_ARCHIVE_BLOCKED"
    (blocker,) = [b for b in detail["blockers"] if b["code"] == "OPEN_BALANCES"]
    assert blocker["count"] == 2                                                      # s2 has no balance: not in the list
    rows = {x["student_id"]: x for x in blocker["balances"]}
    assert set(rows) == {str(s0.id), str(s1.id)}
    assert rows[str(s0.id)]["kind"] == "debt" and rows[str(s0.id)]["amount"] == 190_000
    assert [x["code"] for x in rows[str(s0.id)]["actions"]] == ["payment", "writeoff"]            # accept a payment / forgive
    assert rows[str(s1.id)]["kind"] == "advance" and rows[str(s1.id)]["amount"] == 410_000
    assert [x["code"] for x in rows[str(s1.id)]["actions"]] == ["refund"]
    assert rows[str(s1.id)]["membership_id"] == str(membership(world, s1, a).id)
    assert tx.money(410_000) in rows[str(s1.id)]["text"]
    assert owner(world).get(f"{API}/org/groups/{a.id}/archive-impact").json()["blockers"][-1]["code"] == "OPEN_BALANCES"      # the preview says the same
    world.db.expire_all()
    assert world.db.get(type(a), a.id).status == "active"
    # forgive the debt of s0 (190 000), refund the advance of s1 (410 000): the archive passes
    assert owner(world).post(f"{API}/org/memberships/{membership(world, s0, a).id}/writeoffs", json={"amount": 190_000, "reason": "Договорённость"}).status_code == 201
    assert owner(world).post(f"{API}/org/memberships/{membership(world, s1, a).id}/refunds", json={"amount": 410_000, "method": "cash", "reason": "Возврат"}).status_code == 201
    r = owner(world).post(f"{API}/org/groups/{a.id}/archive", json={"missedLessonsAction": "cancel"})      # (the lessons of the month nobody started are the other blocker)
    assert r.status_code == 200, r.text


def test_t12_an_administrator_without_the_money_right_gets_the_list_without_sums(world, fixed_today, exams):
    from tests.pg_world import make_client

    a, (s0, s1, s2) = _a_group_with_two_leavers(world, fixed_today)
    admin = make_client(world, user=world.admin, org_role="admin")
    r = admin.post(f"{API}/org/groups/{a.id}/archive", json={})
    assert r.status_code == 409, r.text
    (blocker,) = [b for b in r.json()["detail"]["blockers"] if b["code"] == "OPEN_BALANCES"]
    assert blocker["count"] == 2
    for row in blocker["balances"]:
        assert "amount" not in row and not any(ch.isdigit() for ch in row["text"]), row
        assert row["kind"] in ("debt", "advance")
        assert row["actions"] == []                                                    # without the right: nothing to offer, the owner decides


# ---------------------------------------------------------------------------------------------------- T13 the window of adding sees the balances of the past groups


def test_t13_the_preview_of_adding_names_the_debt_and_the_advance_left_in_other_groups_and_the_answer_says_the_same(world, fixed_today):
    from tests.pg_world import make_client

    # as in T12: in A since 02.11, the stay closed on 20.11 = 19 lessons x 10 000 = 190 000.  s0 paid nothing: debt 190 000; s1 paid 600 000: advance 410 000; s2 paid 190 000: nothing left.
    a, (s0, s1, s2) = _a_group_with_two_leavers(world, fixed_today)
    b = world.db.query(type(a)).filter_by(org_id=world.org.id, name="Бета").one()
    url = f"{API}/org/groups/{b.id}/students"
    d0 = owner(world).post(f"{url}/{s0.id}", params={"mode": "add", "dry_run": "true"}, json={}).json()
    (row,) = d0["past_balances"]
    assert (row["group_name"], row["kind"], row["amount"], row["left_on"]) == (a.name, "debt", 190_000, DAY.isoformat())
    assert row["membership_id"] == str(membership(world, s0, a).id) and tx.money(190_000) in row["text"] and a.name in row["text"]
    (row1,) = owner(world).post(f"{url}/{s1.id}", params={"mode": "add", "dry_run": "true"}, json={}).json()["past_balances"]
    assert (row1["kind"], row1["amount"]) == ("advance", 410_000)
    assert owner(world).post(f"{url}/{s2.id}", params={"mode": "add", "dry_run": "true"}, json={}).json()["past_balances"] == []        # nothing left in A
    # the real adding says the same; the new student of the centre with no past has an empty list
    real = owner(world).post(f"{url}/{s0.id}", params={"mode": "add"}, json={})
    assert real.status_code == 200, real.text
    assert real.json()["past_balances"] == [row]
    # without the right to money: the fact, no sum, no figures
    admin = make_client(world, user=world.admin, org_role="admin")
    (plain,) = admin.post(f"{url}/{s1.id}", params={"mode": "add", "dry_run": "true"}, json={}).json()["past_balances"]
    assert "amount" not in plain and plain["kind"] == "advance" and "сум" not in plain["text"] and "410" not in plain["text"], plain      # (the date of leaving is in the text, a sum is not)


# ---------------------------------------------------------------------------------------------------- T14 forgiveness and the pay of the teacher


def test_t14_the_forgiveness_and_its_cancellation_name_the_pay_of_the_teacher_the_closed_month_turns_stale_and_the_night_check_names_the_drift(world, fixed_today):
    from src.auth import money_reconcile as mr
    from tests.ops_helpers import grant_money_right
    from tests.pg_world import make_client
    from tests.test_logicA_payroll_pg import g_pay_world, save_sheet
    from tests.test_payroll_pg import student_group

    # g_pay_world: the previous month, 50 %, price 600 000; S2 paid 300 000 (through the 15th), the sheet of the teacher is saved AFTER the month ended (closed): 475 000.
    # Forgiving the whole debt of S2 pays the teacher the lessons after the 15th: the fresh total is 600 000 (the same as a late payment of 300 000, test_r3_18): +125 000.
    m, s1, s2 = g_pay_world(world)
    saved = save_sheet(world, m)
    assert saved["total_amount"] == 475_000
    grant_money_right(world)
    mem2 = student_group(world, s2)
    url = f"{API}/org/memberships/{mem2.id}/writeoffs"
    dry = owner(world).post(url, params={"dry_run": "true"}, json={"all": True, "reason": "Договорённость с родителем"})
    assert dry.status_code == 201, dry.text
    block = dry.json()["money_effects"]
    (line,) = block["payroll"]
    assert (line["changedBy"], line["state"]) == (125_000, "closed")
    assert dry.json()["money_effects"]["forgiveness"]["teacher_pay"] == 125_000 and "Эти уроки будут оплачены преподавателю" in block["forgiveness"]["text"]
    admin = make_client(world, user=world.admin, org_role="admin")
    plain = admin.post(url, params={"dry_run": "true"}, json={"all": True, "reason": "Договорённость с родителем"}).json()["money_effects"]
    assert plain["forgiveness"]["teacher_pay"] is None and not any(ch.isdigit() for ch in plain["forgiveness"]["text"].split("период")[1].split(".")[-1])         # the pay is named, the sum is the owner's
    # the real forgiveness: the closed month of the teacher turns stale, the night check says so
    done = owner(world).post(url, json={"all": True, "reason": "Договорённость с родителем"})
    assert done.status_code == 201, done.text
    assert owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}").json()["stale"] is True
    (drift,) = [f for f in mr.reconcile_org(world.db, world.org.id) if f.invariant == "I-3"]
    assert (drift.details["what"], drift.expected, drift.actual, drift.details["changedBy"]) == ("drift", 600_000, 475_000, 125_000)
    # the cancellation of the forgiveness says the same about the pay, with the opposite sign: the lessons stop being paid to the teacher
    wid = done.json()["writeoff"]["id"]
    undo = owner(world).post(f"{API}/org/writeoffs/{wid}/void", params={"dry_run": "true"}, json={"reason": "Ошибка"})
    assert undo.status_code == 200, undo.text
    forgiveness = undo.json()["money_effects"]["forgiveness"]
    assert forgiveness["teacher_pay"] == -125_000 and forgiveness["text"].startswith("Отмена прощения вернёт долг. Эти уроки перестанут оплачиваться преподавателю")
    assert owner(world).post(f"{API}/org/writeoffs/{wid}/void", json={"reason": "Ошибка"}).json()["money_effects"]["forgiveness"]["teacher_pay"] == -125_000


# ---------------------------------------------------------------------------------------------------- T17 back-dating is the owner's after 7 days


OWNER_ONLY = "Запись старше 7 дней вносит владелец"


def _admin_with_the_right(world):
    from tests.ops_helpers import grant_money_right
    from tests.pg_world import make_client

    grant_money_right(world)
    return make_client(world, user=world.admin, org_role="admin")


def test_t17_an_administrator_may_enter_a_payment_dated_seven_days_back_and_not_eight_the_owner_any_day(world, fixed_today):
    # today 20.11.2026.  7 days back = 13.11 (allowed), 8 days = 12.11 (refused for an administrator).  The preview refuses as the write does.
    a, b, c = groups3(world, fixed_today)
    s0, s1, s2 = world.students
    for s in (s0, s1, s2):
        stay(world, membership(world, s, a), date(2026, 11, 2))
    admin = _admin_with_the_right(world)
    url = f"{API}/org/memberships/{membership(world, s0, a).id}/payments"
    body = lambda day: {"amount": 100_000, "paid_on": day, "method": "cash"}      # noqa: E731
    assert admin.post(url, json=body("2026-11-13")).status_code == 201
    r = admin.post(url, json=body("2026-11-12"))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "BACKDATE_OWNER_ONLY" and r.json()["detail"]["message"] == OWNER_ONLY, r.text
    assert admin.post(url, params={"dry_run": "true"}, json=body("2026-11-12")).status_code == 403            # the window refuses as the write does
    assert admin.get(f"{url}/preview", params={"amount": 100_000, "paid_on": "2026-11-12"}).status_code == 403
    assert admin.get(f"{url}/preview", params={"amount": 100_000, "paid_on": "2026-11-13"}).status_code == 200
    assert owner(world).post(url, json=body("2026-11-12")).status_code == 201                                  # the owner: as before
    assert owner(world).post(url, json=body("2026-10-05")).status_code == 201


def test_t17_the_correction_of_the_date_the_refund_and_the_forgiveness_follow_the_same_limit_a_correction_that_keeps_the_date_does_not(world, fixed_today):
    # S0 paid 300 000 on 02.11 (owner).  Debt of S1 (nothing paid) for the writeoff.  Today 20.11: 8 days back = 12.11.
    a, b, c = groups3(world, fixed_today)
    s0, s1, s2 = world.students
    for s in (s0, s1, s2):
        stay(world, membership(world, s, a), date(2026, 11, 2))
    paid = pay(world, s0, a, amount=300_000, paid_on=date(2026, 11, 2))["payment"]
    admin = _admin_with_the_right(world)
    replace = lambda **kw: admin.post(f"{API}/org/payments/{paid['id']}/replace", json={"amount": 300_000, "paid_on": "2026-11-02", "method": "cash", "reason": "Исправление", **kw})      # noqa: E731
    # only the method of an old payment: the date stays 02.11 (18 days old) - allowed
    r = replace(method="card")
    assert r.status_code == 201, r.text
    new = r.json()["payment"]
    # a new date older than 7 days: refused (and the preview of the correction with it); 13.11: allowed
    r = admin.post(f"{API}/org/payments/{new['id']}/replace", json={"amount": 300_000, "paid_on": "2026-11-12", "method": "card", "reason": "Дата"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "BACKDATE_OWNER_ONLY", r.text
    r = admin.post(f"{API}/org/payments/{new['id']}/replace", json={"amount": 300_000, "paid_on": "2026-11-13", "method": "card", "reason": "Дата"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "BACKDATE_OWNER_ONLY", r.text      # R3 П4: and the record itself is 18 days old: its date is not the administrator's to move (the border: test_money2_fix_r3_pg)
    # the refund and the forgiveness with a date: 8 days back refused for the administrator, 7 allowed; no date = today
    m1 = membership(world, s1, a)
    wo = lambda day: admin.post(f"{API}/org/memberships/{m1.id}/writeoffs", json={"amount": 10_000, "reason": "Договорённость", **({"effective_on": day} if day else {})})      # noqa: E731
    assert wo("2026-11-12").status_code == 403 and wo("2026-11-12").json()["detail"]["code"] == "BACKDATE_OWNER_ONLY"
    assert wo("2026-11-13").status_code == 201 and wo(None).status_code == 201
    pay(world, s2, a, amount=5_000_000, paid_on=date(2026, 11, 2))         # a big advance for S2: the refund of 1 000 is within the limit on any day
    m0 = membership(world, s2, a)
    rf = lambda day: admin.post(f"{API}/org/memberships/{m0.id}/refunds", json={"amount": 1_000, "method": "cash", "reason": "Возврат", "effective_on": day})      # noqa: E731
    assert rf("2026-11-12").status_code == 403 and rf("2026-11-12").json()["detail"]["code"] == "BACKDATE_OWNER_ONLY"
    ro = owner(world).post(f"{API}/org/memberships/{m0.id}/refunds", json={"amount": 1_000, "method": "cash", "reason": "Возврат", "effective_on": "2026-11-12"})
    assert ro.status_code == 201, ro.text     # the owner: any day


def test_t17_the_restoration_of_a_cancelled_payment_is_not_limited_by_the_age_of_the_payment(world, fixed_today):
    # the owner cancelled a payment of 02.11 (18 days old); the administrator restores it: the money is not new, the date is the old one - allowed (Т7 stays as it is)
    a, b, c = groups3(world, fixed_today)
    s0 = world.students[0]
    stay(world, membership(world, s0, a), date(2026, 11, 2))
    paid = pay(world, s0, a, amount=300_000, paid_on=date(2026, 11, 2))["payment"]
    assert owner(world).post(f"{API}/org/payments/{paid['id']}/void", json={"reason": "Ошибка"}).status_code == 200
    r = _admin_with_the_right(world).post(f"{API}/org/payments/{paid['id']}/restore", json={"reason": "Отменили по ошибке"})
    assert r.status_code == 201, r.text
    assert r.json()["payment"]["paid_on"] == "2026-11-02"


# ---------------------------------------------------------------------------------------------------- T18 debts by age


def test_t18_the_debts_of_the_debtors_fall_into_age_baskets_with_the_borders_30_31_60_61_90_91_and_the_filter_and_the_summary_agree(world, fixed_today):
    # today 20.11.2026; nobody paid; the age is the count of «Просрочено на N дней»: the days after the day paid through, the first unpaid day = the day of joining is day ONE.
    # joined 22.10 -> 30 days (basket «to_30»); 21.10 -> 31 («31_60»); 22.09 -> 60; 21.09 -> 61 («61_90»); 23.08 -> 90; 22.08 -> 91 («over_90»).
    a, b, c = groups3(world, fixed_today)
    settings_block(world, accounting_start_date="2026-06-01", billing_anchor="fixed_day", billing_day=1)
    ages = {30: "to_30", 31: "31_60", 60: "31_60", 61: "61_90", 90: "61_90", 91: "over_90"}
    students = {}
    for n in ages:
        s = fresh_student(world, f"Возраст{n}")
        add(world, s, group=a, mode="add")
        stay(world, membership(world, s, a), DAY - timedelta(days=n - 1))
        students[n] = s
    page = owner(world).get(f"{API}/org/payments/status", params={"bucket": "debtors", "limit": 100}).json()
    rows = {i["debt_age_days"]: (i["debt_age_days"], i["debt_age_bucket"], i["debt"]) for i in page["items"]}
    by_age = {n: rows[n] for n in ages}                       # (the rows of the world's own students are in the page too: they are in the summary below)
    for n, basket in ages.items():
        assert by_age[n][1] == basket, (n, by_age[n])
    # the filter: one basket = exactly its students; the total of the page follows the filter
    for basket in ("to_30", "31_60", "61_90", "over_90"):
        r = owner(world).get(f"{API}/org/payments/status", params={"bucket": "debtors", "age": basket, "limit": 100}).json()
        assert {i["debt_age_days"] for i in r["items"]} >= {n for n, k in ages.items() if k == basket} and {i["debt_age_bucket"] for i in r["items"]} == {basket} and r["total"] == len(r["items"])
    # the summary: per basket the number of students and the sum equal the sums of the rows of that basket
    summary = owner(world).get(f"{API}/org/payments/summary").json()["debt_ages"]
    assert [x["bucket"] for x in summary] == ["to_30", "31_60", "61_90", "over_90"]
    for x in summary:
        of_basket = [v for v in page["items"] if v["debt_age_bucket"] == x["bucket"]]
        assert x["students"] == len({v["student_id"] for v in of_basket}) and x["memberships"] == len(of_basket) and x["debt_total"] == sum(v["debt"] for v in of_basket)
    assert sum(x["students"] for x in summary) == owner(world).get(f"{API}/org/payments/summary").json()["debt_split"]["debtors"]


# ---------------------------------------------------------------------------------------------------- T19 the line of the journal of a replaced opening balance


def test_t19_the_return_into_a_group_that_replaces_an_opening_balance_leaves_a_line_with_the_cancelled_row_and_the_real_role(world, fixed_today):
    # the owner adds S with a debt of 250 000 (opening row 1), S leaves, the owner brings him back with a debt of 100 000 (row 1 is cancelled, row 2 lives).
    # The journal: the line ``opening_balance_set`` that names row 1 in ``voided_opening_id`` and has the role OWNER (before: «admin» written by hand), and a caller without an author is not possible.
    import inspect

    from src.auth.leave_service import apply_add_opening

    a, b, c = groups3(world, fixed_today)
    s0 = world.students[0]
    url = f"{API}/org/groups/{b.id}/students/{s0.id}"
    first = owner(world).post(url, params={"mode": "add"}, json={"opening_balance": {"debt_amount": 250_000, "debt_on": "2026-10-31"}})
    assert first.status_code == 200, first.text
    assert owner(world).post(f"{url}/leave", json={}).status_code == 200
    world.db.expire_all()
    from src.models.auth import GroupAudit, MembershipOpeningBalance

    (row1,) = world.db.query(MembershipOpeningBalance).filter_by(student_id=s0.id, group_id=b.id).all()
    fixed_today(date(2026, 11, 21))                       # the next day: the return the same day would cancel the leaving, which keeps the stay and its opening (R3 П1)
    back = owner(world).post(url, params={"mode": "add"}, json={"opening_balance": {"debt_amount": 100_000, "debt_on": "2026-10-31"}})
    assert back.status_code == 200, back.text
    world.db.expire_all()
    lines = [g for g in world.db.query(GroupAudit).filter_by(org_id=world.org.id, action="opening_balance_set") if g.snapshot.get("voided_opening_id") == str(row1.id)]
    assert len(lines) == 1 and lines[0].actor_role == "owner" and lines[0].actor_id == world.owner.id
    from tests.test_money2_chains_pg import quiet

    quiet(world, "the return replaced the opening balance")          # the nightly reconciliation finds nothing (no ``opening:<id>:no_journal``)
    assert inspect.signature(apply_add_opening).parameters["actor_id"].default is inspect.Parameter.empty and inspect.signature(apply_add_opening).parameters["actor_id"].annotation in (str, "str")


# ---------------------------------------------------------------------------------------------------- T20 the product does not import the module of the check


def test_t20_the_payments_service_takes_the_actions_of_the_journal_from_the_neutral_module_not_from_the_reconciliation():
    import ast
    import inspect

    from src.auth import payments_service

    imported = {n.module for n in ast.walk(ast.parse(inspect.getsource(payments_service))) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(ast.parse(inspect.getsource(payments_service))) if isinstance(n, ast.Import) for a in n.names}
    assert not any("money_reconcile" in (m or "") for m in imported), imported
    assert not any(isinstance(n, ast.ImportFrom) and any(a.name.startswith("money_reconcile") for a in n.names) for n in ast.walk(ast.parse(inspect.getsource(payments_service))))
    assert "money_journal_actions" in inspect.getsource(payments_service)
