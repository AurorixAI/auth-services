"""Optimistic concurrency for excuse decisions (PostgreSQL).

A decision affects payroll, so a stale screen must not silently overwrite what another administrator decided.  The
client may send ``expected_status`` (the status it SAW); if the excuse is in another status when the row is locked the
answer is 409 ``EXCUSE_CHANGED`` with the current item and NOTHING is written.  Without it the behaviour is as before.
"""
import pytest

from src.auth.attendance_excuses import CHANGED_MESSAGE
from src.models.auth import AttendanceExcuse, GroupAttendanceAudit

from tests.pg_world import (  # noqa: F401  (fixtures)
    excused_lesson, make_client, needs_pg, pg_session, save, world,
)
from tests.test_attendance_excuses import excuse_of
from tests.test_excuse_deadline import listing

pytestmark = needs_pg

BASE = "/api/v1/auth/org/attendance/excuses"


def post(w, path, body=None):
    return make_client(w).post(f"{BASE}/{path}", json=body) if body is not None else make_client(w).post(f"{BASE}/{path}")


def snapshot(w):
    """Everything a decision writes: the excuse row and the number of journal rows."""
    w.db.expire_all()
    row = excuse_of(w)
    audit = w.db.query(GroupAttendanceAudit).filter_by(org_id=w.org.id).count()
    return (row.status, row.reviewed_by, row.reviewed_at, row.review_note, row.decided_after_deadline,
            row.deadline_at_decision, audit)


@pytest.fixture
def excuse_id(world):
    excused_lesson(world, offset_days=3)
    return str(excuse_of(world).id)


def assert_changed(response, excuse_id, current):
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "EXCUSE_CHANGED" and detail["message"] == CHANGED_MESSAGE
    assert detail["item"]["id"] == excuse_id and detail["item"]["status"] == current   # the CURRENT item, to refresh the list


# ---- review ------------------------------------------------------------------------------------------


def test_a_review_on_an_excuse_another_administrator_decided_is_refused_and_changes_nothing(world, excuse_id):
    assert post(world, f"{excuse_id}/review", {"decision": "confirmed", "note": "Справка есть"}).status_code == 200
    before = snapshot(world)
    r = post(world, f"{excuse_id}/review", {"decision": "rejected", "note": "Не принята", "expected_status": "pending"})
    assert_changed(r, excuse_id, "confirmed")
    assert r.json()["detail"]["item"]["review_note"] == "Справка есть"
    assert snapshot(world) == before                                                   # no write, no journal row


