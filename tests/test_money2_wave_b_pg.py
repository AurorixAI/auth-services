"""Money-2 / wave B (the server): T10..T21 (PostgreSQL).  Every money number is worked out by hand in the comment of its test."""
from datetime import date, datetime, timedelta

import pytest

from src.models.auth import (
    GroupAudit, MembershipPause, MembershipTransfer, OrgMoneyAudit, Organization, OrganizationMember, Student, StudentGroup, StudentPayment,
)

from src.auth import payments_texts as tx
from tests.money1b_helpers import account, add, fresh_student, set_schedule, slot
from tests.pg_world import business_today, ensure_billing, give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
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
