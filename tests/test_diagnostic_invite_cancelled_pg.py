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


# ---- «начатую диагностику дать закончить»: a session that started BEFORE the cancellation -----------------
#
# The invite can still say ``confirmed`` after the session really started: the start callback of diagnostic-service
# was late (or lost).  If the student is archived in that gap the invite is cancelled - and the callbacks of the
# session that predates the archiving must still reach it (start -> in_progress, complete -> completed).


def naive_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc(moment):
    return moment.replace(tzinfo=timezone.utc)


def start_body(session, started_at):
    body = {"session_id": str(session)}
    if started_at is not None:
        body["session_started_at"] = started_at.isoformat()
    return body


def complete_body(session, started_at, **extra):
    body = {"session_id": str(session), "final_theta": 0.4, "se": 0.3, "total_items": 5, "correct_answers": 3, **extra}
    if started_at is not None:
        body["session_started_at"] = started_at.isoformat()
    return body


def cancelled_confirmed(pack, *, cancelled_minutes_ago=None):
    """The ``confirmed`` invite after the archiving; ``cancelled_minutes_ago`` moves the cancellation back in time."""
    archive(pack, pack.student)
    row = reload(pack, pack.invites["confirmed"])
    assert row.status == "cancelled" and row.cancel_reason == "student_archived"
    if cancelled_minutes_ago is not None:
        row.cancelled_at = naive_now() - timedelta(minutes=cancelled_minutes_ago)
        pack.db.commit()
    return reload(pack, pack.invites["confirmed"])


def test_a_late_start_callback_of_a_session_that_predates_the_archiving_is_accepted(pack):
    session, started = uuid4(), naive_now() - timedelta(minutes=10)         # confirmed 30 minutes ago, started 10 ago
    row = cancelled_confirmed(pack)
    assert row.cancelled_at > started
    client = public_client(pack)
    r = client.post(f"{INTERNAL}/diagnostic/{row.code}/start", json=start_body(session, started), headers=token())
    assert r.status_code == 200, r.text
    assert r.json() == {"id": str(row.id), "status": "in_progress", "session_id": str(session)}
    row = reload(pack, row)
    assert row.status == "in_progress" and row.cat_session_id == session
    assert row.cancelled_at is None and row.cancel_reason is None            # un-cancelled
    assert row.revived_at is not None and abs((naive_now() - row.revived_at).total_seconds()) < 60   # the trail
    assert row.started_at == started                                         # the session's own start, not the callback's
    # a repeated callback of the same session is the usual idempotent one; the student stays archived
    r = client.post(f"{INTERNAL}/diagnostic/{row.code}/start", json=start_body(session, started), headers=token())
    assert r.status_code == 200 and reload(pack, row).status == "in_progress"
    assert pack.db.get(Student, pack.student.id).status == "archived"
    # the other cancelled link (was pending) is untouched
    assert reload(pack, pack.invites["pending"]).status == "cancelled"


def test_the_un_cancelling_is_logged(pack, caplog):
    session, started = uuid4(), naive_now() - timedelta(minutes=10)
    row = cancelled_confirmed(pack)
    with caplog.at_level("INFO", logger="src.auth.service"):
        public_client(pack).post(f"{INTERNAL}/diagnostic/{row.code}/start", json=start_body(session, started), headers=token())
    lines = [r.getMessage() for r in caplog.records if "un_cancelled=session_predates_archive" in r.getMessage()]
    assert len(lines) == 1 and f"invite_id={row.id}" in lines[0] and "status=in_progress" in lines[0]


def test_complete_after_the_archiving_for_a_session_that_started_before_is_accepted_and_attached(pack):
    session, started = uuid4(), naive_now() - timedelta(minutes=10)
    row = cancelled_confirmed(pack)
    client = public_client(pack)
    # the completion callback comes first (the start callback never arrived): it repairs the projection
    r = client.post(f"{INTERNAL}/diagnostic/{row.code}/complete", json=complete_body(session, started), headers=token())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed" and r.json()["student_id"] == str(pack.student.id)
    row = reload(pack, row)
    assert row.status == "completed" and row.cat_session_id == session and row.started_at == started
    assert row.cancelled_at is None and row.cancel_reason is None and row.revived_at is not None
    result = pack.db.query(StudentDiagnosticResult).filter_by(invite_id=row.id).one()    # attached to the invite and the student
    assert result.student_id == pack.student.id and result.session_id == session
    assert pack.db.get(Student, pack.student.id).status == "archived"                    # finishing does not bring him back
    # idempotent like any completion
    r = client.post(f"{INTERNAL}/diagnostic/{row.code}/complete", json=complete_body(session, started), headers=token())
    assert r.status_code == 200 and pack.db.query(StudentDiagnosticResult).filter_by(invite_id=row.id).count() == 1


