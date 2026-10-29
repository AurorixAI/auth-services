"""#159 round of fixes (PostgreSQL): M2 - the counter of the tab «Ждут решения» is what the CALLER has to do; M3 - the label of the money by the right of money on EVERY road that gives it (LESSONS 51).
Roads of ``moneyLabel`` to an owner / admin: the creation answer, ``GET schedule/occurrences``, the journal of the group.  (The student card, the notices and the dashboard of a teacher never give the
money of a manager - their tests are in ``test_p154_create_pg.py``.)  The world is D2 of LOGIC-B2; the admin has NO right until a test grants it."""
import pytest

from tests.b2_helpers import API, build_d2, clock, needs_pg, pg_session  # noqa: F401  (fixtures)
from tests.ops_helpers import client, grant_money_right
from tests.test_p154_create_pg import make
from tests.test_p154_money_pg import hold

pytestmark = needs_pg
BEFORE, AFTER, DAY = "оплата: решит владелец", "оплата: не решено", "2026-10-29"
PARAMS = {"date_from": DAY, "date_to": DAY}


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")
    return build_d2(pg_session)


def summary(w, who):
    return client(w, who).get(f"{API}/org/substitutions/summary").json()


def labels(w, who, lid):
    """``moneyLabel`` of the lesson on the two read roads (the occurrences of the schedule, the journal of the group)."""
    occ = next(o for o in client(w, who).get(f"{API}/org/schedule/occurrences", params=PARAMS).json() if o["id"] == lid)
    journal = client(w, who).get(f"{API}/org/groups/{w.G1.id}/attendance", params=PARAMS).json()
    return occ["moneyLabel"], [l["moneyLabel"] for l in journal["lessons"] if l.get("id") == lid][0]


# ===================================================================================== M2: the counter is the tasks of the caller
def test_m2_the_counter_of_waiting_has_no_money_rows_for_an_admin_without_the_right(w):
    lid = make(w, "A", w.G1, DAY, start="15:00", end="16:00", purpose="extra", money="wait").json()["id"]
    hold(w, w.G1, lid, DAY, {}, start="15:00", end="16:00")
    admin = summary(w, "admin")
    assert (admin["oneOffDecisionsTotal"], admin["pendingTotal"], admin["waitingTotal"]) == (1, 0, 0)        # the row is information, not his task
    assert client(w, "admin").get(f"{API}/org/one-off-decisions").json()["total"] == 1                       # ... and the queue still shows it
    assert summary(w, "owner")["waitingTotal"] == 1
    grant_money_right(w)
    assert (summary(w, "admin")["oneOffDecisionsTotal"], summary(w, "admin")["waitingTotal"]) == (1, 1)     # with the right it is his task
    grant_money_right(w, False)
    assert summary(w, "admin")["waitingTotal"] == 0                                                         # taken away: read at the call


# ===================================================================================== M3: the label by the right, on every road
def test_m3_the_label_before_the_lesson_says_who_decides_on_every_road(w):
    created = make(w, "admin", w.G1, DAY, start="15:00", end="16:00", purpose="extra", money="wait").json()          # the creation answer (an admin without the right)
    lid = created["id"]
    assert created["moneyLabel"] == BEFORE
    assert labels(w, "admin", lid) == (BEFORE, BEFORE)
    assert labels(w, "owner", lid) == ("оплата: решите после урока",) * 2                                    # the owner decides: as before
    assert labels(w, "A", lid) == ("не решено",) * 2                                                         # a teacher: as before
    grant_money_right(w)
    assert labels(w, "admin", lid) == ("оплата: решите после урока",) * 2                                    # the right is back: the old words


def test_m3_after_the_lesson_the_state_is_a_fact_for_everybody(w):
    lid = make(w, "A", w.G1, DAY, start="15:00", end="16:00", purpose="extra", money="wait").json()["id"]
    hold(w, w.G1, lid, DAY, {}, start="15:00", end="16:00")
    assert labels(w, "admin", lid) == (AFTER, AFTER) and labels(w, "owner", lid) == (AFTER, AFTER)
