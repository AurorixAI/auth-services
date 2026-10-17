"""Level fields: contract cut-offs (shared with diagnostic-service), fallback and payload handling."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from src.core.db_timezone import utc_connect_args
from sqlalchemy.orm import sessionmaker

from src.api.database import get_db
from src.api.internal_router import router as internal_router
from src.auth.diagnostic_level import (
    classify_theta, level_fields, normalize_level_payload, theta_to_category, theta_to_status,
)
from src.auth.service import AuthService
from src.core.config import get_settings
from src.models.auth import DiagnosticInvite, Group, Organization, Student, StudentDiagnosticResult, StudentGroup, User


# --- pure helper: boundaries are the cross-service contract -------------------

@pytest.mark.parametrize("theta,category,status", [
    (-3.0, "below_grade", "critical"),
    (-1.4001, "below_grade", "critical"),
    (-1.4, "below_grade", "below"),        # critical is strictly < -1.4
    (-0.5001, "below_grade", "below"),
    (-0.5, "at_grade", "partial_gap"),    # category and status both switch at -0.5
    (0.0999, "at_grade", "partial_gap"),
    (0.1, "at_grade", "on_level"),
    (0.9999, "at_grade", "on_level"),
    (1.0, "above_grade", "above"),        # both switch at 1.0
    (3.0, "above_grade", "above"),
])
def test_cutoffs_match_contract(theta, category, status):
    assert theta_to_category(theta) == category
    assert theta_to_status(theta) == status
    assert classify_theta(theta) == (category, status)


def test_unknown_theta_has_no_level():
    assert classify_theta(None) == (None, None)
    assert level_fields(SimpleNamespace(final_theta=None, overall_level=None, level_status=None,
                                        class_equivalent=None, level_provisional=None)) == {
        "recommended_level": None, "level_status": None, "class_equivalent": None, "level_provisional": None}


def test_stored_values_win_over_theta_and_fallback_fills_old_rows():
    stored = SimpleNamespace(final_theta=0.48, overall_level="below_grade", level_status="critical",
                             class_equivalent="начало 7 класса", level_provisional=True)
    assert level_fields(stored) == {"recommended_level": "below_grade", "level_status": "critical",
                                    "class_equivalent": "начало 7 класса", "level_provisional": True}
    old = SimpleNamespace(final_theta=0.48, overall_level=None, level_status=None,
                          class_equivalent=None, level_provisional=None)
    assert level_fields(old) == {"recommended_level": "at_grade", "level_status": "on_level",
                                 "class_equivalent": None, "level_provisional": None}


def test_normalize_drops_invalid_and_truncates():
    assert normalize_level_payload({}) == {"overall_level": None, "level_status": None,
                                           "class_equivalent": None, "level_provisional": None}
    got = normalize_level_payload({"overall_level": "weird", "level_status": "on_level",
                                   "class_equivalent": "x" * 100, "level_provisional": "yes"})
    assert got["overall_level"] is None and got["level_status"] == "on_level"
    assert len(got["class_equivalent"]) == 64 and got["level_provisional"] is None
    assert normalize_level_payload({"level_provisional": False})["level_provisional"] is False


# --- completion payload -> storage -> API (disposable PostgreSQL) -------------

DB_URL = os.getenv("A2_AUTH_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DB_URL, reason="Requires disposable A2 auth PostgreSQL")


@pytest.fixture
def world():
    assert DB_URL and DB_URL.endswith("/a2_auth"), "Use only the disposable a2_auth database"
    engine = create_engine(DB_URL, connect_args=utc_connect_args(DB_URL))
    Organization.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as db:
        owner = User(email=f"{uuid4()}@n02.invalid", password_hash="synthetic-only", first_name="N", last_name="O", role="owner")
        org = Organization(name="N02 disposable org", status="active")
        db.add_all([owner, org]); db.flush()
        group = Group(org_id=org.id, name="N02 group", class_level=9)
        db.add(group); db.flush()
        now = datetime.now(timezone.utc)
        invites = []
        for _ in range(2):
            student = Student(org_id=org.id, first_name="N02", last_name="Student", class_level=9)
            db.add(student); db.flush()
            db.add(StudentGroup(student_id=student.id, group_id=group.id)); db.flush()
            inv = DiagnosticInvite(org_id=org.id, student_id=student.id, created_by=owner.id, code=uuid4().hex[:10],
                                   class_level=9, status="in_progress", cat_session_id=uuid4(),
                                   confirmed_at=now - timedelta(hours=2), started_at=now - timedelta(hours=1),
                                   expires_at=now + timedelta(hours=1))
            db.add(inv); invites.append(inv)
        db.commit()
        app = FastAPI(); app.include_router(internal_router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: db
        token = {"X-Internal-Service-Token": get_settings().internal_service_token}
        yield db, TestClient(app), token, org, group, invites
    engine.dispose()


def _complete(client, token, invite, **extra):
    body = {"session_id": str(invite.cat_session_id), "final_theta": 0.48, "se": 0.3, "total_items": 30,
            "correct_answers": 6, **extra}
    r = client.post(f"/api/v1/internal/diagnostic/{invite.code}/complete", json=body, headers=token)
    assert r.status_code == 200, r.text
    return r.json()


def _row(db, invite):
    db.expire_all()
    return db.query(StudentDiagnosticResult).filter_by(invite_id=invite.id).one()


@needs_db
def test_payload_with_new_fields_is_stored_and_served(world):
    db, client, token, org, group, (inv, _) = world
    _complete(client, token, inv, overall_level="at_grade", level_status="on_level",
              class_equivalent="конец 8 класса", level_provisional=True)
    row = _row(db, inv)
    assert (row.overall_level, row.level_status, row.class_equivalent, row.level_provisional) == \
        ("at_grade", "on_level", "конец 8 класса", True)
    svc = AuthService(db)
    listed, _ = svc.list_diagnostic_invites(str(org.id))
    res = next(i for i in listed if i["id"] == str(inv.id))["result"]
    assert (res["recommended_level"], res["level_status"], res["class_equivalent"], res["level_provisional"]) == \
        ("at_grade", "on_level", "конец 8 класса", True)
    assert (res["total_questions"], res["correct_answers"]) == (30, 6)
    detail = svc.get_diagnostic_invite(str(org.id), str(inv.id), actor_id=None, is_org_admin=True)
    assert detail["result"]["level_status"] == "on_level"
    assert detail["result"]["class_equivalent"] == "конец 8 класса"
    prof = svc.get_student_profile(str(org.id), str(inv.student_id), actor_id=None, is_org_admin=True)
    d = next(x for x in prof["diagnostics"] if x["invite_id"] == str(inv.id))["result"]
    assert d["level_status"] == "on_level" and d["class_equivalent"] == "конец 8 класса"
    assert d["recommended_load"].startswith("Стандартный")


@needs_db
def test_payload_without_new_fields_falls_back_to_theta(world):
    db, client, token, org, group, (inv, _) = world
    _complete(client, token, inv, final_theta=-1.4)
    row = _row(db, inv)
    assert row.overall_level is None and row.level_status is None and row.level_provisional is None
    listed, _ = AuthService(db).list_diagnostic_invites(str(org.id))
    res = next(i for i in listed if i["id"] == str(inv.id))["result"]
    assert (res["recommended_level"], res["level_status"]) == ("below_grade", "below")
    assert res["class_equivalent"] is None and res["level_provisional"] is None


@needs_db
def test_stored_level_is_not_rederived_from_theta_in_group_views(world):
    db, client, token, org, group, (a, b) = world
    # theta says at_grade/on_level, the stored policy result says otherwise: stored wins.
    _complete(client, token, a, final_theta=0.48, overall_level="below_grade", level_status="critical",
              class_equivalent="начало 7 класса", level_provisional=False)
    _complete(client, token, b, final_theta=1.2)  # old-style payload -> fallback above_grade
    svc = AuthService(db)
    stats = svc.get_group_stats(str(org.id), str(group.id), is_org_admin=True)
    assert stats["theta_distribution"] == {"below_grade": 1, "at_grade": 0, "above_grade": 1}
    detail = svc.get_group_detail(str(org.id), str(group.id), is_org_admin=True)
    by_id = {s["id"]: s for s in detail["students"]}
    sa, sb = by_id[str(a.student_id)], by_id[str(b.student_id)]
    assert (sa["last_level"], sa["last_level_status"], sa["last_class_equivalent"]) == \
        ("below_grade", "critical", "начало 7 класса")
    assert (sb["last_level"], sb["last_level_status"]) == ("above_grade", "above")


@needs_db
def test_resent_completion_updates_level_columns_and_nothing_else(world):
    db, client, token, org, group, (inv, _) = world
    first = _complete(client, token, inv, final_theta=0.48)
    before = _row(db, inv)
    assert before.overall_level is None and before.level_status is None
    snapshot = (before.id, before.final_theta, before.se, before.diagnosed_at)
    assert (before.total_tasks, before.correct_answers) == (30, 6)

    again = _complete(client, token, inv, final_theta=-2.0, total_items=25, correct_answers=8,
                      overall_level="at_grade", level_status="on_level",
                      class_equivalent="конец 8 класса", level_provisional=True)
    assert again == first
    after = _row(db, inv)
    assert (after.overall_level, after.level_status, after.class_equivalent, after.level_provisional) == \
        ("at_grade", "on_level", "конец 8 класса", True)
    assert (after.id, after.final_theta, after.se, after.diagnosed_at) == snapshot
    # The newer report's counts are authoritative (CAT answers only, no deep-scan).
    assert (after.total_tasks, after.correct_answers) == (25, 8)
    assert float(after.accuracy) == 0.32
    assert db.query(StudentDiagnosticResult).filter_by(invite_id=inv.id).count() == 1

    # A newer authoritative report overwrites; a resend without fields keeps what is stored.
    _complete(client, token, inv, total_items=25, correct_answers=8, overall_level="below_grade", level_status="below",
              class_equivalent="начало 8 класса", level_provisional=False)
    assert (_row(db, inv).level_status, _row(db, inv).level_provisional) == ("below", False)
    _complete(client, token, inv, total_items=None, correct_answers=None)
    row = _row(db, inv)
    assert (row.overall_level, row.class_equivalent) == ("below_grade", "начало 8 класса")
    assert (row.total_tasks, row.correct_answers) == (25, 8)
