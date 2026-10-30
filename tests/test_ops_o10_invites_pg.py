"""LOGIC-B O10 - invitations (mandatory e-mail from the invitation, expiry, one pending per e-mail, atomic accept, returning staff).
Matrix rows [O10.R1] ... [O10.R9]; chain 6."""
from datetime import datetime, timedelta

import re

import pytest
from sqlalchemy import text

from src.auth.schemas import LoginRequest
from src.auth.service import hash_password, hash_token
from src.models.auth import Invitation, MemberAudit, OrganizationMember, User

from tests.ops_helpers import (  # noqa: F401  (fixtures)
    build_d0, clock, client, code_of, exams, fixture, needs_pg, pg_session, run_parallel,
)

pytestmark = needs_pg
API = "/api/v1/auth"


@pytest.fixture
def w(pg_session, clock, monkeypatch):
    from src.mail import sender

    clock.set("2026-10-21 14:00")
    world = build_d0(pg_session)
    world.outbox = []
    monkeypatch.setattr(sender, "deliver", lambda msg, cfg=None: world.outbox.append(msg) or True)   # the letters of the invitations (no SMTP)
    return world


def em(w, name):
    """A unique e-mail per test run (users are global)."""
    return f"{name}-{w.salt}@x.uz"


def invite(w, who, email, role="teacher"):
    return client(w, who).post(f"{API}/org/invitations", json={"email": email, "org_role": role})


def code_of_invitation(w, response):
    """K2-1 (A1): no answer carries the code and the database keeps only its hash (it travels in the letter); the test reads it from the letter that went."""
    assert response.status_code == 200, response.text
    w.db.expire_all()
    address = w.db.get(Invitation, response.json()["id"]).email
    mails = [m for m in w.outbox if address in str(m["To"])]
    assert mails, f"no letter to {address}"
    return re.search(r"/invite/([A-Za-z0-9_%-]+)", mails[-1].get_body(preferencelist=("plain",)).get_content()).group(1)


def row_of(w, code):
    w.db.expire_all()
    return w.db.query(Invitation).filter_by(code_hash=hash_token(code)).one()


def invite_code(w, who, email, role="teacher"):
    return code_of_invitation(w, invite(w, who, email, role))


def accept(w, code, password="new-secret-1", **extra):
    return client(w, "owner").post(f"{API}/invite/accept", json={"code": code, "password": password, **extra})


def pending(w):
    w.db.expire_all()
    return [i for i in w.db.query(Invitation).filter_by(org_id=w.org.id) if i.status == "pending"]


def test_r1_the_invitation_carries_the_email_and_the_account_gets_it(w):
    """[O10.R1]"""
    r = invite(w, "owner", f"  {em(w, 't').upper()} ")
    assert r.status_code == 200 and set(r.json()) == {"id", "email", "expires_at", "email_sent"}
    code = code_of_invitation(w, r)
    row = w.db.get(Invitation, r.json()["id"])
    assert row.email == em(w, "t") and row.status == "pending" and row.org_role == "teacher"
    info = client(w, "owner").get(f"{API}/invite/{code}").json()
    assert info == {"org_name": "Алго Тест", "org_role": "teacher", "email_masked": f"{em(w, 't')[0]}***@x.uz", "is_returning": False}
    done = accept(w, code, first_name="Нодир", last_name="Алиев")
    assert done.status_code == 201 and done.json()["user"]["email"] == em(w, "t")
    mismatch_code = invite_code(w, "owner", em(w, "m"))
    bad = accept(w, mismatch_code, first_name="М", last_name="Н", email=em(w, "other"))
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "INVITE_EMAIL_MISMATCH"


