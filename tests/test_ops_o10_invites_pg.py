"""LOGIC-B O10 - invitations (mandatory e-mail from the invitation, expiry, one pending per e-mail, atomic accept, returning staff).
Matrix rows [O10.R1] ... [O10.R9]; chain 6."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from src.auth.schemas import LoginRequest
from src.auth.service import hash_password
from src.models.auth import Invitation, MemberAudit, OrganizationMember, User

from tests.ops_helpers import (  # noqa: F401  (fixtures)
    build_d0, clock, client, code_of, exams, fixture, needs_pg, pg_session, run_parallel,
)

pytestmark = needs_pg
API = "/api/v1/auth"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")
    return build_d0(pg_session)


def em(w, name):
    """A unique e-mail per test run (users are global)."""
    return f"{name}-{w.salt}@x.uz"


def invite(w, who, email, role="teacher"):
    return client(w, who).post(f"{API}/org/invitations", json={"email": email, "org_role": role})


def accept(w, code, password="new-secret-1", **extra):
    return client(w, "owner").post(f"{API}/invite/accept", json={"code": code, "password": password, **extra})


def pending(w):
    w.db.expire_all()
    return [i for i in w.db.query(Invitation).filter_by(org_id=w.org.id) if i.status == "pending"]


def test_r1_the_invitation_carries_the_email_and_the_account_gets_it(w):
    """[O10.R1]"""
    r = invite(w, "owner", f"  {em(w, 't').upper()} ")
    assert r.status_code == 200 and r.json()["email"] == em(w, "t") and r.json()["status"] == "pending" and r.json()["org_role"] == "teacher"
    code = r.json()["code"]
    info = client(w, "owner").get(f"{API}/invite/{code}").json()
    assert info == {"org_name": "Алго Тест", "org_role": "teacher", "email": em(w, "t"), "is_returning": False}
    done = accept(w, code, first_name="Нодир", last_name="Алиев")
    assert done.status_code == 201 and done.json()["user"]["email"] == em(w, "t")
    mismatch_code = invite(w, "owner", em(w, "m")).json()["code"]
    bad = accept(w, mismatch_code, first_name="М", last_name="Н", email=em(w, "other"))
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "INVITE_EMAIL_MISMATCH"


def test_r2_two_accepts_one_account_and_a_new_invitation_cancels_the_old(w):
    """[O10.R2] [INV-O10-1] Parallel accepts of one code: one account; a new invitation to the same e-mail cancels the previous pending one."""
    code = invite(w, "owner", em(w, "p")).json()["code"]
    from src.auth.ops_schemas import AcceptInviteRequestB

    def call(svc):
        return svc.accept_invitation(AcceptInviteRequestB(code=code, password="secret-123", first_name="П", last_name="Р"))

    results = run_parallel(w, call, call)
    ok = [r for r, e in results if e is None]
    bad = [e for _r, e in results if e is not None]
    assert len(ok) == 1 and len(bad) == 1 and "не найдено или уже использовано" in str(bad[0])
    assert w.db.query(User).filter(User.email == em(w, "p")).count() == 1
    first = invite(w, "owner", em(w, "q")).json()["code"]
    second = invite(w, "owner", em(w, "q")).json()["code"]
    w.db.expire_all()
    assert w.db.query(Invitation).filter_by(code=first).one().status == "cancelled" and w.db.query(Invitation).filter_by(code=second).one().status == "pending"


def test_r3_revoke(w):
    """[O10.R3]"""
    r = invite(w, "owner", em(w, "r")).json()
    assert client(w, "owner").delete(f"{API}/org/invitations/{r['id']}").status_code == 200
    late = accept(w, r["code"], first_name="Р", last_name="Р")
    assert late.status_code == 400 and "не найдено или уже использовано" in late.json()["detail"]


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
    code = invite(w, "owner", em(w, "e")).json()["code"]
    clock.set("2026-10-28 13:59")                                           # 1 minute before 14:00 + 7 days (UTC 09:00 + 7d = 28.10 09:00 -> 14:00 local)
    assert client(w, "owner").get(f"{API}/invite/{code}").status_code == 200
    clock.set("2026-10-28 14:00")                                           # exactly the expiry: expired
    gone = client(w, "owner").get(f"{API}/invite/{code}")
    assert gone.status_code == 404 and "истекло" in gone.json()["detail"]
    listing = client(w, "owner").get(f"{API}/org/invitations").json()
    assert [i["status"] for i in listing if i["code"] == code] == ["expired"]
    w.db.expire_all()
    assert not [i for i in w.db.query(Invitation).filter_by(org_id=w.org.id) if i.status == "pending" and i.expires_at <= datetime(2026, 10, 28, 9, 0)]
    code2 = invite(w, "owner", em(w, "e2")).json()["code"]
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
    assert out["expires_at"] == expected["expires_at"] and out["is_returning"] is True and out["status"] == "pending" and out["org_role"] == "teacher"
    info = client(w, "owner").get(f"{API}/invite/{out['code']}").json()
    assert info == {"org_name": "Алго Тест", "org_role": "teacher", "email": w.D.email, "is_returning": True}


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
    code = invite(w, "owner", w.D.email, role="admin").json()["code"]
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
    foreign_code = invite(w, "owner", w.foreign_admin.email).json()["code"]
    other = accept(w, foreign_code, first_name="Х", last_name="Х")
    assert other.status_code == 409 and other.json()["detail"]["code"] == "EMAIL_IN_OTHER_ORG"
    # a new account needs a name
    nameless = invite(w, "owner", em(w, "new")).json()["code"]
    assert accept(w, nameless).status_code == 400
