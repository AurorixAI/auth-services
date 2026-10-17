"""ROADMAP #89: archiving a student switches his unfinished diagnostic links off (PostgreSQL).

pending / confirmed  -> cancelled (cancelled_at, cancel_reason = student_archived);
in_progress          -> stays (the student may finish, the result stays on his card);
completed / expired  -> untouched.
A cancelled code is unusable on every public path and is never revived by the restore.
"""
from datetime import datetime, timedelta, timezone
from threading import Thread
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from src.api.database import get_db as internal_get_db
from src.api.internal_router import router as internal_router
from src.auth.service import AuthService
from src.core.config import get_settings
from src.core.database import get_db
from src.models.auth import DiagnosticInvite, Student, StudentDiagnosticResult

from tests.pg_world import make_client, needs_pg, pg_session, world  # noqa: F401  (fixtures)

pytestmark = needs_pg

API = "/api/v1/auth"
INTERNAL = "/api/v1/internal"
STUDENTS = f"{API}/org/students"
CANCELLED = "Приглашение отменено"
ARCHIVED_CREATE = "Ученик в архиве: диагностику создать нельзя. Сначала верните ученика из архива."


# ---- helpers --------------------------------------------------------------------------------------------


def invite(w, student, status, *, expires_in=timedelta(hours=24), diagnostic_type="initial"):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row = DiagnosticInvite(
        org_id=w.org.id, student_id=student.id, created_by=w.admin.id, code=uuid4().hex[:10].upper(),
        class_level=9, diagnostic_type=diagnostic_type, status=status, expires_at=now + expires_in,
        confirmed_at=now - timedelta(minutes=30) if status in ("confirmed", "in_progress", "completed") else None,
        started_at=now - timedelta(minutes=20) if status in ("in_progress", "completed") else None,
        cat_session_id=uuid4() if status in ("in_progress", "completed") else None,
    )
    w.db.add(row)
    w.db.commit()
    return row


def reload(w, row):
    w.db.expire_all()
    return w.db.get(DiagnosticInvite, row.id)


def archive(w, student, expect=200):
    r = make_client(w, user=w.admin).post(f"{STUDENTS}/{student.id}/archive", json={"reason": "Переехал"})
    assert r.status_code == expect, r.text
    return r.json()


def restore(w, student, expect=200):
    r = make_client(w, user=w.admin).post(f"{STUDENTS}/{student.id}/restore", json=None)
    assert r.status_code == expect, r.text
    return r.json()


def public_client(w):
    """The public invite routes AND the internal callbacks of diagnostic-service, on the world's session."""
    app = FastAPI()
    from src.auth.router import router as auth_router

    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(internal_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: w.db
    app.dependency_overrides[internal_get_db] = lambda: w.db
    return TestClient(app)


def token():
    return {"X-Internal-Service-Token": get_settings().internal_service_token}


@pytest.fixture
def pack(world):
    """The first student with one invite in every state."""
    student = world.students[0]
    world.invites = {
        status: invite(world, student, status)
        for status in ("pending", "confirmed", "in_progress", "completed", "expired")
    }
    world.student = student
    return world


# ---- the archiving --------------------------------------------------------------------------------------


def test_archive_cancels_pending_and_confirmed_and_keeps_in_progress_completed_expired(pack):
    other = invite(pack, pack.students[1], "pending")  # another student's link is not touched
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    body = archive(pack, pack.student)

    assert body["diagnostic_invites_cancelled"] == 2
    assert body["diagnostic_invites_in_progress"] == 1
    for status in ("pending", "confirmed"):
        row = reload(pack, pack.invites[status])
        assert row.status == "cancelled" and row.cancel_reason == "student_archived"
        assert row.cancelled_at is not None and before - timedelta(seconds=5) <= row.cancelled_at
        assert row.cancelled_at <= datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=5)
    for status in ("in_progress", "completed", "expired"):
        row = reload(pack, pack.invites[status])
        assert row.status == status and row.cancelled_at is None and row.cancel_reason is None
    assert reload(pack, other).status == "pending"
    # the archiving itself went through
    assert pack.db.get(Student, pack.student.id).status == "archived"


