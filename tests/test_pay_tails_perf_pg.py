"""PAY-TAILS Б5.3: the list of 300 rows when EVERY row has a forecast (the charge of all of them begins later) against the same list when none has: the forecast is one more reading of the account of a row, no new loader.
The numbers are printed (``-s``); the test only keeps the cost within a loose bound (a regression of an order of magnitude)."""
import time
from datetime import date

from tests.money1b_helpers import add, fresh_student
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, owner  # noqa: F401  (fixtures)
from tests.test_pay_tails_upcoming_pg import world_om

pytestmark = needs_pg
D = date
N = 300


def timed(w, **params):
    best = None
    for _ in range(3):
        w.db.expire_all()
        start = time.perf_counter()
        r = owner(w).get(f"{API}/org/payments/status", params={"bucket": "active", "limit": 500, **params})
        took = time.perf_counter() - start
        assert r.status_code == 200, r.text
        best = took if best is None else min(best, took)
    return best, r.json()


def test_a_list_of_300_rows_with_a_forecast_on_every_row(world, fixed_today):
    g = world_om(world, fixed_today)
    g.max_students = None                                                        # a group of 300 (the setup of the world, not a step of the person)
    world.db.commit()
    ids = [fresh_student(world, f"Ученик{i:03d}") for i in range(N)]
    r = owner(world).post(f"{API}/org/groups/{g.id}/students/bulk", params={"mode": "add"}, json={"student_ids": [str(s.id) for s in ids]})
    assert r.status_code == 200 and r.json()["added"] == N, r.text[:300]
    with_forecast, page = timed(world)
    assert page["total"] == N and sum(1 for x in page["items"] if x["state"] == "starts") == N
    fixed_today(D(2026, 11, 24))                                                 # the charge has begun: no forecast on any row
    without, page2 = timed(world, bucket="to_pay")
    line = f"PERF rows={N} with forecast on every row: {with_forecast:.2f}s; the same list when nothing starts later: {without:.2f}s"
    print(line)
    import os
    if os.environ.get("PAYTAILS_PERF_FILE"):
        open(os.environ["PAYTAILS_PERF_FILE"], "a", encoding="utf-8").write(line + "\n")
    assert with_forecast < 60


def test_a_list_of_300_rows_when_every_account_has_three_lessons_cancelled_by_the_centre(world, fixed_today):
    # R4 (Б7.8 шаг 5): a cancel of a lesson is an event with its own moment, so a world is generated per moment for every account that has a cancel: 300 accounts x 3 cancels entered at three different moments,
    # every account has a payment.  The same list as above (``timed``: the best of three, the tab of the debtors: every account owes), read on 24.11.
    from tests.test_payments_service_pg import pay

    g = world_om(world, fixed_today, today=D(2026, 11, 3))
    g.max_students = None
    world.db.commit()
    assert owner(world).put(f"{API}/org/settings/payments", json={"centre_cancel_recalc": True}).status_code == 200
    ids = [fresh_student(world, f"Ученик{i:03d}") for i in range(N)]
    r = owner(world).post(f"{API}/org/groups/{g.id}/students/bulk", params={"mode": "add"}, json={"student_ids": [str(s.id) for s in ids]})
    assert r.status_code == 200 and r.json()["added"] == N, r.text[:300]
    for s in ids:
        pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 3))
    for entered, lesson_id, day in ((D(2026, 11, 9), "s2", "2026-11-10"), (D(2026, 11, 11), "s4", "2026-11-12"), (D(2026, 11, 12), "s5", "2026-11-13")):
        fixed_today(entered)
        r = owner(world).post(f"{API}/org/groups/{g.id}/attendance", json={"lessonDate": day, "lessonId": lesson_id, "status": "cancelled", "records": {}, "cancellationReason": "Преподаватель заболел", "cancellationKind": "centre"})
        assert r.status_code == 200, r.text
    fixed_today(D(2026, 11, 24))
    took, page = timed(world, bucket="debtors")                                  # every account owes: 600 000 - 300 000 - the deductions
    assert page["total"] == N
    line = f"PERF rows={N} with three cancelled lessons on every account: {took:.2f}s"
    print(line)
    import os
    if os.environ.get("PAYTAILS_PERF_FILE"):
        open(os.environ["PAYTAILS_PERF_FILE"], "a", encoding="utf-8").write(line + "\n")
    assert took < 60