def test_the_sequence_of_diagnostic_service_start_then_complete_after_the_archiving(pack):
    """notify_complete repairs a lost start callback by sending the start first (with the session's start), then the result."""
    session, started = uuid4(), naive_now() - timedelta(minutes=10)
    row = cancelled_confirmed(pack)
    client = public_client(pack)
    assert client.post(f"{INTERNAL}/diagnostic/{row.code}/start", json=start_body(session, started), headers=token()).status_code == 200
    r = client.post(f"{INTERNAL}/diagnostic/{row.code}/complete", json=complete_body(session, started), headers=token())
    assert r.status_code == 200 and reload(pack, row).status == "completed"
    assert pack.db.query(StudentDiagnosticResult).filter_by(invite_id=row.id).count() == 1


@pytest.mark.parametrize("how", ["after", "at"])
def test_a_session_that_started_at_or_after_the_cancellation_is_rejected(pack, how):
    session = uuid4()
    row = cancelled_confirmed(pack, cancelled_minutes_ago=20)               # cancelled 20 minutes ago ...
    started = row.cancelled_at + timedelta(minutes=10) if how == "after" else row.cancelled_at   # ... session after / exactly then
    assert row.confirmed_at < started < naive_now() + timedelta(seconds=1)
    client = public_client(pack)
    for path, body in (
        ("start", start_body(session, started)),
        ("complete", complete_body(session, started)),
    ):
        r = client.post(f"{INTERNAL}/diagnostic/{row.code}/{path}", json=body, headers=token())
        assert r.status_code == 400 and r.json()["detail"] == CANCELLED, (path, r.text)
    row = reload(pack, row)
    assert row.status == "cancelled" and row.cancelled_at is not None and row.cancel_reason == "student_archived"
    assert row.cat_session_id is None and row.revived_at is None
    assert pack.db.query(StudentDiagnosticResult).filter_by(invite_id=row.id).count() == 0


def test_a_session_that_started_just_before_the_cancellation_is_accepted(pack):
    session = uuid4()
    row = cancelled_confirmed(pack, cancelled_minutes_ago=20)
    started = row.cancelled_at - timedelta(seconds=1)
    r = public_client(pack).post(f"{INTERNAL}/diagnostic/{row.code}/start", json=start_body(session, started), headers=token())
    assert r.status_code == 200 and reload(pack, row).status == "in_progress"


@pytest.mark.parametrize("case", ["no_start_time", "before_confirmation", "in_the_future", "other_reason", "never_confirmed", "other_session"])
def test_an_unprovable_start_never_revives_a_cancelled_invite(pack, case):
    session = uuid4()
    row = cancelled_confirmed(pack)
    started = naive_now() - timedelta(minutes=10)
    if case == "no_start_time":
        started = None
    elif case == "before_confirmation":
        started = row.confirmed_at - timedelta(minutes=5)
    elif case == "in_the_future":
        started = naive_now() + timedelta(hours=1)
    elif case == "other_reason":
        row.cancel_reason = "something_else"
        pack.db.commit()
    elif case == "never_confirmed":
        row = reload(pack, pack.invites["pending"])                           # cancelled while still pending: no confirmed_at
    elif case == "other_session":
        row.cat_session_id = uuid4()
        pack.db.commit()
    client = public_client(pack)
    code = row.code
    for path, body in (("start", start_body(session, started)), ("complete", complete_body(session, started))):
        r = client.post(f"{INTERNAL}/diagnostic/{code}/{path}", json=body, headers=token())
        assert r.status_code == 400, (case, path, r.text)
        if case != "other_session":
            assert r.json()["detail"] == CANCELLED
    assert reload(pack, row).status == "cancelled"
    assert pack.db.query(StudentDiagnosticResult).filter_by(invite_id=row.id).count() == 0


