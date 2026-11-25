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


def extras_of(body):
    """Б11.45 С: ``(deferred, period_label)`` of every line (null, null = a line of a period that is not carried)."""
    return [(r["deferred"], r["period_label"]) for r in body["charge_rows"]]


def identity(impact, before_ledger, after_ledger):
    """Б11.51 С3': the identity of EVERY answer of a pause: (debt - advance) after - (debt - advance) before = ``charged_delta`` - ``recalc_delta`` - ``forgiven_delta``; the two new fields are the ledger's own changes of the deductions
    and of the used forgiveness (the same two accounts, no run of its own)."""
    net = (after_ledger.debt - after_ledger.advance) - (before_ledger.debt - before_ledger.advance)
    assert (impact["recalc_delta"], impact["forgiven_delta"]) == (after_ledger.recalculated - before_ledger.recalculated, after_ledger.written_off - before_ledger.written_off), impact
    assert net == impact["charged_delta"] - impact["recalc_delta"] - impact["forgiven_delta"], (net, impact)


def act(world, s, g, today, call, rows, later, delta, name=None, begins=None, extras=None, debts=None):
    """The probe and the save of one action and the FOUR numbers: the lines of the probe = the lines of the save = ``rows`` (the hand count); ``charge_later``; the sum of the lines = ``impact.charged_delta`` of
    the probe = of the save = the charge of the account after - the charge before = ``delta``; ``period_begins`` of every line (Б11.43) = ``begins`` (default: null in every line); ``deferred`` / ``period_label`` of every line (Б11.45) = ``extras`` (default: null, null); the sum of the lines
    is ``Σ amount + Σ deferred``; ``debts`` = (the debt before, the debt after) of the account.  Lesson 168: the line whole, the probe = the save = the account."""
    before_ledger = led(world, s, g, today)
    before = before_ledger.charged
    probe, saved = call(True), call(False)
    assert probe.status_code in (200, 201) and saved.status_code in (200, 201), (probe.text[:300], saved.text[:300])
    p, a = probe.json(), saved.json()
    if name:
        dump(name, p)                                                   # the REAL answer of the probe for the screens (``$ANSWERS_DIR``; nothing is written without it)
    after_ledger = led(world, s, g, today)
    after = after_ledger.charged
    assert lines(p) == lines(a) == rows, ("GOT probe / save", lines(p), lines(a))
    assert begins_of(p) == begins_of(a) == (begins if begins is not None else [None] * len(rows)), ("GOT period_begins", begins_of(p), begins_of(a))
    assert extras_of(p) == extras_of(a) == (extras if extras is not None else [(None, None)] * len(rows)), ("GOT deferred / period_label", extras_of(p), extras_of(a))
    if debts is not None:
        assert (before_ledger.debt, after_ledger.debt) == debts, ("GOT debt before / after", before_ledger.debt, after_ledger.debt)
    assert later_of(p) == later_of(a) == later, (later_of(p), later_of(a))
    assert p["impact"]["charged_delta"] == a["impact"]["charged_delta"] == after - before == delta, (p["impact"]["charged_delta"], a["impact"]["charged_delta"], after - before)
    identity(p["impact"], before_ledger, after_ledger)
    identity(a["impact"], before_ledger, after_ledger)
    if rows:
        assert sum(r[6] for r in rows) + sum(x[0] or 0 for x in extras_of(a)) == delta
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


