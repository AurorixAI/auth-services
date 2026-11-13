"""Money-1 Р-10 (R3 М7): the days a student left other groups are read in a fixed number of queries - the groups come with the memberships, the lessons of those groups are loaded for all of
them at once - and only inside the centre of the page (PostgreSQL)."""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import event

from src.models.auth import StudentGroup

from tests.pg_world import give_group_price, make_group, make_student, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_payments_service_pg import API, day, owner, set_settings

pytestmark = needs_pg


def _students_with_a_finished_old_group(world, count):
    for i in range(count):
        st = make_student(world.db, world.org, world.group, f"Н{i}{uuid4().hex[:4]}", "Прежний", f"OLD-{uuid4().hex[:8]}")
        old = make_group(world.db, world.org, world.teacher, f"Старая {uuid4().hex[:6]}")           # a group of his own for each: the page meets as many groups as students
        give_group_price(world.db, old, 400_000)
        world.db.add(StudentGroup(student_id=st.id, group_id=old.id, is_active=False, joined_at=datetime(2026, 9, 1, 6), left_at=datetime(2026, 9, 20, 6)))
    world.db.commit()


def _queries(world, path):
    statements = []
    engine = world.db.get_bind()
    listener = lambda conn, cur, stmt, *a: statements.append(stmt)          # noqa: E731
    event.listen(engine, "before_cursor_execute", listener)
    try:
        assert owner(world).get(path).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    return len(statements)


def test_the_page_of_statuses_does_not_query_per_old_group(world):
    set_settings(world, period_mode="calendar")
    give_group_price(world.db, world.group, 500_000)
    path = f"{API}/org/payments/status?limit=50"
    _students_with_a_finished_old_group(world, 3)
    small = _queries(world, path)
    _students_with_a_finished_old_group(world, 12)
    large = _queries(world, path)
    assert large == small, (small, large)                       # before: +1 for every group he left (the group, the moved lessons of the group)
