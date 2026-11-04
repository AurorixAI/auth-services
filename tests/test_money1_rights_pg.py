"""Money-1 A4 in action: the routes that teachers and administrators use for everyday work answer 403 ``MONEY_RIGHT_REQUIRED`` only for the part that moves money (PostgreSQL)."""
from datetime import date, timedelta
from uuid import uuid4

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


def test_r3_1_a_day_off_changes_the_deductions_so_it_is_for_the_money_right_but_the_journal_is_for_any_manager(world, now_4_nov):
    """«Выходной день центра» turns the cancellations «по вине центра» into «праздник» (and back): the same money as ``cancellation-kind`` - the same right."""
    days = f"{P}/days-off"
    body = {"dateFrom": "2026-11-20", "dateTo": "2026-11-20", "reason": "Праздник"}
    admin, owner, teacher = (make_client(world, user=u) for u in (world.admin, world.owner, world.teacher))
    for call in (lambda c: c.post(f"{days}/preview", json=body), lambda c: c.post(days, json=body),
                 lambda c: c.post(f"{days}/{uuid4()}/apply"), lambda c: c.post(f"{days}/{uuid4()}/void")):
        assert call(admin).status_code == 403 and call(teacher).status_code == 403          # an administrator without the right; a teacher
    assert admin.get(days).status_code == 200                                               # the journal is a read: any manager
    made = owner.post(days, json=body)
    assert made.status_code == 201, made.text
    day_off_id = made.json()["dayOff"]["id"]
    assert admin.post(f"{days}/{day_off_id}/void").status_code == 403
    grant_money_right(world)
    assert admin.post(f"{days}/preview", json={"dateFrom": "2026-11-23", "dateTo": "2026-11-23"}).status_code == 200
    assert admin.post(f"{days}/{day_off_id}/apply").status_code == 200
    assert admin.post(f"{days}/{day_off_id}/void").status_code == 200


def test_r3_acceptance_the_type_of_lessons_in_the_edit_of_a_group_needs_the_money_right_and_the_request_is_all_or_nothing(world, now_4_nov):
    """``PUT /org/groups/{id}`` with ``lesson_type`` changes the pay rate of the teacher (rates are per teacher x type): an administrator without the right gets 403 and NOTHING of the request is written
    (not the name of the same request either); the other fields stay for any manager."""
    from src.models.auth import GroupAudit, GroupLessonTypeHistory

    url = f"{P}/groups/{world.group.id}"
    name = world.group.name
    admin, owner = make_client(world, user=world.admin), make_client(world, user=world.owner)

    def state():
        world.db.expire_all()
        g = world.db.get(type(world.group), world.group.id)
        return (g.name, g.lesson_type, world.db.query(GroupLessonTypeHistory).filter_by(group_id=g.id).count(), world.db.query(GroupAudit).filter_by(group_id=g.id).count())

    before = state()
    assert before[1] == "group"
    refused = admin.put(url, json={"name": "Переименована", "lesson_type": "pair"})
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "MONEY_RIGHT_REQUIRED"
    assert state() == before and before[0] == name                                                   # neither the type nor the name of the same request, no history row, no audit row
    assert admin.put(url, json={"name": "Только имя"}).status_code == 200                              # the other fields: any manager
    after_name = state()
    assert after_name[0] == "Только имя" and after_name[1] == "group"
    assert admin.put(url, json={"name": "Только имя", "lesson_type": "group"}).status_code == 200      # the SAME type is not a change: no right needed
    assert owner.put(url, json={"lesson_type": "pair"}).status_code == 200                              # the owner
    changed = state()
    assert changed[1] == "pair" and changed[2] > after_name[2] and changed[3] > after_name[3]            # the history row of the new type (+ the seed row of a group without history) and an audit row
    grant_money_right(world)
    assert admin.put(url, json={"lesson_type": "individual"}).status_code == 200                       # an administrator the owner allowed
    assert state()[1] == "individual"