def test_c3_working_date_02_12_the_period_that_the_pause_moves_to_the_future_two_facts_the_price_and_the_deferred(world, fixed_today):
    # Б11.45 С, world 1.  Hand count BEFORE the run.  Working date 02.12, the pause 24.11 - 03.12 (24, 26, 27.11 and 01.12, 03.12 lose their lessons).  Debt before: November 600 000 + December 600 000
    # (begun 01.12) = 1 200 000.  November: 9 of 12 -> 450 000: «24.11–30.11», 7 days, 3 lessons, -150 000, no deferred.  December after the pause: 12 of 14 -> 600 000 x 12 / 14 = 514 285.7 -> 514 000; its
    # first paid day is 04.12 > 02.12, so the period has not begun: the line is the PRICE change 514 000 - 600 000 = -86 000 (3 days 01-03.12, 2 lessons), ``deferred`` = -514 000 (leaves the account today),
    # ``period_label`` «Декабрь» (the word of the account lines), ``period_begins`` 04.12.  Σ -236 000 - 514 000 = -750 000 = the change of the charge.  Debt after: 450 000 (November only).
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 12, 2))
    act(world, s, g, D(2026, 12, 2), make(world, s, g, "2026-11-24", "2026-12-03"),
        [("24.11–30.11", "2026-11-24", "2026-11-30", 7, 3, None, -150_000), ("01.12–03.12", "2026-12-01", "2026-12-03", 3, 2, None, -86_000)], None, -750_000, name="pause_probe_create_two_months_begun_0212",
        begins=[None, "2026-12-04"], extras=[(None, None), (-514_000, "Декабрь")], debts=(1_200_000, 450_000))


def test_c3_the_promise_comes_true_on_the_date_period_begins_december_is_charged_by_the_deferred_sum(world, fixed_today):
    # Б11.45 С, «обещание сбывается», world 1.  Hand count BEFORE the run.  The pause of the world 1 is saved on 02.12 (deferred -514 000, ``period_begins`` 04.12).  On 04.12 (the first paid day) December has
    # begun: it charges 514 000 = |deferred|: the charge 450 000 -> 964 000, the debt 450 000 -> 964 000; November does not move; 03.12 (a Thursday with no lesson - on pause): the day before the charge is still 450 000.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 12, 2))
    saved = act(world, s, g, D(2026, 12, 2), make(world, s, g, "2026-11-24", "2026-12-03"),
                [("24.11–30.11", "2026-11-24", "2026-11-30", 7, 3, None, -150_000), ("01.12–03.12", "2026-12-01", "2026-12-03", 3, 2, None, -86_000)], None, -750_000,
                begins=[None, "2026-12-04"], extras=[(None, None), (-514_000, "Декабрь")], debts=(1_200_000, 450_000))
    row = saved["charge_rows"][1]
    on_the_day = led(world, s, g, D.fromisoformat(row["period_begins"]))
    day_before = led(world, s, g, D(2026, 12, 3))
    assert (day_before.charged, on_the_day.charged) == (450_000, 450_000 + abs(row["deferred"])) == (450_000, 964_000)
    assert (day_before.debt, on_the_day.debt) == (450_000, 964_000)


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
        after_ledger = led(world, s, g, D(2026, 11, 24))
        assert probe.json()["impact"]["charged_delta"] == saved.json()["impact"]["charged_delta"] == after_ledger.charged - before
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
    # Б11.45 С, world 2 (was Б11.43 п. 1).  Hand count BEFORE the run.  The pause 24.11 - 03.12 is put on 02.12 (the charge 450 000, the debt 450 000: December has not begun).  Taken off the same day:
    # November 600 000: «24.11–30.11», 7 days, 3 lessons (all three passed), +150 000, begun on both sides.  December: the price 514 000 -> 600 000 = +86 000 (the days 01-03.12, 2 lessons, passed 1 - 01.12),
    # ``deferred`` +514 000 (comes back into the account today), ``period_label`` «Декабрь», ``period_begins`` 01.12 (<= 02.12: it has begun).  Σ 236 000 + 514 000 = +750 000 = the change of the charge;
    # the debt 450 000 -> 1 200 000.  The promise: the period began on 01.12, so the account on 01.12 already holds it: the charge 1 200 000 on 01.12 and on 02.12.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 12, 2))
    pause_id = pause_on(world, s, g, "2026-11-24", "2026-12-03")
    saved = act(world, s, g, D(2026, 12, 2), void(world, pause_id),
                [("24.11–30.11", "2026-11-24", "2026-11-30", 7, 3, 3, 150_000), ("01.12–03.12", "2026-12-01", "2026-12-03", 3, 2, 1, 86_000)], None, 750_000,
                name="pause_probe_void_two_months_0212", begins=[None, "2026-12-01"], extras=[(None, None), (514_000, "Декабрь")], debts=(450_000, 1_200_000))
    begins = D.fromisoformat(saved["charge_rows"][1]["period_begins"])
    assert led(world, s, g, begins).charged == led(world, s, g, D(2026, 12, 2)).charged == 1_200_000


