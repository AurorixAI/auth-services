"""N13 «Ученик ушёл из центра»: archive / restore a student, counts, payments, payroll, locks (PostgreSQL)."""
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from src.auth.schemas import StartAttendanceRequest
from src.models.auth import (
    AttendanceExcuse, Group, GroupAttendance, GroupAudit, Student, StudentDiagnosticResult, StudentGroup,
    StudentPayment,
)

from tests.pg_world import (  # noqa: F401  (fixtures)
    LIVE_SLOT, business_today, give_price, make_client, make_group, make_student, needs_pg, pg_session, reconcile, save,
    world,
)
from tests.test_payments_service_pg import API, mid, owner, pay, row_of, status_page, summary  # noqa: F401

pytestmark = needs_pg

STUDENTS = f"{API}/org/students"
GROUPS = f"{API}/org/groups"
FULL_NAME_MSG = "Ученик уже в архиве"


# ---- helpers --------------------------------------------------------------------------------------------


def as_admin(w):
    return make_client(w, user=w.admin)


def as_teacher(w, teacher=None):
    return make_client(w, user=teacher or w.teacher)


def archive(w, student, *, client=None, expect=200, **body):
    r = (client or as_admin(w)).post(f"{STUDENTS}/{student.id}/archive", json=body or None)
    assert r.status_code == expect, r.text
    return r.json()


def restore(w, student, group_ids=None, *, client=None, expect=200):
    r = (client or as_admin(w)).post(
        f"{STUDENTS}/{student.id}/restore", json=None if group_ids is None else {"group_ids": [str(g) for g in group_ids]}
    )
    assert r.status_code == expect, r.text
    return r.json()


def fresh(w, student):
    w.db.expire_all()
    return w.db.get(Student, student.id)


def memberships(w, student):
    w.db.expire_all()
    return w.db.query(StudentGroup).filter_by(student_id=student.id).order_by(StudentGroup.joined_at).all()


def audit(w, student=None, action=None):
    w.db.expire_all()
    rows = w.db.query(GroupAudit).filter(GroupAudit.org_id == w.org.id)
    if action:
        rows = rows.filter(GroupAudit.action == action)
    rows = rows.all()
    if student is not None:
        rows = [r for r in rows if (r.snapshot or {}).get("student_id") == str(student.id)]
    return rows


def add_to(w, group, student, mode="add"):
    result = w.svc.add_student_to_group(
        str(w.org.id), str(group.id), str(student.id), mode=mode, actor_id=str(w.owner.id)
    )
    w.db.expire_all()
    return result


@pytest.fixture
def two(world):
    """The world plus a second group; ``student`` (Алия) is in BOTH, the first one being primary."""
    world.second = make_group(world.db, world.org, world.teacher2, "Вторая")
    world.db.commit()
    world.student = world.students[0]
    add_to(world, world.second, world.student)
    return world


def start_lesson(w, group=None):
    """The lesson of today (the 24-hour slot of the test world) is started: the roster is frozen."""
    w.svc.start_group_attendance(
        str(w.org.id), str((group or w.group).id),
        StartAttendanceRequest(lessonDate=business_today(), lessonId=LIVE_SLOT),
        started_by=str(w.admin.id), is_org_admin=True,
    )


# ==============================================================================================
# archive
# ==============================================================================================


@pytest.mark.parametrize("who, role", [("admin", "admin"), ("owner", "owner")])
def test_archive_closes_every_membership_empties_the_group_and_writes_the_journal(two, who, role):
    s = two.student
    before = fresh(two, s)
    assert before.group_id == two.group.id and len(memberships(two, s)) == 2
    body = archive(two, s, client=make_client(two, user=getattr(two, who)), reason="  Переехал в другой город  ")

    assert body["status"] == "archived" and body["group_id"] is None and body["id"] == str(s.id)
    assert body["archive_reason"] == "Переехал в другой город" and body["archived_at"]
    assert [g["group_id"] for g in body["closed_groups"]] == [str(two.group.id), str(two.second.id)]   # primary first
    assert [g["group_name"] for g in body["closed_groups"]] == ["Группа", "Вторая"]

    after = fresh(two, s)
    assert (after.status, after.group_id, after.archive_reason) == ("archived", None, "Переехал в другой город")
    rows = memberships(two, s)
    assert [m.is_active for m in rows] == [False, False] and len({m.left_at for m in rows}) == 1
    assert rows[0].left_at == after.archived_at and rows[0].left_at is not None
    assert body["archived_at"] == after.archived_at.isoformat()

    journal = audit(two, s, "student_archived")
    assert sorted(r.group_id for r in journal) == sorted([two.group.id, two.second.id])
    for row in journal:
        assert (row.actor_id, row.actor_role, row.org_id) == (getattr(two, who).id, role, two.org.id)
        assert row.assigned_teacher_id == (two.teacher.id if row.group_id == two.group.id else two.teacher2.id)
        assert row.created_at == after.archived_at
        assert row.snapshot["reason"] == "Переехал в другой город" and row.snapshot["student_id"] == str(s.id)
        assert row.snapshot["was_primary"] is (row.group_id == two.group.id)
    # the others stay where they were
    for other in two.students[1:]:
        assert fresh(two, other).status != "archived" and [m.is_active for m in memberships(two, other)] == [True]


def test_archive_twice_is_a_conflict_and_changes_nothing(two):
    s = two.student
    first = archive(two, s, reason="Первый раз")
    rows_before = [(r.id, r.created_at) for r in audit(two, s)]
    r = as_admin(two).post(f"{STUDENTS}/{s.id}/archive", json={"reason": "Второй раз"})
    assert r.status_code == 409 and r.json()["detail"] == FULL_NAME_MSG
    after = fresh(two, s)
    assert after.archive_reason == "Первый раз" and after.archived_at.isoformat() == first["archived_at"]
    assert [(r.id, r.created_at) for r in audit(two, s)] == rows_before


def test_teacher_cannot_archive_or_restore_and_nothing_changes(two):
    s = two.student
    teacher = as_teacher(two)                                    # the teacher of the student's primary group
    assert teacher.post(f"{STUDENTS}/{s.id}/archive", json={}).status_code == 403
    assert fresh(two, s).status != "archived" and all(m.is_active for m in memberships(two, s))
    archive(two, s)
    assert teacher.post(f"{STUDENTS}/{s.id}/restore", json={"group_ids": [str(two.group.id)]}).status_code == 403
    assert fresh(two, s).status == "archived"
    assert as_teacher(two, two.teacher2).post(f"{STUDENTS}/{s.id}/restore", json={}).status_code == 403


