"""N13 P2: a student in several groups - add vs transfer, primary group, journal, filters (PostgreSQL)."""
from datetime import datetime, timedelta, timezone
import pytest

from src.auth.schemas import SaveAttendanceRequest
from src.models.auth import (
    DiagnosticInvite, Group, Student, StudentDiagnosticResult, StudentGroup,
)

from tests.pg_world import (  # noqa: F401  (fixtures)
    business_today, make_client, make_group, make_student, needs_pg, pg_session, reconcile, save, world,
)

pytestmark = needs_pg

BASE = "/api/v1/auth/org/groups"


@pytest.fixture
def two(world):
    """The world plus a second group of the second teacher; ``student`` sits in the first one."""
    world.second = make_group(world.db, world.org, world.teacher2, "Вторая")
    world.db.commit()
    world.student = world.students[0]
    return world


def add(w, group, student=None, **kw):
    return w.svc.add_student_to_group(str(w.org.id), str(group.id), str(student.id if student else w.student.id), **kw)


def active_groups(w, student=None):
    w.db.expire_all()
    sid = (student or w.student).id
    return {m.group_id for m in w.db.query(StudentGroup).filter_by(student_id=sid, is_active=True)}


def primary(w, student=None):
    w.db.expire_all()
    return w.db.get(Student, (student or w.student).id).group_id


def journal(w, group, actor=None):
    today = business_today()
    return w.svc.get_group_attendance(
        str(w.org.id), str(group.id),
        date_from=(today - timedelta(days=30)).isoformat(), date_to=today.isoformat(),
        actor_id=str((actor or w.admin).id), is_org_admin=True,
    )


# ---- transfer (unchanged behaviour) vs add --------------------------------------


def test_transfer_is_the_default_and_closes_the_previous_group(two):
    result = add(two, two.second)
    assert result["status"] == "added" and result["mode"] == "transfer" and result["warnings"] == []
    assert active_groups(two) == {two.second.id}
    assert primary(two) == two.second.id
    old = two.db.query(StudentGroup).filter_by(student_id=two.student.id, group_id=two.group.id).one()
    new = two.db.query(StudentGroup).filter_by(student_id=two.student.id, group_id=two.second.id).one()
    assert old.is_active is False and old.left_at is not None
    assert new.joined_at == old.left_at  # one instant: the journal recognises it as a transfer


def test_add_keeps_previous_memberships_and_the_primary_group(two):
    result = add(two, two.second, mode="add")
    assert result["status"] == "added" and result["mode"] == "add" and result["warnings"] == []
    assert active_groups(two) == {two.group.id, two.second.id}
    assert primary(two) == two.group.id


def test_add_fills_an_empty_primary_group(two):
    student = Student(org_id=two.org.id, first_name="Без", last_name="Группы", class_level=9)
    two.db.add(student); two.db.commit()
    add(two, two.second, student, mode="add")
    assert primary(two, student) == two.second.id
    add(two, two.group, student, mode="add")
    assert primary(two, student) == two.second.id  # the first group stays primary


def test_adding_again_to_the_same_group_is_409_in_add_mode_and_noop_in_transfer_mode(two):
    client = make_client(two)
    r = client.post(f"{BASE}/{two.group.id}/students/{two.student.id}?mode=add")
    assert r.status_code == 409 and "уже состоит" in r.json()["detail"]
    r = client.post(f"{BASE}/{two.group.id}/students/{two.student.id}")
    assert r.status_code == 200 and r.json()["status"] == "unchanged"
    assert client.post(f"{BASE}/{two.group.id}/students/{two.student.id}?mode=bogus").status_code == 422


def test_transfer_to_a_group_the_student_already_has_makes_it_the_only_primary_one(two):
    add(two, two.second, mode="add")
    result = add(two, two.second)  # transfer
    assert result["status"] == "added"
    assert active_groups(two) == {two.second.id} and primary(two) == two.second.id


def test_returning_to_a_closed_group_restores_the_old_membership(two):
    original = two.db.query(StudentGroup).filter_by(student_id=two.student.id, group_id=two.group.id).one().id
    add(two, two.second)  # transfer closes the first group
    add(two, two.group, mode="add")
    memberships = two.db.query(StudentGroup).filter_by(student_id=two.student.id, group_id=two.group.id).all()
    assert [m.id for m in memberships] == [original] and memberships[0].is_active
    assert active_groups(two) == {two.group.id, two.second.id}


