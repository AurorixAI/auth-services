"""One session per signed-in device (ROADMAP #101), against the disposable ``a2_auth`` PostgreSQL.

Defect: a user had ONE refresh token (``users.refresh_token_hash``) and every login / refresh overwrote it, so the
same person on two devices (the owner on phone + laptop, one reception account on two PCs) was signed out of the
first device at its next refresh (401 "Refresh token отозван"). Now every sign-in is its own row of
``user_sessions`` and a refresh rotates only the row it belongs to. The migration and its backfill are covered in
``test_postgres_migrations.py``; the cookie and throttle tests are untouched.
"""
import json
import logging
import threading
import time
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import sessionmaker

from src.auth import user_sessions
from src.auth.cookies import AUTH_COOKIE_NAME, REFRESH_COOKIE_NAME
from src.auth.schemas import LoginRequest
from src.auth.service import AuthService, create_refresh_token, hash_password, hash_token
from src.models.auth import OrganizationMember, User, UserSession, UserSessionToken

from tests.pg_world import needs_pg, pg_session, session_clock, world  # noqa: F401  (fixtures)
from tests.test_session_cookies import (  # noqa: F401  (session_world is a fixture)
    API,
    assert_cleared,
    live_hashes,
    PASSWORD,
    client_for,
    cookie_header,
    expired_access_token,
    session_world,
    set_cookies,
)

REFUSED = {"detail": "Refresh token отозван"}  # the answer to every refused refresh token, as before #101


# ---- a "device": its own cookie pair, as a browser would hold it ---------------------------------


class Device:
    def __init__(self, w, user=None, ip=None, agent=None):
        self.w = w
        self.user = user or w.owner
        self.client = client_for(w.db)
        self.headers = {k: v for k, v in (("x-real-ip", ip), ("user-agent", agent)) if v}
        self.access = self.refresh = None

    def _take(self, response):
        jar = set_cookies(response)
        self.access, self.refresh = jar[AUTH_COOKIE_NAME].value, jar[REFRESH_COOKIE_NAME].value

    def login(self, password=PASSWORD, remember=False):
        r = self.client.post(
            f"{API}/login", json={"email": self.user.email, "password": password, "remember_me": remember}, headers=self.headers
        )
        assert r.status_code == 200, r.text
        self._take(r)
        return self

    def refresh_with(self, token):
        return self.client.post(
            f"{API}/refresh", json={}, headers={**cookie_header(refresh=token), **self.headers}
        )

    def refresh_ok(self):
        r = self.refresh_with(self.refresh)
        assert r.status_code == 200, r.text
        old = self.refresh
        self._take(r)
        assert self.refresh != old  # rotated
        return r

    def status_of_refresh(self):
        return self.refresh_with(self.refresh).status_code

    def logout(self):
        r = self.client.post(
            f"{API}/logout", headers=cookie_header(access=self.access, refresh=self.refresh)
        )
        assert r.status_code == 200, r.text

    def logout_with(self, cookie_token=None, body=None, content_type="application/json"):
        """POST /logout with the given refresh cookie and/or request body; returns the response. A client with an
        empty cookie jar: the jar of ``self.client`` holds this device's own cookies and would be sent along."""
        client = client_for(self.w.db)
        headers = cookie_header(refresh=cookie_token) if cookie_token else {}
        if body is not None:
            headers = {**headers, "content-type": content_type}
            return client.post(f"{API}/logout", content=body, headers=headers)
        return client.post(f"{API}/logout", headers=headers)


def sessions(w, user=None):
    w.db.expire_all()
    return (
        w.db.query(UserSession)
        .filter(UserSession.user_id == (user or w.owner).id)
        .order_by(UserSession.created_at, UserSession.id)
        .all()
    )


def live(w, user=None):
    return [s for s in sessions(w, user) if s.revoked_at is None]


def session_of(w, token_hash):
    """The session a refresh token (given as its hash; live or superseded) belongs to."""
    w.db.expire_all()
    row = w.db.query(UserSessionToken).filter_by(token_hash=token_hash).one()
    return w.db.query(UserSession).filter_by(id=row.session_id).one()