def test_another_centre_gets_404_in_both_directions(two):
    s = two.student
    foreign = make_client(two, user=two.foreign_admin, org="other")
    assert foreign.post(f"{STUDENTS}/{s.id}/archive", json={}).status_code == 404
    assert foreign.post(f"{STUDENTS}/{s.id}/restore", json={}).status_code == 404
    assert as_admin(two).post(f"{STUDENTS}/{two.foreign_student.id}/archive", json={}).status_code == 404
    assert as_admin(two).post(f"{STUDENTS}/{uuid4()}/archive", json={}).status_code == 404
    assert as_admin(two).post(f"{STUDENTS}/not-a-uuid/archive", json={}).status_code == 404
    assert fresh(two, s).status != "archived" and fresh(two, two.foreign_student).status != "archived"
    assert audit(two, action="student_archived") == []


def test_reason_is_optional_trimmed_and_limited(two):
    a, b, c = two.students
    assert archive(two, a)["archive_reason"] is None                                       # no reason at all
    r = as_admin(two).post(f"{STUDENTS}/{b.id}/archive")                                    # no body at all
    assert r.status_code == 200 and r.json()["archive_reason"] is None
    assert archive(two, c, reason="   ")["archive_reason"] is None                          # blank
    assert as_admin(two).post(f"{STUDENTS}/{two.foreign_student.id}/archive", json={"reason": "x" * 301}).status_code == 422
    d = make_student(two.db, two.org, two.group, "Эльза", "Новая", "N13-004")
    two.db.commit()
    assert archive(two, d, reason="я" * 300)["archive_reason"] == "я" * 300                 # the limit itself is fine


def test_a_student_without_a_group_keeps_his_date_and_reason(world):
    s = Student(org_id=world.org.id, first_name="Без", last_name="Группы", class_level=9, status="diagnosed")
    world.db.add(s)
    world.db.commit()
    body = archive(world, s, reason="Не стал ходить")
    assert body["closed_groups"] == [] and body["status"] == "archived" and body["archive_reason"] == "Не стал ходить"
    assert audit(world, s) == []                                    # there is no group to write a journal row to
    got = as_admin(world).get(f"{STUDENTS}/{s.id}").json()
    assert got["archived_at"] == body["archived_at"] and got["archive_reason"] == "Не стал ходить"
    assert got["archived_groups"] == []
    restored = restore(world, s)
    assert restored["status"] == "new" and restored["archived_at"] is None and restored["archive_reason"] is None   # no ID, no diagnostic
    assert restored["restored_groups"] == [] and restored["skipped"] == []


def test_archive_is_refused_while_a_lesson_of_his_group_is_in_progress(two):
    s = two.student
    start_lesson(two)
    r = as_admin(two).post(f"{STUDENTS}/{s.id}/archive", json={"reason": "x"})
    assert r.status_code == 409 and "пока идёт урок" in r.json()["detail"]
    after = fresh(two, s)
    assert after.status != "archived" and after.group_id == two.group.id and after.archived_at is None
    assert all(m.is_active and m.left_at is None for m in memberships(two, s))
    assert audit(two, s, "student_archived") == []


# ==============================================================================================
# nothing is deleted
# ==============================================================================================


def test_payments_receipts_lessons_and_excuses_survive_the_archive(two):
    s = two.student
    paid = pay(two, s, two.group)["payment"]
    pay(two, s, two.second, amount=300000)
    day_, lesson_id = reconcile(two, 1)
    save(two, day_, lesson_id, [(s, "excused"), (two.students[1], "present"), (two.students[2], "present")],
         status="completed", excuseReasons={s.id: "Болел"})
    two.db.expire_all()
    att = two.db.query(GroupAttendance).filter_by(group_id=two.group.id, lesson_date=day_.isoformat()).one()
    roster_before, records_before = list(att.roster_snapshot), dict(att.records)
    payments_before = {p.id: (p.amount, p.receipt_no, p.voided_at) for p in two.db.query(StudentPayment).filter_by(student_id=s.id)}
    excuse_before = two.db.query(AttendanceExcuse).filter_by(attendance_id=att.id, student_id=s.id).one()
    excuse_key = (excuse_before.id, excuse_before.reason, excuse_before.status)
    assert len(payments_before) == 2

    archive(two, s)

    two.db.expire_all()
    att = two.db.query(GroupAttendance).filter_by(group_id=two.group.id, lesson_date=day_.isoformat()).one()
    assert att.roster_snapshot == roster_before and att.records == records_before
    assert {p.id: (p.amount, p.receipt_no, p.voided_at) for p in two.db.query(StudentPayment).filter_by(student_id=s.id)} == payments_before
    excuse = two.db.query(AttendanceExcuse).filter_by(attendance_id=att.id, student_id=s.id).one()
    assert (excuse.id, excuse.reason, excuse.status) == excuse_key
    # the receipt of a payment is still readable
    assert owner(two).get(f"{API}/org/payments/{paid['id']}/receipt").status_code == 200
    # the journal of the past lesson still shows him: as a former student, with the day he left
    journal = two.svc.get_group_attendance(str(two.org.id), str(two.group.id), month=day_.strftime("%Y-%m"),
                                           actor_id=str(two.admin.id), is_org_admin=True, is_org_manager=True)
    assert str(s.id) in journal.rosterStudentIds[next(iter(journal.rosterStudentIds))]
    assert [h["id"] for h in journal.historicalStudents] == [str(s.id)]
    assert journal.historicalStudents[0]["left_at"] is not None


def test_the_card_still_lists_closed_memberships_with_their_history_but_without_actions(two):
    s = two.student
    pay(two, s, two.group)
    pay(two, s, two.second, amount=1000)
    r = owner(two).get(f"{STUDENTS}/{s.id}/memberships")
    assert [(x["is_active"], x["last_payment"] is not None) for x in r.json()] == [(True, True), (True, True)]
    archive(two, s)
    r = owner(two).get(f"{STUDENTS}/{s.id}/memberships")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2 and all(x["is_active"] is False and x["last_payment"] is not None for x in rows)
    # and the payment list of a closed membership is complete
    listed = owner(two).get(f"{API}/org/memberships/{rows[0]['membership_id']}/payments").json()
    assert len(listed) == 1 and listed[0]["voided_at"] is None


