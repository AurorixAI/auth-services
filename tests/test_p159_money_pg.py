"""#159 (PostgreSQL): the money of a teacher is decided by the owner and by an admin WITH the right of payments (chains Ч1-Ч4, invariant 1); a make-up never waits for a decision (Ч5-Ч7 are in
``test_p154_money_pg.py`` next to the chains of #154).  The right is the existing setting ``organizations.settings.payments.admin_can_manage`` (nothing new).
The world is D2 of LOGIC-B2 (``test_p154_create_pg.py``): Wednesday 21.10.2026 14:00, G1 «9-А» with teacher A; the admin has NO right until a test grants it."""
import pytest

from src.models.auth import GroupAudit, GroupScheduleOneOffLesson
from tests.b2_helpers import API, build_d2, clock, needs_pg, pg_session  # noqa: F401  (fixtures)
from tests.ops_helpers import client, grant_money_right
from tests.test_p154_create_pg import code, make, makeup, cancel_origin, one_off
from tests.test_p154_money_pg import decide, hold

pytestmark = needs_pg
TEXT = "Решение об оплате принимает владелец или администратор с правом на оплаты"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")
    return build_d2(pg_session)                           # the admin has no right


def waiting(w, day="2026-10-29", start="15:00", end="16:00"):
    """A held additional lesson that the teacher asked to be paid (``wait``)."""
    lid = make(w, "A", w.G1, day, start=start, end=end, purpose="extra", money="wait").json()["id"]
    hold(w, w.G1, lid, day, {}, start=start, end=end)
    return lid


def decided_rows(w):
    w.db.expire_all()
    return w.db.query(GroupAudit).filter_by(org_id=w.org.id, action="one_off_money_decided").all()


def created_count(w):
    w.db.expire_all()
    return w.db.query(GroupScheduleOneOffLesson).filter_by(group_id=w.G1.id).count()


# ===================================================================================== Ч1: an admin without the right - the queue is seen, the money is not decided
def test_ch1_an_admin_without_the_right_sees_the_queue_but_the_server_refuses_the_decision(w):
    lid = waiting(w)
    queue = client(w, "admin").get(f"{API}/org/one-off-decisions").json()
    assert queue["total"] == 1 and queue["items"][0]["lessonId"] == lid and queue["items"][0]["payrollSheet"] in (None, {"month": "2026-10", "status": None})
    assert client(w, "admin").get(f"{API}/org/substitutions/summary").json()["oneOffDecisionsTotal"] == 1
    for decision in ("pay", "skip"):
        r = decide(w, "admin", lid, decision, expect=403)
        assert r.json()["detail"] == {"code": "MONEY_RIGHT_REQUIRED", "message": TEXT}
    assert one_off(w, lid).money == "wait" and decided_rows(w) == []                         # nothing changed, nothing written
    assert client(w, "admin").get(f"{API}/org/one-off-decisions").json()["total"] == 1
    # the owner needs no setting
    assert decide(w, "owner", lid, "pay").json()["money"] == "paid"
    (entry,) = decided_rows(w)
    assert (entry.actor_id, entry.actor_role) == (w.owner.id, "owner")


def test_ch1_the_flag_must_be_exactly_true_and_a_teacher_never_decides(w):
    lid = waiting(w)
    org = w.org
    org.settings = {"payments": {"admin_can_manage": "true"}}                                # a string is not the right (the very rule of the payments)
    w.db.commit()
    assert decide(w, "admin", lid, "pay", expect=403).json()["detail"]["code"] == "MONEY_RIGHT_REQUIRED"
    org.settings = {"payments": {"admin_can_manage": False}}
    w.db.commit()
    assert decide(w, "admin", lid, "pay", expect=403).json()["detail"]["code"] == "MONEY_RIGHT_REQUIRED"
    assert decide(w, "A", lid, "pay", expect=403) and one_off(w, lid).money == "wait"


# ===================================================================================== Ч2: the owner switched the right on - the same admin decides, the journal names him
def test_ch2_with_the_right_the_same_admin_decides_and_the_audit_names_him(w):
    lid = waiting(w)
    assert decide(w, "admin", lid, "pay", expect=403)
    grant_money_right(w)
    out = decide(w, "admin", lid, "pay").json()
    assert out["money"] == "paid" and one_off(w, lid).money == "paid"
    (entry,) = decided_rows(w)
    assert (entry.actor_id, entry.actor_role, entry.snapshot["decision"], entry.snapshot["from"], entry.snapshot["to"]) == (w.admin.id, "admin", "pay", "wait", "paid")
    assert decide(w, "admin", lid, "skip", expect=409).json()["detail"]["code"] == "ALREADY_DECIDED"