def test_a_list_of_300_rows_where_a_third_of_the_students_left_and_came_back(world, fixed_today):
    # PAY-TAILS Б11.9 п. 5: every closed stay is an event of the account (one more world when there is a forgiveness): 300 accounts, a payment on every one, a third of them LEFT the group on 10.11 and were
    # taken back on 17.11 (two stays); the list of the debtors read on 24.11.
    from tests.test_payments_service_pg import pay

    g = world_om(world, fixed_today, today=D(2026, 11, 3))
    g.max_students = None
    world.db.commit()
    ids = [fresh_student(world, f"Ученик{i:03d}") for i in range(N)]
    r = owner(world).post(f"{API}/org/groups/{g.id}/students/bulk", params={"mode": "add"}, json={"student_ids": [str(s.id) for s in ids]})
    assert r.status_code == 200 and r.json()["added"] == N, r.text[:300]
    for s in ids:
        pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 3))
    fixed_today(D(2026, 11, 10))
    for s in ids[::3]:
        assert owner(world).post(f"{API}/org/groups/{g.id}/students/{s.id}/leave", json={}).status_code == 200
    fixed_today(D(2026, 11, 17))
    for s in ids[::3]:
        add(world, s, group=g, mode="add")
    fixed_today(D(2026, 11, 24))
    took, page = timed(world, bucket="debtors")
    assert page["total"] == N
    line = f"PERF rows={N} where a third of the students left and came back: {took:.2f}s"
    print(line)
    import os
    if os.environ.get("PAYTAILS_PERF_FILE"):
        open(os.environ["PAYTAILS_PERF_FILE"], "a", encoding="utf-8").write(line + "\n")
    assert took < 60


def test_the_nightly_run_over_300_accounts_with_a_forgiveness_before_and_after_i6(world, fixed_today, monkeypatch):
    """PAY-TAILS Б10: the cost of the probes of I-6 in the night: the same run over one centre of 300 accounts, EVERY with a payment and a forgiveness, without the probes (as before) and with them.
    The numbers are printed (``-s``); the test keeps the cost within a loose bound, and no I-6 on honest accounts."""
    from src.auth import money_reconcile as mr
    from tests.test_payments_service_pg import mid, pay

    g = world_om(world, fixed_today, today=D(2026, 11, 3))
    g.max_students = None
    world.db.commit()
    ids = [fresh_student(world, f"Ученик{i:03d}") for i in range(N)]
    r = owner(world).post(f"{API}/org/groups/{g.id}/students/bulk", params={"mode": "add"}, json={"student_ids": [str(s.id) for s in ids]})
    assert r.status_code == 200 and r.json()["added"] == N, r.text[:300]
    for s in ids:
        pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 3))
    fixed_today(D(2026, 11, 24))
    for s in ids:
        r = owner(world).post(f"{API}/org/memberships/{mid(world, s, g)}/writeoffs", json={"amount": 50_000, "reason": "болезнь", "effective_on": "2026-11-24"})
        assert r.status_code == 201, r.text

    def run_once():
        world.db.expire_all()
        start = time.perf_counter()
        found = mr.reconcile_org(world.db, world.org.id)
        return time.perf_counter() - start, found

    with monkeypatch.context() as m:
        m.setattr(mr, "check_probes", lambda *a, **k: [])
        before = min(run_once()[0] for _ in range(3))
    results = [run_once() for _ in range(3)]
    after = min(t for t, _ in results)
    assert [f for f in results[0][1] if f.invariant == "I-6"] == []
    line = f"PERF night rows={N} with a forgiveness on every account: before I-6 {before:.2f}s, with I-6 {after:.2f}s"
    print(line)
    import os
    if os.environ.get("PAYTAILS_PERF_FILE"):
        open(os.environ["PAYTAILS_PERF_FILE"], "a", encoding="utf-8").write(line + "\n")
    assert after < 120