def test_the_payments_status_list_and_the_banner_forget_the_archived_student(two):
    s = two.student
    for st in two.students:
        pay(two, st, two.group)
    pay(two, s, two.second, amount=1)
    assert summary(two)["tracked_total"] == 4
    names = {x["student_id"] for x in status_page(two, limit=100)["items"]}
    assert str(s.id) in names
    archive(two, s)
    assert summary(two)["tracked_total"] == 2
    page = status_page(two, limit=100)
    assert str(s.id) not in {x["student_id"] for x in page["items"]} and page["total"] == 2
    assert {x["student_id"] for x in status_page(two, group_id=str(two.second.id))["items"]} == set()


# ==============================================================================================
# lists, counts, lessons
# ==============================================================================================


def test_the_archived_student_is_not_in_a_new_lesson(two):
    s = two.student
    archive(two, s)
    start_lesson(two)
    two.db.expire_all()
    att = two.db.query(GroupAttendance).filter_by(group_id=two.group.id, lesson_id=LIVE_SLOT).one()
    assert {x["student_id"] for x in att.roster_snapshot} == {str(x.id) for x in two.students[1:]}


def test_teachers_never_see_the_archive_and_admins_find_it_by_status(two):
    s = two.student
    archive(two, s, reason="ушёл")
    teacher = as_teacher(two)
    items = teacher.get(f"{STUDENTS}", params={"limit": 100}).json()
    assert str(s.id) not in {x["id"] for x in items} and len(items) == 2
    assert teacher.get(f"{STUDENTS}/{s.id}").status_code == 403
    assert teacher.get(f"{STUDENTS}/{s.id}/profile").status_code == 403
    assert teacher.get(f"{STUDENTS}", params={"status": "archived"}).json() == []
    # a stale row cannot bring him back to the teacher: active membership + primary group of an archived student
    two.db.query(StudentGroup).filter_by(student_id=s.id, group_id=two.group.id).update({"is_active": True, "left_at": None})
    two.db.query(Student).filter_by(id=s.id).update({"group_id": two.group.id})
    two.db.commit()
    assert str(s.id) not in {x["id"] for x in teacher.get(f"{STUDENTS}", params={"limit": 100}).json()}
    assert teacher.get(f"{STUDENTS}/{s.id}").status_code == 403
    assert teacher.get(f"{STUDENTS}/{s.id}/profile").status_code == 403

    admin = as_admin(two)
    archived = admin.get(f"{STUDENTS}", params={"status": "archived", "include_meta": True}).json()
    assert archived["total"] == 1 and archived["items"][0]["id"] == str(s.id)
    assert archived["items"][0]["archive_reason"] == "ушёл" and archived["items"][0]["archived_at"]
    assert archived["items"][0]["groups_count"] == 0 and archived["items"][0]["current_group_id"] is None
    kept = admin.get(f"{STUDENTS}", params={"exclude_status": "archived", "limit": 100}).json()
    assert str(s.id) not in {x["id"] for x in kept}
    assert admin.get(f"{STUDENTS}", params={"status": "archived", "search": "Каримова"}).json()[0]["id"] == str(s.id)
    assert admin.get(f"{STUDENTS}", params={"status": "archived", "search": "Юсупов"}).json() == []
    assert as_admin(two).get(f"{API}/org/stats").json()["total_students"] == 2                # the archive is not counted


def group_numbers(w, group):
    """Every place that counts the students of a group."""
    admin, teacher = as_admin(w), as_teacher(w, w.teacher if group.id == w.group.id else w.teacher2)
    listed = {g["id"]: g["student_count"] for g in admin.get(GROUPS).json()}
    teachers = {g["id"]: g["student_count"] for g in teacher.get(GROUPS).json()}
    card = admin.get(f"{GROUPS}/{group.id}").json()
    stats = admin.get(f"{GROUPS}/{group.id}/stats").json()
    overview = {g["group_id"]: g["student_count"] for g in admin.get(f"{API}/org/groups-overview").json()["groups"]}
    dashboard = teacher.get(f"{API}/org/teacher/dashboard/summary").json()
    return {
        "list": listed[str(group.id)],
        "teacher_list": teachers[str(group.id)],
        "card_count": card["student_count"],
        "card_students": len(card["students"]),
        "stats": stats["total_students"],
        "overview": overview[str(group.id)],
        "dashboard_group": next(g["student_count"] for g in dashboard["groups"] if g["id"] == str(group.id)),
        "dashboard_total": dashboard["studentCount"],
    }


def test_group_counters_agree_everywhere_and_drop_the_archived_student(two):
    s = two.student
    assert set(group_numbers(two, two.group).values()) == {3}
    assert set(group_numbers(two, two.second).values()) == {1}
    archive(two, s)
    assert set(group_numbers(two, two.group).values()) == {2}
    numbers = group_numbers(two, two.second)
    assert numbers == {k: 0 for k in numbers}
    # a rolled-in student grows the count again
    make_student(two.db, two.org, two.group, "Новый", "Ученик", "N13-010")
    two.db.commit()
    assert group_numbers(two, two.group)["list"] == 3


def test_a_stale_active_membership_of_an_archived_student_counts_nowhere(two):
    """Defence in depth (№64): even a row that violates the invariant cannot inflate a group or take a seat."""
    s = two.student
    two.db.query(Student).filter_by(id=s.id).update({"status": "archived"})      # the old generic PUT did exactly this
    two.db.commit()
    assert [m.is_active for m in memberships(two, s)] == [True, True]
    assert set(group_numbers(two, two.group).values()) == {2}
    numbers = group_numbers(two, two.second)
    assert numbers == {k: 0 for k in numbers}
    assert as_admin(two).get(f"{GROUPS}/{two.group.id}").json()["students"] and \
        str(s.id) not in {x["id"] for x in as_admin(two).get(f"{GROUPS}/{two.group.id}").json()["students"]}
    two.group.max_students = 3                       # 2 real students + the stale row: there is a seat
    two.db.commit()
    newcomer = make_student(two.db, two.org, two.second, "Новенький", "Ученик", "N13-011")
    extra = make_student(two.db, two.org, two.second, "Лишний", "Ученик", "N13-012")
    two.db.commit()
    assert add_to(two, two.group, newcomer)["status"] == "added"
    with pytest.raises(ValueError, match="лимит"):
        add_to(two, two.group, extra)


# ==============================================================================================
# restore
# ==============================================================================================


