"""N13 P1: group edits are validated and journaled; bulk add explains every skip (PostgreSQL)."""
from uuid import uuid4

import pytest

from src.models.auth import Group, GroupAudit, Student, StudentGroup

from tests.pg_world import (  # noqa: F401  (fixtures)
    make_client, make_group, make_student, needs_pg, pg_session, world,
)

pytestmark = needs_pg


def put(client, w, **body):
    return client.put(f"/api/v1/auth/org/groups/{w.group.id}", json=body)


def audits(w):
    w.db.expire_all()
    return w.db.query(GroupAudit).filter_by(group_id=w.group.id).order_by(GroupAudit.created_at).all()


def test_teacher_change_is_validated_and_audited(world):
    client = make_client(world)
    r = put(client, world, teacher_id=str(world.teacher2.id))
    assert r.status_code == 200, r.text
    assert r.json()["teacher_id"] == str(world.teacher2.id)
    (row,) = audits(world)
    assert row.action == "teacher_changed" and row.actor_id == world.admin.id and row.actor_role == "admin"
    assert row.assigned_teacher_id == world.teacher2.id
    assert row.snapshot == {"teacher_id": [str(world.teacher.id), str(world.teacher2.id)]}


@pytest.mark.parametrize("who", ["foreign_teacher", "random", "inactive", "garbage"])  # LOGIC-B (lead decision): an admin / owner of the centre may be a group teacher
def test_wrong_teacher_is_400_and_nothing_changes(world, who):
    from tests.pg_world import make_member, make_user

    inactive = make_user(world.db, "teacher")
    make_member(world.db, world.org, inactive, "teacher")
    from src.models.auth import OrganizationMember
    world.db.query(OrganizationMember).filter_by(user_id=inactive.id).update({"is_active": False})
    foreign = make_user(world.db, "teacher")
    make_member(world.db, world.other_org, foreign, "teacher")
    world.db.commit()
    value = {
        "foreign_teacher": str(foreign.id), "admin_user": str(world.admin.id), "random": str(uuid4()),
        "inactive": str(inactive.id), "garbage": "not-a-uuid",
    }[who]
    r = put(make_client(world), world, teacher_id=value, name="Новое имя")
    assert r.status_code == 400, r.text
    world.db.expire_all()
    group = world.db.get(Group, world.group.id)
    assert group.teacher_id == world.teacher.id and group.name == "Группа"  # nothing was applied
    assert audits(world) == []
    if who != "garbage":
        assert r.json()["detail"] == "Выбранный сотрудник не преподаватель центра"


def test_other_fields_write_updated_audit_with_old_and_new(world):
    client = make_client(world, user=world.owner)
    # LOGIC-B O4: the status is not a field of this save any more (422 STATUS_USE_ARCHIVE_ACTION, see test_ops_o4_group_archive_pg)
    assert put(client, world, name="Новая группа", max_students=12).status_code == 200
    (row,) = audits(world)
    assert row.action == "updated" and row.actor_id == world.owner.id and row.actor_role == "owner"
    assert row.snapshot == {"name": ["Группа", "Новая группа"], "max_students": [30, 12]}


def test_teacher_and_other_changes_in_one_save_is_a_single_teacher_changed_row(world):
    assert put(make_client(world), world, name="Другое", teacher_id=str(world.teacher2.id)).status_code == 200
    (row,) = audits(world)
    assert row.action == "teacher_changed" and set(row.snapshot) == {"name", "teacher_id"}


def test_nothing_changed_writes_nothing(world):
    client = make_client(world)
    assert put(client, world, name="Группа", teacher_id=str(world.teacher.id)).status_code == 200
    assert put(client, world).status_code == 200
    assert audits(world) == []


def test_teacher_cannot_edit_groups_and_foreign_group_is_not_found(world):
    assert put(make_client(world, user=world.teacher), world, name="Взлом").status_code == 403
    foreign = make_client(world, user=world.foreign_admin, org="other")
    assert put(foreign, world, name="Чужая").status_code == 400  # "Группа не найдена" for another tenant
    world.db.expire_all()
    assert world.db.get(Group, world.group.id).name == "Группа"


# ---- bulk add ------------------------------------------------------------


def test_bulk_add_reports_a_reason_for_every_skipped_student(world):
    db = world.db
    target = make_group(db, world.org, world.teacher2, "Вторая")
    fresh = Student(org_id=world.org.id, first_name="Новый", last_name="Ученик", class_level=9)
    archived = Student(org_id=world.org.id, first_name="Архив", last_name="Ученик", class_level=9, status="archived")
    db.add_all([fresh, archived]); db.flush()
    already = make_student(db, world.org, target, "Уже", "Здесь", "N13-777")
    db.commit()
    ids = [fresh.id, archived.id, already.id, uuid4(), world.foreign_student.id]
    r = make_client(world).post(f"/api/v1/auth/org/groups/{target.id}/students/bulk", json={"student_ids": [str(i) for i in ids]})
    assert r.status_code == 200, r.text
    body = r.json()
    # "already here" is an idempotent no-op that the service counts as handled.
    assert body["group_id"] == str(target.id) and body["added"] == 2
    assert body["skipped"] == 3 and len(body["skipped_details"]) == 3
    reasons = {d["student_id"]: d["reason"] for d in body["skipped_details"]}
    assert set(reasons) == {str(archived.id), str(ids[3]), str(world.foreign_student.id)}
    assert "Архивного" in reasons[str(archived.id)]
    assert reasons[str(ids[3])] == "Ученик не найден" == reasons[str(world.foreign_student.id)]
    db.expire_all()
    assert db.query(StudentGroup).filter_by(student_id=fresh.id, group_id=target.id, is_active=True).count() == 1


def test_bulk_add_to_archived_group_skips_everyone_with_reason(world):
    group = world.db.get(Group, world.group.id)
    group.status = "archived"
    world.db.commit()
    extra = Student(org_id=world.org.id, first_name="Эхо", last_name="Ученик", class_level=9)
    world.db.add(extra); world.db.commit()
    body = make_client(world).post(f"/api/v1/auth/org/groups/{world.group.id}/students/bulk",
                                   json={"student_ids": [str(extra.id)]}).json()
    assert (body["added"], body["skipped"]) == (0, 1)
    assert body["skipped_details"] == [{"student_id": str(extra.id), "reason": "Нельзя зачислить ученика в архивную группу"}]