def test_the_response_count_is_zero_when_there_is_nothing_to_cancel(world):
    body = archive(world, world.students[1])
    assert body["diagnostic_invites_cancelled"] == 0 and body["diagnostic_invites_in_progress"] == 0
    # completed / expired links alone do not count either
    other = world.students[2]
    invite(world, other, "completed")
    invite(world, other, "expired")
    body = archive(world, other)
    assert body["diagnostic_invites_cancelled"] == 0 and body["diagnostic_invites_in_progress"] == 0


def test_a_student_without_a_group_is_archived_with_the_same_counts(world):
    loner = Student(org_id=world.org.id, first_name="Без", last_name="Группы", class_level=9, status="new")
    world.db.add(loner)
    world.db.commit()
    invite(world, loner, "pending")
    body = archive(world, loner)
    assert body["closed_groups"] == [] and body["diagnostic_invites_cancelled"] == 1


def test_a_failed_archiving_cancels_nothing(pack):
    archive(pack, pack.student)
    again = invite(pack, pack.student, "pending")  # a link added by hand after the archiving
    archive(pack, pack.student, expect=409)  # «Ученик уже в архиве»
    assert reload(pack, again).status == "pending"


# ---- a cancelled code is unusable on every public path -------------------------------------------------


def test_a_cancelled_code_is_rejected_on_every_public_path(pack):
    archive(pack, pack.student)
    cancelled = reload(pack, pack.invites["pending"])
    code = cancelled.code
    client = public_client(pack)

    # lookup: the projection of diagnostic-service - status «cancelled» (as «expired» is) and the text
    info = client.get(f"{API}/diagnostic/{code}")
    assert info.status_code == 200
    assert info.json()["status"] == "cancelled" and info.json()["message"] == CANCELLED
    # confirm: a refusal of the same class as an expired link (400), with the text
    r = client.post(f"{API}/diagnostic/{code}/confirm")
    assert r.status_code == 400 and r.json()["detail"] == CANCELLED
    # the callbacks of diagnostic-service
    session = str(uuid4())
    r = client.post(f"{INTERNAL}/diagnostic/{code}/start", json={"session_id": session}, headers=token())
    assert r.status_code == 400 and r.json()["detail"] == CANCELLED
    r = client.post(
        f"{INTERNAL}/diagnostic/{code}/complete",
        json={"session_id": session, "final_theta": 0.1, "se": 0.3, "total_items": 3, "correct_answers": 1},
        headers=token(),
    )
    assert r.status_code == 400 and r.json()["detail"] == CANCELLED
    # nothing changed
    row = reload(pack, cancelled)
    assert row.status == "cancelled" and row.cat_session_id is None
    assert pack.db.query(StudentDiagnosticResult).filter_by(invite_id=row.id).count() == 0

    # the second cancelled one (was «confirmed») answers the same
    code2 = reload(pack, pack.invites["confirmed"]).code
    assert client.post(f"{API}/diagnostic/{code2}/confirm").json()["detail"] == CANCELLED
    # an expired code keeps its own, different answer
    expired = pack.invites["expired"]
    assert client.get(f"{API}/diagnostic/{expired.code}").json()["status"] == "expired"
    assert "message" not in client.get(f"{API}/diagnostic/{expired.code}").json()


def test_service_level_refusals_carry_the_text(pack):
    archive(pack, pack.student)
    code = reload(pack, pack.invites["pending"]).code
    svc = AuthService(pack.db)
    for call in (
        lambda: svc.confirm_diagnostic(code),
        lambda: svc.start_diagnostic(code, str(uuid4())),
        lambda: svc.complete_diagnostic(code, {"session_id": str(uuid4())}),
    ):
        with pytest.raises(ValueError, match=CANCELLED):
            call()
        pack.db.rollback()


def test_a_cancelled_code_is_not_a_miss_for_the_guess_throttle(pack):
    """«Приглашение отменено» is a state a student meets, not a guess: it does not contain «не найдена»."""
    assert "не найдена" not in CANCELLED


