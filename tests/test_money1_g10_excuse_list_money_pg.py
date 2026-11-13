"""Money-1 G-10 (MONEY1_G1_REVIEW П5/T30): the list of certificates shows ``deadline`` and ``billing_mode`` - the account of the student - only to the money right;
without it the list is the reason, the lesson and the status."""
from tests.ops_helpers import grant_money_right
from tests.pg_world import make_client, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_excuse_billing_mode_pg import excuse_all
from tests.test_excuse_deadline import API, listing
from tests.test_payments_service_pg import day, pay, set_settings

pytestmark = needs_pg


def _seed(w):
    set_settings(w, period_mode="lessons", lessons_per_payment=12)
    pay(w, w.students[0], paid_on=day(-30), lessons_count=12)
    excuse_all(w)


def test_with_the_money_right_the_list_carries_the_account(world):
    grant_money_right(world)
    _seed(world)
    paid = next(i for i in listing(world) if i["student"]["id"] == str(world.students[0].id))
    assert paid["billing_mode"] == "package" and paid["deadline"] is not None


def test_without_the_money_right_the_list_has_no_deadline_and_no_mode_but_everything_else(world):
    grant_money_right(world)
    _seed(world)
    with_right = {i["id"]: i for i in listing(world)}
    grant_money_right(world, False)
    queue = make_client(world).get(f"{API}/org/attendance/excuses").json()["items"]               # no dates: the queue of all periods
    for items in (queue, listing(world), listing(world, status="pending")):
        assert items and all(i["deadline"] is None and i["billing_mode"] is None and i["deadline_at_decision"] is None for i in items)
        for i in items:
            full = with_right[i["id"]]
            assert {k: v for k, v in i.items() if k not in ("deadline", "billing_mode", "deadline_at_decision")} == \
                   {k: v for k, v in full.items() if k not in ("deadline", "billing_mode", "deadline_at_decision")}


# ---- Г-10b: the same for the journal of attendance ---------------------------------------------------------------------------------------------------

def test_the_journal_carries_the_account_of_an_excuse_only_with_the_right_and_a_teacher_never(world):
    from datetime import timedelta

    from tests.test_excuse_deadline import business_today

    grant_money_right(world)
    _seed(world)

    path = f"{API}/org/groups/{world.group.id}/attendance"
    window = {"date_from": (business_today() - timedelta(days=5)).isoformat(), "date_to": business_today().isoformat()}

    def excuses(user):
        got = make_client(world, user=user).get(path, params=window)
        assert got.status_code == 200, got.text
        (by_student,) = got.json()["excuses"].values()
        return by_student

    sid = str(world.students[0].id)
    assert excuses(world.admin)[sid]["billing_mode"] == "package"
    grant_money_right(world, False)
    for entry in excuses(world.admin).values():
        assert entry["billing_mode"] is None and entry["deadline_at_decision"] is None and "reason" in entry and "status" in entry
    assert all("billing_mode" not in e for e in excuses(world.teacher).values())


# ---- Р-9 (R3 М5): «решено после срока» is the account of the student too ------------------------------------------------------------------------------

def test_r9_the_mark_decided_after_the_deadline_is_for_the_money_right_in_the_list_and_in_the_journal(world):
    from datetime import timedelta

    from src.models.auth import AttendanceExcuse
    from tests.test_excuse_deadline import business_today

    grant_money_right(world)
    _seed(world)
    world.db.query(AttendanceExcuse).update({"status": "confirmed", "decided_after_deadline": True})
    world.db.commit()
    path = f"{API}/org/groups/{world.group.id}/attendance"
    window = {"date_from": (business_today() - timedelta(days=5)).isoformat(), "date_to": business_today().isoformat()}

    def marks():
        listed = {i["decided_after_deadline"] for i in listing(world)}
        journal = {e["decided_after_deadline"] for by_student in make_client(world, user=world.admin).get(path, params=window).json()["excuses"].values() for e in by_student.values()}
        return listed, journal

    assert marks() == ({True}, {True})
    grant_money_right(world, False)
    assert marks() == ({None}, {None})                                    # before: {True}, {True} - the admin without the right read who was late
