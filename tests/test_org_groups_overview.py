"""N13 P1.5: the owner dashboard's per-group level comes from the monolith, not from a missing analytics route."""
from datetime import datetime, timedelta

from sqlalchemy import event

from src.models.auth import StudentDiagnosticResult

from tests.pg_world import make_client, make_group, make_student, needs_pg, pg_session, world  # noqa: F401

pytestmark = needs_pg

URL = "/api/v1/auth/org/groups-overview"


def result(w, student, theta, ago_days=0):
    w.db.add(StudentDiagnosticResult(
        student_id=student.id, org_id=w.org.id, final_theta=theta,
        diagnosed_at=datetime.utcnow() - timedelta(days=ago_days),
    ))
    w.db.commit()


def test_average_theta_uses_the_latest_result_of_each_student(world):
    s0, s1, s2 = world.students
    result(world, s0, -1.0, ago_days=30)    # superseded
    result(world, s0, 1.0, ago_days=1)
    result(world, s1, 0.5)
    body = make_client(world).get(URL).json()
    row = next(g for g in body["groups"] if g["group_id"] == str(world.group.id))
    assert row["student_count"] == 3 and row["diagnosed_count"] == 2
    assert abs(row["avg_theta"] - 0.75) < 1e-9
    assert row["name"] == world.group.name and row["class_level"] == 9


def test_group_without_results_has_a_null_average_not_zero(world):
    empty = make_group(world.db, world.org, world.teacher, "Без результатов")
    world.db.commit()
    body = make_client(world).get(URL).json()
    row = next(g for g in body["groups"] if g["group_id"] == str(empty.id))
    assert row == {"group_id": str(empty.id), "name": "Без результатов", "class_level": 9,
                   "student_count": 0, "diagnosed_count": 0, "avg_theta": None}


def test_only_active_groups_of_the_own_organisation(world):
    other = make_client(world, user=world.foreign_admin, org="other").get(URL).json()
    assert {g["group_id"] for g in other["groups"]} == {str(world.foreign_group.id)}
    world.group.status = "archived"
    world.db.commit()
    own = make_client(world).get(URL).json()
    assert str(world.group.id) not in {g["group_id"] for g in own["groups"]}


def test_teacher_cannot_read_the_org_overview(world):
    assert make_client(world, user=world.teacher).get(URL).status_code == 403


def test_overview_query_count_does_not_grow_with_groups(world):
    def count_queries():
        statements = []
        engine = world.db.get_bind()

        def record(conn, cursor, statement, *a):
            statements.append(statement)

        world.db.expire_all()
        event.listen(engine, "before_cursor_execute", record)
        try:
            world.svc.get_org_groups_overview(str(world.org.id))
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return len(statements)

    small = count_queries()
    for i in range(12):
        group = make_group(world.db, world.org, world.teacher, f"Доп {i}")
        student = make_student(world.db, world.org, group, f"Имя{i}", f"Фамилия{i}")
        result(world, student, 0.1 * i)
    assert count_queries() == small <= 4
