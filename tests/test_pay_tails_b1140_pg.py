"""PAY-TAILS Б11.40 (step 8.14.9): the lines of a pause «период · уроков → сумма» (``charge_rows``) and the days of a period that is not charged yet (``charge_later``).  PostgreSQL, through the routes of the
product (LESSONS 135).  The FOUR numbers of lesson 168 in every world: the probe, the answer of the save (the lines and ``impact.charged_delta``), and the account after (the charge after - the charge before);
the sum of the lines is the change of the charge (Б11.40 С4').

World (ОМ): 600 000 a month, Tue / Thu / Fri, billing day 1; November 2026 has 12 lessons (3 5 6 10 12 13 17 19 20 24 26 27), 50 000 each (the whole month is 600 000); December 2026 has 14 lessons
(1 3 4 8 10 11 15 17 18 22 24 25 29 31).  The working date 24.11 unless a world says another.  Hand counts, world by world (the table lives in the test of the world):
- a lesson that leaves the price of a whole month leaves it by the rule of the month: the price x (lessons left / lessons of the month), whole thousands, a half goes up (``payment_status.prorated``);
- December 12 of 14 lessons: 600 000 x 12 / 14 = 514 285.7 -> 514 000; the change of December is 514 000 - 600 000 = -86 000."""
from datetime import date

from src.auth.logica_service import PauseService
from src.auth.payments_schemas import PauseCreate
from tests.answers_dump import dump
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixtures)
from tests.test_pay_tails_b11_pg import om_at
from tests.test_pay_tails_b13_pg import edit_pause  # noqa: F401
from tests.test_pay_tails_b77_pg import pause_on
from tests.test_pay_tails_money_pg import forgive, led, lp
from tests.test_payments_service_pg import pay

pytestmark = needs_pg
D = date


def lines(body):
    """The lines of an answer as tuples: (label, first, last, days, lessons, lessons_passed, amount)."""
    return [(r["label"], r["first_day"], r["last_day"], r["days"], r["lessons"], r["lessons_passed"], r["amount"]) for r in body["charge_rows"]]


def later_of(body):
    x = body["charge_later"]
    return None if x is None else (x["label"], x["first_day"], x["last_day"])


def begins_of(body):
    """Б11.43: ``period_begins`` of every line, as ISO text (null = the line of a period that began on both sides or is not carried across the working date)."""
    return [r["period_begins"] for r in body["charge_rows"]]


def make(world, s, g, first, last):
    url = f"{API}/org/memberships/{membership(world, s, g).id}/pauses"
    body = {"first_day": first, "last_day": last, "reason": "Болезнь"}
    return lambda dry: owner(world).post(url, params={"dry_run": "true"} if dry else {}, json=body)


def edit(world, pause_id, first, last):
    body = {"first_day": first, "last_day": last, "reason": "Поправили"}
    return lambda dry: owner(world).patch(f"{API}/org/pauses/{pause_id}", params={"dry_run": "true"} if dry else {}, json=body)


def void(world, pause_id):
    return lambda dry: owner(world).post(f"{API}/org/pauses/{pause_id}/void", params={"dry_run": "true"} if dry else {}, json={"reason": "Передумали"})


def act(world, s, g, today, call, rows, later, delta, name=None, begins=None):
    """The probe and the save of one action and the FOUR numbers: the lines of the probe = the lines of the save = ``rows`` (the hand count); ``charge_later``; the sum of the lines = ``impact.charged_delta`` of
    the probe = of the save = the charge of the account after - the charge before = ``delta``; ``period_begins`` of every line (Б11.43) = ``begins`` (default: null in every line)."""
    before = led(world, s, g, today).charged
    probe, saved = call(True), call(False)
    assert probe.status_code in (200, 201) and saved.status_code in (200, 201), (probe.text[:300], saved.text[:300])
    p, a = probe.json(), saved.json()
    if name:
        dump(name, p)                                                   # the REAL answer of the probe for the screens (``$ANSWERS_DIR``; nothing is written without it)
    after = led(world, s, g, today).charged
    assert lines(p) == lines(a) == rows, ("GOT probe / save", lines(p), lines(a))
    assert begins_of(p) == begins_of(a) == (begins if begins is not None else [None] * len(rows)), ("GOT period_begins", begins_of(p), begins_of(a))
    assert later_of(p) == later_of(a) == later, (later_of(p), later_of(a))
    assert p["impact"]["charged_delta"] == a["impact"]["charged_delta"] == after - before == delta, (p["impact"]["charged_delta"], a["impact"]["charged_delta"], after - before)
    if rows:
        assert sum(r[6] for r in rows) == delta
    return a


