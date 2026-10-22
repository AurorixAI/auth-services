"""Student list: server-side paging with an exact total (centres have 200+ students)."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from src.core.db_timezone import utc_connect_args
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")

from src.auth.service import AuthService
from src.models.auth import Group, Organization, Student, StudentGroup, User


DB_URL = os.getenv("A2_AUTH_TEST_DATABASE_URL")
# PostgreSQL only: the models use JSONB, which SQLite cannot create, so a SQLite
# fixture would skip itself silently and a "cut at 100" regression would pass CI.
pytestmark = pytest.mark.skipif(not DB_URL, reason="Requires disposable A2 auth PostgreSQL")


@pytest.fixture
def world():
    assert DB_URL and DB_URL.endswith("/a2_auth"), "Use only the disposable a2_auth database"
    engine = create_engine(DB_URL, connect_args=utc_connect_args(DB_URL))
    Organization.metadata.create_all(engine)
    with sessionmaker(engine, autoflush=False)() as db:
        org = Organization(name="Org", status="active")
        other = Organization(name="Other", status="active")
        db.add_all([org, other]); db.flush()
        group = Group(org_id=org.id, name="G", class_level=8)
        db.add(group); db.flush()
        for i in range(250):
            student = Student(org_id=org.id, first_name=f"Имя{i}", last_name="Иванов" if i % 2 else "Петров",
                              class_level=8, group_id=group.id if i < 220 else None)
            db.add(student)
            db.flush()
            if i < 220:
                # The group filter reads active memberships (a student may be in several groups).
                db.add(StudentGroup(student_id=student.id, group_id=group.id, is_active=True))
        db.add(Student(org_id=other.id, first_name="Чужой", last_name="Ученик", class_level=8))
        db.commit()
        yield db, org.id, group.id
    engine.dispose()


def _page(db, org, **kw):
    return AuthService(db).list_students_page(org, actor_id="admin", is_org_admin=True, **kw)


def test_more_than_100_students_are_reachable_with_exact_total(world):
    db, org, _ = world
    items, total = _page(db, org, limit=100, offset=0)
    assert total == 250 and len(items) == 100
    ids = {s.id for s in items}
    for offset in (100, 200):
        page, t = _page(db, org, limit=100, offset=offset)
        assert t == 250
        ids |= {s.id for s in page}
    assert len(ids) == 250  # no gaps / duplicates across pages (stable order)


def test_total_respects_filters_and_search(world):
    db, org, group = world
    assert _page(db, org, group_id=group, limit=10)[1] == 220
    assert _page(db, org, has_primary_group=True, limit=10)[1] == 220
    assert _page(db, org, search="Иванов", limit=10)[1] == 125
    # full-name query: every word must match
    items, total = _page(db, org, search="Имя249 Иванов", limit=50)
    assert total == 1 and items[0].first_name == "Имя249"


def test_list_students_keeps_plain_list_contract(world):
    db, org, _ = world
    items = AuthService(db).list_students(org, actor_id="admin", is_org_admin=True)
    assert isinstance(items, list) and len(items) == 100


def test_teacher_without_groups_sees_nobody(world):
    db, org, _ = world
    items, total = AuthService(db).list_students_page(org, actor_id=uuid4(), is_org_admin=False)
    assert items == [] and total == 0
