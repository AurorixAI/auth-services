"""IA fix pack (server needs, 22.10): student edit with a journal row + clearing phone / note, receipt links, the teacher filter of the students list,
the owner's vocabulary in the statement."""
from datetime import timedelta

from tests.pg_world import give_group_price, make_client, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import (  # noqa: F401
    API, D, TODAY, fixed_today, membership, owner, post_payment, pytestmark, seed_payment, settings_block, stay,
)


def test_the_pause_row_of_the_statement_says_the_cost_is_not_counted(world, fixed_today):
    world.group.schedule = []
    world.db.commit()
    settings_block(world, accounting_start_date="2026-10-01", billing_anchor="fixed_day", billing_day=1)
    give_group_price(world.db, world.group, 600_000, valid_from=D(2026, 1, 1))
    m = membership(world)
    stay(world, m, D(2026, 10, 1))
    r = owner(world).post(f"{API}/org/memberships/{m.id}/pauses", json={"first_day": (TODAY - timedelta(days=5)).isoformat(), "last_day": (TODAY - timedelta(days=2)).isoformat()})
    assert r.status_code == 201, r.text
    rows = owner(world).get(f"{API}/org/memberships/{m.id}/statement").json()["rows"]
    title = next(x["title"] for x in rows if x["type"] == "pause")
    assert title.endswith("— стоимость не считается") and "начисл" not in title


# ---- the student edit: clearing + the journal -------------------------------------------------------------------------------------------------


def student_row(w, s):
    from src.models.auth import Student

    w.db.expire_all()
    return w.db.get(Student, s.id)


def journal_of(w, s):
    from src.models.auth import StudentAudit

    w.db.expire_all()
    return w.db.query(StudentAudit).filter_by(student_id=s.id).order_by(StudentAudit.created_at, StudentAudit.id).all()


def put_student(w, s, body, client=None, expect=200):
    r = (client or owner(w)).put(f"{API}/org/students/{s.id}", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def test_clearing_phone_and_note_an_omitted_field_stays(world):
    s = world.students[0]
    put_student(world, s, {"student_phone": "+998901112233", "comment": "аллергия", "class_level": 9})
    # an omitted field stays
    out = put_student(world, s, {"first_name": "Алия-Мария"})
    assert out["student_phone"] == "+998901112233" and out["comment"] == "аллергия" and out["first_name"] == "Алия-Мария"
    # explicit null clears the phone, "" clears the note
    out = put_student(world, s, {"student_phone": None, "comment": ""})
    assert out["student_phone"] is None and out["comment"] is None
    row = student_row(world, s)
    assert row.student_phone is None and row.comment is None and row.first_name == "Алия-Мария"
    # null of a field that cannot be cleared is ignored as before
    out = put_student(world, s, {"first_name": None, "last_name": None, "class_level": None})
    assert out["first_name"] == "Алия-Мария" and out["last_name"] and out["class_level"] == 9
    # whitespace-only = cleared too
    put_student(world, s, {"comment": "заметка"})
    assert put_student(world, s, {"comment": "   "})["comment"] is None


def test_the_journal_row_records_who_when_and_old_to_new_per_field(world):
    s = world.students[0]
    old_last, old_first = s.last_name, s.first_name
    put_student(world, s, {"first_name": "Новое", "class_level": 10, "student_phone": "+998900000001"})
    (row,) = journal_of(world, s)
    assert row.actor_id == world.owner.id and row.actor_role == "owner" and row.org_id == world.org.id and row.created_at is not None
    assert row.action == "profile_updated"
    assert row.changes == {"first_name": {"old": old_first, "new": "Новое"}, "class_level": {"old": 9, "new": 10},
                           "student_phone": {"old": None, "new": "+998900000001"}}
    # clearing is journaled (the old value is kept), an admin is recorded as admin
    put_student(world, s, {"student_phone": None, "comment": "новая заметка"}, client=make_client(world, user=world.admin))
    first, second = journal_of(world, s)
    assert second.actor_id == world.admin.id and second.actor_role == "admin"
    assert second.changes == {"student_phone": {"old": "+998900000001", "new": None}, "comment": {"old": None, "new": "новая заметка"}}
    # nothing changed (the same values, an omitted body) = no row; a field that is not journaled = no row
    put_student(world, s, {"first_name": "Новое", "class_level": 10})
    put_student(world, s, {"student_number": "77"})
    assert len(journal_of(world, s)) == 2
    assert student_row(world, s).last_name == old_last


def test_a_refused_edit_leaves_no_journal_and_the_rights_are_the_old_ones(world):
    s = world.students[0]
    put_student(world, s, {"first_name": "Х", "status": "archived"}, expect=422)                   # the status goes through «В архив»
    assert not journal_of(world, s) and student_row(world, s).first_name != "Х"
    teacher = make_client(world, user=world.teacher)
    assert teacher.put(f"{API}/org/students/{s.id}", json={"first_name": "Т"}).status_code == 403
    foreign = make_client(world, user=world.foreign_admin, org="other", org_role="admin")
    assert foreign.put(f"{API}/org/students/{s.id}", json={"first_name": "Ч"}).status_code in (400, 404)
    assert not journal_of(world, s)
