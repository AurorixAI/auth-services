"""LESSON-RULES С11 п.2: every writer of ``organizations.settings`` reads the row under its lock FRESH (``populate_existing``): two writes at once - the rules of the lessons, the payment settings, the
divisor of the payroll, the state of the opening balances - lose nothing, in EVERY order.

How (real threads, each request on its OWN session and connection): a third connection holds ``SELECT ... FOR UPDATE`` of the row of the organisation; the first request is started and waits on the lock
(``pg_stat_activity``: waiting), then the second; the lock is released - the first writes and commits, the second wakes up with the row ITS session read BEFORE the first commit.  A writer that keeps that
(old) copy of the JSON writes the old JSON back over the new block: the first write is lost.
Hand: each write puts one value in its own block: lessons 14 days (``policy``), payments: the recalculation of the centre on (``recalc_history``), payroll divisor 26 (from the month 2026-10), openings state ``skipped``, one batch of the opening balances (``openings_batches``: 1).  After any pair,
in either order, both values are read back."""
import itertools
import threading
import time
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from src.models.auth import Organization

from tests.b2_helpers import API, build_d2, client, clock, needs_pg, pg_session  # noqa: F401  (fixtures)
from tests.ops_helpers import grant_money_right

pytestmark = needs_pg
ORG = f"{API}/org"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")
    world = build_d2(pg_session)
    grant_money_right(world)
    return world


def own_session_client(w, who="owner"):
    """The client of the test whose every request gets a NEW session (the default client of the tests shares the session of the test: no two requests at once)."""
    from src.core.database import get_db

    c = client(w, who)
    factory = sessionmaker(w.db.get_bind(), autoflush=False)

    def fresh():
        s = factory()
        try:
            # What the real request does first: the live-access check reads the ORGANISATION as an entity (``dependencies.py``: ``db.query(Organization)...first()``), so the row is already in the
            # session when the writer asks for it under the lock.  The test replaces that dependency by a ready payload, so it does the same read here.
            s.info["live_organisation"] = s.query(Organization).filter(Organization.id == w.org.id, Organization.status == "active").first()      # held, as the real request holds it
            yield s
        finally:
            s.close()

    c.app.dependency_overrides[get_db] = fresh
    return c


WRITERS = {
    "policy": lambda c, body: c.put(f"{ORG}/settings/lessons", json={"teacher_lesson_window_days": 14}),
    "payments": lambda c, body: c.put(f"{ORG}/settings/payments", json={"centre_cancel_recalc": True}),
    "payroll": lambda c, body: c.put(f"{ORG}/payroll/settings", json={"divisor": 26, "valid_from": "2026-10"}),
    "openings": lambda c, body: c.post(f"{ORG}/opening-balances/state", json={"state": "skipped"}),
    "batch": lambda c, body: c.post(f"{ORG}/opening-balances/batch", json=body),          # the batch of the opening balances: the key of the answer goes in the same JSON
}


def effects(w):
    """What each write left in the JSON (read from the row, fresh)."""
    w.db.expire_all()
    s = w.db.get(Organization, w.org.id).settings or {}
    return {"policy": ((s.get("policy") or {}).get("teacher_lesson_window_days") or [{}])[-1].get("value"), "payments": ((s.get("payments") or {}).get("recalc_history") or [{}])[-1].get("on"),
            "payroll": (s.get("payroll") or {}).get("divisor"), "openings": (s.get("payments") or {}).get("openings_state"), "batch": len((s.get("payments") or {}).get("openings_batches") or [])}


WANT = {"policy": 14, "payments": True, "payroll": 26, "openings": "skipped", "batch": 1}


def _waiting(engine, n, threads=(), seconds=180):
    """Wait until ``n`` backends wait on a row lock (a real wait, not a sleep; the only row the test holds is the organisation, the text of a long query is cut by the server, so the query is not matched).
    LESSONS 190: the way out is an EVENT, not the clock: a writer that has FINISHED without reaching the lock (an early answer, an error) ends the wait at once and the answer says so; the time bound
    (180 s, was 15 s: under the load of the full suite a request that has not yet reached the lock was taken for a lost one) is only the last resort."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        with engine.connect() as conn:
            got = conn.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock' AND wait_event IN ('transactionid', 'tuple')")).scalar()
        if got >= n:
            return True
        if any(not t.is_alive() for t in threads):
            return False
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("first,second", [p for p in itertools.permutations(WRITERS, 2) if set(p) != {"openings", "batch"}])          # the state and the batch both write the state of the launch: not a pair of independent blocks
def test_two_writers_of_the_settings_at_once_lose_neither_in_either_order(w, first, second):
    run_pair(w, first, second)


def run_pair(w, first, second):
    """``first`` and ``second`` write at once (the first is at the lock before the second); both values are read back.  Also used by the test of the rules (C0)."""
    owner = client(w, "owner")          # the preview of the batch is made on the session of the test, before the threads: one membership, «оплачено до» the day before the start (nothing owed)
    rows = [m for g in owner.get(f"{ORG}/opening-balances").json()["groups"] for m in g["memberships"]]
    item = {"membership_id": rows[0]["membership_id"], "version": rows[0]["version"], "paid_until": "2026-10-20"}
    digest = owner.post(f"{ORG}/opening-balances/batch/preview", json={"items": [item]}).json()["digest"]
    body = {"items": [item], "idempotency_key": str(uuid.uuid4()), "digest": digest}
    w.db.commit()
    engine = w.db.get_bind()
    clients = [own_session_client(w) for _ in range(2)]
    holder = engine.connect()
    trans = holder.begin()
    holder.execute(text("SELECT id FROM organizations WHERE id = :o FOR UPDATE"), {"o": str(w.org.id)})
    answers = {}

    def run(name, c):
        answers[name] = WRITERS[name](c, body)

    threads = []
    try:
        for i, name in enumerate((first, second)):
            t = threading.Thread(target=run, args=(name, clients[i]), daemon=True)
            t.start()
            threads.append(t)
            assert _waiting(engine, i + 1, threads), f"{name} did not reach the lock of the organisation (answers so far: { {k: (v.status_code, v.text[:200]) for k, v in answers.items()} })"
    finally:
        trans.commit()
        holder.close()
        for t in threads:
            t.join(timeout=30)
    assert {k: v.status_code for k, v in answers.items()} == {first: 200, second: 200}, {k: v.text[:200] for k, v in answers.items()}
    got = effects(w)
    assert got[first] == WANT[first] and got[second] == WANT[second], (first, second, got)