def token_rows(w, session):
    w.db.expire_all()
    return w.db.query(UserSessionToken).filter_by(session_id=session.id).order_by(UserSessionToken.issued_at).all()


def forget_superseded_tokens(w):
    """The global sweep deletes the SWEEP_BATCH oldest candidates per sign-in, so superseded tokens that other tests (or
    an earlier run on the same database) left behind would take the batch before the tokens of the test at hand.
    A test of the sweep starts from none."""
    w.db.execute(delete(UserSessionToken).where(UserSessionToken.superseded_at.is_not(None)))
    w.db.commit()


def live_tokens(w, user=None):
    """Hashes of the LIVE tokens of the user's live sessions."""
    return live_hashes(w, user)


def parallel_refresh(w, tokens, monkeypatch, hold=0.7):
    """Refresh every token in *tokens* at (nearly) the same time, each in its own database session. The first
    request holds its row locks for *hold* seconds inside the rotation; the others start while it holds them.
    Returns (results, elapsed): a TokenPair or the exception per token."""
    engine = w.db.get_bind()
    entered, mutex, state = threading.Event(), threading.Lock(), {"first_taken": False}
    real_rotate = user_sessions.rotate

    def slow_rotate(*args, **kwargs):
        with mutex:
            first = not state["first_taken"]
            state["first_taken"] = True
        if first:
            entered.set()  # this transaction now holds the locks ...
            time.sleep(hold)  # ... for a while
        return real_rotate(*args, **kwargs)

    monkeypatch.setattr(user_sessions, "rotate", slow_rotate)
    results, elapsed = [None] * len(tokens), [0.0] * len(tokens)

    def run(i, wait_for_the_lock):
        if wait_for_the_lock:
            assert entered.wait(10)
        began = time.monotonic()
        with sessionmaker(engine)() as db:
            try:
                results[i] = AuthService(db).refresh_tokens(tokens[i])
            except Exception as exc:  # noqa: BLE001 - reported by the assertions of the caller
                results[i] = exc
        elapsed[i] = time.monotonic() - began

    threads = [threading.Thread(target=run, args=(i, i > 0)) for i in range(len(tokens))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    monkeypatch.setattr(user_sessions, "rotate", real_rotate)
    return results, elapsed


def enable_login(w, user):
    user.password_hash = hash_password(PASSWORD)
    user.default_org_id = w.org.id
    user.is_active = True
    w.db.commit()


# ---- two devices -------------------------------------------------------------------------------


@needs_pg
def test_two_devices_refresh_alternately_five_times_and_never_sign_each_other_out(session_world):
    w = session_world
    a, b = Device(w).login(), Device(w).login()
    assert len(live(w)) == 2 and a.refresh != b.refresh
    for _ in range(5):
        a.refresh_ok()
        b.refresh_ok()
    assert len(live(w)) == 2  # still two sessions, none revoked, none added
    assert all(s.revoke_reason is None for s in sessions(w))
    # the legacy column is not used any more
    w.db.refresh(w.owner)
    assert w.owner.refresh_token_hash is None


@needs_pg
def test_a_new_login_does_not_touch_the_other_sessions_of_the_user(session_world):
    w = session_world
    a = Device(w).login()
    before = live_tokens(w)
    Device(w).login()
    assert before < live_tokens(w) and len(live_tokens(w)) == 2  # the first token is still live, untouched
    assert a.status_of_refresh() == 200


@needs_pg
def test_logout_on_one_device_signs_out_only_that_device(session_world):
    w = session_world
    a, b = Device(w).login(), Device(w).login()
    a.logout()
    assert a.status_of_refresh() == 401
    assert b.status_of_refresh() == 200
    revoked = [s for s in sessions(w) if s.revoked_at is not None]
    assert len(revoked) == 1 and revoked[0].revoke_reason == "logout"
    assert len(live(w)) == 1


@needs_pg
def test_logout_by_access_token_alone_revokes_the_session_it_names_and_only_that_one(session_world):
    """K2-1 (A5): the access token carries ``sid``, so it does identify a device: that session ends at once; the other device stays."""
    w = session_world
    a, b = Device(w).login(), Device(w).login()
    r = a.client.post(f"{API}/logout", headers=cookie_header(access=a.access))
    assert r.status_code == 200
    assert len(live(w)) == 1 and a.status_of_refresh() == 401 and b.status_of_refresh() == 200


@needs_pg
def test_logout_with_a_token_superseded_within_the_grace_revokes_the_session(session_world):
    """The browser just raced a refresh and still holds the older cookie: it is still this device's session."""
    w = session_world
    a, b = Device(w).login(), Device(w).login()
    old = a.refresh
    a.refresh_ok()
    r = a.logout_with(cookie_token=old)
    assert r.status_code == 200
    assert_cleared(r)
    assert session_of(w, hash_token(a.refresh)).revoke_reason == "logout"
    assert a.status_of_refresh() == 401 and b.status_of_refresh() == 200


@needs_pg
def test_logout_with_any_live_token_of_the_session_revokes_it(session_world):
    w = session_world
    a = Device(w).login()
    t0 = a.refresh
    a.refresh_ok()
    t1 = a.refresh
    sibling = set_cookies(a.refresh_with(t0))[REFRESH_COOKIE_NAME].value  # two live tokens: t1 and the sibling
    assert live_tokens(w) == {hash_token(t1), hash_token(sibling)}
    assert a.logout_with(cookie_token=t1).status_code == 200
    assert live_tokens(w) == set()
    d = Device(w)
    d.refresh = sibling
    assert d.status_of_refresh() == 401


@needs_pg
def test_logout_with_a_token_superseded_longer_ago_revokes_nothing_and_still_clears_the_cookies(session_world, session_clock):
    w = session_world
    a = Device(w).login()
    old = a.refresh
    a.refresh_ok()
    session_clock.advance(user_sessions.GRACE_SECONDS + 1)
    r = a.logout_with(cookie_token=old)
    assert r.status_code == 200
    assert_cleared(r)
    assert len(live(w)) == 1 and a.status_of_refresh() == 200


@needs_pg
def test_logout_accepts_the_refresh_token_in_the_json_body_like_refresh_and_the_cookie_wins(session_world):
    w = session_world
    a, b, c, d = (Device(w).login() for _ in range(4))
    # only the body: it names the session, exactly as /auth/refresh accepts it
    r = a.logout_with(body=json.dumps({"refresh_token": a.refresh}))
    assert r.status_code == 200
    assert_cleared(r)
    assert a.status_of_refresh() == 401 and b.status_of_refresh() == 200
    # both: the cookie wins, the body token is left alone
    r = c.logout_with(cookie_token=c.refresh, body=json.dumps({"refresh_token": d.refresh}))
    assert r.status_code == 200
    assert c.status_of_refresh() == 401 and d.status_of_refresh() == 200


@needs_pg
def test_logout_reads_only_a_small_json_body_a_valid_token_in_a_big_one_is_ignored(session_world):
    w = session_world
    a, b = Device(w).login(), Device(w).login()
    big = json.dumps({"refresh_token": a.refresh, "pad": "x" * (4096 + 1)})
    assert a.logout_with(body=big).status_code == 200  # the body is not even read
    assert len(live(w)) == 2 and a.status_of_refresh() == 200
    small = json.dumps({"refresh_token": b.refresh})
    r = b.logout_with(body=small, content_type="application/json; charset=utf-8")
    assert r.status_code == 200
    assert b.status_of_refresh() == 401 and a.status_of_refresh() == 200


@needs_pg
@pytest.mark.parametrize(
    "body,content_type",
    [("not json at all", "application/json"), ("[1, 2]", "application/json"), ('{"refresh_token": 5}', "application/json"),
     ('{"refresh_token": ""}', "application/json"), ("refresh_token=x", "application/x-www-form-urlencoded"),
     ("anything", "text/plain")],
)
def test_logout_never_answers_422_for_an_unusable_body(session_world, body, content_type):
    w = session_world
    a = Device(w).login()
    r = a.logout_with(cookie_token=a.refresh, body=body, content_type=content_type)
    assert r.status_code == 200, r.text
    assert a.status_of_refresh() == 401  # the cookie still did its work


@needs_pg
def test_refusals_keep_their_status_and_message(session_world):
    w = session_world
    a = Device(w).login()
    # an unknown (but correctly signed) token
    r = a.refresh_with(create_refresh_token(str(w.owner.id)))
    assert r.status_code == 401 and r.json() == REFUSED
    # a revoked session
    a.logout()
    r = a.refresh_with(a.refresh)
    assert r.status_code == 401 and r.json() == REFUSED


@needs_pg
def test_a_token_whose_subject_is_not_the_owner_of_its_session_is_refused_and_revokes_nothing(session_world):
    w = session_world
    enable_login(w, w.admin)
    a, other = Device(w).login(), Device(w, w.admin).login()
    stolen = create_refresh_token(str(w.admin.id))  # signed for the admin ...
    user_sessions.issue(w.db, w.owner.id, hash_token(stolen), timedelta(days=30))  # ... but stored under the owner
    w.db.commit()
    r = a.refresh_with(stolen)
    assert r.status_code == 401 and r.json() == REFUSED
    assert len(live(w)) == 2 and len(live(w, w.admin)) == 1 and other.status_of_refresh() == 200


@needs_pg
def test_an_expired_session_is_refused_even_with_a_valid_jwt(session_world, session_clock):
    w = session_world
    a = Device(w).login()
    row = sessions(w)[0]
    row.expires_at = user_sessions._now() - timedelta(seconds=1)
    w.db.commit()
    r = a.refresh_with(a.refresh)
    assert r.status_code == 401 and r.json() == REFUSED


# ---- rotation: the 60 s grace, the replay, the siblings -----------------------------------------


@needs_pg
def test_a_just_superseded_token_is_answered_within_the_grace_with_a_new_sibling(session_world):
    """Another tab of the same browser sent the old cookie at the same moment: no sign-out, nothing demoted."""
    w = session_world
    a = Device(w).login()
    t0 = a.refresh
    a.refresh_ok()
    t1 = a.refresh
    again = a.refresh_with(t0)  # the concurrent tab
    assert again.status_code == 200, again.text
    t2 = set_cookies(again)[REFRESH_COOKIE_NAME].value
    assert t2 not in (t0, t1)
    # nothing was revoked; BOTH answers are live (the first one was not demoted), the old one is superseded
    (session,) = sessions(w)
    assert session.revoked_at is None
    assert live_tokens(w) == {hash_token(t1), hash_token(t2)}
    assert [t.superseded_at is None for t in token_rows(w, session)].count(False) == 1
    assert a.refresh_with(t1).status_code == 200  # the tab that got t1 from its own answer
    assert len(live(w)) == 1


@needs_pg
def test_a_replayed_old_token_after_the_grace_revokes_only_that_session(session_world, session_clock, caplog):
    w = session_world
    a, b = Device(w).login(), Device(w).login()
    t0 = a.refresh
    a.refresh_ok()
    session_clock.advance(61)
    with caplog.at_level(logging.WARNING, logger="src.auth.user_sessions"):
        r = a.refresh_with(t0)
    assert r.status_code == 401 and r.json() == REFUSED
    row_a = session_of(w, hash_token(a.refresh))
    assert row_a.revoked_at is not None and row_a.revoke_reason == "refresh_reuse"
    # the WARNING names the user and the session, never a token
    messages = " ".join(rec.getMessage() for rec in caplog.records if rec.levelno == logging.WARNING)
    assert str(w.owner.id) in messages and str(row_a.id) in messages
    assert t0 not in messages and a.refresh not in messages and hash_token(t0) not in messages
    # the replayed session is dead (also with its current token), the other device is untouched
    assert a.status_of_refresh() == 401
    assert b.status_of_refresh() == 200
    assert session_of(w, hash_token(b.refresh)).revoked_at is None


@needs_pg
def test_a_token_replayed_exactly_at_the_end_of_the_grace_is_still_answered(session_world, session_clock):
    w = session_world
    a = Device(w).login()
    t0 = a.refresh
    a.refresh_ok()
    session_clock.advance(user_sessions.GRACE_SECONDS - 1)
    assert a.refresh_with(t0).status_code == 200


@needs_pg
def test_the_grace_is_counted_from_the_supersession_of_each_token_not_from_the_last_refresh(session_world, session_clock):
    w = session_world
    a = Device(w).login()
    t0 = a.refresh
    a.refresh_ok()  # t=0: t0 superseded
    t1 = a.refresh
    session_clock.advance(40)
    a.refresh_ok()  # t=40: t1 superseded
    t2 = a.refresh
    session_clock.advance(10)  # t=50: t0 is two generations old, superseded 50 s ago
    assert a.refresh_with(t0).status_code == 200
    session_clock.advance(11)  # t=61: t1 was superseded 21 s ago, t0 61 s ago
    assert a.refresh_with(t1).status_code == 200
    assert a.refresh_with(t0).status_code == 401  # past its own grace: a replay
    row = session_of(w, hash_token(t2))
    assert row.revoked_at is not None and row.revoke_reason == "refresh_reuse"
    assert len(live(w)) == 0


@needs_pg
def test_rotation_slides_the_expiry_and_records_the_last_use(session_world, session_clock):
    w = session_world
    a = Device(w).login(remember=True)             # only a remembered session slides (a short one keeps its end: see test_k2_1_pg.py)
    first = sessions(w)[0]
    created, expires = first.created_at, first.expires_at
    session_clock.advance(3600)
    a.refresh_ok()
    (row,) = sessions(w)
    assert row.created_at == created
    assert row.last_used_at - created >= timedelta(seconds=3590)
    assert row.expires_at - expires >= timedelta(seconds=3590)
    assert len(token_rows(w, row)) == 2 and len(live_tokens(w)) == 1


# ---- parallel refreshes of the same token ----------------------------------------------------------


def _new_login_token(w):
    w.db.refresh(w.owner)
    return AuthService(w.db).login(LoginRequest(email=w.owner.email, password=PASSWORD)).tokens.refresh_token


@needs_pg
@pytest.mark.parametrize("count", [2, 3, 5])
def test_simultaneous_refreshes_of_one_token_all_succeed_and_every_answer_stays_valid(
    session_world, session_clock, monkeypatch, count
):
    w = session_world
    token = _new_login_token(w)
    results, elapsed = parallel_refresh(w, [token] * count, monkeypatch)
    assert not any(isinstance(r, Exception) for r in results), results
    answers = [r.refresh_token for r in results]
    assert len(set(answers)) == count and token not in answers
    assert max(elapsed[1:]) >= 0.5  # they waited for the session lock instead of rotating from the same token
    (session,) = sessions(w)
    assert session.revoked_at is None and session.revoke_reason is None
    # one superseded token, and every answer is a LIVE sibling
    assert live_tokens(w) == {hash_token(a) for a in answers}
    assert [t.superseded_at is not None for t in token_rows(w, session)].count(True) == 1
    # more than a minute later every answer still refreshes: the first use supersedes the others, which are then
    # within the grace of THAT supersession
    session_clock.advance(user_sessions.GRACE_SECONDS * 5)
    for answer in answers:
        AuthService(w.db).refresh_tokens(answer)  # raises on a refusal
    assert sessions(w)[0].revoked_at is None


@needs_pg
def test_after_a_two_way_race_the_jar_may_end_on_the_first_answer_even_a_long_time_later(session_world, session_clock, monkeypatch):
    w = session_world
    token = _new_login_token(w)
    results, _ = parallel_refresh(w, [token, token], monkeypatch)
    first, second = results[0].refresh_token, results[1].refresh_token  # whichever the browser kept
    session_clock.advance(7200)
    d = Device(w)
    d.refresh = first
    assert d.status_of_refresh() == 200
    assert sessions(w)[0].revoked_at is None
    d.refresh = second  # the other answer was superseded by that very refresh: still inside its grace
    assert d.status_of_refresh() == 200


@needs_pg
def test_two_simultaneous_refreshes_with_two_different_live_siblings_do_not_deadlock(session_world, monkeypatch):
    """Token first, session second would deadlock here (each holds its own token row, the winner supersedes both)."""
    w = session_world
    token = _new_login_token(w)
    results, _ = parallel_refresh(w, [token, token], monkeypatch, hold=0.1)
    siblings = [r.refresh_token for r in results]
    assert len(live_tokens(w)) == 2
    results, _ = parallel_refresh(w, siblings, monkeypatch, hold=0.7)
    assert not any(isinstance(r, Exception) for r in results), results
    assert sessions(w)[0].revoked_at is None


# ---- superseded tokens are kept 7 days, then swept ------------------------------------------------------


@needs_pg
def test_superseded_tokens_are_kept_7_days_for_replay_detection_then_deleted(session_world, session_clock):
    w = session_world
    forget_superseded_tokens(w)  # independent of the rows other tests left (the sweep takes the 100 oldest)
    a = Device(w).login()
    t0 = a.refresh
    a.refresh_ok()
    t1 = a.refresh
    session_clock.advance(6 * 86400)  # t0 is 6 days old: still known, so a replay is detected
    stale = hash_token(t0)
    assert w.db.query(UserSessionToken).filter_by(token_hash=stale).count() == 1
    Device(w).login()  # a sign-in sweeps; nothing is 7 days old yet
    assert w.db.query(UserSessionToken).filter_by(token_hash=stale).count() == 1
    session_clock.advance(2 * 86400)  # now t0 was superseded 8 days ago
    Device(w).login()
    assert w.db.query(UserSessionToken).filter_by(token_hash=stale).count() == 0
    assert w.db.query(UserSessionToken).filter_by(token_hash=hash_token(t1)).count() == 1  # the live token stays
    # the replay of a token that is gone is an UNKNOWN token: a plain refusal that revokes nothing
    r = a.refresh_with(t0)
    assert r.status_code == 401 and r.json() == REFUSED
    assert all(s.revoked_at is None for s in sessions(w))
    # ... while the replay of a token that is still kept revokes (a fresh session for that)
    b = Device(w).login()
    old = b.refresh
    b.refresh_ok()
    session_clock.advance(user_sessions.GRACE_SECONDS + 1)
    assert b.refresh_with(old).status_code == 401
    assert session_of(w, hash_token(b.refresh)).revoke_reason == "refresh_reuse"


@needs_pg
def test_a_sign_in_sweeps_at_most_one_batch_of_old_superseded_tokens(session_world):
    w = session_world
    forget_superseded_tokens(w)
    a = Device(w).login()
    (session,) = sessions(w)
    old = user_sessions._now() - timedelta(days=user_sessions.SUPERSEDED_RETENTION_DAYS + 1)
    batch = user_sessions.SWEEP_BATCH
    w.db.add_all(
        UserSessionToken(token_hash=f"old-{uuid4()}", session_id=session.id, issued_at=old, superseded_at=old)
        for _ in range(batch * 2 + 50)
    )
    w.db.commit()

    def superseded():
        return w.db.query(UserSessionToken).filter(UserSessionToken.superseded_at.is_not(None)).count()

    assert superseded() == batch * 2 + 50
    Device(w).login()
    assert superseded() == batch + 50  # one sign-in: one batch, never the whole backlog
    Device(w).login()
    assert superseded() == 50
    Device(w).login()
    assert superseded() == 0
    assert live_tokens(w) >= {hash_token(a.refresh)}  # live tokens are never swept


@needs_pg
def test_a_refresh_prunes_the_old_superseded_tokens_of_its_own_session(session_world, session_clock):
    w = session_world
    a = Device(w).login(remember=True)
    for _ in range(3):
        a.refresh_ok()
    (session,) = sessions(w)
    assert len(token_rows(w, session)) == 4
    session_clock.advance((user_sessions.SUPERSEDED_RETENTION_DAYS + 1) * 86400)
    a.refresh_with(a.refresh)  # the live token: supersedes itself, the three old superseded ones are deleted
    assert len(token_rows(w, session)) == 2 and len(live_tokens(w)) == 1


# ---- password change, blocking ----------------------------------------------------------------


@needs_pg
def test_a_password_change_signs_out_every_other_device_and_keeps_this_one(session_world):
    """K2-1 M1: the device that changes the password stays signed in (its cookies are renewed); every other one signs in again."""
    w = session_world
    a, b = Device(w).login(), Device(w).login()
    r = a.client.post(
        f"{API}/change-password",
        json={"old_password": PASSWORD, "new_password": "Another-Pass-1!"},
        headers=cookie_header(access=a.access, refresh=a.refresh),
    )
    assert r.status_code == 200, r.text
    assert b.status_of_refresh() == 401
    assert len(live(w)) == 1
    assert [s.revoke_reason for s in sessions(w)] == [None, "password_changed"]
    # this device keeps working with the renewed cookies
    jar = set_cookies(r)
    assert jar[AUTH_COOKIE_NAME].value and a.client.get(f"{API}/me").status_code == 200
    # signing in with the new password starts a fresh session
    c = Device(w)
    c.client.post(f"{API}/login", json={"email": w.owner.email, "password": "Another-Pass-1!"})
    assert len(live(w)) == 2


@needs_pg
def test_a_blocked_user_is_signed_out_everywhere_and_others_are_not_touched(session_world):
    w = session_world
    enable_login(w, w.admin)
    owner_dev = Device(w).login()
    admin_a, admin_b = Device(w, w.admin).login(), Device(w, w.admin).login()
    member = w.db.query(OrganizationMember).filter_by(org_id=w.org.id, user_id=w.admin.id).one()
    assert AuthService(w.db).remove_member(str(w.org.id), str(member.id))["message"] == "Сотрудник удалён"
    assert admin_a.status_of_refresh() == 401 and admin_b.status_of_refresh() == 401
    assert len(live(w, w.admin)) == 0 and {s.revoke_reason for s in sessions(w, w.admin)} == {"user_blocked"}
    assert owner_dev.status_of_refresh() == 200 and len(live(w)) == 1


@needs_pg
def test_a_user_who_became_inactive_is_refused_with_the_blocked_message(session_world):
    w = session_world
    a = Device(w).login()
    w.owner.is_active = False
    w.db.commit()
    r = a.refresh_with(a.refresh)
    assert r.status_code == 401 and r.json() == {"detail": "Пользователь не найден или заблокирован"}


# ---- the cap, the sweep ------------------------------------------------------------------------


@needs_pg
def test_at_most_20_active_sessions_the_least_recently_used_beyond_the_cap_is_revoked(session_world, session_clock):
    w = session_world
    svc = AuthService(w.db)
    tokens = []
    for _ in range(user_sessions.MAX_ACTIVE_SESSIONS):
        tokens.append(svc._create_tokens(w.owner).refresh_token)
        w.db.commit()
        session_clock.advance(1)
    assert len(live(w)) == 20
    # the very first device is used again: it is no longer the one to go
    first = Device(w)
    first.refresh = tokens[0]
    first.refresh_ok()
    session_clock.advance(1)
    newest = svc._create_tokens(w.owner).refresh_token
    w.db.commit()
    rows = sessions(w)
    assert len(rows) == 21 and len(live(w)) == 20
    (gone,) = [s for s in rows if s.revoked_at is not None]
    assert gone.revoke_reason == "session_cap" and gone.id == session_of(w, hash_token(tokens[1])).id
    assert session_of(w, hash_token(newest)).revoked_at is None
    assert session_of(w, hash_token(first.refresh)).revoked_at is None
    # the revoked device is refused like any revoked session
    d = Device(w)
    d.refresh = tokens[1]
    assert d.status_of_refresh() == 401
    # sessions of OTHER users are not counted
    enable_login(w, w.admin)
    Device(w, w.admin).login()
    assert len(live(w)) == 20 and len(live(w, w.admin)) == 1


@needs_pg
def test_sessions_expired_more_than_30_days_ago_are_deleted_on_the_next_sign_in(session_world):
    w = session_world
    now = user_sessions._now()
    mk = lambda days: user_sessions.issue(w.db, w.owner.id, hash_token(str(uuid4())), timedelta(days=30))  # noqa: E731
    old, recent, active = mk(0), mk(0), mk(0)
    old.expires_at = now - timedelta(days=31)
    recent.expires_at = now - timedelta(days=29)
    w.db.commit()
    old_id, recent_id, active_id = old.id, recent.id, active.id
    Device(w).login()
    remaining = {s.id for s in sessions(w)}
    assert old_id not in remaining and recent_id in remaining and active_id in remaining


# ---- where it came from ---------------------------------------------------------------------------


@needs_pg
def test_ip_and_user_agent_come_from_the_trusted_helper_and_are_bounded(session_world):
    w = session_world
    long_agent = "Mozilla/5.0 " + "x" * 400
    Device(w, ip="203.0.113.7", agent=long_agent).login()
    # a client-typed X-Forwarded-For does not win over X-Real-IP; without X-Real-IP the rightmost element is used
    spoof = Device(w, ip="198.51.100.9", agent="spoof")
    spoof.headers["x-forwarded-for"] = "1.2.3.4, 5.6.7.8"
    spoof.login()
    forwarded_only = Device(w, agent="fwd")
    forwarded_only.headers["x-forwarded-for"] = "1.2.3.4, 192.0.2.55"
    forwarded_only.login()
    rows = {s.user_agent[:5] if s.user_agent else None: s for s in sessions(w)}
    assert rows["Mozil"].ip == "203.0.113.7" and len(rows["Mozil"].user_agent) == 200
    assert rows["spoof"].ip == "198.51.100.9"
    assert rows["fwd"].ip == "192.0.2.55"
    # a refresh records the place it came from
    d = Device(w, ip="203.0.113.99", agent="moved")
    d.login()
    d.headers["x-real-ip"] = "203.0.113.100"
    d.refresh_ok()
    assert session_of(w, hash_token(d.refresh)).ip == "203.0.113.100"


# ---- the other sign-in paths each create their own session -----------------------------------------


@needs_pg
def test_invitation_accept_creates_a_session(session_world):
    from src.auth.schemas import AcceptInviteRequest
    from src.models.auth import Invitation, Organization

    w = session_world
    svc = AuthService(w.db)
    code = uuid4().hex[:10].upper()
    invited_email = f"{uuid4()}@n13.invalid"          # LOGIC-B O10: the account gets the e-mail of the invitation
    w.db.add(
        Invitation(
            org_id=w.org.id, invited_by=w.owner.id, email=invited_email, org_role="teacher", code_hash=hash_token(code),
            expires_at=user_sessions._now() + timedelta(days=3),
        )
    )
    w.db.commit()
    result = svc.accept_invitation(
        AcceptInviteRequest(code=code, email=invited_email, password="Invite-Pass-1!", first_name="Н", last_name="Н")
    )
    invited = w.db.query(User).filter(User.id == UUID(result.user.id)).one()
    (row,) = sessions(w, invited)
    assert live_tokens(w, invited) == {hash_token(result.tokens.refresh_token)} and row.revoked_at is None


@needs_pg
def test_dev_quick_login_creates_a_session_per_call(session_world):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.auth.dev_router import router as dev_router
    from src.core.database import get_db

    w = session_world
    app = FastAPI()
    app.include_router(dev_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: w.db
    client = TestClient(app)
    hashes = []
    for _ in range(2):
        r = client.post(f"{API}/dev-login?role=teacher", headers={"x-real-ip": "203.0.113.5"})
        assert r.status_code == 200, r.text
        assert "tokens" not in r.json()
        hashes.append(hash_token(set_cookies(r)[REFRESH_COOKIE_NAME].value))
    dev_user = w.db.query(User).filter(User.email == "dev_teacher@algo.test").one()
    rows = {h: session_of(w, h) for h in hashes}
    assert len({r.id for r in rows.values()}) == 2 and set(hashes) <= live_tokens(w, dev_user)
    assert all(r.revoked_at is None and r.ip == "203.0.113.5" for r in rows.values())
