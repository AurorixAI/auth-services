"""A2 regressions on disposable PostgreSQL (A2_AUTH_TEST_DATABASE_URL)."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from src.core.db_timezone import utc_connect_args
from sqlalchemy.orm import sessionmaker
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.internal_router import router as internal_router
from src.api.database import get_db
from src.core.config import get_settings

from src.auth.service import AuthService
from src.models.auth import DiagnosticInvite, Organization, Student, StudentDiagnosticResult, User

DB_URL = os.getenv("A2_AUTH_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="Requires disposable A2 auth PostgreSQL")


@pytest.fixture
def scenario():
    assert DB_URL and DB_URL.endswith("/a2_auth"), "Use only the disposable a2_auth database"
    engine = create_engine(DB_URL, connect_args=utc_connect_args(DB_URL))
    Organization.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as db:
        owner = User(email=f"{uuid4()}@a2.invalid", password_hash="synthetic-only", first_name="A2", last_name="Owner", role="owner")
        org = Organization(name="A2 disposable org", status="active")
        db.add_all([owner, org])
        db.flush()
        student = Student(org_id=org.id, first_name="A2", last_name="Student", class_level=9)
        db.add(student)
        db.flush()
        now = datetime.now(timezone.utc)
        invite = DiagnosticInvite(org_id=org.id, student_id=student.id, created_by=owner.id, code=uuid4().hex[:10],
                                  class_level=9, status="in_progress", cat_session_id=uuid4(),
                                  confirmed_at=now-timedelta(hours=2), started_at=now-timedelta(hours=1),
                                  expires_at=now+timedelta(hours=1))
        db.add(invite)
        db.commit()
        yield db, factory, invite
    engine.dispose()


def payload(session_id, **kwargs):
    return {"session_id": str(session_id), "final_theta": 0.4, "se": 0.3, "total_items": 1, "correct_answers": 1, **kwargs}


@pytest.mark.parametrize("expired", [False, True])
def test_completed_projection_is_idempotent_even_after_invite_expiry(scenario, expired):
    db, _factory, invite = scenario
    if expired:
        invite.expires_at = datetime.now(timezone.utc)-timedelta(seconds=1)
        db.commit()
        assert AuthService(db).get_diagnostic_by_code(invite.code)["status"] == "expired"
    first = AuthService(db).complete_diagnostic(invite.code, payload(invite.cat_session_id))
    second = AuthService(db).complete_diagnostic(invite.code, payload(invite.cat_session_id))
    assert second == first
    assert invite.status == "completed"
    assert db.query(StudentDiagnosticResult).filter_by(invite_id=invite.id).count() == 1
    assert db.get(Student, invite.student_id).status == "diagnosed"


@pytest.mark.parametrize("status", ["in_progress", "expired", "completed"])
def test_different_attempt_cannot_replace_bound_session(scenario, status):
    db, _factory, invite = scenario
    invite.status = status
    db.commit()
    original_id = invite.cat_session_id
    with pytest.raises(ValueError, match="Сессия не соответствует"):
        AuthService(db).complete_diagnostic(invite.code, payload(uuid4()))
    db.rollback()
    assert invite.cat_session_id == original_id
    assert db.query(StudentDiagnosticResult).filter_by(invite_id=invite.id).count() == 0


@pytest.mark.parametrize("status", ["confirmed", "expired"])
def test_internal_completion_repairs_lost_start_callback_with_db_timestamp(scenario, status):
    db, _factory, invite = scenario
    session_id = invite.cat_session_id
    invite.cat_session_id = None
    invite.status = status
    invite.expires_at = datetime.now(timezone.utc)-timedelta(minutes=30) if status == "expired" else invite.expires_at
    db.commit()
    started = (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    result = AuthService(db).complete_diagnostic(invite.code, payload(session_id, session_started_at=started))
    assert result["status"] == "completed"
    assert invite.cat_session_id == session_id
    assert db.query(StudentDiagnosticResult).filter_by(invite_id=invite.id).count() == 1


@pytest.mark.parametrize("timestamp", [None, "invalid", "before_confirm", "after_expiry", "future"])
def test_lost_start_repair_requires_a_confirmed_start_within_time_bounds(scenario, timestamp):
    db, _factory, invite = scenario
    invite.status, invite.cat_session_id = "expired", None
    invite.expires_at = datetime.now(timezone.utc)-timedelta(minutes=30)
    db.commit()
    values = {
        "before_confirm": (datetime.now(timezone.utc)-timedelta(hours=3)).isoformat(),
        "after_expiry": (datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat(),
        "future": (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
    }
    with pytest.raises(ValueError, match="Не подтверждён допустимый старт"):
        AuthService(db).complete_diagnostic(invite.code, payload(uuid4(), session_started_at=values.get(timestamp, timestamp)))
    db.rollback()
    assert invite.cat_session_id is None
    assert invite.status == "expired"
    assert db.query(StudentDiagnosticResult).filter_by(invite_id=invite.id).count() == 0


def test_never_confirmed_expired_invite_cannot_be_completed(scenario):
    db, _factory, invite = scenario
    invite.status, invite.cat_session_id, invite.confirmed_at = "expired", None, None
    db.commit()
    with pytest.raises(ValueError, match="Не подтверждён допустимый старт"):
        AuthService(db).complete_diagnostic(invite.code, payload(uuid4(), session_started_at=datetime.now(timezone.utc).isoformat()))
    db.rollback()
    assert db.query(StudentDiagnosticResult).filter_by(invite_id=invite.id).count() == 0


def test_overlapping_completion_callbacks_keep_one_admin_result(scenario):
    db, factory, invite = scenario
    code, session_id, invite_id = invite.code, invite.cat_session_id, invite.id
    def complete(_):
        with factory() as request_db:
            return AuthService(request_db).complete_diagnostic(code, payload(session_id))
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(complete, range(3)))
    assert results[0] == results[1] == results[2]
    assert db.query(StudentDiagnosticResult).filter_by(invite_id=invite_id).count() == 1


def test_http_completion_keeps_start_timestamp_and_requires_internal_token(scenario):
    db, factory, invite = scenario
    session_id = invite.cat_session_id
    invite.cat_session_id, invite.status = None, "expired"
    invite.expires_at = datetime.now(timezone.utc)-timedelta(minutes=30)
    db.commit()
    body = payload(session_id, session_started_at=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat())
    app = FastAPI()
    app.include_router(internal_router, prefix="/api/v1")
    def request_db():
        with factory() as callback_db:
            yield callback_db
    app.dependency_overrides[get_db] = request_db
    with TestClient(app) as client:
        endpoint = f"/api/v1/internal/diagnostic/{invite.code}/complete"
        assert client.post(endpoint, json=body).status_code == 403
        assert client.post(endpoint, json=body, headers={"X-Internal-Service-Token": "wrong"}).status_code == 403
        response = client.post(endpoint, json=body, headers={"X-Internal-Service-Token": get_settings().internal_service_token})
        assert response.status_code == 200, response.text
        assert client.post(endpoint, json=body, headers={"X-Internal-Service-Token": get_settings().internal_service_token}).json() == response.json()
    assert db.query(StudentDiagnosticResult).filter_by(invite_id=invite.id).count() == 1