def test_c1_a_week_inside_the_month_is_one_line_three_lessons(world, fixed_today):
    # 24.11 - 30.11 takes 24 26 27 (3 of 12): 9 x 50 000 = 450 000; the change -150 000.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 24))
    act(world, s, g, D(2026, 11, 24), make(world, s, g, "2026-11-24", "2026-11-30"), [("24.11–30.11", "2026-11-24", "2026-11-30", 7, 3, None, -150_000)], None, -150_000, name="pause_probe_create_week")


def test_c2_two_months_december_not_charged_one_line_and_the_rest_later(world, fixed_today):
    # 24.11 - 03.12: November -150 000 (3 lessons); December is not charged (nobody paid for it, it is not begun): no line, the days 01.12 - 03.12 are ``charge_later``; the sum = the change = -150 000.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 24))
    act(world, s, g, D(2026, 11, 24), make(world, s, g, "2026-11-24", "2026-12-03"), [("24.11–30.11", "2026-11-24", "2026-11-30", 7, 3, None, -150_000)], ("01.12–03.12", "2026-12-01", "2026-12-03"), -150_000, name="pause_probe_create_two_months")


def test_c2_two_months_december_is_an_advance_no_false_line_of_december_or_january(world, fixed_today):
    # 05.11 paid 1 100 000 (November 600 000 + December 600 000 do not cover it all: the horizon holds January only after the pause: 450 000 + 600 000 < 1 100 000).  The pause 24.11 - 03.12: one line
    # (November -150 000); December is built (an advance) but not begun: it has no line; January that appears only by the shift of the advance has none; ``charge_later`` 01.12 - 03.12; the sum -150 000.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 5))
    pay(world, s, g, amount=1_100_000, paid_on=D(2026, 11, 5))
    fixed_today(D(2026, 11, 24))
    act(world, s, g, D(2026, 11, 24), make(world, s, g, "2026-11-24", "2026-12-03"), [("24.11–30.11", "2026-11-24", "2026-11-30", 7, 3, None, -150_000)], ("01.12–03.12", "2026-12-01", "2026-12-03"), -150_000)


def test_c3_working_date_02_12_the_period_that_the_pause_moves_to_the_future_gives_its_whole_charge_back(world, fixed_today):
    # The working date 02.12, the pause 24.11 - 03.12 put back in time (on 24, 26, 27.11 and 01.12 the student has no mark «был»).  THE TZ SAYS (Б11.40 С6') December = 600 000 - 514 000 = -86 000; the CODE: the
    # first billable day of December after the pause is 04.12, LATER than the working date, so the period has not begun in the account after (``begun`` = the first billable day <= today) and charges nothing:
    # before 600 000 (begun 01.12), after 0.  So the line of December is -600 000 (2 lessons, 3 days) and the change of the charge is -150 000 - 600 000 = -750 000 (the same in the probe, the save, the account).
    # November: 24 26 27 leave -> 9 of 12 -> 450 000: -150 000, 3 lessons, 7 days.  The sum -750 000 = the change of the charge (the insurance of С4' holds).
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 12, 2))
    act(world, s, g, D(2026, 12, 2), make(world, s, g, "2026-11-24", "2026-12-03"),
        [("24.11–30.11", "2026-11-24", "2026-11-30", 7, 3, None, -150_000), ("01.12–03.12", "2026-12-01", "2026-12-03", 3, 2, None, -600_000)], None, -750_000, name="pause_probe_create_two_months_begun_0212",
        begins=[None, "2026-12-04"])         # Б11.43 п. 1, world 1: the first paid day of December after the pause is 04.12 (> 02.12): December is carried out of the price; November began on both sides


