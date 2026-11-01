"""DM-036: the diagnostic completion callback is replayed by the sender's outbox, so it must be idempotent and
tolerant (disposable PostgreSQL, A2_AUTH_TEST_DATABASE_URL).

Time never comes from the real clock here: ``apply_skill_states`` takes ``now`` and the HTTP path is checked only for
what does not depend on it.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.database import get_db
from src.api.internal_router import router as internal_router
from src.auth import diagnostic_skill_states as skills
from src.auth.diagnostic_skill_states import apply_skill_states, mastery_for, status_for
from src.core.config import get_settings
from src.models.analytics import StudentSkillState
from src.models.auth import StudentDiagnosticResult
from tests.test_diagnostic_completion_lifecycle import DB_URL, scenario  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(not DB_URL, reason="Requires disposable A2 auth PostgreSQL")
NOW = datetime(2026, 10, 1, 12, 0)
LATER = datetime(2026, 10, 1, 12, 5)
STARTED_1 = datetime(2026, 9, 28, 9, 0)       # diagnostic No. 1 of a student
STARTED_2 = datetime(2026, 10, 2, 9, 0)       # diagnostic No. 2, a new link after No. 1 was lost for hours


def states(db, student_id):
    db.expire_all()
    return {s.skill_id: s for s in db.query(StudentSkillState).filter_by(student_id=student_id)}


def test_status_and_mastery_keep_the_thresholds_of_the_previous_implementation():
    assert [status_for(a, False) for a in (0.4, 0.39, -0.2, -0.21, -0.8, -0.81)] == \
        ["mastered", "learning", "learning", "struggling", "struggling", "gap"]
    assert status_for(1.0, True) == "gap"                                     # confirmed by the engine: gap wins
    assert (mastery_for(0.0), mastery_for(2.0), mastery_for(-9.0), mastery_for(9.0)) == (0.5, 1.0, 0.0, 1.0)


def test_replay_of_the_same_payload_changes_nothing_not_even_the_trend(scenario):
    db, _factory, invite = scenario
    sid, org = invite.student_id, invite.org_id
    db.add(StudentSkillState(student_id=sid, org_id=org, skill_id="T1", mastery_probability=0.3, status="learning",
                             trend="stable", theta=0.0, last_attempt_at=NOW, updated_at=NOW))
    db.commit()
    breakdown = {"T1": {"alpha": 0.8}, "T2": {"alpha": -1.0}}
    first = apply_skill_states(db, student_id=str(sid), org_id=str(org), theta=0.4, topic_breakdown=breakdown, gaps=set(), now=LATER)
    assert first == {"written": 2, "unchanged": 0, "skipped": 0, "stale": 0}
    before = {k: (v.trend, v.status, float(v.mastery_probability), v.last_attempt_at) for k, v in states(db, sid).items()}
    assert before["T1"][:2] == ("improving", "mastered") and before["T2"][:2] == ("stable", "gap")
    replay = apply_skill_states(db, student_id=str(sid), org_id=str(org), theta=0.4, topic_breakdown=breakdown, gaps=set(),
                                now=datetime(2026, 10, 1, 13, 0))
    assert replay == {"written": 0, "unchanged": 2, "skipped": 0, "stale": 0}
    after = {k: (v.trend, v.status, float(v.mastery_probability), v.last_attempt_at) for k, v in states(db, sid).items()}
    assert after == before                                                    # "improving" is still "improving"
    assert db.query(StudentSkillState).filter_by(student_id=sid).count() == 2


def test_a_new_result_for_a_known_topic_still_moves_the_trend(scenario):
    db, _factory, invite = scenario
    sid, org = invite.student_id, invite.org_id
    apply_skill_states(db, student_id=str(sid), org_id=str(org), theta=0.1, topic_breakdown={"T1": {"alpha": 0.8}}, gaps=set(), now=NOW)
    apply_skill_states(db, student_id=str(sid), org_id=str(org), theta=0.2, topic_breakdown={"T1": {"alpha": -0.8}}, gaps=set(), now=LATER)
    row = states(db, sid)["T1"]
    assert (row.trend, row.status, float(row.mastery_probability), row.last_attempt_at) == ("declining", "struggling", 0.3, LATER)


def test_an_old_callback_replayed_after_a_newer_diagnostic_does_not_roll_the_states_back(scenario):
    db, _factory, invite = scenario
    sid, org = invite.student_id, invite.org_id
    first = {"T1": {"alpha": -1.0}, "T2": {"alpha": 0.9}}
    second = {"T1": {"alpha": 0.9}, "T2": {"alpha": -1.0}}
    kw = dict(student_id=str(sid), org_id=str(org), gaps=set())
    # No. 2 was applied (it was delivered first); No. 1 is the requeued callback that finally reaches auth-service
    assert apply_skill_states(db, theta=0.8, topic_breakdown=second, result_at=STARTED_2, now=LATER, **kw)["written"] == 2
    after_second = {k: (v.status, float(v.mastery_probability), v.trend, v.last_attempt_at) for k, v in states(db, sid).items()}
    late = apply_skill_states(db, theta=-0.5, topic_breakdown=first, result_at=STARTED_1, now=datetime(2026, 10, 3, 9, 0), **kw)
    assert late == {"written": 0, "unchanged": 0, "skipped": 0, "stale": 2}
    assert {k: (v.status, float(v.mastery_probability), v.trend, v.last_attempt_at) for k, v in states(db, sid).items()} == after_second
    assert after_second["T1"][3] == STARTED_2                                  # the time of the result, not of the delivery
    # the SAME result delivered again is a plain replay
    assert apply_skill_states(db, theta=0.8, topic_breakdown=second, result_at=STARTED_2, now=datetime(2026, 10, 4, 9, 0), **kw)["unchanged"] == 2


def test_in_order_results_still_apply_and_an_equal_time_with_other_values_is_not_stale(scenario):
    db, _factory, invite = scenario
    kw = dict(student_id=str(invite.student_id), org_id=None, gaps=set(), now=LATER)
    apply_skill_states(db, theta=0.1, topic_breakdown={"T1": {"alpha": -1.0}}, result_at=STARTED_1, **kw)
    assert apply_skill_states(db, theta=0.2, topic_breakdown={"T1": {"alpha": 0.9}}, result_at=STARTED_2, **kw)["written"] == 1
    assert apply_skill_states(db, theta=0.3, topic_breakdown={"T1": {"alpha": 0.1}}, result_at=STARTED_2, **kw)["written"] == 1
    assert states(db, invite.student_id)["T1"].last_attempt_at == STARTED_2


def test_aware_result_time_is_stored_as_naive_utc():
    from src.auth.diagnostic_skill_states import naive_utc
    assert naive_utc(datetime(2026, 10, 1, 10, 0, tzinfo=timezone(timedelta(hours=5)))) == datetime(2026, 10, 1, 5, 0)
    assert naive_utc(datetime(2026, 10, 1, 5, 0)) == datetime(2026, 10, 1, 5, 0) and naive_utc(None) is None


def test_malformed_topics_are_skipped_the_rest_is_written(scenario):
    db, _factory, invite = scenario
    sid, org = invite.student_id, invite.org_id
    breakdown = {
        "OK": {"alpha": 0.5},
        "NO_ALPHA": {"alpha": None},                 # DM-036: used to be a TypeError -> 500
        "NOT_A_DICT": 5,                             # used to be an AttributeError -> 500
        "TEXT_ALPHA": {"alpha": "high"},
        "BOOL_ALPHA": {"alpha": True},
        "INF_ALPHA": {"alpha": math.inf},
        "X" * 31: {"alpha": 0.1},                    # does not fit String(30)
        "EMPTY_DICT": {},
    }
    result = apply_skill_states(db, student_id=str(sid), org_id=str(org), theta=0.0, topic_breakdown=breakdown, gaps={"OK"}, now=NOW)
    assert result == {"written": 3, "unchanged": 0, "skipped": 5, "stale": 0}
    rows = states(db, sid)
    assert set(rows) == {"OK", "NO_ALPHA", "EMPTY_DICT"}
    assert rows["OK"].status == "gap" and rows["NO_ALPHA"].status == "learning" and float(rows["NO_ALPHA"].mastery_probability) == 0.5


@pytest.mark.parametrize("theta,stored", [(None, None), (1e9, None), (math.inf, None), (-3.123456, -3.1235), (0.5, 0.5)])
def test_theta_that_does_not_fit_the_column_is_dropped_not_a_database_error(scenario, theta, stored):
    db, _factory, invite = scenario
    apply_skill_states(db, student_id=str(invite.student_id), org_id=None, theta=theta,
                       topic_breakdown={"T1": {"alpha": 0.1}}, gaps=set(), now=NOW)
    row = states(db, invite.student_id)["T1"]
    assert (None if row.theta is None else float(row.theta)) == stored


def test_times_are_naive_utc_like_every_other_column(scenario):
    db, _factory, invite = scenario
    apply_skill_states(db, student_id=str(invite.student_id), org_id=None, theta=0.0, topic_breakdown={"T1": {"alpha": 0.1}}, gaps=set())
    row = states(db, invite.student_id)["T1"]
    assert row.last_attempt_at.tzinfo is None and row.updated_at.tzinfo is None
    assert skills._utcnow().tzinfo is None


def test_breakdown_that_is_not_a_mapping_is_ignored(scenario):
    db, _factory, invite = scenario
    assert apply_skill_states(db, student_id=str(invite.student_id), org_id=None, theta=0.0, topic_breakdown=["T1"], gaps=set()) == \
        {"written": 0, "unchanged": 0, "skipped": 0, "stale": 0}


# ── over HTTP: a failure after the result was committed is repaired by the sender's retry ─────────────────────────

def callback(client, invite, **extra):
    body = {"session_id": str(invite.cat_session_id), "final_theta": 0.4, "se": 0.3, "total_items": 3, "correct_answers": 2,
            **extra}
    return client.post(f"/api/v1/internal/diagnostic/{invite.code}/complete", json=body,
                       headers={"X-Internal-Service-Token": get_settings().internal_service_token})


@pytest.fixture
def http(scenario):
    db, factory, invite = scenario
    app = FastAPI()
    app.include_router(internal_router, prefix="/api/v1")

    def request_db():
        with factory() as callback_db:
            yield callback_db
    app.dependency_overrides[get_db] = request_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, db, invite


def test_malformed_breakdown_over_http_is_a_200_and_the_result_is_saved_once(http):
    client, db, invite = http
    response = callback(client, invite, topic_breakdown={"T1": {"alpha": None}, "T2": "x", "T3": {"alpha": 0.9}},
                        identified_gaps=["T3"])
    assert response.status_code == 200, response.text
    assert db.query(StudentDiagnosticResult).filter_by(invite_id=invite.id).count() == 1
    assert set(states(db, invite.student_id)) == {"T1", "T3"}
    again = callback(client, invite, topic_breakdown={"T1": {"alpha": None}, "T2": "x", "T3": {"alpha": 0.9}}, identified_gaps=["T3"])
    assert again.status_code == 200 and again.json() == response.json()
    assert db.query(StudentDiagnosticResult).filter_by(invite_id=invite.id).count() == 1


def test_a_crash_after_the_result_commit_is_finished_by_the_retry_without_a_second_result(http, monkeypatch):
    client, db, invite = http
    real = skills.apply_skill_states
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Synthetic database blip while writing skill states")
        return real(*args, **kwargs)
    from src.api import internal_router as module
    monkeypatch.setattr(module, "apply_skill_states", flaky)
    breakdown = {"T1": {"alpha": 0.9}}
    assert callback(client, invite, topic_breakdown=breakdown).status_code == 500
    assert db.query(StudentDiagnosticResult).filter_by(invite_id=invite.id).count() == 1     # committed, states are not
    assert states(db, invite.student_id) == {}
    retry = callback(client, invite, topic_breakdown=breakdown)                              # the sender's next attempt
    assert retry.status_code == 200, retry.text
    assert set(states(db, invite.student_id)) == {"T1"}
    assert db.query(StudentDiagnosticResult).filter_by(invite_id=invite.id).count() == 1


def test_list_instead_of_mapping_is_a_validation_error_not_a_500(http):
    client, _db, invite = http
    assert callback(client, invite, topic_breakdown=["T1"]).status_code == 422


def test_old_callback_after_a_newer_diagnostic_over_http(http):
    """The callback of diagnostic No. 1 arrives after No. 2 already wrote the states of the student."""
    client, db, invite = http
    apply_skill_states(db, student_id=str(invite.student_id), org_id=str(invite.org_id), theta=0.9,
                       topic_breakdown={"T1": {"alpha": 0.9}}, gaps=set(), result_at=STARTED_2, now=LATER)
    response = callback(client, invite, topic_breakdown={"T1": {"alpha": -1.0}}, session_started_at="2026-09-28T14:00:00+05:00")
    assert response.status_code == 200, response.text                         # accepted: the result row is saved
    row = states(db, invite.student_id)["T1"]
    assert (row.status, float(row.mastery_probability), row.last_attempt_at) == ("mastered", 0.725, STARTED_2)