def test_r2_two_accepts_one_account_and_a_new_invitation_cancels_the_old(w):
    """[O10.R2] [INV-O10-1] Parallel accepts of one code: one account; a new invitation to the same e-mail cancels the previous pending one."""
    code = invite_code(w, "owner", em(w, "p"))
    from src.auth.ops_schemas import AcceptInviteRequestB

    def call(svc):
        return svc.accept_invitation(AcceptInviteRequestB(code=code, password="secret-123", first_name="П", last_name="Р"))

    results = run_parallel(w, call, call)
    ok = [r for r, e in results if e is None]
    bad = [e for _r, e in results if e is not None]
    assert len(ok) == 1 and len(bad) == 1 and "не найдено или уже использовано" in str(bad[0])
    assert w.db.query(User).filter(User.email == em(w, "p")).count() == 1
    first = invite_code(w, "owner", em(w, "q"))
    second = invite_code(w, "owner", em(w, "q"))
    w.db.expire_all()
    assert row_of(w, first).status == "cancelled" and row_of(w, second).status == "pending"


def test_r3_revoke(w):
    """[O10.R3]"""
    r = invite(w, "owner", em(w, "r"))
    code = code_of_invitation(w, r)
    assert client(w, "owner").delete(f"{API}/org/invitations/{r.json()['id']}").status_code == 200
    late = accept(w, code, first_name="Р", last_name="Р")
    assert late.status_code == 410 and late.json()["code"] == "link_revoked"          # K2-1: a cancelled link says so


def test_r4_owner_and_admin_create_at_once_one_pending(w):
    """[O10.R4] The partial unique index: whoever commits last wins, there is one pending invitation."""
    org = str(w.org.id)
    from src.auth.ops_schemas import InviteMemberRequestB

    def make(who):
        return lambda svc: svc.create_invitation(org, str(getattr(w, who).id), InviteMemberRequestB(email=em(w, "same"), org_role="teacher"), creator_role=who)

    results = run_parallel(w, make("owner"), make("admin"))
    assert all(e is None for _r, e in results), results
    assert len(pending(w)) == 1


def test_r5_expired_at_the_first_read_and_the_boundary(w, clock):
    """[O10.R5] [INV-O10-3] The 8th day: status expired at the first touch (list / info / accept); ``expires_at <= now`` is already expired."""
    code = invite_code(w, "owner", em(w, "e"))
    clock.set("2026-10-28 13:59")                                           # 1 minute before 14:00 + 7 days (UTC 09:00 + 7d = 28.10 09:00 -> 14:00 local)
    assert client(w, "owner").get(f"{API}/invite/{code}").status_code == 200
    clock.set("2026-10-28 14:00")                                           # exactly the expiry: expired
    gone = client(w, "owner").get(f"{API}/invite/{code}")
    assert gone.status_code == 404 and "истекло" in gone.json()["detail"]
    listing = client(w, "owner").get(f"{API}/org/invitations").json()
    assert all("code" not in i for i in listing)
    expired_row = row_of(w, code)
    assert [i["status"] for i in listing if i["id"] == str(expired_row.id)] == ["expired"]
    w.db.expire_all()
    assert not [i for i in w.db.query(Invitation).filter_by(org_id=w.org.id) if i.status == "pending" and i.expires_at <= datetime(2026, 10, 28, 9, 0)]
    code2 = invite_code(w, "owner", em(w, "e2"))
    clock.set("2026-11-10 10:00")
    late = accept(w, code2, first_name="Е", last_name="Е")
    assert late.status_code == 400 and "истекло" in late.json()["detail"]