def test_c3_working_date_04_12_both_months_begun_two_lines_december_by_the_rounding_of_the_product(world, fixed_today):
    # The working date 04.12: the first billable day of December after the pause is 04.12 = today: the period has begun in both accounts.
    # November: 24 26 27 leave -> 9 of 12 -> 450 000: -150 000, 3 lessons, 7 days.
    # December: 01.12 and 03.12 leave (02.12 is a Wednesday: no lesson) -> 12 of 14 -> 600 000 x 12 / 14 = 514 285.7 -> 514 000 (whole thousands): 514 000 - 600 000 = -86 000, 2 lessons, 3 days.
    # The sum -236 000 = the change of the charge.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 12, 4))
    act(world, s, g, D(2026, 12, 4), make(world, s, g, "2026-11-24", "2026-12-03"),
        [("24.11–30.11", "2026-11-24", "2026-11-30", 7, 3, None, -150_000), ("01.12–03.12", "2026-12-01", "2026-12-03", 3, 2, None, -86_000)], None, -236_000, name="pause_probe_create_two_months_begun")


def _pause_17_30(world, fixed_today):
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 10))
    pause_id = pause_on(world, s, g, "2026-11-17", "2026-11-30")
    fixed_today(D(2026, 11, 24))
    return g, s, pause_id


def test_c4_the_pause_17_30_put_in_advance_ended_today(world, fixed_today):
    # 17 - 30.11 = 17 19 20 24 26 27 (6 of 12): 300 000.  24.11 "end today" = the last day 23.11: 24 26 27 come back: 9 of 12 = 450 000, +150 000; none of the three has passed (24.11 is today).
    g, s, pause_id = _pause_17_30(world, fixed_today)
    act(world, s, g, D(2026, 11, 24), edit(world, pause_id, "2026-11-17", "2026-11-23"), [("24.11–30.11", "2026-11-24", "2026-11-30", 7, 3, 0, 150_000)], None, 150_000, name="pause_probe_end_today")


def test_c5_the_same_pause_taken_off_whole_three_lessons_have_passed(world, fixed_today):
    # taken off: all 12 lessons, 600 000: +300 000, 6 lessons (17 19 20 have passed: 3; 24.11 is today, 26 27 are ahead).
    g, s, pause_id = _pause_17_30(world, fixed_today)
    act(world, s, g, D(2026, 11, 24), void(world, pause_id), [("17.11–30.11", "2026-11-17", "2026-11-30", 14, 6, 3, 300_000)], None, 300_000, name="pause_probe_void_running")


def test_c6_a_pause_put_back_in_time_17_30_six_lessons(world, fixed_today):
    # 24.11: the pause 17 - 30.11 (no mark «был» on 17, 19, 20): 6 of 12 lessons leave: 300 000, -300 000; ``lessons_passed`` is for a line whose days come back only.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 24))
    act(world, s, g, D(2026, 11, 24), make(world, s, g, "2026-11-17", "2026-11-30"), [("17.11–30.11", "2026-11-17", "2026-11-30", 14, 6, None, -300_000)], None, -300_000, name="pause_probe_create_backdated")


def test_c7_a_pause_wholly_in_a_month_that_is_not_charged_has_no_line_only_charge_later(world, fixed_today):
    # 01.12 - 10.12 on 24.11: December has not begun: no line, no change of the charge (0); the days are ``charge_later``.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 24))
    act(world, s, g, D(2026, 11, 24), make(world, s, g, "2026-12-01", "2026-12-10"), [], ("01.12–10.12", "2026-12-01", "2026-12-10"), 0, name="pause_probe_create_future_month")


def test_c7_the_same_future_pause_taken_off_is_charge_later_as_well(world, fixed_today):
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 20))
    pause_id = pause_on(world, s, g, "2026-12-01", "2026-12-10")
    fixed_today(D(2026, 11, 24))
    act(world, s, g, D(2026, 11, 24), void(world, pause_id), [], ("01.12–10.12", "2026-12-01", "2026-12-10"), 0, name="pause_probe_void_future")