def test_restore_returns_the_student_to_the_chosen_groups(two):
    s = two.student
    pay(two, s, two.group)
    give_price(two.db, memberships(two, s)[0], 450000, reason="договорённость")
    fee_before = {m.group_id: price_rows(two, s, m.group_id) for m in memberships(two, s)}
    old_joined = {m.group_id: m.joined_at for m in memberships(two, s)}
    archive(two, s, reason="Уехал")

    body = restore(two, s, [two.second.id, two.group.id])               # the order is the admin's: second first

    assert body["status"] == "active" and body["archived_at"] is None and body["archive_reason"] is None
    assert body["group_id"] == str(two.second.id)                       # the first group enrolled is primary
    assert [g["group_id"] for g in body["restored_groups"]] == [str(two.second.id), str(two.group.id)]
    assert body["skipped"] == []
    after = fresh(two, s)
    assert (after.status, after.group_id, after.archived_at, after.archive_reason) == ("active", two.second.id, None, None)
    rows = {m.group_id: m for m in memberships(two, s)}
    assert len(rows) == 2 and all(m.is_active and m.left_at is None for m in rows.values())
    for gid, m in rows.items():                                          # the membership is the OLD one: new period
        assert m.joined_at > old_joined[gid] and price_rows(two, s, gid) == fee_before[gid]
    assert [v for v in fee_before.values() if v] == [[(450000, "договорённость")]]        # the exception survived the archive
    assert two.db.query(StudentPayment).filter_by(student_id=s.id).count() == 1
    assert row_of(two, s, two.group)["last_payment"] is not None          # its payments are attached again
    assert {r.group_id for r in audit(two, s, "student_restored")} == {two.group.id, two.second.id}
    assert as_admin(two).get(f"{STUDENTS}/{s.id}").json()["archived_groups"] is None
    assert {x["id"] for x in as_admin(two).get(f"{GROUPS}/{two.second.id}").json()["students"]} == {str(s.id)}


def test_restore_skips_a_full_group_and_an_archived_group_and_does_the_rest(two):
    s = two.student
    third = make_group(two.db, two.org, two.teacher2, "Третья")
    full = make_group(two.db, two.org, two.teacher2, "Полная")
    full.max_students = 1
    two.db.commit()
    third_id, full_id = third.id, full.id
    # the student has been in three groups
    add_to(two, third, s)
    archive(two, s)
    filler = make_student(two.db, two.org, full, "Занял", "Место", "N13-020")          # the only seat of «Полная» is taken
    two.db.commit()
    assert filler.id
    two.second.status = "archived"
    two.db.commit()

    ghost = uuid4()
    body = restore(two, s, [two.group.id, two.second.id, third_id, full_id, ghost])

    assert [g["group_id"] for g in body["restored_groups"]] == [str(two.group.id), str(third_id)]
    reasons = {x["group_id"]: (x["reason"], x["message"]) for x in body["skipped"]}
    assert reasons == {
        str(two.second.id): ("group_archived", "Нельзя зачислить ученика в архивную группу"),
        str(full_id): ("group_full", "В группе достигнут лимит учеников"),
        str(ghost): ("not_found", "Группа не найдена"),
    }
    assert {x["group_name"] for x in body["skipped"]} == {"Вторая", "Полная", None}
    after = fresh(two, s)
    assert after.status == "active" and after.group_id == two.group.id
    assert {m.group_id for m in memberships(two, s) if m.is_active} == {two.group.id, third_id}
    assert [m.is_active for m in memberships(two, s) if m.group_id == two.second.id] == [False]   # stays closed


def test_restore_with_a_foreign_or_duplicate_group_and_with_no_groups(two):
    s = two.student
    archive(two, s)
    body = restore(two, s, [two.foreign_group.id, two.group.id, two.group.id])
    assert [g["group_id"] for g in body["restored_groups"]] == [str(two.group.id)]
    assert [(x["reason"], x["message"]) for x in body["skipped"]] == [("not_found", "Группа не найдена")]
    assert two.db.query(StudentGroup).filter_by(group_id=two.foreign_group.id, student_id=s.id).count() == 0
    # no display_id and no completed diagnostic: back to «new»; with a diagnostic result: «diagnosed»
    n = Student(org_id=two.org.id, first_name="Нет", last_name="Номера", class_level=9, status="active")
    two.db.add(n)
    two.db.commit()
    archive(two, n)
    assert restore(two, n)["status"] == "new"
    archive(two, n)
    assert restore(two, n, [])["status"] == "new"
    assert fresh(two, n).display_id is None
    two.db.add(StudentDiagnosticResult(student_id=n.id, org_id=two.org.id))
    two.db.commit()
    archive(two, n)
    assert restore(two, n, [])["status"] == "diagnosed"
    foreign_result = Student(org_id=two.org.id, first_name="Чужой", last_name="Результат", class_level=9, status="active")
    two.db.add(foreign_result)
    two.db.commit()
    two.db.add(StudentDiagnosticResult(student_id=two.foreign_student.id, org_id=two.other_org.id))   # another centre's result
    two.db.commit()
    archive(two, foreign_result)
    assert restore(two, foreign_result)["status"] == "new"
    # a student WITH a display_id comes back as «active» even into no group
    archive(two, two.students[1])
    assert restore(two, two.students[1])["status"] == "active"


def test_restore_skips_a_group_whose_lesson_is_in_progress_and_does_the_rest(two):
    s = two.student
    archive(two, s)
    start_lesson(two)                                                    # «Группа»: the roster is frozen for now
    body = restore(two, s, [two.group.id, two.second.id])
    assert [g["group_id"] for g in body["restored_groups"]] == [str(two.second.id)]
    (skipped,) = body["skipped"]
    assert (skipped["group_id"], skipped["reason"]) == (str(two.group.id), "lesson_in_progress")
    assert "пока идёт урок" in skipped["message"] and skipped["group_name"] == "Группа"
    after = fresh(two, s)
    assert after.status == "active" and after.group_id == two.second.id