def test_c16_working_date_04_12_both_months_begun_no_line_is_carried(world, fixed_today):
    # Б11.43 п. 1, world 3: the numbers of the test c3_04_12 (-150 000 and -86 000); ``period_begins`` is null in both lines (the default of ``act`` checks it there): the first paid day of December is 04.12 = today,
    # the period has begun on both sides.  This test names the world; the check lives in ``act``.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 12, 4))
    a = act(world, s, g, D(2026, 12, 4), make(world, s, g, "2026-11-24", "2026-12-03"),
            [("24.11–30.11", "2026-11-24", "2026-11-30", 7, 3, None, -150_000), ("01.12–03.12", "2026-12-01", "2026-12-03", 3, 2, None, -86_000)], None, -236_000)
    assert begins_of(a) == [None, None]


def test_c17_a_pause_put_back_in_time_from_the_first_of_the_month_the_whole_period_leaves_with_its_first_paid_day(world, fixed_today):
    # Б11.45 С, world 3 (was Б11.43 п. 1, world 4).  Hand count BEFORE the run.  02.12; the pause 01.12 - 10.12.  The lessons of December in 01 - 10: 01 03 04 08 10 = 5 (of 14): 9 of 14 x 600 000 =
    # 385 714.3 -> 386 000 (whole thousands).  The period began on 01.12 in the account before (600 000); after the pause its first paid day is 11.12 > 02.12: not begun.  One line «01.12–10.12», 10 days,
    # 5 lessons, ``amount`` 386 000 - 600 000 = -214 000, ``deferred`` -386 000, «Декабрь», ``period_begins`` 11.12.  Σ -214 000 - 386 000 = -600 000 = the change of the charge; the debt 1 200 000 - 600 000
    # (November 600 000 + December 600 000) -> 600 000 (November).  The promise: on 11.12 December charges 386 000: the charge 600 000 -> 986 000.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 12, 2))
    saved = act(world, s, g, D(2026, 12, 2), make(world, s, g, "2026-12-01", "2026-12-10"), [("01.12–10.12", "2026-12-01", "2026-12-10", 10, 5, None, -214_000)], None, -600_000,
                name="pause_probe_create_backdated_month_start", begins=["2026-12-11"], extras=[(-386_000, "Декабрь")], debts=(1_200_000, 600_000))
    begins = D.fromisoformat(saved["charge_rows"][0]["begins"] if "begins" in saved["charge_rows"][0] else saved["charge_rows"][0]["period_begins"])
    assert (led(world, s, g, D(2026, 12, 10)).charged, led(world, s, g, begins).charged) == (600_000, 600_000 + 386_000)