def test_c8_the_forgiveness_changes_no_line(world, fixed_today):
    # 05.11 paid 150 000, 10.11 forgiven 100 000: the debt and the used forgiveness move, the CHARGE does not: 24.11 - 30.11 = 3 lessons, -150 000, the same line as without the forgiveness.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 5))
    pay(world, s, g, amount=150_000, paid_on=D(2026, 11, 5))
    fixed_today(D(2026, 11, 10))
    forgive(world, s, g, 100_000)
    fixed_today(D(2026, 11, 24))
    act(world, s, g, D(2026, 11, 24), make(world, s, g, "2026-11-24", "2026-11-30"), [("24.11–30.11", "2026-11-24", "2026-11-30", 7, 3, None, -150_000)], None, -150_000)


def test_c8_a_lesson_cancelled_by_the_centre_inside_the_pause_moves_no_charge_and_no_line(world, fixed_today):
    # The setting «уроки, отменённые центром» is on; Tuesday 24.11 is cancelled by the centre (10.11).  The month counts the lessons of the schedule (the cancel is a SEPARATE deduction of the account, ``recalculated``:
    # it moves no charge and no ``lesson_dates``), so the pause 24.11 - 30.11 takes 24 26 27 out as before: 3 lessons, 9 of 12 = 450 000, -150 000; the sum = the change of the charge.  (The first guess of
    # this test, 2 lessons, was wrong: the code does not drop a cancelled lesson from the count of the month.)
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    assert owner(world).put(f"{API}/org/settings/payments", json={"centre_cancel_recalc": True}).status_code == 200
    fixed_today(D(2026, 11, 10))
    r = owner(world).post(f"{API}/org/groups/{g.id}/attendance", json={"lessonDate": "2026-11-24", "lessonId": "s2", "status": "cancelled", "records": {}, "cancellationReason": "Преподаватель заболел", "cancellationKind": "centre"})
    assert r.status_code == 200, r.text
    fixed_today(D(2026, 11, 20))
    act(world, s, g, D(2026, 11, 20), make(world, s, g, "2026-11-24", "2026-11-30"), [("24.11–30.11", "2026-11-24", "2026-11-30", 7, 3, None, -150_000)], None, -150_000)


def test_c9_a_personal_period_has_no_lines(world, fixed_today):
    # The personal period moves its periods by a pause (it takes no lessons out of a month): no lines, no ``charge_later``; the old line (``charged_delta``) stays for the screen.  A pause from the FIRST day of the period too.
    g, s = lp(world, fixed_today, joined=D(2026, 11, 1))
    fixed_today(D(2026, 11, 24))
    for first, last in (("2026-11-24", "2026-11-30"), ("2026-11-01", "2026-11-07")):
        before = led(world, s, g, D(2026, 11, 24)).charged
        probe, saved = make(world, s, g, first, last)(True), make(world, s, g, first, last)(False)
        assert probe.status_code == saved.status_code == 201, (probe.text[:200], saved.text[:200])
        if first == "2026-11-24":
            dump("pause_probe_create_personal", probe.json())
        assert probe.json()["charge_rows"] == saved.json()["charge_rows"] == [] and probe.json()["charge_later"] is None and saved.json()["charge_later"] is None
        assert probe.json()["impact"]["charged_delta"] == saved.json()["impact"]["charged_delta"] == led(world, s, g, D(2026, 11, 24)).charged - before
        pid = saved.json()["pause"]["id"]
        gone = void(world, pid)(False)
        assert gone.status_code == 200 and gone.json()["charge_rows"] == [] and gone.json()["charge_later"] is None


def test_c9_packages_have_no_lines(world, fixed_today):
    # Packages of 8 lessons (the world of Б11.19): a pause over future lessons, taken off again: no lines, no ``charge_later``.
    from tests.money1b_helpers import hold_lessons
    from tests.test_pay_tails_b78_pg import centre_of, days

    g, s = centre_of(world, fixed_today)
    hold_lessons(world, s, days(D(2026, 10, 1), D(2026, 10, 16)), group=g)
    fixed_today(D(2026, 10, 20))
    probe, saved = make(world, s, g, "2026-10-27", "2026-11-10")(True), make(world, s, g, "2026-10-27", "2026-11-10")(False)
    assert probe.status_code == saved.status_code == 201, (probe.text[:200], saved.text[:200])
    dump("pause_probe_create_packages", probe.json())
    assert probe.json()["charge_rows"] == saved.json()["charge_rows"] == [] and probe.json()["charge_later"] is None and saved.json()["charge_later"] is None
    gone = void(world, saved.json()["pause"]["id"])(False)
    assert gone.status_code == 200 and gone.json()["charge_rows"] == [] and gone.json()["charge_later"] is None


