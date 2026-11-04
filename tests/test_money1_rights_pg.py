"""Money-1 A4 in action: the routes that teachers and administrators use for everyday work answer 403 ``MONEY_RIGHT_REQUIRED`` only for the part that moves money (PostgreSQL)."""
from datetime import date, timedelta

from tests.money1_helpers import hold_lesson, now_4_nov  # noqa: F401  (fixtures)
from tests.ops_helpers import grant_money_right
from tests.pg_world import make_client, make_group, make_student, needs_pg, pg_session, world  # noqa: F401  (fixtures)

pytestmark = needs_pg
P = "/api/v1/auth/org"


def test_who_taught_a_held_lesson_is_for_the_money_right_but_a_substitute_for_a_coming_lesson_is_not(world, now_4_nov):
    day, lid = hold_lesson(world, date(2026, 10, 30))
    admin = make_client(world, user=world.admin)
    body = {"lessonDate": day.isoformat(), "lessonId": lid, "teacherId": str(world.teacher2.id), "reason": "Заменил коллега"}
    refused = admin.put(f"{P}/groups/{world.group.id}/lesson-teacher", json=body)
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "MONEY_RIGHT_REQUIRED"
    coming = date(2026, 11, 9)           # Monday, not held: a substitute is everyday work
    ok = admin.put(f"{P}/groups/{world.group.id}/lesson-teacher", json={"lessonDate": coming.isoformat(), "lessonId": f"s{coming.isoweekday()}", "teacherId": str(world.teacher2.id), "reason": "Болезнь"})
    assert ok.status_code == 200, ok.text
    grant_money_right(world)
    assert admin.put(f"{P}/groups/{world.group.id}/lesson-teacher", json=body).status_code == 200


def test_a_transfer_is_for_the_money_right_a_plain_enrolment_is_not(world, now_4_nov):
    other = make_group(world.db, world.org, world.teacher2, "Вторая")
    world.db.commit()
    s0 = world.students[0]
    admin = make_client(world, user=world.admin)
    moved = admin.post(f"{P}/groups/{other.id}/students/{s0.id}?mode=transfer")
    assert moved.status_code == 403 and moved.json()["detail"]["code"] == "MONEY_RIGHT_REQUIRED"
    bulk = admin.post(f"{P}/groups/{other.id}/students/bulk?mode=transfer", json={"student_ids": [str(s0.id)]})
    assert bulk.status_code == 200 and bulk.json()["added"] == 0 and "переносит" in bulk.json()["skipped_details"][0]["reason"]
    added = admin.post(f"{P}/groups/{other.id}/students/{s0.id}?mode=add")                  # a second group, nothing closed: not a transfer
    assert added.status_code == 200, added.text
    grant_money_right(world)
    other2 = make_group(world.db, world.org, world.teacher2, "Третья")
    world.db.commit()
    assert admin.post(f"{P}/groups/{other2.id}/students/{world.students[1].id}?mode=transfer").status_code == 200


def test_a_student_who_paid_ahead_leaves_with_the_money_right_only(world, now_4_nov):
    from tests.pg_world import ensure_billing
    from tests.test_payments_service_pg import pay

    ensure_billing(world)
    s1, s2 = world.students[1], world.students[2]
    pay(world, s1, amount=2_000_000)                                  # far more than the debt: an advance
    admin = make_client(world, user=world.admin)
    plain = admin.delete(f"{P}/groups/{world.group.id}/students/{s2.id}")       # nothing paid ahead: everyday work
    assert plain.status_code == 200, plain.text
    refused = admin.delete(f"{P}/groups/{world.group.id}/students/{s1.id}")
    assert refused.status_code == 403, refused.text
    grant_money_right(world)
    assert admin.delete(f"{P}/groups/{world.group.id}/students/{s1.id}").status_code == 200