def test_c20_a_pause_on_a_day_with_no_lesson_that_moves_the_first_paid_day_is_a_line_with_no_amount_only_the_deferred(world, fixed_today):
    # Б11.45 С, world 4.  Hand count BEFORE the run.  The student joined on 01.11.2026 and the clock stands on 01.02.2027 (a Monday: no lesson - the days are Tue / Thu / Fri).  Four months are charged
    # (Nov 600 000, Dec 600 000, Jan 600 000, Feb 600 000 - a cell of the grid is the whole price, the first paid day of February is the 1st = today): the debt 2 400 000.  A pause on the one day 01.02 takes
    # no lesson (February 2027 has 12 of them: Tue 2 9 16 23, Thu 4 11 18 25, Fri 5 12 19 26): the price stays 600 000, ``amount`` 0; but the first paid day of February moves to 02.02 > 01.02: the period
    # has not begun, the account loses 600 000 today.  The line «01.02», 1 day, 0 lessons, 0, ``deferred`` -600 000, «Февраль», ``period_begins`` 02.02.  Σ 0 - 600 000 = -600 000 = the change of the
    # charge; the debt 2 400 000 -> 1 800 000.  The same pause taken off: the line returns with ``deferred`` +600 000 (``period_begins`` 01.02), the debt 1 800 000 -> 2 400 000.  NOT matched by the hand count of the first run: I wrote «passed 0», the code gives null (``lessons_passed`` is given only when
    # ``amount`` > 0, the written contract of the field); the line has 0 lessons, so there is nothing to pass - the contract is kept, the expectation here follows it.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2027, 2, 1))
    saved = act(world, s, g, D(2027, 2, 1), make(world, s, g, "2027-02-01", "2027-02-01"), [("01.02", "2027-02-01", "2027-02-01", 1, 0, None, 0)], None, -600_000,
                begins=["2027-02-02"], extras=[(-600_000, "Февраль")], debts=(2_400_000, 1_800_000))
    act(world, s, g, D(2027, 2, 1), void(world, saved["pause"]["id"]), [("01.02", "2027-02-01", "2027-02-01", 1, 0, None, 0)], None, 600_000,
        begins=["2027-02-01"], extras=[(600_000, "Февраль")], debts=(1_800_000, 2_400_000))


def test_c21_a_role_without_money_the_carried_line_has_no_sums_but_keeps_the_label_and_the_date(world, fixed_today):
    # Б11.45 С п.2.  By hand: the world 1 (02.12, the pause 24.11 - 03.12) for a role without the right to money: ``impact`` null; the lines are the same two (7 days 3 lessons; 3 days 2 lessons), ``amount`` and
    # ``deferred`` null in both; the carried line keeps ``period_label`` «Декабрь» and ``period_begins`` 04.12 (the screen writes «начнётся 04.12» with no sum), the first line has null in both.
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 12, 2))
    m = membership(world, s, g)
    probe = PauseService(world.db).create(str(world.org.id), m.id, str(world.owner.id), PauseCreate(first_day=D(2026, 11, 24), last_day=D(2026, 12, 3)), dry_run=True, money=False, actor_role="owner")
    assert probe.impact is None
    assert [(r.label, r.days, r.lessons, r.amount, r.deferred, r.period_label, r.period_begins) for r in probe.charge_rows] == [
        ("24.11–30.11", 7, 3, None, None, None, None), ("01.12–03.12", 3, 2, None, None, "Декабрь", D(2026, 12, 4))]


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


def test_c22_the_save_under_the_lock_builds_no_answer_it_throws_away_the_number_of_queries(world, fixed_today):
    # Б11.46 п. 5.  The real save of a pause and of its edit built the whole answer (``_saved``: the marks of the lessons, the payroll, the row) UNDER the lock and threw it away, then built it again after the commit;
    # the first call is gone: only the probe (``dry_run``) builds the answer under the lock.  The number of SQL statements of a call of the route (the account of the world: 2 months, one group):
    # measured on the head 6ee236c BEFORE the change (the run printed it): create_probe / create_save / edit_probe / edit_save = 66 / 108 / 87 / 129; after the change 66 / 87 / 87 / 108: the probe is
    # the same, each real save is 21 statements lower (one build of the answer, not two).  The test holds the new numbers as the ceiling (no more than now), so a second build under the lock turns it red.
    from tests.pg_world import count_queries

    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 24))
    mk = make(world, s, g, "2026-11-24", "2026-11-30")
    n = {}
    n["create_probe"], r = count_queries(world, lambda: mk(True))
    assert r.status_code == 201 or r.status_code == 200, r.text[:200]
    n["create_save"], r = count_queries(world, lambda: mk(False))
    assert r.status_code in (200, 201), r.text[:200]
    pid = r.json()["pause"]["id"]
    n["edit_probe"], r = count_queries(world, lambda: edit(world, pid, "2026-11-24", "2026-11-25")(True))
    n["edit_save"], r = count_queries(world, lambda: edit(world, pid, "2026-11-24", "2026-11-25")(False))
    assert n["create_probe"] <= 66 and n["create_save"] <= 87 and n["edit_probe"] <= 87 and n["edit_save"] <= 108, n