def test_c10_a_role_without_the_right_to_money_gets_the_lines_without_sums(world, fixed_today):
    # The routes of a pause need the right to money (Money-1 A4), so the answer without money is the service's: ``impact`` null, the lines are there, ``amount`` null (the same lines as with money).
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 24))
    m = membership(world, s, g)
    probe = PauseService(world.db).create(str(world.org.id), m.id, str(world.owner.id), PauseCreate(first_day=D(2026, 11, 24), last_day=D(2026, 12, 3)), dry_run=True, money=False, actor_role="owner")
    assert probe.impact is None
    dump("pause_probe_create_no_money_role", probe.model_dump(mode="json"))
    assert [(r.label, r.days, r.lessons, r.lessons_passed, r.amount) for r in probe.charge_rows] == [("24.11–30.11", 7, 3, None, None)]
    assert (probe.charge_later.label, probe.charge_later.first_day, probe.charge_later.last_day) == ("01.12–03.12", D(2026, 12, 1), D(2026, 12, 3))
    saved = PauseService(world.db).create(str(world.org.id), m.id, str(world.owner.id), PauseCreate(first_day=D(2026, 11, 24), last_day=D(2026, 12, 3)), money=False, actor_role="owner")
    assert saved.impact is None and saved.charge_rows == probe.charge_rows and saved.charge_later == probe.charge_later


def test_c11_the_year_is_written_to_both_dates_when_one_is_not_this_year(world, fixed_today):
    # The label of a line: the year is written to BOTH dates when at least one is not in the year of the working date (the pure rule of the text).
    from src.auth import payments_texts as tx

    assert tx.range_text(D(2026, 11, 24), D(2026, 11, 30), 2026) == "24.11–30.11"
    assert tx.range_text(D(2026, 11, 24), D(2026, 11, 24), 2026) == "24.11"
    assert tx.range_text(D(2026, 12, 28), D(2027, 1, 3), 2026) == "28.12.2026–03.01.2027"
    assert tx.range_text(D(2026, 11, 24), D(2026, 11, 30), 2027) == "24.11.2026–30.11.2026"
    assert tx.range_text(D(2026, 11, 24), D(2026, 11, 24), 2027) == "24.11.2026"


def test_c12_the_same_range_again_changes_nothing_no_lines(world, fixed_today):
    # A double click (the same range again): nothing new, no lines, the charge as it was.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 24))
    first = make(world, s, g, "2026-11-24", "2026-11-30")(False)
    again = make(world, s, g, "2026-11-24", "2026-11-30")(False)
    assert first.status_code == 201 and again.status_code in (200, 201), again.text[:200]
    assert again.json()["charge_rows"] == [] and again.json()["charge_later"] is None


def test_c13_the_insurance_sum_of_the_lines_not_the_change_of_the_charge_gives_no_lines_and_an_error_in_the_log(world, fixed_today, caplog):
    # The two real accounts of the world of С1 (before / after the pause 24.11 - 30.11): with the true change of the charge the lines stand; when the account after claims another change (+1) the lines and ``charge_later``
    # are taken away and the log says why (the screen then shows the old one line).
    import dataclasses
    import logging

    from src.auth import ledger_service as ls
    from src.auth import payments_views as pv

    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 24))
    regimes = ls.load_settings(world.db, str(world.org.id)).regimes
    before = led(world, s, g, D(2026, 11, 24))
    assert make(world, s, g, "2026-11-24", "2026-11-30")(False).status_code == 201
    after = led(world, s, g, D(2026, 11, 24))
    days = [D(2026, 11, 24) + __import__("datetime").timedelta(days=k) for k in range(7)]
    rows, later = pv.pause_charge_rows(before, after, D(2026, 11, 24), days, money=True, regimes=regimes)
    assert [(r.label, r.lessons, r.amount) for r in rows] == [("24.11–30.11", 3, -150_000)] and later is None
    with caplog.at_level(logging.ERROR, logger="src.auth.payments_views"):
        rows, later = pv.pause_charge_rows(before, dataclasses.replace(after, charged=after.charged + 1), D(2026, 11, 24), days, money=True, membership_id="m-1", regimes=regimes)
    assert rows == [] and later is None
    assert any("sum to -150000" in r.getMessage() and "m-1" in r.getMessage() for r in caplog.records)


