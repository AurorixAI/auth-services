"""Shared PostgreSQL fixture for the N13 P1 tests (attendance, excuses, groups).

Runs only on the disposable ``a2_auth`` database (A2_AUTH_TEST_DATABASE_URL):
SQLite silently skips anything with JSONB, so these tests never use it.
Every world gets its own organisations, so tests never see each other's rows.
"""
import os
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")

from src.auth.schemas import (  # noqa: E402
    ReconcileMissedLessonRequest,
    SaveAttendanceRequest,
    StartAttendanceRequest,
)
from src.auth.service import BUSINESS_TIMEZONE, AuthService  # noqa: E402
from src.models.auth import (  # noqa: E402
    Group,
    Organization,
    OrganizationMember,
    Student,
    StudentGroup,
    User,
)

DB_URL = os.getenv("A2_AUTH_TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(not DB_URL, reason="Requires disposable A2 auth PostgreSQL")

# A 24-hour slot makes "start lesson today" independent of the wall clock.
LIVE_SLOT = "live-today"


def business_today() -> date:
    return datetime.now(BUSINESS_TIMEZONE).date()


def make_user(db, role="teacher", name="Иван"):
    user = User(
        email=f"{uuid4()}@n13.invalid",
        password_hash="synthetic-only",
        first_name=name,
        last_name=role.capitalize(),
        role=role,
    )
    db.add(user)
    db.flush()
    return user


def make_org(db, name="N13 org"):
    org = Organization(name=name, status="active")
    db.add(org)
    db.flush()
    return org


def make_member(db, org, user, org_role):
    db.add(OrganizationMember(org_id=org.id, user_id=user.id, org_role=org_role, is_active=True))
    db.flush()


def make_group(db, org, teacher, name="Группа"):
    schedule = [
        {"id": f"s{day}", "dayOfWeek": day, "startTime": "10:00", "endTime": "11:00", "format": "offline"}
        for day in range(1, 8)
    ] + [
        {
            "id": LIVE_SLOT,
            "dayOfWeek": business_today().isoweekday(),
            "startTime": "00:00",
            "endTime": "23:59",
            "format": "offline",
        }
    ]
    group = Group(org_id=org.id, name=name, class_level=9, teacher_id=teacher.id, schedule=schedule)
    db.add(group)
    db.flush()
    return group


def make_student(db, org, group, first="Алия", last="Каримова", display_id=None):
    student = Student(
        org_id=org.id, first_name=first, last_name=last, class_level=9, group_id=group.id, display_id=display_id
    )
    db.add(student)
    db.flush()
    db.add(
        StudentGroup(
            student_id=student.id,
            group_id=group.id,
            is_active=True,
            joined_at=datetime.now() - timedelta(days=60),
        )
    )
    db.flush()
    return student


@pytest.fixture
def pg_session():
    assert DB_URL and DB_URL.endswith("/a2_auth"), "Use only the disposable a2_auth database"
    engine = create_engine(DB_URL)
    Organization.metadata.create_all(engine)
    with sessionmaker(engine)() as db:
        yield db
    engine.dispose()


@pytest.fixture
def world(pg_session, monkeypatch):
    """Org with owner, admin, two teachers, a group of three students, and a second org."""
    db = pg_session
    # Cancelling a lesson would ask exam-service; it is out of scope here.
    monkeypatch.setattr(AuthService, "_sync_exam_lesson_cancellation", lambda self, **kw: None)
    org, other_org = make_org(db), make_org(db, "Foreign org")
    owner, admin = make_user(db, "owner"), make_user(db, "admin")
    teacher, teacher2 = make_user(db, "teacher", "Пётр"), make_user(db, "teacher", "Мария")
    foreign_admin = make_user(db, "admin")
    for user, role in ((owner, "owner"), (admin, "admin"), (teacher, "teacher"), (teacher2, "teacher")):
        make_member(db, org, user, role)
    make_member(db, other_org, foreign_admin, "admin")
    group = make_group(db, org, teacher)
    students = [
        make_student(db, org, group, "Алия", "Каримова", "N13-001"),
        make_student(db, org, group, "Бек", "Юсупов", "N13-002"),
        make_student(db, org, group, "Дана", "Орлова", "N13-003"),
    ]
    foreign_group = make_group(db, other_org, foreign_admin, "Чужая")
    foreign_student = make_student(db, other_org, foreign_group, "Чужой", "Ученик", "N13-900")
    db.commit()
    return SimpleNamespace(
        db=db, svc=AuthService(db), org=org, other_org=other_org, owner=owner, admin=admin,
        teacher=teacher, teacher2=teacher2, foreign_admin=foreign_admin, group=group, students=students,
        foreign_group=foreign_group, foreign_student=foreign_student,
    )


# ---- lesson helpers (go through the real service paths) --------------------


def past_lesson(offset_days=1):
    day = business_today() - timedelta(days=offset_days)
    return day, f"s{day.isoweekday()}"


def reconcile(w, offset_days=1, group=None):
    """Admin opens a missed past lesson; returns (date, lesson_id)."""
    day, lesson_id = past_lesson(offset_days)
    w.svc.reconcile_missed_group_attendance(
        str(w.org.id), str((group or w.group).id),
        StartAttendanceRequest(lessonDate=day, lessonId=lesson_id),
        reconciled_by=str(w.admin.id), is_org_admin=True,
    )
    return day, lesson_id


def save(w, day, lesson_id, records, *, status="in_progress", actor=None, is_admin=True, **extra):
    actor = actor or w.admin
    return w.svc.save_group_attendance(
        str(w.org.id), str(w.group.id),
        SaveAttendanceRequest(
            lessonDate=day, lessonId=lesson_id, status=status,
            records={s.id: st for s, st in records}, **extra,
        ),
        marked_by=str(actor.id), is_org_admin=is_admin, is_org_manager=is_admin,
    )


def excused_lesson(w, reason="Болел, написал в Telegram", offset_days=1, complete=False, **extra):
    """A lesson where the first student is excused (pending) and the others present."""
    day, lesson_id = reconcile(w, offset_days)
    s1, s2, s3 = w.students
    records = [(s1, "excused"), (s2, "present"), (s3, "present")]
    save(w, day, lesson_id, records, status="completed" if complete else "in_progress",
         excuseReasons={s1.id: reason}, **extra)
    return day, lesson_id


# ---- HTTP client with a fixed identity -------------------------------------


def make_client(w, user=None, org="own", org_role=None, role=None):
    """TestClient where the *live* identity (what RoleChecker receives) is fixed."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import src.api.internal_router  # noqa: F401  (import order: avoids a pre-existing router cycle)
    from src.auth.attendance_excuses_router import router as excuses_router
    from src.auth.payments_router import router as payments_router
    from src.auth.dependencies import get_current_active_user_payload
    from src.auth.router import router as auth_router
    from src.core.database import get_db

    org_obj = w.org if org == "own" else w.other_org if org == "other" else None
    user = user or w.admin
    payload = {"sub": str(user.id), "role": role or user.role, "type": "access"}
    if org_obj is not None:
        payload["org_id"] = str(org_obj.id)
        payload["org_role"] = org_role or user.role
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(excuses_router, prefix="/api/v1")
    app.include_router(payments_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: w.db
    app.dependency_overrides[get_current_active_user_payload] = lambda: payload
    return TestClient(app)