def test_c16_the_list_of_a_group_carries_the_id_of_the_participation_of_each_student_in_this_group(world, fixed_today):
    # Б11.48 С (step 8.14.14): ``GET /org/groups/{id}`` gives each student ``membership_id`` = the id of HIS participation in THIS group (the screen puts a pause on it).  A student of two groups
    # has two different ids in the two lists.  A student on a running pause and one without: the answer for the screens (``group_detail_with_pause``).  The number of statements does not grow.
    from tests.money1b_helpers import add, fresh_student
    from tests.pg_world import capture_queries, ensure_billing, give_group_price, make_group
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 24))
    other = fresh_student(world, "Ученик без паузы")
    add(world, other, group=g, mode="add")
    g2 = make_group(world.db, world.org, world.teacher, "Вторая")
    give_group_price(world.db, g2, 600_000)
    ensure_billing(world, g2, price=None)
    add(world, s, group=g2, mode="add")
    pause_on(world, s, g, "2026-11-24", "2026-11-30")
    statements, r = capture_queries(world.db, lambda: owner(world).get(f"{API}/org/groups/{g.id}"))
    assert r.status_code == 200, r.text
    print("GROUP_DETAIL_STATEMENTS", len(statements))
    by_id = {x["id"]: x for x in r.json()["students"]}
    assert by_id[str(s.id)]["membership_id"] == str(membership(world, s, g).id)
    assert by_id[str(other.id)]["membership_id"] == str(membership(world, other, g).id)
    assert (by_id[str(s.id)]["on_pause"], by_id[str(s.id)]["paused_until"]) == (True, "2026-11-30")
    assert (by_id[str(other.id)]["on_pause"], by_id[str(other.id)]["paused_until"]) == (False, None)
    second = {x["id"]: x for x in owner(world).get(f"{API}/org/groups/{g2.id}").json()["students"]}
    assert second[str(s.id)]["membership_id"] == str(membership(world, s, g2).id) != by_id[str(s.id)]["membership_id"]
    assert len(statements) <= QUERIES_GROUP_DETAIL, len(statements)
    dump("group_detail_with_pause", r.json())


QUERIES_GROUP_DETAIL = 10


def geometry_8(world, fixed_today, today):
    """«Геометрия 8»: Mon/Thu, 360 000 a month = 9 lessons of 40 000 (Mon 2 9 16 23 30; Thu 5 12 19 26), the common month, a student of 01.11 (charged from the first lesson 02.11); the centre cancelled 9, 12, 23 and 26.11 (entered 10.11,
    the setting «отменённые центром уроки» is on); the working date ``today``."""
    from tests.money1b_helpers import add, fresh_student, set_schedule, slot
    from tests.pg_world import ensure_billing, give_group_price, make_group
    from tests.test_pay2_api_pg import settings_block

    fixed_today(D(2026, 11, 1))
    settings_block(world, accounting_start_date="2026-09-01", billing_anchor="fixed_day", billing_day=1)
    g = make_group(world.db, world.org, world.teacher, "Геометрия 8")
    give_group_price(world.db, g, 360_000)
    set_schedule(world, [slot("s1", 1, "2026-01-01", "14:00", "15:00"), slot("s4", 4, "2026-01-01", "14:00", "15:00")], g)
    ensure_billing(world, g, price=None)
    s = fresh_student(world, "Ученик Г8")
    add(world, s, group=g, mode="add")
    assert owner(world).put(f"{API}/org/settings/payments", json={"centre_cancel_recalc": True}).status_code == 200
    fixed_today(D(2026, 11, 10))
    for day, lesson in (("2026-11-09", "s1"), ("2026-11-12", "s4"), ("2026-11-23", "s1"), ("2026-11-26", "s4")):
        r = owner(world).post(f"{API}/org/groups/{g.id}/attendance", json={"lessonDate": day, "lessonId": lesson, "status": "cancelled", "records": {}, "cancellationReason": "Преподаватель заболел", "cancellationKind": "centre"})
        assert r.status_code == 200, r.text
    fixed_today(today)
    return g, s