def test_c14_the_list_of_the_pauses_of_a_participation_running_scheduled_and_two_past(world, fixed_today):
    # The answer of the list for the screens (``pauses_list_mixed``): a running pause, a scheduled one and two past ones, the newest first; the list has no lines (they live in the answers of the actions).
    g, s = om_at(world, fixed_today, D(2026, 10, 1))
    fixed_today(D(2026, 10, 12))
    pause_on(world, s, g, "2026-10-05", "2026-10-11", "Болел")
    fixed_today(D(2026, 10, 26))
    pause_on(world, s, g, "2026-10-20", "2026-10-25", "Семейные обстоятельства")
    fixed_today(D(2026, 11, 18))
    pause_on(world, s, g, "2026-11-18", "2026-12-01", "Болезнь")
    fixed_today(D(2026, 11, 24))
    pause_on(world, s, g, "2026-12-10", "2026-12-14", "Каникулы")
    r = owner(world).get(f"{API}/org/memberships/{membership(world, s, g).id}/pauses")
    assert r.status_code == 200, r.text
    assert [(p["first_day"], p["last_day"]) for p in r.json()] == [("2026-12-10", "2026-12-14"), ("2026-11-18", "2026-12-01"), ("2026-10-20", "2026-10-25"), ("2026-10-05", "2026-10-11")]
    dump("pauses_list_mixed", r.json())


def test_c15_working_date_02_12_the_pause_taken_off_whole_gives_the_period_back_that_has_begun_with_its_own_first_day(world, fixed_today):
    # Б11.43 п. 1, world 2.  Hand count BEFORE the run.  The pause 24.11 - 03.12 is put on 02.12 (as in the world 1): the charge is 450 000 (November 9 of 12; December has not begun in the account: 0).  Taken off the same
    # day: November 600 000 (+150 000, 3 lessons 24 26 27, all three have passed, 7 days, begun on both sides: ``period_begins`` null); December begins on 01.12 <= 02.12: it comes back WHOLE, 600 000, the days 01 - 03.12
    # (3), the lessons 01.12 and 03.12 (2), of them passed 1 (01.12), ``period_begins`` = 2026-12-01 (the first paid day of the account after).  The sum +750 000 = the change of the charge.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 12, 2))
    pause_id = pause_on(world, s, g, "2026-11-24", "2026-12-03")
    act(world, s, g, D(2026, 12, 2), void(world, pause_id),
        [("24.11–30.11", "2026-11-24", "2026-11-30", 7, 3, 3, 150_000), ("01.12–03.12", "2026-12-01", "2026-12-03", 3, 2, 1, 600_000)], None, 750_000,
        name="pause_probe_void_two_months_0212", begins=[None, "2026-12-01"])


def test_c16_working_date_04_12_both_months_begun_no_line_is_carried(world, fixed_today):
    # Б11.43 п. 1, world 3: the numbers of the test c3_04_12 (-150 000 and -86 000); ``period_begins`` is null in both lines (the default of ``act`` checks it there): the first paid day of December is 04.12 = today,
    # the period has begun on both sides.  This test names the world; the check lives in ``act``.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 12, 4))
    a = act(world, s, g, D(2026, 12, 4), make(world, s, g, "2026-11-24", "2026-12-03"),
            [("24.11–30.11", "2026-11-24", "2026-11-30", 7, 3, None, -150_000), ("01.12–03.12", "2026-12-01", "2026-12-03", 3, 2, None, -86_000)], None, -236_000)
    assert begins_of(a) == [None, None]