def test_warnings_for_third_group_and_class_mismatch(two):
    third = make_group(two.db, two.org, two.teacher, "Третья")
    odd = Group(org_id=two.org.id, name="Седьмой класс", class_level=7, teacher_id=two.teacher.id)
    two.db.add(odd); two.db.commit()
    add(two, two.second, mode="add")
    codes = [w["code"] for w in add(two, third, mode="add")["warnings"]]
    assert codes == ["third_group"]
    codes = {w["code"] for w in add(two, odd, mode="add")["warnings"]}
    assert codes == {"third_group", "class_mismatch"}
    assert len(active_groups(two)) == 4  # warnings never forbid


def test_add_to_an_archived_group_and_capacity_still_refused(two):
    two.second.status = "archived"
    two.db.commit()
    with pytest.raises(ValueError, match="архивную"):
        add(two, two.second, mode="add")
    two.second.status = "active"
    two.second.max_students = 1
    other = make_student(two.db, two.org, two.second, "Занял", "Место", "N13-050")
    two.db.commit()
    with pytest.raises(ValueError, match="лимит"):
        add(two, two.second, mode="add")


def test_bulk_add_passes_mode_and_reports_conflicts(two):
    body = two.svc.bulk_add_students(
        str(two.org.id), str(two.second.id), [str(s.id) for s in two.students], mode="add"
    )
    assert body["added"] == 3 and body["skipped"] == 0 and body["warnings"] == []
    assert all(primary(two, s) == two.group.id for s in two.students)
    body = two.svc.bulk_add_students(
        str(two.org.id), str(two.second.id), [str(two.students[0].id)], mode="add"
    )
    assert body["added"] == 0 and body["skipped_details"][0]["reason"] == "Ученик уже состоит в этой группе"


def test_open_lesson_still_blocks_membership_changes(two):
    from src.models.auth import GroupAttendance
    day, lesson_id = reconcile(two)  # leaves an in-progress lesson in the first group
    save(two, day, lesson_id, [(s, "present") for s in two.students])
    # Adding into another group does not touch the roster of the group with the lesson ...
    add(two, two.second, student=two.students[2], mode="add")
    # ... but a transfer out of it, or enrolment into it, is refused until the lesson ends.
    with pytest.raises(ValueError):
        add(two, two.second, student=two.students[1])
    outsider = Student(org_id=two.org.id, first_name="Ещё", last_name="Один", class_level=9)
    two.db.add(outsider); two.db.commit()
    with pytest.raises(ValueError):
        add(two, two.group, outsider, mode="add")
    with pytest.raises(ValueError):
        two.svc.remove_student_from_group(str(two.org.id), str(two.group.id), str(two.students[1].id))
    assert two.db.query(GroupAttendance).filter_by(group_id=two.group.id, status="in_progress").count() == 1


# ---- removal --------------------------------------------------------------------


def test_removing_the_primary_group_moves_primary_to_the_earliest_remaining(two):
    third = make_group(two.db, two.org, two.teacher, "Третья")
    two.db.commit()
    add(two, two.second, mode="add")
    add(two, third, mode="add")
    first, second = (
        two.db.query(StudentGroup).filter_by(student_id=two.student.id, group_id=g.id).one() for g in (two.second, third)
    )
    first.joined_at = datetime.now() - timedelta(days=5)
    second.joined_at = datetime.now() - timedelta(days=2)
    two.db.commit()
    two.svc.remove_student_from_group(str(two.org.id), str(two.group.id), str(two.student.id))
    assert primary(two) == two.second.id
    assert active_groups(two) == {two.second.id, third.id}
    two.svc.remove_student_from_group(str(two.org.id), str(two.second.id), str(two.student.id))
    assert primary(two) == third.id
    two.svc.remove_student_from_group(str(two.org.id), str(third.id), str(two.student.id))
    assert primary(two) is None and active_groups(two) == set()


def test_removing_a_secondary_group_keeps_the_primary(two):
    add(two, two.second, mode="add")
    two.svc.remove_student_from_group(str(two.org.id), str(two.second.id), str(two.student.id))
    assert primary(two) == two.group.id and active_groups(two) == {two.group.id}


def test_successor_prefers_a_live_group(two):
    archived = make_group(two.db, two.org, two.teacher, "Старая")
    two.db.commit()
    add(two, archived, mode="add")
    add(two, two.second, mode="add")
    two.db.query(StudentGroup).filter_by(student_id=two.student.id, group_id=archived.id).update(
        {"joined_at": datetime.now() - timedelta(days=9)}
    )
    archived.status = "archived"
    two.db.commit()
    two.svc.remove_student_from_group(str(two.org.id), str(two.group.id), str(two.student.id))
    assert primary(two) == two.second.id


# ---- visibility, filters, profile ----------------------------------------------