def test_r6_seven_days_from_the_creation_in_utc_chain6(w, clock):
    """[O10.R6] Chain 6: created 10.11 14:00 Tashkent -> expires 17.11 09:00 UTC (shown 17.11 14:00); the golden answer."""
    clock.set("2026-11-10 14:00")
    r = invite(w, "owner", w.A.email)                                       # A is an ACTIVE member here: refused
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ALREADY_MEMBER"
    client(w, "owner").delete(f"{API}/org/members/{w.ids('member_B')}")     # B has groups: blocked; use D (no groups) as "the returning one"
    client(w, "owner").delete(f"{API}/org/members/{w.ids('member_D')}")
    r = invite(w, "owner", w.D.email)
    assert r.status_code == 200
    expected = w.ids.remap(fixture("c6_invitation_create.json"))
    out = r.json()
    assert out["expires_at"] == expected["expires_at"] and set(out) == {"id", "email", "expires_at", "email_sent"} and out["email"] == w.D.email
    row = w.db.get(Invitation, out["id"])
    assert row.status == "pending" and row.org_role == "teacher"
    info = client(w, "owner").get(f"{API}/invite/{code_of_invitation(w, r)}").json()
    assert info == {"org_name": "Алго Тест", "org_role": "teacher", "email_masked": f"{w.D.email[0]}***@{w.D.email.split('@')[1]}", "is_returning": True}


def test_r7_email_is_mandatory(w):
    """[O10.R7] A new invitation without an e-mail is 422 EMAIL_REQUIRED."""
    r = client(w, "owner").post(f"{API}/org/invitations", json={"org_role": "teacher"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "EMAIL_REQUIRED"
    assert client(w, "owner").post(f"{API}/org/invitations", json={"email": "   ", "org_role": "teacher"}).json()["detail"]["code"] == "EMAIL_REQUIRED"


def test_r8_who_invites_whom(w):
    """[O10.R8] admin -> teacher yes; admin -> admin 403 ONLY_OWNER_INVITES_ADMIN; teacher 403; owner -> admin yes."""
    assert invite(w, "admin", em(w, "t1")).status_code == 200
    r = invite(w, "admin", em(w, "a1"), role="admin")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "ONLY_OWNER_INVITES_ADMIN"
    assert invite(w, "A", em(w, "t2")).status_code == 403
    assert invite(w, "owner", em(w, "a2"), role="admin").status_code == 200


def test_r9_active_member_removed_member_and_another_centre_chain6(w, clock):
    """[O10.R9] [O2.R9] Chain 6: an active member's e-mail is 409 ALREADY_MEMBER; a removed member's invitation reactivates the SAME user (new password, old sessions revoked, name kept,
    member_audit reactivated_by_invite, role from the invitation, no groups back); an e-mail of another centre is 409 EMAIL_IN_OTHER_ORG at accept."""
    assert invite(w, "owner", w.C.email).status_code == 409
    w.D.password_hash = hash_password("old-password")
    w.db.commit()
    from src.auth import user_sessions  # noqa: F401

    client(w, "owner").delete(f"{API}/org/members/{w.ids('member_D')}")
    code = invite_code(w, "owner", w.D.email, role="admin")
    done = accept(w, code, password="brand-new-1")                       # no names sent: the returning member keeps his own (lead decision)
    assert done.status_code == 201, done.text
    w.db.expire_all()
    user = w.db.get(User, w.D.id)
    assert str(done.json()["user"]["id"]) == str(w.D.id) and user.is_active and user.first_name == "Тимур" and user.last_name == "Рахимов"
    member = w.member("D")
    assert member.is_active and member.org_role == "admin"
    assert w.svc.login(LoginRequest(email=w.D.email, password="brand-new-1")).user.email == w.D.email
    with pytest.raises(Exception):
        w.svc.login(LoginRequest(email=w.D.email, password="old-password"))
    audits = w.db.query(MemberAudit).filter_by(member_id=w.ids("member_D"), action="reactivated_by_invite").all()
    assert len(audits) == 1 and audits[0].snapshot["org_role"] == "admin"
    # another centre
    foreign_code = invite_code(w, "owner", w.foreign_admin.email)
    other = accept(w, foreign_code, first_name="Х", last_name="Х")
    assert other.status_code == 409 and other.json()["detail"]["code"] == "EMAIL_IN_OTHER_ORG"
    # a new account needs a name
    nameless = invite_code(w, "owner", em(w, "new"))
    assert accept(w, nameless).status_code == 400
