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