def test_a_review_with_the_status_the_client_saw_goes_through(world, excuse_id):
    r = post(world, f"{excuse_id}/review", {"decision": "confirmed", "expected_status": "pending"})
    assert r.status_code == 200 and r.json()["status"] == "confirmed"
    # a deliberate re-review of a decided excuse: the client saw «confirmed»
    r = post(world, f"{excuse_id}/review", {"decision": "rejected", "expected_status": "confirmed"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert excuse_of(world).status == "rejected"


def test_a_review_without_expected_status_keeps_todays_behaviour(world, excuse_id):
    assert post(world, f"{excuse_id}/review", {"decision": "confirmed"}).status_code == 200
    r = post(world, f"{excuse_id}/review", {"decision": "rejected", "note": "Передумали"})   # overwrites silently, as before
    assert r.status_code == 200 and r.json()["status"] == "rejected" and excuse_of(world).review_note == "Передумали"


def test_the_stale_check_runs_before_anything_else_about_the_decision(world, excuse_id):
    post(world, f"{excuse_id}/reset")                                                  # still pending: matches
    assert post(world, f"{excuse_id}/review", {"decision": "confirmed", "expected_status": "pending"}).status_code == 200
    assert_changed(post(world, f"{excuse_id}/review", {"decision": "confirmed", "expected_status": "rejected"}),
                   excuse_id, "confirmed")


def test_an_unknown_expected_status_is_a_validation_error(world, excuse_id):
    for bad in ("nope", "", "CONFIRMED"):
        assert post(world, f"{excuse_id}/review", {"decision": "confirmed", "expected_status": bad}).status_code == 422, bad
        assert post(world, f"{excuse_id}/reset", {"expected_status": bad}).status_code == 422, bad
    assert excuse_of(world).status == "pending"


# ---- reset -------------------------------------------------------------------------------------------


def test_a_reset_of_an_excuse_that_changed_is_refused_and_changes_nothing(world, excuse_id):
    post(world, f"{excuse_id}/review", {"decision": "confirmed", "note": "Справка есть"})
    post(world, f"{excuse_id}/review", {"decision": "rejected", "note": "Нет, не принята"})   # the other administrator
    before = snapshot(world)
    assert_changed(post(world, f"{excuse_id}/reset", {"expected_status": "confirmed"}), excuse_id, "rejected")
    assert snapshot(world) == before
    # the client that saw the current status resets it
    r = post(world, f"{excuse_id}/reset", {"expected_status": "rejected"})
    assert r.status_code == 200 and r.json()["status"] == "pending" and excuse_of(world).status == "pending"


def test_a_reset_after_someone_else_already_reset_is_refused(world, excuse_id):
    post(world, f"{excuse_id}/review", {"decision": "confirmed"})
    assert post(world, f"{excuse_id}/reset", {"expected_status": "confirmed"}).status_code == 200
    assert_changed(post(world, f"{excuse_id}/reset", {"expected_status": "confirmed"}), excuse_id, "pending")


def test_a_reset_without_a_body_or_without_expected_status_keeps_todays_behaviour(world, excuse_id):
    post(world, f"{excuse_id}/review", {"decision": "confirmed"})
    assert post(world, f"{excuse_id}/reset").json()["status"] == "pending"                        # no body at all
    post(world, f"{excuse_id}/review", {"decision": "rejected"})
    assert post(world, f"{excuse_id}/reset", {}).json()["status"] == "pending"                    # an empty one
    assert post(world, f"{excuse_id}/reset").json()["status"] == "pending"                        # a reset of a pending one: no-op


# ---- bulk --------------------------------------------------------------------------------------------


def test_bulk_reports_the_excuses_that_changed_per_item_and_decides_the_rest(world):
    s0, s1, s2 = world.students
    from tests.pg_world import reconcile

    day, lesson_id = reconcile(world, 3)
    save(world, day, lesson_id, [(s0, "excused"), (s1, "excused"), (s2, "excused")], status="in_progress",
         excuseReasons={s0.id: "Болел", s1.id: "Болел", s2.id: "Болел"})
    world.db.expire_all()
    by_student = {str(e.student_id): str(e.id) for e in world.db.query(AttendanceExcuse).filter_by(org_id=world.org.id)}
    a, b, c = (by_student[str(s.id)] for s in (s0, s1, s2))
    post(world, f"{b}/review", {"decision": "rejected", "note": "Нет"})                          # decided by someone else
    r = post(world, "review-bulk", {"ids": [a, b, c], "decision": "confirmed", "expected_status": "pending"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updated"] == 2
    assert body["skipped"] == [{"id": b, "reason": CHANGED_MESSAGE, "code": "EXCUSE_CHANGED"}]
    world.db.expire_all()
    status = {str(e.id): e.status for e in world.db.query(AttendanceExcuse).filter_by(org_id=world.org.id)}
    assert status == {a: "confirmed", b: "rejected", c: "confirmed"}                               # b untouched
    assert excuse_of(world, s1).review_note == "Нет"


def test_bulk_without_expected_status_skips_a_decided_excuse_as_before(world):
    excused_lesson(world, offset_days=3)
    item_id = str(excuse_of(world).id)
    post(world, f"{item_id}/review", {"decision": "rejected"})
    body = post(world, "review-bulk", {"ids": [item_id], "decision": "confirmed"}).json()
    assert body["updated"] == 0 and len(body["skipped"]) == 1
    assert body["skipped"][0]["id"] == item_id and body["skipped"][0]["code"] is None and body["skipped"][0]["reason"]
    assert excuse_of(world).status == "rejected"


# ---- the check is made UNDER the row lock ------------------------------------------------------------


def test_a_decision_waiting_for_the_row_lock_sees_what_the_other_administrator_committed(world):
    """Two real sessions: the stale review blocks on the row, the other administrator commits, the review is refused."""
    import threading
    import time

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from src.core.database import get_db
    from src.core.db_timezone import utc_connect_args
    from tests.pg_world import DB_URL

    excused_lesson(world, offset_days=3)
    item_id = str(excuse_of(world).id)
    engine = create_engine(DB_URL, connect_args=utc_connect_args(DB_URL))
    factory = sessionmaker(engine)

    def own_session():                                  # the request gets a session of its own, like in production
        session = factory()
        try:
            yield session
        finally:
            session.close()

    client = make_client(world)
    client.app.dependency_overrides[get_db] = own_session
    answer = {}
    try:
        # the "other administrator" holds the row ...
        world.db.execute(text("SELECT id FROM attendance_excuses WHERE id = :i FOR UPDATE"), {"i": item_id})
        worker = threading.Thread(target=lambda: answer.update(
            r=client.post(f"{BASE}/{item_id}/review", json={"decision": "rejected", "expected_status": "pending"})))
        worker.start()
        for _ in range(100):                            # ... until the stale review is really waiting for it
            world.db.execute(text("SELECT pg_stat_clear_snapshot()"))      # the activity view is cached per transaction
            waiting = world.db.execute(text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'")).scalar()
            if waiting:
                break
            time.sleep(0.05)
        assert waiting and worker.is_alive()
        world.db.execute(text("UPDATE attendance_excuses SET status = 'confirmed' WHERE id = :i"), {"i": item_id})
        world.db.commit()                               # ... and decides first
        worker.join(10)
        assert not worker.is_alive()
    finally:
        world.db.rollback()
        engine.dispose()
    assert_changed(answer["r"], item_id, "confirmed")
    assert excuse_of(world).status == "confirmed" and excuse_of(world).reviewed_by is None
    assert not [a for a in world.db.query(GroupAttendanceAudit).filter_by(org_id=world.org.id, action="excuse_reviewed")]