def test_the_public_lookup_of_a_cancelled_code_does_not_say_why_but_the_internal_one_does(pack):
    """That the student was archived is not for whoever holds the link; diagnostic-service (the token) needs it."""
    row = cancelled_confirmed(pack)
    client = public_client(pack)
    for headers in ({}, {"X-Internal-Service-Token": "wrong"}, {"X-Internal-Service-Token": ""}):
        info = client.get(f"{API}/diagnostic/{row.code}", headers=headers).json()
        assert info["status"] == "cancelled" and info["message"] == CANCELLED
        assert "cancelled_at" not in info and "cancel_reason" not in info and "student_archived" not in repr(info)
    info = client.get(f"{API}/diagnostic/{row.code}", headers=token()).json()
    assert info["status"] == "cancelled" and info["message"] == CANCELLED
    assert info["cancel_reason"] == "student_archived" and info["cancelled_at"] == row.cancelled_at.isoformat()
    # a code that is not cancelled never has them, token or not
    other = client.get(f"{API}/diagnostic/{pack.invites['in_progress'].code}", headers=token()).json()
    assert "cancelled_at" not in other and "cancel_reason" not in other and "message" not in other


def test_without_a_configured_secret_nobody_is_internal(pack, monkeypatch):
    row = cancelled_confirmed(pack)
    monkeypatch.setattr(get_settings(), "internal_service_token", "")
    info = public_client(pack).get(f"{API}/diagnostic/{row.code}", headers={"X-Internal-Service-Token": ""}).json()
    assert "cancelled_at" not in info and "cancel_reason" not in info


def test_in_progress_invites_are_never_cancelled_and_may_finish(world):
    student = world.students[2]
    running = invite(world, student, "in_progress")
    body = archive(world, student)
    assert body["diagnostic_invites_cancelled"] == 0 and body["diagnostic_invites_in_progress"] == 1
    row = reload(world, running)
    assert row.status == "in_progress" and row.cancelled_at is None and row.cancel_reason is None
    r = public_client(world).post(
        f"{INTERNAL}/diagnostic/{row.code}/complete", json=complete_body(row.cat_session_id, None), headers=token()
    )
    assert r.status_code == 200 and reload(world, row).status == "completed"


# ---- the trail of the cancelling and of the un-cancelling -----------------------------------------------------


def test_the_archive_journal_lists_the_cancelled_and_the_running_invites(pack):
    from src.models.auth import GroupAudit

    archive(pack, pack.student)
    pack.db.expire_all()
    rows = pack.db.query(GroupAudit).filter_by(action="student_archived", org_id=pack.org.id).all()
    mine = [r for r in rows if r.snapshot["student_id"] == str(pack.student.id)]
    assert mine, "one journal row per closed group"
    cancelled = sorted([str(pack.invites["pending"].id), str(pack.invites["confirmed"].id)])
    for row in mine:
        assert row.snapshot["diagnostic_invites_cancelled"] == cancelled
        assert row.snapshot["diagnostic_invites_in_progress"] == [str(pack.invites["in_progress"].id)]


def test_a_student_without_invites_has_empty_lists_in_the_journal(world):
    from src.models.auth import GroupAudit

    archive(world, world.students[1])
    world.db.expire_all()
    row = world.db.query(GroupAudit).filter_by(action="student_archived", org_id=world.org.id).one()
    assert row.snapshot["diagnostic_invites_cancelled"] == [] and row.snapshot["diagnostic_invites_in_progress"] == []


def test_revived_at_is_shown_to_staff_next_to_cancelled_at(pack):
    admin = make_client(pack, user=pack.admin)
    session, started = uuid4(), naive_now() - timedelta(minutes=10)
    row = cancelled_confirmed(pack)
    public_client(pack).post(f"{INTERNAL}/diagnostic/{row.code}/start", json=start_body(session, started), headers=token())
    revived_iso = reload(pack, row).revived_at.isoformat()

    listing = {i["id"]: i for i in admin.get(f"{API}/org/diagnostics", params={"limit": 100}).json()}
    assert listing[str(row.id)]["revived_at"] == revived_iso and listing[str(row.id)]["cancelled_at"] is None
    assert listing[str(pack.invites["pending"].id)]["revived_at"] is None and listing[str(pack.invites["pending"].id)]["cancelled_at"]
    detail = admin.get(f"{API}/org/diagnostics/{row.id}").json()
    assert detail["revived_at"] == revived_iso and detail["status"] == "in_progress"
    profile = {d["invite_id"]: d for d in admin.get(f"{STUDENTS}/{pack.student.id}/profile").json()["diagnostics"]}
    assert profile[str(row.id)]["revived_at"] == revived_iso
    assert profile[str(pack.invites["completed"].id)]["revived_at"] is None