def test_a_group_that_fails_unexpectedly_is_skipped_and_the_others_keep_their_journal_row(two, monkeypatch):
    from src.auth.service import AuthService

    s = two.student
    third = make_group(two.db, two.org, two.teacher2, "Третья")
    two.db.commit()
    third_id, group_id, second_id = third.id, two.group.id, two.second.id
    archive(two, s)
    original = AuthService.add_student_to_group

    def flaky(self, org_id, gid, student_id, **kw):
        if str(gid) == str(second_id):
            self.db.query(Student).filter_by(id=student_id).update({"comment": "half-done"})   # a dirty session at the failure
            raise RuntimeError("boom")
        return original(self, org_id, gid, student_id, **kw)

    monkeypatch.setattr(AuthService, "add_student_to_group", flaky)
    body = restore(two, s, [group_id, second_id, third_id])                 # the 2nd of three explodes: still 200

    assert body["status"] == "active" and body["group_id"] == str(group_id)
    assert [g["group_id"] for g in body["restored_groups"]] == [str(group_id), str(third_id)]
    (skipped,) = body["skipped"]
    assert skipped == {"group_id": str(second_id), "group_name": "Вторая", "reason": "refused",
                       "message": "Не удалось вернуть в группу — повторите позже"}
    after = fresh(two, s)
    assert after.status == "active" and after.comment is None               # the half-done write was rolled back
    assert {m.group_id for m in memberships(two, s) if m.is_active} == {group_id, third_id}
    restored_rows = audit(two, s, "student_restored")
    assert {r.group_id for r in restored_rows} == {group_id, third_id}       # each group's row, written right after its commit


def test_a_failing_journal_row_does_not_undo_the_group_that_was_restored(two, monkeypatch):
    from src.auth.student_archive import StudentArchiveService

    s = two.student
    group_id, second_id = two.group.id, two.second.id
    archive(two, s)
    original = StudentArchiveService._write_restored_audit

    def broken(self, org_id, student, gid, *args):
        if gid == str(group_id):
            raise RuntimeError("journal is down")
        return original(self, org_id, student, gid, *args)

    monkeypatch.setattr(StudentArchiveService, "_write_restored_audit", broken)
    body = restore(two, s, [group_id, second_id])
    assert [g["group_id"] for g in body["restored_groups"]] == [str(group_id), str(second_id)] and body["skipped"] == []
    assert {m.group_id for m in memberships(two, s) if m.is_active} == {group_id, second_id}
    assert {r.group_id for r in audit(two, s, "student_restored")} == {second_id}     # only the lost row is missing


def test_a_student_archived_by_an_old_image_has_no_date_but_everything_still_answers(two):
    s = two.student
    two.db.query(Student).filter_by(id=s.id).update({"status": "archived", "group_id": None})   # the old generic PUT
    two.db.query(StudentGroup).filter_by(student_id=s.id).update({"is_active": False, "left_at": datetime.utcnow()})
    two.db.commit()
    profile = as_admin(two).get(f"{STUDENTS}/{s.id}/profile").json()
    assert profile["student"]["status"] == "archived" and profile["student"]["archived_at"] is None
    assert profile["student"]["archive_reason"] is None and profile["archived_groups"] == []
    assert as_admin(two).get(f"{STUDENTS}/{s.id}").json()["archived_at"] is None
    assert restore(two, s, [two.group.id])["status"] == "active"


def test_restore_of_a_student_who_is_not_archived_is_a_conflict(two):
    s = two.student
    r = as_admin(two).post(f"{STUDENTS}/{s.id}/restore", json={"group_ids": [str(two.second.id)]})
    assert r.status_code == 409 and r.json()["detail"] == "Ученик не в архиве"
    archive(two, s)
    restore(two, s)
    assert as_admin(two).post(f"{STUDENTS}/{s.id}/restore", json={}).status_code == 409
    assert as_admin(two).post(f"{STUDENTS}/{s.id}/restore", json={"group_ids": ["nope"]}).status_code == 422


def test_archived_groups_tell_the_window_what_can_be_offered(two):
    s = two.student
    assert as_admin(two).get(f"{STUDENTS}/{s.id}").json()["archived_groups"] is None            # not archived
    live = as_admin(two).get(f"{STUDENTS}/{s.id}/profile").json()
    assert live["archived_groups"] is None and live["student"]["archived_at"] is None and live["student"]["archive_reason"] is None
    archive(two, s, reason="Ушёл")
    body = as_admin(two).get(f"{STUDENTS}/{s.id}").json()
    assert [(g["group_name"], g["group_status"], g["is_full"]) for g in body["archived_groups"]] == [
        ("Группа", "active", False), ("Вторая", "active", False)]
    assert body["archived_groups"][0]["group_id"] == str(two.group.id)
    assert set(body["archived_groups"][0]) == {"group_id", "group_name", "group_status", "is_full"}
    profile = as_admin(two).get(f"{STUDENTS}/{s.id}/profile").json()        # the card reads this one
    assert profile["archived_groups"] == body["archived_groups"]                # top level of the profile
    assert [g["group_name"] for g in profile["archived_groups"]] == ["Группа", "Вторая"]
    assert profile["student"]["status"] == "archived" and profile["student"]["group_id"] is None
    assert profile["student"]["archived_at"] == body["archived_at"] and profile["student"]["archive_reason"] == "Ушёл"
    assert profile["groups"] == [] and len(profile["group_history"]) == 2       # the closed memberships stay in the history
    # the group fills up and another one is archived in the meantime
    two.second.max_students = 1
    two.db.commit()
    add_to(two, two.second, make_student(two.db, two.org, two.group, "Занял", "Вторую", "N13-030"))
    two.group.status = "archived"
    two.db.commit()
    got = {g["group_name"]: g for g in as_admin(two).get(f"{STUDENTS}/{s.id}").json()["archived_groups"]}
    assert got["Вторая"]["is_full"] is True and got["Вторая"]["group_status"] == "active"
    assert got["Группа"]["group_status"] == "archived" and got["Группа"]["is_full"] is False
    # a teacher cannot read any of it
    assert as_teacher(two).get(f"{STUDENTS}/{s.id}").status_code == 403


def test_the_last_archiving_decides_which_groups_are_offered(two):
    s = two.student
    archive(two, s)
    assert len(as_admin(two).get(f"{STUDENTS}/{s.id}").json()["archived_groups"]) == 2
    restore(two, s, [two.second.id])
    archive(two, s)                                                    # now only «Вторая» is closed
    body = as_admin(two).get(f"{STUDENTS}/{s.id}").json()
    assert [g["group_name"] for g in body["archived_groups"]] == ["Вторая"]
    restore(two, s)                                                    # back into nothing ...
    archive(two, s, reason="Опять")                                    # ... and out again: nothing is offered, not the old groups
    body = as_admin(two).get(f"{STUDENTS}/{s.id}").json()
    assert body["archived_groups"] == [] and body["archive_reason"] == "Опять"
    assert len(audit(two, s, "student_archived")) == 3


# ==============================================================================================
# the generic edit
# ==============================================================================================