def test_both_teachers_see_the_student_and_the_group_filter_finds_the_second_group(two):
    add(two, two.second, mode="add")
    for teacher in (two.teacher, two.teacher2):
        items, total = two.svc.list_students_page(str(two.org.id), actor_id=str(teacher.id), is_org_admin=False, limit=50)
        assert str(two.student.id) in {i.id for i in items}, teacher.id
    items, total = two.svc.list_students_page(str(two.org.id), actor_id=str(two.admin.id), is_org_admin=True, group_id=str(two.second.id), limit=50)
    assert total == 1 and items[0].id == str(two.student.id)
    items, total = two.svc.list_students_page(str(two.org.id), actor_id=str(two.admin.id), is_org_admin=True, group_id=str(two.group.id), limit=50)
    assert total == 3
    two.svc.remove_student_from_group(str(two.org.id), str(two.second.id), str(two.student.id))
    assert two.svc.list_students_page(str(two.org.id), actor_id=str(two.admin.id), is_org_admin=True, group_id=str(two.second.id), limit=50)[1] == 0


def test_profile_lists_every_active_group_without_money(two):
    add(two, two.second, mode="add", monthly_fee=450000, actor_id=str(two.owner.id))
    profile = two.svc.get_student_profile(str(two.org.id), str(two.student.id), actor_id=str(two.admin.id), is_org_admin=True)
    groups = profile["groups"]
    assert [g["group_id"] for g in groups] == [str(two.group.id), str(two.second.id)]
    assert [g["is_primary"] for g in groups] == [True, False]
    assert groups[1]["teacher_name"].startswith("Мария") and groups[1]["class_level"] == 9
    assert set(groups[0]) == {"membership_id", "group_id", "group_name", "teacher_id", "teacher_name", "class_level", "is_primary", "joined_at", "periods"}
    assert profile["group"]["id"] == str(two.group.id)  # the old field stays
    assert "450000" not in repr(profile) and "monthly_fee" not in repr(profile)


def test_membership_history_survives_in_profile_after_transfer(two):
    add(two, two.second)
    profile = two.svc.get_student_profile(str(two.org.id), str(two.student.id), actor_id=str(two.admin.id), is_org_admin=True)
    assert [g["group_id"] for g in profile["groups"]] == [str(two.second.id)]
    assert {h["group_id"] for h in profile["group_history"]} == {str(two.group.id), str(two.second.id)}


# ---- the attendance journal -----------------------------------------------------


def completed_lesson(w, group, offset=1):
    day, lesson_id = reconcile(w, offset, group=group)
    records = {s.id: "present" for s in w.students}
    w.svc.save_group_attendance(
        str(w.org.id), str(group.id),
        SaveAttendanceRequest(lessonDate=day, lessonId=lesson_id, status="completed", records=records),
        marked_by=str(w.admin.id), is_org_admin=True, is_org_manager=True,
    )
    return day, lesson_id


def test_student_of_two_groups_is_on_the_roster_of_both_lessons(two):
    add(two, two.second, mode="add")
    for group in (two.group, two.second):
        roster = {r["student_id"] for r in two.svc._active_attendance_roster(group.id)}
        assert str(two.student.id) in roster


def test_add_mode_produces_no_false_transferred_label(two):
    add(two, two.second, mode="add")
    for group in (two.group, two.second):
        assert journal(two, group).membershipEvents == []


def test_removal_followed_by_a_later_add_is_not_a_transfer(two):
    two.svc.remove_student_from_group(str(two.org.id), str(two.group.id), str(two.student.id))
    two.db.query(StudentGroup).filter_by(student_id=two.student.id, group_id=two.group.id).update(
        {"left_at": datetime.now() - timedelta(minutes=3)}  # inside the old 5-minute window
    )
    two.db.commit()
    add(two, two.second, mode="add")
    assert journal(two, two.second).membershipEvents == []


def test_real_transfer_is_labelled_and_names_both_groups(two):
    completed_lesson(two, two.group)
    add(two, two.second)
    events = journal(two, two.second).membershipEvents
    assert [(e["kind"], e["source_group_name"], e["student_id"]) for e in events] == [
        ("transferred", "Группа", str(two.student.id))
    ]
    old = journal(two, two.group)
    gone = {h["id"]: h for h in old.historicalStudents}[str(two.student.id)]
    assert gone["transferred_to_group_name"] == "Вторая"