def test_cancelled_never_turns_into_expired(pack):
    archive(pack, pack.student)
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    cancelled = reload(pack, pack.invites["pending"])
    running = reload(pack, pack.invites["in_progress"])
    cancelled.expires_at = running.expires_at = past
    pack.db.commit()
    svc = AuthService(pack.db)
    assert svc.get_diagnostic_by_code(cancelled.code)["status"] == "cancelled"  # the single-invite expiry helper
    # the bulk helper (it runs before every organisation list / counter) flips the due unfinished one only
    assert svc._expire_due_diagnostic_invites(str(pack.org.id)) == 1
    assert reload(pack, running).status == "expired"
    row = reload(pack, cancelled)
    assert row.status == "cancelled" and row.cancelled_at is not None and row.cancel_reason == "student_archived"


# ---- an invite in progress may finish -------------------------------------------------------------------


def test_an_invite_in_progress_can_still_be_completed_and_the_result_stays_on_the_card(pack):
    archive(pack, pack.student)
    running = reload(pack, pack.invites["in_progress"])
    session_id = running.cat_session_id
    client = public_client(pack)
    r = client.post(
        f"{INTERNAL}/diagnostic/{running.code}/complete",
        json={"session_id": str(session_id), "final_theta": 0.4, "se": 0.3, "total_items": 5, "correct_answers": 3},
        headers=token(),
    )
    assert r.status_code == 200, r.text
    assert reload(pack, running).status == "completed"
    assert pack.db.query(StudentDiagnosticResult).filter_by(invite_id=running.id).count() == 1
    student = pack.db.get(Student, pack.student.id)
    assert student.status == "archived"  # finishing the test does not bring him back
    profile = make_client(pack, user=pack.admin).get(f"{STUDENTS}/{pack.student.id}/profile").json()
    assert any(d["invite_id"] == str(running.id) and d["status"] == "completed" and d["result"] for d in profile["diagnostics"])


# ---- the restore --------------------------------------------------------------------------------------


def test_restore_does_not_revive_a_cancelled_invite(pack):
    archive(pack, pack.student)
    restore(pack, pack.student)
    for status in ("pending", "confirmed"):
        row = reload(pack, pack.invites[status])
        assert row.status == "cancelled" and row.cancel_reason == "student_archived"
    assert public_client(pack).get(f"{API}/diagnostic/{reload(pack, pack.invites['pending']).code}").json()["status"] == "cancelled"


# ---- creating a link for an archived student ------------------------------------------------------------