def test_the_generic_edit_cannot_move_a_student_into_or_out_of_the_archive(two):
    s = two.student
    put = lambda body, client=None: (client or as_admin(two)).put(f"{STUDENTS}/{s.id}", json=body)   # noqa: E731
    r = put({"status": "archived"})
    assert r.status_code == 422 and r.json()["detail"] == "Используйте «В архив» / «Вернуть из архива»"
    after = fresh(two, s)
    assert after.status == "active" and after.group_id == two.group.id and all(m.is_active for m in memberships(two, s))
    assert put({"status": "archived", "comment": "x"}).status_code == 422
    assert fresh(two, s).comment is None                               # nothing of the request was applied
    assert put({"status": "diagnosed"}).status_code == 200            # the other statuses: as before
    assert put({"status": "active", "comment": "ok"}).json()["comment"] == "ok"
    assert put({"status": "bogus"}).status_code == 422
    archive(two, s)
    for status in ("active", "new", "diagnosed"):
        r = put({"status": status})
        assert r.status_code == 422 and fresh(two, s).status == "archived"
    assert put({"status": "archived", "comment": "правка"}).status_code == 200      # not a change of the status
    assert fresh(two, s).comment == "правка" and fresh(two, s).status == "archived"
    assert put({"first_name": "Алия-Мария"}).json()["first_name"] == "Алия-Мария"
    assert put({"group_id": str(two.second.id)}).status_code == 400                  # still no transfer through the edit
    assert fresh(two, s).group_id is None and not any(m.is_active for m in memberships(two, s))
    assert put({"status": "archived"}, as_teacher(two)).status_code == 403


def test_an_archived_student_cannot_be_enrolled_until_he_is_restored(two):
    s = two.student
    archive(two, s)
    r = as_admin(two).post(f"{GROUPS}/{two.second.id}/students/{s.id}")
    assert r.status_code == 400 and "Архивного ученика" in r.json()["detail"]
    r = as_admin(two).post(f"{GROUPS}/{two.second.id}/students/bulk", json={"student_ids": [str(s.id)]})
    assert r.json()["added"] == 0 and r.json()["skipped"] == 1
    assert fresh(two, s).group_id is None and not any(m.is_active for m in memberships(two, s))


# ==============================================================================================
# payroll
# ==============================================================================================


def test_payroll_of_a_past_month_does_not_change_when_a_student_is_archived(world):
    from tests.test_payroll_pg import FEE, full_month, line_of, owner as payroll_owner, previous_month, preview, save as save_sheet

    m = previous_month()
    full_month(world, m, days=range(1, 6))
    s0, s1, s2 = world.students
    before = preview(world, m)
    assert [line_of(before, s)["counted"] for s in (s0, s1, s2)] == [5, 5, 5]
    saved = save_sheet(world, m)
    assert saved["stale"] is False

    archive(world, s1, reason="Ушёл в другой центр")

    after = preview(world, m)
    assert [line_of(after, s) for s in (s0, s1, s2)] == [line_of(before, s) for s in (s0, s1, s2)]
    assert after["total_amount"] == before["total_amount"] and after["total_amount"] == 3 * (FEE * 50 * 5 // 1200)
    assert line_of(after, s1)["counted"] == 5 and line_of(after, s1)["amount"] == FEE * 50 * 5 // 1200
    # the saved sheet is not "stale" because of the archiving, and re-reading it gives the same figures
    got = payroll_owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}").json()
    assert got["stale"] is False and got["total_amount"] == saved["total_amount"]
    assert line_of(got, s1)["amount"] == line_of(saved, s1)["amount"]


# ==============================================================================================
# locks: archive against a payment, a transfer, another archive
# ==============================================================================================


@pytest.fixture
def engine():
    from sqlalchemy import create_engine
    from src.core.db_timezone import utc_connect_args

    from tests.pg_world import DB_URL

    engine = create_engine(DB_URL, connect_args=utc_connect_args(DB_URL))
    yield engine
    engine.dispose()


def run_in_thread(engine, fn):
    import threading

    from sqlalchemy.orm import Session

    outcome = {}

    def target():
        with Session(engine) as session:
            try:
                outcome["value"] = fn(session)
            except BaseException as exc:  # noqa: BLE001  (reported to the test)
                outcome["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    return thread, outcome


def wait_until_blocked(engine, seconds=10):
    import time

    from sqlalchemy import text

    deadline = time.time() + seconds
    while time.time() < deadline:
        with engine.connect() as conn:
            waiting = conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )).scalar_one()
        if waiting:
            return
        time.sleep(0.05)
    raise AssertionError("the request never waited for a lock")


def archive_call(w, student):
    from src.auth.student_archive import StudentArchiveService

    return lambda session: StudentArchiveService(session).archive(
        str(w.org.id), str(student.id), actor_id=str(w.admin.id), actor_role="admin", reason="гонка")


def test_archive_does_not_deadlock_with_a_payment_being_recorded(two, engine):
    """A payment holds the membership and then takes FOR KEY SHARE on the student (its foreign key); the
    archive locks the student FIRST.  FOR UPDATE there would be a deadlock; FOR NO KEY UPDATE is compatible."""
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    s = two.student
    m = mid(two, s, two.group)
    holder = Session(engine)
    holder.execute(text("SET LOCAL lock_timeout = '5s'"))
    holder.execute(text("SELECT id FROM student_groups WHERE id = :m FOR NO KEY UPDATE"), {"m": m})   # create_payment's lock
    thread, outcome = run_in_thread(engine, archive_call(two, s))
    wait_until_blocked(engine)                                    # the archive has the student and waits for the membership
    holder.execute(text(                                          # ... and the payment insert still goes through
        "INSERT INTO student_payments (id, org_id, membership_id, student_id, group_id, amount, paid_on, valid_from, "
        "valid_to, created_by, receipt_no) VALUES (gen_random_uuid(), :o, :m, :s, :g, 450000, current_date, current_date, "
        "current_date + 30, :u, 999001)"),
        {"o": two.org.id, "m": m, "s": s.id, "g": two.group.id, "u": two.owner.id})
    holder.commit()
    holder.close()
    thread.join(timeout=20)
    assert "error" not in outcome, outcome.get("error")
    two.db.expire_all()
    after = two.db.get(Student, s.id)
    assert after.status == "archived" and not any(x.is_active for x in two.db.query(StudentGroup).filter_by(student_id=s.id))
    assert two.db.query(StudentPayment).filter_by(student_id=s.id, receipt_no=999001).count() == 1   # the payment is kept