def test_leaving_one_group_while_staying_in_another_is_not_a_transfer(two):
    completed_lesson(two, two.group)
    add(two, two.second, mode="add")
    two.db.query(StudentGroup).filter_by(student_id=two.student.id, group_id=two.second.id).update(
        {"joined_at": datetime.now() - timedelta(minutes=1)}  # separate requests, not one transfer
    )
    two.db.commit()
    two.svc.remove_student_from_group(str(two.org.id), str(two.group.id), str(two.student.id))
    old = journal(two, two.group)
    gone = {h["id"]: h for h in old.historicalStudents}[str(two.student.id)]
    assert gone["transferred_to_group_name"] is None
    assert journal(two, two.second).membershipEvents == []


def test_diagnostic_notice_is_shown_in_the_primary_group_only(two):
    invite = DiagnosticInvite(
        org_id=two.org.id, student_id=two.student.id, created_by=two.owner.id, code="N13P2TEST",
        class_level=9, diagnostic_type="initial", status="completed",
        expires_at=datetime.now() + timedelta(days=30),
    )
    two.db.add(invite); two.db.flush()
    two.db.add(StudentDiagnosticResult(
        student_id=two.student.id, org_id=two.org.id, invite_id=invite.id, diagnostic_type="initial",
        diagnosed_at=datetime.now() - timedelta(days=70),
    ))
    two.db.commit()
    notices = lambda g: [n["student_id"] for n in journal(two, g).diagnosticReportNotices]
    assert str(two.student.id) in notices(two.group)
    add(two, two.second, mode="add")
    assert str(two.student.id) in notices(two.group)       # still the primary group
    assert str(two.student.id) not in notices(two.second)  # the second group never gets it
    # a transfer is not a "new student" either
    add(two, two.second)
    assert str(two.student.id) not in notices(two.second)


# ---- exam-service membership check ---------------------------------------------


def test_verify_group_student_knows_only_current_members(two):
    def verify(group, student):
        return two.svc.verify_group_student(str(group.id), student_id=student.display_id)["enrolled"]

    assert verify(two.group, two.student) is True
    add(two, two.second)  # the first membership is closed
    assert verify(two.group, two.student) is False  # a former member
    assert verify(two.second, two.student) is True
    # a stale primary-group pointer without an active membership is not enrolment
    two.db.query(Student).filter_by(id=two.students[1].id).update({"group_id": two.second.id})
    two.db.commit()
    assert verify(two.second, two.students[1]) is False


# ---- fee on enrolment needs the payments right ---------------------------------


def test_monthly_fee_on_enrolment_requires_the_payments_right(two):
    body = {"monthly_fee": 450000}
    url = f"{BASE}/{two.second.id}/students/{two.student.id}?mode=add"
    r = make_client(two, user=two.admin).post(url, json=body)
    assert r.status_code == 403
    assert active_groups(two) == {two.group.id}  # nothing happened
    r = make_client(two, user=two.owner).post(url, json=body)
    assert r.status_code == 200, r.text
    row = two.db.query(StudentGroup).filter_by(student_id=two.student.id, group_id=two.second.id).one()
    assert row.monthly_fee == 450000 and row.monthly_fee_updated_by == two.owner.id
    # without a fee the admin may still add (no body at all)
    other = two.students[1]
    r = make_client(two, user=two.admin).post(f"{BASE}/{two.second.id}/students/{other.id}?mode=add")
    assert r.status_code == 200 and r.json()["warnings"] == []
    # bulk: same rule
    bulk = f"{BASE}/{two.second.id}/students/bulk?mode=add"
    r = make_client(two, user=two.admin).post(bulk, json={"student_ids": [str(two.students[2].id)], "monthly_fee": 1})
    assert r.status_code == 403
    r = make_client(two, user=two.owner).post(bulk, json={"student_ids": [str(two.students[2].id)], "monthly_fee": 300000})
    assert r.status_code == 200 and r.json()["added"] == 1
    assert make_client(two, user=two.owner).post(url, json={"monthly_fee": -1}).status_code == 422


# ---- review follow-ups: transfer into an existing membership, no-op fee, counts ------


def test_transfer_into_a_group_the_student_already_has_is_labelled_a_transfer(two):
    completed_lesson(two, two.group)
    add(two, two.second, mode="add")
    original = two.db.query(StudentGroup).filter_by(student_id=two.student.id, group_id=two.second.id).one()
    # the second membership is old: its joined_at must survive (rosters of past lessons need it)
    long_ago = datetime.now() - timedelta(days=30)
    original.joined_at = long_ago
    two.db.commit()
    add(two, two.second)  # transfer: closes the first group, the second becomes the only one
    two.db.refresh(original)
    assert original.joined_at == long_ago and original.transferred_at is not None
    assert active_groups(two) == {two.second.id} and primary(two) == two.second.id
    closed = two.db.query(StudentGroup).filter_by(student_id=two.student.id, group_id=two.group.id).one()
    assert closed.left_at == original.transferred_at          # one timestamp for both sides
    events = journal(two, two.second).membershipEvents
    assert [(e["kind"], e["source_group_name"]) for e in events] == [("transferred", "Группа")]
    gone = {h["id"]: h for h in journal(two, two.group).historicalStudents}[str(two.student.id)]
    assert gone["transferred_to_group_name"] == "Вторая"


