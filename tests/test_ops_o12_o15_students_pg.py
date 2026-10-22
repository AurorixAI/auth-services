"""LOGIC-B O12 (a teacher does not create invisible students) and O15 ("no group" instead of "active").  Matrix rows [O12.R1..R9] and [O15.R1..R9]."""
import pytest

from src.models.auth import Student

from tests.ops_helpers import (  # noqa: F401  (fixtures)
    build_d0, clock, client, fixture, needs_pg, new_group, new_slot, new_student, pg_session,
)

pytestmark = needs_pg
API = "/api/v1/auth/org"
NEW = {"first_name": "Новый", "last_name": "Ученик", "class_level": 9, "student_phone": "+998901234567"}


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")
    return build_d0(pg_session)


def test_o12_r1_r2_r8_only_managers_create_students(w):
    """[O12.R1] [O12.R2] [O12.R8] [INV-O12-1] owner / admin create (twice = two students, as before); a teacher gets 403 STAFF_ROLE_REQUIRED and nothing is created."""
    before = w.db.query(Student).filter_by(org_id=w.org.id).count()
    assert client(w, "admin").post(f"{API}/students", json=NEW).status_code == 201
    assert client(w, "owner").post(f"{API}/students", json=NEW).status_code == 201
    denied = client(w, "A").post(f"{API}/students", json=NEW)
    assert denied.status_code == 403 and denied.json()["detail"] == {"code": "STAFF_ROLE_REQUIRED", "message": "Добавлять учеников может администратор центра"}
    assert w.db.query(Student).filter_by(org_id=w.org.id).count() == before + 2
    assert w.db.query(Student).filter_by(org_id=w.org.id, created_by=w.A.id).count() == 0


def test_o12_r3_r9_archive_as_usual_and_legacy_students_of_a_teacher_are_the_admins(w):
    """[O12.R3] [O12.R9] A legacy student created by a teacher, with no group: the admin sees him, the teacher does not; archiving works the usual way."""
    legacy = new_student(w.db, w.org, w.ids, "legacy", "Старый", "Невидимка", None, status="new")
    legacy.created_by = w.A.id
    w.db.commit()
    names = lambda who: [s["last_name"] for s in client(w, who).get(f"{API}/students", params={"limit": 500}).json()]  # noqa: E731
    assert "Старый" in names("admin") and "Старый" not in names("A")
    assert client(w, "admin").post(f"{API}/students/{legacy.id}/archive", json={"reason": "Дубль"}).status_code == 200


def sv(w, who, student):
    return client(w, who).get(f"{API}/students/{student.id}").json()


def test_o15_r1_r2_r3_in_a_group_then_no_group_then_back(w):
    """[O15.R1] [O15.R2] [O15.R3] In the group -> active; removed from his only group -> no_group ("Без группы"); enrolled again -> active; a repeated removal is 400."""
    out = sv(w, "admin", w.s6)
    assert (out["status"], out["has_group"], out["status_view"]) == ("active", True, "active")
    assert client(w, "admin").delete(f"{API}/groups/{w.G3.id}/students/{w.s6.id}").status_code == 200
    out = sv(w, "admin", w.s6)
    assert (out["status"], out["has_group"], out["status_view"]) == ("active", False, "no_group")
    again = client(w, "admin").delete(f"{API}/groups/{w.G3.id}/students/{w.s6.id}")
    assert again.status_code == 400 and again.json()["detail"] == "Ученик не в этой группе"
    assert client(w, "admin").post(f"{API}/groups/{w.G3.id}/students/{w.s6.id}").status_code == 200
    assert sv(w, "admin", w.s6)["status_view"] == "active"


def test_o15_r4_r5_r8_lists_filter_and_the_stats(w):
    """[O15.R4] [O15.R5] [O15.R8] ?status=no_group = active and no live group (an archived group does not count); the list items carry has_group / status_view; the stats count them; teachers see their own."""
    client(w, "admin").delete(f"{API}/groups/{w.G3.id}/students/{w.s6.id}")
    new_student(w.db, w.org, w.ids, "n1", "Новичков", "Новый", None, status="new")
    w.G2.status = "archived"                                              # a legacy archived group: its students are "no group"
    w.db.commit()
    c = client(w, "admin")
    listed = c.get(f"{API}/students", params={"status": "no_group", "limit": 500}).json()
    assert sorted(s["last_name"] for s in listed) == ["Ахмедов", "Каримов", "Рашидова"]
    assert {s["status_view"] for s in listed} == {"no_group"} and {s["has_group"] for s in listed} == {False}
    everyone = c.get(f"{API}/students", params={"limit": 500}).json()
    assert {s["last_name"]: s["status_view"] for s in everyone}["Новичков"] == "new"
    stats = c.get(f"{API}/stats").json()
    assert stats["no_group_students"] == 3 and stats["new_students"] == 1
    teacher_view = client(w, "A").get(f"{API}/students", params={"limit": 500}).json()
    by_name = {s["last_name"]: s["status_view"] for s in teacher_view}
    assert by_name["Каримов"] == "active" and by_name["Ахмедов"] == "no_group"          # 9-А is live; 7-Б was archived the old way
    golden = fixture("x_student_no_group.json")
    assert golden["status_view"] == "no_group" and fixture("x_org_stats.json")["no_group_students"] == 1


def test_o15_r7_a_null_status_is_new(w):
    """[O15.R7]"""
    from src.auth.ops_rules import student_status_view

    assert student_status_view(None, False) == "new" and student_status_view("diagnosed", False) == "diagnosed"
    assert student_status_view("active", False) == "no_group" and student_status_view("archived", True) == "archived"


def test_o15_diagnostics_is_assigned_means_a_live_group(w):
    """[O15.R1] The diagnostics list: ``is_assigned`` = in a live group, not "status active"."""
    from src.models.auth import DiagnosticInvite

    w.db.add(DiagnosticInvite(org_id=w.org.id, student_id=w.s6.id, created_by=w.admin.id, code=f"D{w.salt}"[:8].upper(), class_level=8,
                              diagnostic_type="initial", status="pending", expires_at=__import__("datetime").datetime(2026, 12, 1)))
    w.db.commit()
    c = client(w, "admin")
    items = c.get(f"{API}/diagnostics").json()
    items = items["items"] if isinstance(items, dict) else items
    assert [d["is_assigned"] for d in items if d["student_id"] == str(w.s6.id)] == [True]
    c.delete(f"{API}/groups/{w.G3.id}/students/{w.s6.id}")
    items = c.get(f"{API}/diagnostics").json()
    items = items["items"] if isinstance(items, dict) else items
    assert [d["is_assigned"] for d in items if d["student_id"] == str(w.s6.id)] == [False]