def test_c23_a_pause_over_a_lesson_the_centre_cancelled_the_deduction_goes_away_and_the_identity_holds(world, fixed_today):
    # By hand («Геометрия 8», working date 25.11, nobody paid): the charge 9 x 40 000 = 360 000, the deductions for the 4 cancelled lessons 160 000 (the 26.11 counts at once: the account counts it the day the period begins) -> the debt 200 000.
    # The pause 26 - 30.11 takes the lessons 26.11 and 30.11 out of the charge: 7 x 40 000 = 280 000 (``charged_delta`` -80 000, the one line of 2 lessons); the cancelled 26.11 is on the pause now, its deduction goes away:
    # the deductions 3 x 40 000 = 120 000 (``recalc_delta`` -40 000); the debt 280 000 - 120 000 = 160 000.  The identity: 160 000 - 200 000 = -80 000 - (-40 000) - 0.
    g, s = geometry_8(world, fixed_today, D(2026, 11, 25))
    before = led(world, s, g, D(2026, 11, 25))
    assert (before.charged, before.recalculated, before.debt) == (360_000, 160_000, 200_000)
    body = act(world, s, g, D(2026, 11, 25), make(world, s, g, "2026-11-26", "2026-11-30"), [("26.11–30.11", "2026-11-26", "2026-11-30", 5, 2, None, -80_000)], None, -80_000, name="pause_probe_create_with_centre_cancel", debts=(200_000, 160_000))
    assert (body["impact"]["recalc_delta"], body["impact"]["forgiven_delta"]) == (-40_000, 0)
    after = led(world, s, g, D(2026, 11, 25))
    assert (after.charged, after.recalculated) == (280_000, 120_000)


def test_c24_a_pause_over_a_cancelled_lesson_with_a_forgiveness_the_debt_stays_0_the_forgiveness_covers_less(world, fixed_today):
    # The same world, the whole debt 200 000 forgiven first (the used forgiveness 200 000, the debt 0).  After the pause the debt to cover is 280 000 - 120 000 = 160 000: the forgiveness used is 160 000 (``forgiven_delta`` -40 000),
    # the debt 0 -> 0.  The identity: 0 - 0 = -80 000 - (-40 000) - (-40 000).
    from tests.test_pay_tails_money_pg import forgive

    g, s = geometry_8(world, fixed_today, D(2026, 11, 25))
    forgive(world, s, g, None)
    before = led(world, s, g, D(2026, 11, 25))
    assert (before.debt, before.written_off) == (0, 200_000)
    body = act(world, s, g, D(2026, 11, 25), make(world, s, g, "2026-11-26", "2026-11-30"), [("26.11–30.11", "2026-11-26", "2026-11-30", 5, 2, None, -80_000)], None, -80_000, name="pause_probe_create_with_forgiveness_cut", debts=(0, 0))
    assert (body["impact"]["recalc_delta"], body["impact"]["forgiven_delta"]) == (-40_000, -40_000)
    assert led(world, s, g, D(2026, 11, 25)).written_off == 160_000
