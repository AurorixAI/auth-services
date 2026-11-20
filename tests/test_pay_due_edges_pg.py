"""PAY-DUE «Е», second part: the edges of the term and of the packages that had no test (PostgreSQL, through the routes of the product, numbers worked out by hand, the clock is moved explicitly).

World A (``pay_due_helpers.world``): Mon 12.10.2026, «Свой месяц», 600 000 each, Mon / Wed / Fri, term 5 days: A joined 01.10 (D 05.10: a debt), B 06.10 (D 10.10: a debt), C 09.10 (D 13.10: «к оплате»).
World B (packages): ``period_mode = lessons`` of 8 lessons for 600 000 = 75 000 a lesson; the unpaid lessons are the held ones: Fri 02.10, Mon 05.10, Wed 07.10 (3 x 75 000)."""
from datetime import date

from tests.money1b_helpers import add, fresh_student, hold_lessons
from tests.ops_helpers import new_group
from tests.payf_helpers import slots
from tests.pay_due_helpers import API, D, client, put_settings, status_page, summary, world, clock, pg_session, needs_pg  # noqa: F401  (fixtures)
from tests.pg_world import give_group_price

pytestmark = needs_pg


def row_of(page, membership):
    (row,) = [r for r in page["items"] if r["membership_id"] == str(membership.id)]
    return row


def package_world(pg_session, clock, **block):
    w, g, st, ms = world(pg_session, clock, joins=(D(2026, 10, 1),), period_mode="lessons", lessons_per_payment=8, **block)
    hold_lessons(w, st[0], [D(2026, 10, 2), D(2026, 10, 5), D(2026, 10, 7)], group=g)
    return w, g, st, ms


def test_the_term_of_zero_and_of_fourteen_days_move_the_whole_centre_between_the_names(pg_session, clock):
    w, g, st, ms = world(pg_session, clock)
    put_settings(w, pay_due_days=0)                                    # a debt from the first day: C (D would be 13.10) is a debtor too
    s = summary(w)
    assert (s["debtors"], s["due_students"], s["debt_total"], s["due_total"]) == (3, 0, 1_800_000, 0)
    put_settings(w, pay_due_days=14)                                   # A: D = 14.10, B: 19.10, C: 22.10 - nobody owes yet
    s = summary(w)
    assert (s["debtors"], s["due_students"], s["debt_total"], s["due_total"]) == (0, 3, 0, 1_800_000)
    assert s["due_nearest"]["date"] == "2026-10-14"


def test_the_packages_with_m_zero_and_m_four(pg_session, clock):
    w, g, st, ms = package_world(pg_session, clock)
    put_settings(w, pay_due_lessons=0)                                 # M = 0: every unpaid lesson is a debt at once
    a = row_of(status_page(w, "all"), ms[0])
    assert (a["debt_amount"], a["due_amount"], a["state"]) == (225_000, 0, "debt")
    put_settings(w, pay_due_lessons=4)                                 # M = 4: all three are among the newest (the limit of 14 days: 02.10 + 14 = 16.10 has not come)
    a = row_of(status_page(w, "all"), ms[0])
    assert (a["debt_amount"], a["due_amount"], a["due_until"], a["state"]) == (0, 225_000, "2026-10-16", "to_pay")
    clock.set("2026-10-17 09:00")                                      # the day after 02.10 + 14: the oldest is a debt whatever M says
    a = row_of(status_page(w, "all"), ms[0])
    assert (a["debt_amount"], a["due_amount"]) == (75_000, 150_000)


def test_a_package_participation_that_is_closed_owes_everything_at_once(pg_session, clock):
    w, g, st, ms = package_world(pg_session, clock)
    a = row_of(status_page(w, "all"), ms[0])
    assert (a["debt_amount"], a["due_amount"]) == (75_000, 150_000)    # before leaving: the newest two are «к оплате»
    r = client(w, "owner").post(f"{API}/org/groups/{g.id}/students/{st[0].id}/leave", json={})
    assert r.status_code == 200, r.text
    a = row_of(status_page(w, "debtors", scope="all"), ms[0])
    assert (a["debt_amount"], a["due_amount"], a["state"]) == (225_000, 0, "debt")          # the stay is closed: the whole unpaid sum is a debt at once (К3а)


def test_a_transfer_of_a_package_student_makes_the_unpaid_lessons_of_the_old_group_a_debt(pg_session, clock):
    w, g, st, ms = package_world(pg_session, clock)
    other = new_group(w.db, w.org, w.ids, "G1", "Вторая", w.A, slots())
    give_group_price(w.db, other, 600_000, valid_from=D(2026, 1, 1))
    w.db.commit()
    add(w, st[0], group=other, mode="transfer", from_group_id=str(g.id))
    same_day = row_of(status_page(w, "debtors"), ms[0])
    assert (same_day["debt_amount"], same_day["due_amount"]) == (75_000, 150_000)    # the day of the transfer: the old stay lasts through it, the lessons are counted as before
    clock.set("2026-10-13 09:00")                                      # the next day the stay of the old group is closed
    old = row_of(status_page(w, "debtors"), ms[0])                     # the tabs of an unpaid sum read the closed participations too
    assert (old["debt_amount"], old["due_amount"], old["state"]) == (225_000, 0, "debt")     # К3а: the three lessons of a closed package stay are a debt, not «к оплате»