# ===================================================================================== Ч3: the right is taken away between opening the window and pressing the button
def test_ch3_a_right_taken_away_before_the_press_is_a_403_with_the_words_and_nothing_changes(w):
    lid = waiting(w)
    grant_money_right(w)
    assert client(w, "admin").get(f"{API}/org/one-off-decisions").json()["total"] == 1       # the window was opened with the right
    grant_money_right(w, False)                                                              # the owner switched it off
    r = decide(w, "admin", lid, "pay", expect=403)
    assert r.json()["detail"] == {"code": "MONEY_RIGHT_REQUIRED", "message": TEXT}
    assert one_off(w, lid).money == "wait" and decided_rows(w) == []
    assert client(w, "admin").get(f"{API}/org/one-off-decisions").json()["total"] == 1       # the queue is still seen: the window can be closed and re-read


# ===================================================================================== Ч4: an admin without the right creates an additional lesson as a teacher does
def test_ch4_an_admin_without_the_right_chooses_wait_or_none_and_paid_is_a_403_that_writes_nothing(w):
    before = created_count(w)
    r = make(w, "admin", w.G1, "2026-10-30", purpose="extra", money="paid", expect=403)
    assert r.json()["detail"] == {"code": "MONEY_RIGHT_REQUIRED", "message": TEXT}
    assert created_count(w) == before                                                        # no lesson, no audit row of a creation
    assert w.db.query(GroupAudit).filter_by(org_id=w.org.id, action="one_off_created").count() == 0
    default = make(w, "admin", w.G1, "2026-10-30", purpose="extra").json()                   # the default of the form: «Попросить оплатить»
    assert (default["money"], default["moneyLabel"]) == ("wait", "оплата: решит владелец") and one_off(w, default["id"]).money == "wait"
    free = make(w, "admin", w.G1, "2026-10-30", start="17:00", end="17:30", purpose="extra", money="none").json()
    assert one_off(w, free["id"]).money == "none"
    asked = make(w, "admin", w.G1, "2026-10-31", purpose="extra", money="wait").json()["id"]
    hold(w, w.G1, asked, "2026-10-31", {}, start="15:00", end="16:30")
    assert [i["lessonId"] for i in client(w, "owner").get(f"{API}/org/one-off-decisions").json()["items"]] == [asked]       # a row in «Ждут решения»
    # the owner chooses without a setting; a teacher still cannot ask for paid (422, as in #154)
    assert one_off(w, make(w, "owner", w.G1, "2026-11-02", purpose="extra", money="paid").json()["id"]).money == "paid"
    assert make(w, "A", w.G1, "2026-11-02", start="17:00", end="17:30", purpose="extra", money="paid", expect=422).json()["detail"]["code"] == "MONEY_INVALID"


def test_ch4_with_the_right_the_admin_chooses_none_or_paid_as_before(w):
    grant_money_right(w)
    assert make(w, "admin", w.G1, "2026-10-30", purpose="extra", expect=422).json()["detail"]["code"] == "MONEY_REQUIRED"
    assert make(w, "admin", w.G1, "2026-10-30", purpose="extra", money="wait", expect=422).json()["detail"]["code"] == "MONEY_INVALID"
    assert one_off(w, make(w, "admin", w.G1, "2026-10-30", purpose="extra", money="paid").json()["id"]).money == "paid"
    grant_money_right(w, False)                                                              # taken away: the same admin is a teacher again
    assert make(w, "admin", w.G1, "2026-11-02", purpose="extra", money="paid", expect=403).json()["detail"]["code"] == "MONEY_RIGHT_REQUIRED"


def test_a_make_up_has_no_money_for_anybody_and_does_not_need_the_right(w):
    cancel_origin(w)
    assert makeup(w, "admin", w.G1, "2026-10-29", start="12:00", end="13:00", money="paid", expect=422).json()["detail"]["code"] == "MONEY_INVALID"
    mk = makeup(w, "admin", w.G1, "2026-10-29", start="12:00", end="13:00").json()
    assert (mk["purpose"], mk["money"], mk["moneyLabel"]) == ("makeup", None, None) and one_off(w, mk["id"]).money is None