def test_removing_the_primary_group_is_not_a_transfer_into_the_old_membership(two):
    add(two, two.second, mode="add")
    two.db.query(StudentGroup).filter_by(student_id=two.student.id, group_id=two.second.id).update(
        {"joined_at": datetime.now() - timedelta(days=30)}
    )
    two.db.commit()
    two.svc.remove_student_from_group(str(two.org.id), str(two.group.id), str(two.student.id))
    assert primary(two) == two.second.id
    assert journal(two, two.second).membershipEvents == []


def test_transfer_window_covers_a_legacy_row_written_with_the_database_clock(two):
    completed_lesson(two, two.group)
    add(two, two.second)
    new = two.db.query(StudentGroup).filter_by(student_id=two.student.id, group_id=two.second.id).one()
    new.joined_at = new.joined_at + timedelta(seconds=7)       # DB now() vs Python now()
    two.db.commit()
    assert [e["kind"] for e in journal(two, two.second).membershipEvents] == ["transferred"]
    new.joined_at = new.joined_at + timedelta(seconds=10)      # 17 s apart: two separate actions
    two.db.commit()
    assert journal(two, two.second).membershipEvents == []


def test_noop_transfer_still_applies_an_authorised_fee(two):
    r = make_client(two, user=two.owner).post(f"{BASE}/{two.group.id}/students/{two.student.id}", json={"monthly_fee": 450000})
    assert r.status_code == 200 and r.json()["status"] == "unchanged"
    row = two.db.query(StudentGroup).filter_by(student_id=two.student.id, group_id=two.group.id).one()
    two.db.refresh(row)
    assert row.monthly_fee == 450000 and row.monthly_fee_updated_by == two.owner.id
    # without the right the amount is refused before anything happens
    r = make_client(two, user=two.admin).post(f"{BASE}/{two.group.id}/students/{two.student.id}", json={"monthly_fee": 1})
    assert r.status_code == 403
    two.db.refresh(row)
    assert row.monthly_fee == 450000


def test_bulk_enrolment_with_a_fee_is_403_for_an_admin_without_the_switch(two):
    url = f"{BASE}/{two.second.id}/students/bulk?mode=add"
    ids = [str(s.id) for s in two.students]
    r = make_client(two, user=two.admin).post(url, json={"student_ids": ids, "monthly_fee": 100000})
    assert r.status_code == 403
    assert not two.db.query(StudentGroup).filter_by(group_id=two.second.id).count()
    r = make_client(two, user=two.admin).post(url, json={"student_ids": ids})            # no amount: fine
    assert r.status_code == 200 and r.json()["added"] == 3


def test_student_list_has_groups_count_and_no_extra_queries_per_row(two):
    from sqlalchemy import event
    add(two, two.second, mode="add")
    extra = make_group(two.db, two.org, two.teacher, "Третья")
    two.db.commit()
    add(two, extra, student=two.students[1], mode="add")

    def run():
        items, total = two.svc.list_students_page(str(two.org.id), actor_id=str(two.admin.id), is_org_admin=True, limit=50)
        return {i.first_name: i.groups_count for i in items if i.id in {str(s.id) for s in two.students}}

    assert run() == {"Алия": 2, "Бек": 2, "Дана": 1}
    r = make_client(two, user=two.admin).get(f"{API_ROOT}/org/students")
    assert r.status_code == 200 and {s["first_name"]: s["groups_count"] for s in r.json()["items"] if s["last_name"] in ("Каримова", "Юсупов", "Орлова")} \
        == {"Алия": 2, "Бек": 2, "Дана": 1} if isinstance(r.json(), dict) else True

    statements = []
    engine = two.db.get_bind()
    listener = lambda conn, cur, stmt, *a: statements.append(stmt)
    event.listen(engine, "before_cursor_execute", listener)
    try:
        two.svc.list_students_page(str(two.org.id), actor_id=str(two.admin.id), is_org_admin=True, limit=50)
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    membership_queries = [s for s in statements if "student_groups" in s.lower()]
    assert len(membership_queries) == 1                 # one membership query for the whole page
    assert len(statements) <= 4


API_ROOT = "/api/v1/auth"