def package_names(w, m):
    """What the package participation says today: (debt, to pay, nearest day of «к оплате») from the list the administrator reads."""
    a = row_of(status_page(w, "all"), m)
    return a["debt_amount"], a["due_amount"], a["due_until"]


def test_a_switch_from_packages_to_the_calendar_makes_the_whole_unpaid_package_a_debt_from_the_next_day(pg_session, clock):
    """К3б.  A package of 3 lessons for 600 000 (200 000 a lesson), all three held (Fri 02.10, Mon 05.10, Wed 07.10) and unpaid; M = 2 (the default).  The owner switches the centre to the calendar on Mon 12.10.
    The package window ends that day: on 12.10 (to 23:50) the package is counted as before - 02.10 is a debt (the newest two are «к оплате»); from 13.10 00:10 EVERY unpaid lesson of it is a debt, and the new
    calendar period (600 000 from 13.10, the term of 5 days) is «к оплате».  The money of the package does not change across the switch (the names only)."""
    w, g, st, ms = world(pg_session, clock, joins=(D(2026, 10, 1),), period_mode="lessons", lessons_per_payment=3)
    hold_lessons(w, st[0], [D(2026, 10, 2), D(2026, 10, 5), D(2026, 10, 7)], group=g)
    assert package_names(w, ms[0]) == (200_000, 400_000, "2026-10-19")                 # 02.10 is out of the newest two; the oldest of the other two is 05.10: + 14 days
    put_settings(w, period_mode="calendar")                                            # the change of the mode: deferred, the running package ends today
    clock.set("2026-10-12 23:50")
    assert package_names(w, ms[0]) == (200_000, 400_000, "2026-10-19")                 # the last minutes of the window: nothing has changed
    clock.set("2026-10-13 00:10")
    s = [r for r in client(w, "owner").get(f"{API}/org/memberships/{ms[0].id}/statement").json()["rows"] if r["type"] == "charge"]
    package, calendar = s
    assert (package["open_name"], package["open_amount"], [(p["name"], p["amount"]) for p in package["open_parts"]]) == ("debt", 600_000, [("debt", 600_000)])      # all three lessons, no «к оплате» part
    assert package["open_sub"] == "срок был 06.10"                                    # the term of the oldest lesson that left the newest two (07.10 - 1 day)
    assert (calendar["open_name"], calendar["open_amount"], calendar["open_sub"]) == ("due", 600_000, "до 17.10")         # the new calendar period: 13.10 + 5 days
    total = summary(w)
    assert (total["debt_total"], total["due_total"], total["debtors"]) == (600_000, 600_000, 1)
    a = row_of(status_page(w, "debtors"), ms[0])
    assert (a["debt_amount"], a["due_amount"]) == (600_000, 600_000)                  # one participation, both names, the rows add up to the card


def test_a_lesson_marked_after_the_fact_moves_the_names_and_a_held_lesson_cannot_be_cancelled_to_take_them_back(pg_session, clock):
    """Package of 8 lessons for 600 000 (75 000 a lesson), held and unpaid: Fri 02.10, Mon 05.10, Wed 07.10; M = 2, today Mon 12.10.  Through the routes of the product: the teacher opens the journal of Fri 09.10
    backwards and marks the lesson as held three days late - the fourth unpaid lesson pushes 05.10 out of the newest two: debt 150 000 (02.10, 05.10), «к оплате» 150 000 (07.10, 09.10) until 21.10 (07.10 + 14).
    «Урока не было» for the held lesson is refused (409), and nothing moves; a correction of the mark to «excused» does not take the lesson off the package either (the package counts the held lesson, not the
    presence): the product has NO road that returns the names, which is why the marking is the only step that changes them (found 20.11, in the final report)."""
    from tests.payf_helpers import lesson_id_on

    w, g, st, ms = package_world(pg_session, clock)
    before = package_names(w, ms[0])
    assert before == (75_000, 150_000, "2026-10-19")
    friday = D(2026, 10, 9)
    lesson = {"lessonDate": friday.isoformat(), "lessonId": lesson_id_on(friday)}
    opened = client(w, "owner").post(f"{API}/org/groups/{g.id}/attendance/reconcile", json=lesson)           # the day has passed: the journal of a lesson never begun is opened backwards
    assert opened.status_code == 200, opened.text
    assert package_names(w, ms[0]) == before                                            # opened, not held yet: nothing moves
    r = client(w, "owner").post(f"{API}/org/groups/{g.id}/attendance", json={**lesson, "status": "completed", "records": {str(st[0].id): "present"}, "expectedVersion": opened.json()["version"]})
    assert r.status_code == 200, r.text
    assert package_names(w, ms[0]) == (150_000, 150_000, "2026-10-21")
    total = summary(w)
    assert (total["debt_total"], total["due_total"], total["debtors"]) == (150_000, 150_000, 1)
    refused = client(w, "owner").post(f"{API}/org/groups/{g.id}/attendance", json={**lesson, "status": "cancelled", "records": {}, "cancellationReason": "Урока не было", "expectedVersion": r.json()["version"]})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "LESSON_ALREADY_STARTED"            # a held lesson is not cancelled
    assert package_names(w, ms[0]) == (150_000, 150_000, "2026-10-21")                                            # the refusal changed nothing