def test_c17_a_pause_put_back_in_time_from_the_first_of_the_month_the_whole_period_leaves_with_its_first_paid_day(world, fixed_today):
    # Б11.43 п. 1, world 4.  Hand count BEFORE the run.  02.12; the pause 01.12 - 10.12 (the child is ill from the 1st).  The lessons of December in 01 - 10: 01 03 04 08 10 = 5 (of 14); the days 10.  The period
    # began on 01.12 in the account before (600 000); after the pause its first paid day is 11.12 > 02.12: it has not begun, it charges 0.  One line "01.12–10.12", 10 days, 5 lessons, -600 000,
    # ``period_begins`` = 2026-12-11.  November has no changed day: no line.  The change of the charge -600 000 = the sum; the whole period is begun in the account before, so no ``charge_later``.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 12, 2))
    act(world, s, g, D(2026, 12, 2), make(world, s, g, "2026-12-01", "2026-12-10"), [("01.12–10.12", "2026-12-01", "2026-12-10", 10, 5, None, -600_000)], None, -600_000,
        name="pause_probe_create_backdated_month_start", begins=["2026-12-11"])


def test_c18_the_centre_cancelled_lesson_inside_the_pause_the_account_after_has_no_deduction_and_the_debt_is_the_whole_charge(world, fixed_today):
    # Б11.43 п. 2.  The world of c8 (the setting is on, Tuesday 24.11 is cancelled by the centre, the pause 24.11 - 30.11 on 20.11).  Hand count: the charge after the pause is 450 000 (9 of 12); the deduction for
    # the cancelled lesson (``recalculated``) is 0 (the day is on the pause: the owner of the day is the paid day of the period only, so the lesson is not deducted twice); nobody paid: the debt = 450 000.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    assert owner(world).put(f"{API}/org/settings/payments", json={"centre_cancel_recalc": True}).status_code == 200
    fixed_today(D(2026, 11, 10))
    r = owner(world).post(f"{API}/org/groups/{g.id}/attendance", json={"lessonDate": "2026-11-24", "lessonId": "s2", "status": "cancelled", "records": {}, "cancellationReason": "Преподаватель заболел", "cancellationKind": "centre"})
    assert r.status_code == 200, r.text
    fixed_today(D(2026, 11, 20))
    act(world, s, g, D(2026, 11, 20), make(world, s, g, "2026-11-24", "2026-11-30"), [("24.11–30.11", "2026-11-24", "2026-11-30", 7, 3, None, -150_000)], None, -150_000)
    after = led(world, s, g, D(2026, 11, 20))
    assert (after.recalculated, after.charged, after.debt) == (0, 450_000, 450_000), (after.recalculated, after.charged, after.debt)


def test_c19_the_insurance_error_is_written_once_per_action_the_lines_are_counted_once_after_the_commit(world, fixed_today, caplog, monkeypatch):
    # Б11.44 п. 6.  The save of a pause, of its edit and of its void used to count the lines twice (under the lock, the result thrown away, and after the commit): the lines are counted ONCE per action
    # now (the dry run: once, the real save: once after the commit).  With the insurance forced to fire (the account after claims one more in the change of the charge) the log has ONE error per action
    # and the answer has no lines.  By hand: the probe 1 call / 1 error, the save 1 / 1, the probe of the edit 1 / 1, the edit 1 / 1, the probe of the void 1 / 1, the void 1 / 1: 6 calls, 6 errors.
    import dataclasses
    import logging

    from src.auth import payments_views as pv

    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 24))
    real, calls = pv.pause_charge_rows, []

    def forced(before, after, *a, **k):
        calls.append(1)
        return real(before, dataclasses.replace(after, charged=after.charged + 1), *a, **k)

    monkeypatch.setattr(pv, "pause_charge_rows", forced)

    def once(call):
        calls.clear()
        caplog.clear()
        r = call()
        assert r.status_code in (200, 201), r.text[:200]
        errors = [x for x in caplog.records if "pause_charge_rows" in x.getMessage()]
        assert (len(calls), len(errors)) == (1, 1) and r.json()["charge_rows"] == [] and r.json()["charge_later"] is None, (len(calls), len(errors))
        return r

    with caplog.at_level(logging.ERROR, logger="src.auth.payments_views"):
        make_ = make(world, s, g, "2026-11-24", "2026-12-03")
        once(lambda: make_(True))
        pid = once(lambda: make_(False)).json()["pause"]["id"]
        once(lambda: edit(world, pid, "2026-11-24", "2026-11-25")(True))
        once(lambda: edit(world, pid, "2026-11-24", "2026-11-25")(False))
        once(lambda: void(world, pid)(True))
        once(lambda: void(world, pid)(False))