def test_archive_waits_for_a_transfer_and_closes_the_membership_it_made(two, engine, caplog):
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    caplog.set_level("WARNING", logger="src.auth.membership_periods")
    s = two.students[1]                                          # Бек: only in the first group
    third = make_group(two.db, two.org, two.teacher2, "Третья")
    two.db.commit()
    holder = Session(engine)                                      # add_student_to_group has locked the student ...
    holder.execute(text("SELECT id FROM students WHERE id = :s FOR UPDATE"), {"s": s.id})
    thread, outcome = run_in_thread(engine, archive_call(two, s))
    wait_until_blocked(engine)
    # ... and now adds a second membership - as add_student_to_group does: the row AND its open period
    holder.execute(text(
        "INSERT INTO student_groups (id, student_id, group_id, is_active, joined_at) "
        "VALUES ('00000000-0000-4000-8000-0000000a1c01', :s, :g, true, now())"),
        {"s": s.id, "g": third.id})
    holder.execute(text(
        "INSERT INTO student_group_periods (id, membership_id, joined_at) "
        "VALUES (gen_random_uuid(), '00000000-0000-4000-8000-0000000a1c01', now())"))
    holder.commit()
    holder.close()
    thread.join(timeout=20)
    assert "error" not in outcome, outcome.get("error")
    assert not [r for r in caplog.records if "close_period" in r.getMessage()], "no membership needed the self-heal"
    assert sorted(g.group_name for g in outcome["value"].closed_groups) == ["Группа", "Третья"]   # the new one is closed too
    two.db.expire_all()
    assert not any(x.is_active for x in two.db.query(StudentGroup).filter_by(student_id=s.id))
    assert len(audit(two, s, "student_archived")) == 2


def test_a_transfer_waits_for_an_archiving_and_is_then_refused(two, engine):
    """The archive holds the student; add_student_to_group queues behind it and finds an archived student."""
    from sqlalchemy.orm import Session

    from src.auth.service import AuthService

    s = two.students[1]
    third = make_group(two.db, two.org, two.teacher2, "Третья")
    two.db.commit()
    holder = Session(engine)
    holder_student = holder.query(Student).filter_by(id=s.id).with_for_update(key_share=True).one()   # the archive's lock
    thread, outcome = run_in_thread(engine, lambda session: AuthService(session).add_student_to_group(
        str(two.org.id), str(third.id), str(s.id), actor_id=str(two.owner.id)))
    wait_until_blocked(engine)
    for m in holder.query(StudentGroup).filter_by(student_id=s.id):
        m.is_active, m.left_at = False, datetime.utcnow()
    holder_student.status, holder_student.group_id = "archived", None
    holder.commit()
    holder.close()
    thread.join(timeout=20)
    assert isinstance(outcome.get("error"), ValueError) and "Архивного ученика" in str(outcome["error"])
    two.db.expire_all()
    assert not any(x.is_active for x in two.db.query(StudentGroup).filter_by(student_id=s.id))
    assert two.db.query(StudentGroup).filter_by(student_id=s.id, group_id=third.id).count() == 0


def test_two_simultaneous_archivings_make_one_event(two, engine):
    import threading

    s = two.student
    barrier = threading.Barrier(2)

    def attempt(session):
        barrier.wait()
        return archive_call(two, s)(session)

    first, out1 = run_in_thread(engine, attempt)
    second, out2 = run_in_thread(engine, attempt)
    first.join(timeout=30)
    second.join(timeout=30)
    results = [out1, out2]
    done = [o for o in results if "value" in o]
    refused = [o for o in results if "error" in o]
    assert len(done) == 1 and len(refused) == 1, results
    assert type(refused[0]["error"]).__name__ == "ArchiveConflict" and str(refused[0]["error"]) == FULL_NAME_MSG
    assert len(audit(two, s, "student_archived")) == 2                      # one row per group, written once


def test_a_second_session_editing_the_student_waits_and_cannot_unarchive_him(two, engine):
    """The generic edit locks the student too: it cannot overwrite an archiving that is in flight."""
    from sqlalchemy.orm import Session

    from src.auth.schemas import StudentUpdateRequest
    from src.auth.service import AuthService
    from src.auth.student_archive import ArchiveStatusViolation

    s = two.student
    holder = Session(engine)
    held = holder.query(Student).filter_by(id=s.id).with_for_update(key_share=True).one()
    thread, outcome = run_in_thread(engine, lambda session: AuthService(session).update_student(
        str(two.org.id), str(s.id), StudentUpdateRequest(status="diagnosed")))
    wait_until_blocked(engine)
    held.status, held.group_id = "archived", None
    holder.commit()
    holder.close()
    thread.join(timeout=20)
    assert isinstance(outcome.get("error"), ArchiveStatusViolation)
    assert fresh(two, s).status == "archived"


def test_the_audit_check_accepts_the_new_actions_and_nothing_else(two):
    from sqlalchemy.exc import IntegrityError

    row = dict(org_id=two.org.id, group_id=two.group.id, actor_id=two.admin.id, actor_role="admin", snapshot={})
    for action in ("student_archived", "student_restored", "updated"):
        two.db.add(GroupAudit(action=action, **row))
    two.db.commit()
    two.db.add(GroupAudit(action="student_deleted", **row))
    with pytest.raises(IntegrityError, match="ck_group_audit_action"):
        two.db.commit()
    two.db.rollback()


# ==============================================================================================
# money is not entered for an archived student (create / replace / fee refused, void and reading allowed)
# ==============================================================================================

ARCHIVED_MESSAGE = "Ученик в архиве — сначала верните его"


def price_rows(w, student, group):
    """``[(amount, reason)]`` of the individual prices of a membership (``group`` is a group or its id)."""
    from src.models.auth import MembershipPriceOverride

    w.db.expire_all()
    group_id = getattr(group, "id", group)
    m = w.db.query(StudentGroup).filter_by(student_id=student.id, group_id=group_id).one()
    return [(r.amount, r.reason) for r in w.db.query(MembershipPriceOverride).filter_by(membership_id=m.id)
            .order_by(MembershipPriceOverride.valid_from)]


def put_fee(w, student, group, fee, expect=200):
    r = owner(w).put(f"{API}/org/memberships/{mid(w, student, group)}/price-override",
                     json={"amount": fee, "reason": "тест"})
    assert r.status_code == expect, r.text
    return r