# ---- one instant, taken after the locks -----------------------------------------------------------------------


def test_the_archiving_instant_is_taken_after_the_locks_and_is_the_same_everywhere(pack):
    """A session the engine creates while the archiving waits for a lock started BEFORE the cancellation."""
    from src.models.auth import StudentGroup, StudentGroupPeriod

    holder = _own_session(pack)
    try:
        holder.query(type(pack.group)).filter_by(id=pack.group.id).with_for_update().one()   # a lock the archiving needs
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
        assert thread.is_alive(), "the archiving must wait for the group lock"
        session_created = naive_now()                      # a session starts during the wait
        holder.commit()
        thread.join(timeout=15)
        assert not thread.is_alive()
    finally:
        holder.close()
    pack.db.expire_all()
    student = pack.db.get(Student, pack.student.id)
    cancelled = reload(pack, pack.invites["confirmed"])
    assert cancelled.status == "cancelled"
    assert cancelled.cancelled_at > session_created, "the instant is taken after the wait, not before it"
    # ONE instant: the stamp, the closed membership and period, the journal row
    member = pack.db.query(StudentGroup).filter_by(student_id=student.id).one()
    stay = pack.db.query(StudentGroupPeriod).filter_by(membership_id=member.id).one()
    from src.models.auth import GroupAudit

    audit = pack.db.query(GroupAudit).filter_by(action="student_archived").all()
    audit = [a for a in audit if a.snapshot["student_id"] == str(student.id)]
    assert student.archived_at == cancelled.cancelled_at == member.left_at == stay.left_at == audit[0].created_at
    assert audit[0].snapshot["archived_at"] == student.archived_at.isoformat()
    # and so the callbacks of the session created during the wait are accepted
    session_id = uuid4()
    r = public_client(pack).post(
        f"{INTERNAL}/diagnostic/{cancelled.code}/start", json=start_body(session_id, session_created), headers=token()
    )
    assert r.status_code == 200 and reload(pack, cancelled).status == "in_progress"


# ---- lock order: student -> invite everywhere -----------------------------------------------------------------


def test_complete_takes_the_student_before_the_invite_so_it_cannot_deadlock_with_an_archiving(pack):
    """The archiving holds the student and then updates the invite rows.  A completion that locked the invite first
    and then wanted the student (a 'new' student becomes 'diagnosed') would wait for the archiving while the
    archiving waits for the invite: a deadlock.  Now it waits for the student BEFORE it holds the invite."""
    student = pack.student
    assert student.status == "new"
    confirmed = pack.invites["confirmed"]
    code, session_id, started = confirmed.code, uuid4(), naive_now() - timedelta(minutes=10)
    archiver = _own_session(pack)
    try:
        # the archiving's first lock ...
        archiver.query(Student).filter_by(id=student.id).with_for_update(key_share=True).one()
        results = {}

        def run():
            session = _own_session(pack)
            try:
                try:
                    results["done"] = AuthService(session).complete_diagnostic(
                        code, {"session_id": str(session_id), "session_started_at": started.isoformat(),
                               "final_theta": 0.4, "se": 0.3, "total_items": 5, "correct_answers": 3},
                    )
                except Exception as exc:  # noqa: BLE001
                    results["error"] = repr(exc)
            finally:
                session.close()

        thread = Thread(target=run)
        thread.start()
        thread.join(timeout=1.0)
        assert thread.is_alive(), "the completion must wait for the student lock"
        # ... then its UPDATE of the invites, which must NOT wait for the completion (it holds no invite lock yet)
        archiver.query(DiagnosticInvite).filter(
            DiagnosticInvite.student_id == student.id, DiagnosticInvite.status.in_(["pending", "confirmed"])
        ).update({"status": "cancelled", "cancel_reason": "student_archived", "cancelled_at": naive_now()}, synchronize_session=False)
        archiver.query(Student).filter_by(id=student.id).update({"status": "archived"})
        archiver.commit()
        thread.join(timeout=15)
        assert not thread.is_alive()
    finally:
        archiver.close()
    assert "error" not in results, results.get("error")       # no deadlock (PostgreSQL would have aborted one of them)
    row = reload(pack, confirmed)
    assert results["done"]["status"] == "completed" and row.status == "completed" and row.revived_at is not None
    assert pack.db.get(Student, student.id).status == "archived"   # the archiving's status survived the completion
