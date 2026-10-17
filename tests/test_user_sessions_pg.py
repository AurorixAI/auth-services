"""One session per signed-in device (ROADMAP #101), against the disposable ``a2_auth`` PostgreSQL.

Defect: a user had ONE refresh token (``users.refresh_token_hash``) and every login / refresh overwrote it, so the
same person on two devices (the owner on phone + laptop, one reception account on two PCs) was signed out of the
first device at its next refresh (401 "Refresh token отозван"). Now every sign-in is its own row of
``user_sessions`` and a refresh rotates only the row it belongs to. The migration and its backfill are covered in
``test_postgres_migrations.py``; the cookie and throttle tests are untouched.
"""
import logging
import threading
import time
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from src.auth import user_sessions
from src.auth.cookies import AUTH_COOKIE_NAME, REFRESH_COOKIE_NAME
from src.auth.schemas import LoginRequest
from src.auth.service import AuthService, create_refresh_token, hash_password, hash_token
from src.models.auth import OrganizationMember, User, UserSession

from tests.pg_world import needs_pg, pg_session, session_clock, world  # noqa: F401  (fixtures)
from tests.test_session_cookies import (  # noqa: F401  (session_world is a fixture)
    API,
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

    def login(self, password=PASSWORD):
        r = self.client.post(
            f"{API}/login", json={"email": self.user.email, "password": password}, headers=self.headers
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


def session_of(w, token, user=None):
    rows = [s for s in sessions(w, user) if token in (s.refresh_token_hash, s.previous_token_hash)]
    assert len(rows) == 1
    return rows[0]


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
    before = {s.id: s.refresh_token_hash for s in sessions(w)}
    Device(w).login()
    after = {s.id: s.refresh_token_hash for s in sessions(w)}
    assert len(after) == 2 and all(after[i] == h for i, h in before.items())
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
def test_logout_by_access_token_alone_identifies_no_device_and_revokes_nothing(session_world):
    w = session_world
    a, b = Device(w).login(), Device(w).login()
    r = a.client.post(f"{API}/logout", headers=cookie_header(access=a.access))
    assert r.status_code == 200
    assert len(live(w)) == 2 and a.status_of_refresh() == 200 and b.status_of_refresh() == 200


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


# ---- rotation: the 60 s grace and the replay ---------------------------------------------------


@needs_pg
def test_a_just_rotated_token_is_answered_within_the_grace_with_a_valid_pair(session_world):
    """Another tab of the same browser sent the old cookie at the same moment: no sign-out."""
    w = session_world
    a = Device(w).login()
    t0 = a.refresh
    a.refresh_ok()
    t1 = a.refresh
    again = a.refresh_with(t0)  # the concurrent tab
    assert again.status_code == 200, again.text
    t2 = set_cookies(again)[REFRESH_COOKIE_NAME].value
    assert t2 not in (t0, t1)
    # nothing was revoked; the session now holds t2 (current) and t1 (previous, still in its grace)
    (row,) = sessions(w)
    assert row.revoked_at is None and row.refresh_token_hash == hash_token(t2) and row.previous_token_hash == hash_token(t1)
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
    # the rotated session is dead, the other device is untouched
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
def test_rotation_slides_the_expiry_and_records_the_last_use(session_world, session_clock):
    w = session_world
    a = Device(w).login()
    first = sessions(w)[0]
    created, expires = first.created_at, first.expires_at
    session_clock.advance(3600)
    a.refresh_ok()
    (row,) = sessions(w)
    assert row.created_at == created
    assert row.last_used_at > created and row.rotated_at == row.last_used_at
    assert row.expires_at - expires >= timedelta(seconds=3590)


# ---- parallel refreshes of the same token --------------------------------------------------------


@needs_pg
def test_two_parallel_refreshes_of_one_token_one_rotates_the_other_takes_the_grace_path(session_world, monkeypatch):
    w = session_world
    w.db.refresh(w.owner)
    token = AuthService(w.db).login(LoginRequest(email=w.owner.email, password=PASSWORD)).tokens.refresh_token
    engine = w.db.get_bind()

    first_in_rotation = threading.Event()
    real_rotate = user_sessions.rotate

    def slow_rotate(db, session, *args, **kwargs):
        first_in_rotation.set()  # this transaction now holds the row lock ...
        time.sleep(0.7)  # ... for a while
        return real_rotate(db, session, *args, **kwargs)

    monkeypatch.setattr(user_sessions, "rotate", slow_rotate)
    results, elapsed = {}, {}

    def run(name, start_after_lock):
        if start_after_lock:
            assert first_in_rotation.wait(10)
        began = time.monotonic()
        with sessionmaker(engine)() as db:
            try:
                results[name] = AuthService(db).refresh_tokens(token)
            except Exception as exc:  # noqa: BLE001 - reported by the assertions below
                results[name] = exc
        elapsed[name] = time.monotonic() - began

    threads = [threading.Thread(target=run, args=("first", False)), threading.Thread(target=run, args=("second", True))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    first, second = results["first"], results["second"]
    assert not isinstance(first, Exception) and not isinstance(second, Exception), results
    assert first.refresh_token != second.refresh_token and token not in (first.refresh_token, second.refresh_token)
    assert elapsed["second"] >= 0.5  # it waited for the row lock instead of rotating from the same hash in parallel
    (row,) = sessions(w)
    assert row.revoked_at is None and row.revoke_reason is None
    assert row.refresh_token_hash == hash_token(second.refresh_token)  # the later one rotated again ...
    assert row.previous_token_hash == hash_token(first.refresh_token)  # ... from the first rotation's token, not from `token`
    monkeypatch.setattr(user_sessions, "rotate", real_rotate)
    d = Device(w)
    d.refresh = first.refresh_token
    assert d.status_of_refresh() == 200  # whichever answer the browser kept, it still works


# ---- password change, blocking ----------------------------------------------------------------


@needs_pg
def test_a_password_change_signs_out_every_device(session_world):
    w = session_world
    a, b = Device(w).login(), Device(w).login()
    r = a.client.post(
        f"{API}/change-password",
        json={"old_password": PASSWORD, "new_password": "Another-Pass-1!"},
        headers=cookie_header(access=a.access),
    )
    assert r.status_code == 200, r.text
    assert a.status_of_refresh() == 401 and b.status_of_refresh() == 401
    assert len(live(w)) == 0
    assert {s.revoke_reason for s in sessions(w)} == {"password_changed"}
    # signing in with the new password starts a fresh session
    c = Device(w)
    c.client.post(f"{API}/login", json={"email": w.owner.email, "password": "Another-Pass-1!"})
    assert len(live(w)) == 1


@needs_pg
def test_a_blocked_user_is_signed_out_everywhere_and_others_are_not_touched(session_world):
    w = session_world
    enable_login(w, w.admin)
    owner_dev = Device(w).login()
    admin_a, admin_b = Device(w, w.admin).login(), Device(w, w.admin).login()
    member = w.db.query(OrganizationMember).filter_by(org_id=w.org.id, user_id=w.admin.id).one()
    assert AuthService(w.db).remove_member(str(w.org.id), str(member.id)) is True
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
    assert gone.revoke_reason == "session_cap" and gone.refresh_token_hash == hash_token(tokens[1])
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
def test_invitation_accept_and_activation_each_create_a_session(session_world):
    from src.auth.schemas import AcceptInviteRequest
    from src.models.auth import Invitation, Organization

    w = session_world
    svc = AuthService(w.db)
    code = uuid4().hex[:10].upper()
    w.db.add(
        Invitation(
            org_id=w.org.id, invited_by=w.owner.id, email="invited@n13.invalid", org_role="teacher", code=code,
            expires_at=user_sessions._now() + timedelta(days=3),
        )
    )
    w.db.commit()
    result = svc.accept_invitation(
        AcceptInviteRequest(code=code, email=f"{uuid4()}@n13.invalid", password="Invite-Pass-1!", first_name="Н", last_name="Н")
    )
    invited = w.db.query(User).filter(User.id == UUID(result.user.id)).one()
    (row,) = sessions(w, invited)
    assert row.refresh_token_hash == hash_token(result.tokens.refresh_token) and row.revoked_at is None

    # activation of an organisation: a new session for its owner, the owner's other sessions stay
    Device(w).login()
    token = uuid4().hex
    org = Organization(name="To activate", status="pending", owner_id=w.owner.id, activation_token=hash_token(token))
    w.db.add(org)
    w.db.commit()
    before = {s.id for s in live(w)}
    activated = svc.activate_account(token)
    after = live(w)
    assert before <= {s.id for s in after} and len(after) == len(before) + 1
    assert any(s.refresh_token_hash == hash_token(activated.tokens.refresh_token) for s in after)


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
        hashes.append(hash_token(r.json()["tokens"]["refresh_token"]))
    dev_user = w.db.query(User).filter(User.email == "dev_teacher@algo.test").one()
    rows = {s.refresh_token_hash: s for s in sessions(w, dev_user)}
    assert set(hashes) <= set(rows) and all(rows[h].revoked_at is None and rows[h].ip == "203.0.113.5" for h in hashes)