def test_create_replace_and_fee_are_refused_for_an_archived_student_but_void_and_reading_work(two):
    s = two.student
    put_fee(two, s, two.group, 500000)
    first = pay(two, s, two.group)["payment"]
    second = pay(two, s, two.group, amount=1000, paid_on=(business_today() - timedelta(days=2)).isoformat(),
                 valid_from=(business_today() - timedelta(days=2)).isoformat(),
                 valid_to=(business_today() + timedelta(days=20)).isoformat())["payment"]
    archive(two, s)
    two.db.expire_all()
    stored_before = {p.id: (p.amount, p.voided_at) for p in two.db.query(StudentPayment).filter_by(student_id=s.id)}
    fee_before = price_rows(two, s, two.group)

    r = owner(two).post(f"{API}/org/memberships/{mid(two, s, two.group)}/payments",
                        json={"amount": 5, "paid_on": business_today().isoformat()})
    assert r.status_code == 409 and r.json()["detail"] == ARCHIVED_MESSAGE
    r = owner(two).post(f"{API}/org/payments/{first['id']}/replace",
                        json={"amount": 7, "paid_on": business_today().isoformat(), "reason": "исправление"})
    assert r.status_code == 409 and r.json()["detail"] == ARCHIVED_MESSAGE
    r = owner(two).put(f"{API}/org/memberships/{mid(two, s, two.second)}/price-override", json={"amount": 1, "reason": "тест"})
    assert r.status_code == 409 and r.json()["detail"] == ARCHIVED_MESSAGE
    assert put_fee(two, s, two.group, 1, expect=409).json()["detail"] == ARCHIVED_MESSAGE

    two.db.expire_all()
    assert {p.id: (p.amount, p.voided_at) for p in two.db.query(StudentPayment).filter_by(student_id=s.id)} == stored_before
    assert price_rows(two, s, two.group) == fee_before == [(500000, "тест")]          # the individual price stayed

    # reading the history stays allowed ...
    assert owner(two).get(f"{API}/org/memberships/{mid(two, s, two.group)}/payments").status_code == 200
    assert owner(two).get(f"{STUDENTS}/{s.id}/memberships").status_code == 200
    assert owner(two).get(f"{API}/org/payments/{first['id']}/receipt").status_code == 200
    # ... and so does cancelling a mistake
    r = owner(two).post(f"{API}/org/payments/{second['id']}/void", json={"reason": "внесено по ошибке"})
    assert r.status_code == 200 and r.json()["payment"]["voided_at"] is not None
    two.db.expire_all()
    assert two.db.get(StudentPayment, second["id"]).voided_at is not None

    # back in the centre: money can be entered again
    restore(two, s, [two.group.id])
    assert pay(two, s, two.group, amount=2000)["payment"]["amount"] == 2000
    put_fee(two, s, two.group, 600000)


def test_a_closed_membership_of_a_student_who_is_not_archived_keeps_working_as_before(two):
    s = two.student
    first = pay(two, s, two.second)["payment"]
    two.svc.remove_student_from_group(str(two.org.id), str(two.second.id), str(s.id))      # left ONE group only
    two.db.expire_all()
    assert fresh(two, s).status == "active" and not two.db.query(StudentGroup).filter_by(
        student_id=s.id, group_id=two.second.id).one().is_active
    assert pay(two, s, two.second, amount=1234)["payment"]["amount"] == 1234               # transfer history: unchanged
    put_fee(two, s, two.second, 700000)
    r = owner(two).post(f"{API}/org/payments/{first['id']}/replace",
                        json={"amount": 99, "paid_on": business_today().isoformat(), "reason": "исправление"})
    assert r.status_code == 201, r.text
    assert owner(two).post(f"{API}/org/payments/{r.json()['payment']['id']}/void", json={"reason": "ошибка"}).status_code == 200


def test_a_payment_that_waits_for_an_archiving_is_refused_when_it_gets_the_lock(two, engine):
    """Whoever wins the membership lock sees the outcome of the archiving: no payment slips onto a closed membership."""
    from sqlalchemy.orm import Session

    from src.auth.payments_schemas import PaymentCreate
    from src.auth.payments_service import PaymentService, StudentArchived

    s = two.student
    m = mid(two, s, two.group)
    holder = Session(engine)                                    # the archiving in flight: student + membership held
    held_student = holder.query(Student).filter_by(id=s.id).with_for_update(key_share=True).one()
    held_memberships = holder.query(StudentGroup).filter_by(student_id=s.id).with_for_update(key_share=True).all()
    thread, outcome = run_in_thread(engine, lambda session: PaymentService(session).create_payment(
        str(two.org.id), m, str(two.owner.id), PaymentCreate(amount=450000, paid_on=business_today())))
    wait_until_blocked(engine)
    for membership in held_memberships:
        membership.is_active, membership.left_at = False, datetime.utcnow()
    held_student.status, held_student.group_id = "archived", None
    holder.commit()
    holder.close()
    thread.join(timeout=20)
    assert isinstance(outcome.get("error"), StudentArchived) and str(outcome["error"]) == ARCHIVED_MESSAGE
    two.db.expire_all()
    assert two.db.query(StudentPayment).filter_by(student_id=s.id).count() == 0


@pytest.mark.parametrize("round_", range(6))
def test_archive_and_a_payment_in_parallel_never_deadlock_and_never_leave_a_payment_behind_a_refusal(two, engine, round_):
    import threading

    from src.auth.payments_schemas import PaymentCreate
    from src.auth.payments_service import PaymentService, StudentArchived

    student = make_student(two.db, two.org, two.group, f"Гонка{round_}", "Платёж", f"N13-9{round_}")
    two.db.commit()
    m = mid(two, student, two.group)
    barrier = threading.Barrier(2)

    def archive_it(session):
        barrier.wait()
        return archive_call(two, student)(session)

    def pay_it(session):
        barrier.wait()
        return PaymentService(session).create_payment(
            str(two.org.id), m, str(two.owner.id), PaymentCreate(amount=450000, paid_on=business_today()))

    t1, archived = run_in_thread(engine, archive_it)
    t2, paid = run_in_thread(engine, pay_it)
    t1.join(timeout=30)
    t2.join(timeout=30)
    assert "error" not in archived, archived.get("error")                               # the archiving always succeeds
    assert "value" in paid or isinstance(paid.get("error"), StudentArchived), paid       # paid first, or refused: never a deadlock / 500
    two.db.expire_all()
    assert two.db.query(StudentPayment).filter_by(student_id=student.id).count() == (1 if "value" in paid else 0)
    assert fresh(two, student).status == "archived" and not any(x.is_active for x in memberships(two, student))