def test_creating_an_invite_for_an_archived_student_is_409(world):
    student = world.students[0]
    archive(world, student)
    r = make_client(world, user=world.admin).post(
        f"{API}/org/diagnostics", json={"student_id": str(student.id), "class_level": 9, "diagnostic_type": "recheck"}
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == ARCHIVED_CREATE
    assert world.db.query(DiagnosticInvite).filter_by(student_id=student.id).count() == 0
    # after the return a new link can be created
    restore(world, student)
    r = make_client(world, user=world.admin).post(
        f"{API}/org/diagnostics", json={"student_id": str(student.id), "class_level": 9, "diagnostic_type": "recheck"}
    )
    assert r.status_code == 201, r.text


# ---- staff views ----------------------------------------------------------------------------------------


def test_staff_listings_show_cancelled_and_the_counters_do_not_count_it(pack):
    admin = make_client(pack, user=pack.admin)
    stats_before = admin.get(f"{API}/org/stats").json()
    archive(pack, pack.student)
    stats = admin.get(f"{API}/org/stats").json()
    # pending + confirmed + in_progress counted before; only in_progress is «active» now
    assert stats_before["active_diagnostics"] - stats["active_diagnostics"] == 2

    listing = admin.get(f"{API}/org/diagnostics", params={"status": "cancelled"}).json()
    assert {item["id"] for item in listing} == {str(pack.invites["pending"].id), str(pack.invites["confirmed"].id)}
    assert all(item["status"] == "cancelled" and item["cancel_reason"] == "student_archived" and item["cancelled_at"]
               for item in listing)
    everything = admin.get(f"{API}/org/diagnostics", params={"limit": 100}).json()
    assert {i["status"] for i in everything if i["student_id"] == str(pack.student.id)} >= {"cancelled", "in_progress"}

    detail = admin.get(f"{API}/org/diagnostics/{pack.invites['pending'].id}").json()
    assert detail["status"] == "cancelled" and detail["cancel_reason"] == "student_archived"
    profile = admin.get(f"{STUDENTS}/{pack.student.id}/profile").json()
    by_status = {d["status"]: d for d in profile["diagnostics"]}
    assert by_status["cancelled"]["cancelled_at"] and by_status["cancelled"]["cancel_reason"] == "student_archived"
    assert by_status["completed"]["cancelled_at"] is None


# ---- races ----------------------------------------------------------------------------------------------


def _own_session(w):
    return sessionmaker(w.db.get_bind())()


def test_an_invite_that_starts_during_the_archiving_is_left_in_progress(pack):
    """start (FOR UPDATE on the invite) wins the row: the archiving's UPDATE re-reads it and skips it."""
    confirmed = pack.invites["confirmed"]
    code = confirmed.code
    starter = _own_session(pack)
    try:
        started = starter.query(DiagnosticInvite).filter_by(code=code).with_for_update().one()
        started.status = "in_progress"
        started.cat_session_id = uuid4()
        results = {}

        def run():
            session = _own_session(pack)
            try:
                from src.auth.student_archive import StudentArchiveService

                results["body"] = StudentArchiveService(session).archive(
                    str(pack.org.id), str(pack.student.id), actor_id=str(pack.admin.id), actor_role="admin"
                )
            finally:
                session.close()

        thread = Thread(target=run)
        thread.start()
        thread.join(timeout=1.0)
        assert thread.is_alive(), "the archiving must wait for the row lock held by the start"
        starter.commit()
        thread.join(timeout=15)
        assert not thread.is_alive()
    finally:
        starter.close()
    body = results["body"]
    assert body.diagnostic_invites_cancelled == 1  # only the pending one
    assert body.diagnostic_invites_in_progress == 2  # the one that just started and the one that already ran
    assert reload(pack, confirmed).status == "in_progress"
    assert reload(pack, pack.invites["pending"]).status == "cancelled"


def test_a_confirm_that_waits_for_the_archiving_does_not_overwrite_the_cancellation(pack):
    pending = pack.invites["pending"]
    code = pending.code
    archiver = _own_session(pack)
    try:
        # the archiving's UPDATE holds the row lock (as inside its transaction) ...
        archiver.query(DiagnosticInvite).filter_by(code=code).update(
            {"status": "cancelled", "cancel_reason": "student_archived", "cancelled_at": datetime.utcnow()}
        )
        results = {}

        def run():
            session = _own_session(pack)
            try:
                try:
                    results["confirm"] = AuthService(session).confirm_diagnostic(code)
                except ValueError as exc:
                    results["error"] = str(exc)
            finally:
                session.close()

        thread = Thread(target=run)
        thread.start()
        thread.join(timeout=1.0)
        assert thread.is_alive(), "the confirm must wait for the row lock"
        archiver.commit()  # ... and the confirm then finds a cancelled invite
        thread.join(timeout=15)
    finally:
        archiver.close()
    assert results.get("error") == CANCELLED and "confirm" not in results
    assert reload(pack, pending).status == "cancelled"


def test_creating_a_link_waits_for_a_running_archiving_and_is_then_refused(world):
    student = world.students[1]
    archiver = _own_session(world)
    try:
        archiver.query(Student).filter_by(id=student.id).with_for_update(key_share=True).one().status = "archived"
        results = {}

        def run():
            session = _own_session(world)
            try:
                from src.auth.schemas import DiagnosticCreateRequest

                try:
                    results["created"] = AuthService(session).create_diagnostic_invite(
                        str(world.org.id), str(world.admin.id),
                        DiagnosticCreateRequest(student_id=str(student.id), class_level=9, diagnostic_type="initial"),
                    )
                except ValueError as exc:
                    results["error"] = str(exc)
            finally:
                session.close()

        thread = Thread(target=run)
        thread.start()
        thread.join(timeout=1.0)
        assert thread.is_alive(), "a new link must wait for the archiving of the student"
        archiver.commit()
        thread.join(timeout=15)
    finally:
        archiver.close()
    assert results.get("error") == ARCHIVED_CREATE and "created" not in results
