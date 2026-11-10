"""Money-1 pack V7 (PostgreSQL): the salary - the advance of a teacher who has no lessons, the confirmation of an unusual rate, the sheet of a teacher who is being removed.

World of ``test_money1_payout_list_pg``: October of teacher A, five students, 25 000 per lesson-student (600 000 x 50 % / 12); the sheet is saved on 22.10 (125 000), an advance of 300 000 is paid."""
from tests.ops_helpers import clock, needs_pg, pg_session  # noqa: F401  (fixtures)
from tests.payf_helpers import API, client
from tests.test_money1_payout_list_pg import MONTH, _world
from tests.test_p154_money_pg import hold, sheet

pytestmark = needs_pg


def _closed(pg_session, clock):
    w, g, st = _world(pg_session, clock)
    clock.set("2026-11-02 12:00")
    calc = sheet(w, MONTH)
    assert client(w, "owner").post(f"{API}/org/payroll/sheets", json={"month": MONTH, "teacher_id": str(w.A.id), "revision": 1, "expected_fingerprint": calc["source_fingerprint"]}).status_code in (200, 201)
    return w, g, st


def test_v7_the_advance_of_a_teacher_without_lessons_is_listed_in_the_next_month(pg_session, clock):
    w, g, st = _closed(pg_session, clock)                                              # October closed at 250 000, paid 300 000: 50 000 goes forward
    g.schedule = []                                                                     # no lessons planned in November: nothing unresolved either - the teacher has no row of his own
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(g, "schedule")
    w.db.commit()
    listed = client(w, "owner").get(f"{API}/org/payroll/months/2026-11")
    assert listed.status_code == 200, listed.text
    rows = [t for t in listed.json()["teachers"] if t["teacher_id"] == str(w.A.id)]
    assert len(rows) == 1, listed.json()["teachers"]                                   # the old code: no lessons, no sheet - the row was skipped and the 50 000 was nowhere
    row = rows[0]
    assert row["advance_only"] is True and row["carry_in"] == 50_000 and row["member_active"] is True and row["lessons_count"] == 0


def test_v7_a_teacher_with_lessons_in_the_month_is_not_marked_advance_only(pg_session, clock):
    w, g, st = _closed(pg_session, clock)
    hold(w, g, "lesson-wed", "2026-11-02", {x: "present" for x in st}, start="10:00", end="11:00")
    (row,) = [t for t in client(w, "owner").get(f"{API}/org/payroll/months/2026-11").json()["teachers"] if t["teacher_id"] == str(w.A.id)]
    assert row["advance_only"] is False and row["lessons_count"] == 1 and row["carry_in"] == 50_000


def test_v7_the_signal_of_a_changed_month_has_no_false_alarm_on_a_zero_difference(pg_session, clock):
    w, g, st = _closed(pg_session, clock)
    assert client(w, "owner").get(f"{API}/org/payroll/changed-months").json()["items"] == []        # saved == fresh: nothing to say
    hold(w, g, "lesson-wed", "2026-10-28", {x: "present" for x in st}, start="10:00", end="11:00")  # a repair after the closing
    assert client(w, "owner").get(f"{API}/org/payroll/changed-months").json()["items"] == [{"month": "2026-10", "teachers": 1}]


def put_rate(w, percent, expect=200, **extra):
    r = client(w, "owner").put(f"{API}/org/payroll/rates/{w.A.id}", json={"valid_from": "2026-11", "percents": {"group": percent}, **extra})
    assert r.status_code == expect, (percent, r.text)
    return r


def test_v7_a_rate_of_zero_or_in_times_needs_a_confirmation(pg_session, clock):
    w, g, st = _world(pg_session, clock)                                               # the rate in force: 50 %
    for percent in ("24", "0"):                                                        # less than a half, zero
        r = put_rate(w, percent, expect=409)
        assert r.json()["detail"]["code"] == "RATE_UNUSUAL" and r.json()["detail"]["old"] == "50" and r.json()["detail"]["new"] == percent
    assert client(w, "owner").get(f"{API}/org/payroll/rates/{w.A.id}/history").json()[0]["percent"] != "0"      # nothing was written by the refusals
    put_rate(w, "24", confirm_unusual=True)
    for percent in ("30", "60"):                                                       # inside the borders (here 24 -> 30 is +25 %, 30 -> 60 is exactly twice)
        put_rate(w, percent)


def test_v7_the_removal_impact_names_the_months_of_a_teacher_whose_salary_is_not_saved_without_sums(pg_session, clock):
    from tests.test_p154_money_pg import build_org, five

    w = build_org(pg_session)
    g, st, ms = five(w, clock)
    clock.set("2026-10-22 18:00")
    hold(w, g, "lesson-wed", "2026-10-21", {x: "present" for x in st}, start="10:00", end="11:00")      # October is not saved
    url = f"{API}/org/members/{w.ids('member_A')}/removal-impact"
    got = client(w, "owner").get(url).json()
    assert got["payroll"]["unsaved_months"] == ["2026-10"] and got["payroll"]["unpaid_months"] == []
    assert got["payroll"]["text"] == "Зарплата за октябрь 2026 не сохранена"
    admin = client(w, "admin").get(url).json()["payroll"]
    assert set(admin) == {"unsaved_months", "unpaid_months", "text"}                    # months and words only: no sum for an administrator
    saved = client(w, "owner").post(f"{API}/org/payroll/sheets", json={"month": "2026-10", "teacher_id": str(w.A.id), "expected_fingerprint": sheet(w, "2026-10")["source_fingerprint"]})
    assert saved.status_code == 201, saved.text
    after = client(w, "owner").get(url).json()["payroll"]
    assert after["unsaved_months"] == [] and after["unpaid_months"] == ["2026-10"]       # saved, nothing paid yet
    assert after["text"] == "Зарплата за октябрь 2026 не выплачена"


def test_v7b_n7_the_names_of_a_teacher_come_apart_next_to_the_name_in_the_screens_of_the_salary(pg_session, clock):
    w, g, st = _closed(pg_session, clock)
    owner = client(w, "owner")
    (row,) = [t for t in owner.get(f"{API}/org/payroll/months/{MONTH}").json()["teachers"] if t["teacher_id"] == str(w.A.id)]
    assert (row["teacher_first_name"], row["teacher_last_name"]) == (w.A.first_name, w.A.last_name) and row["teacher_name"]
    brief = [s for s in owner.get(f"{API}/org/payroll/sheets", params={"month": MONTH}).json() if s["teacher_id"] == str(w.A.id)][0]
    assert (brief["teacher_first_name"], brief["teacher_last_name"]) == (w.A.first_name, w.A.last_name)
    full = owner.get(f"{API}/org/payroll/sheets/{brief['id']}").json()
    assert (full["teacher_first_name"], full["teacher_last_name"]) == (w.A.first_name, w.A.last_name)
    periods = owner.get(f"{API}/org/payroll-periods", params={"month": MONTH})
    if periods.status_code == 200:
        assert any(p.get("teacherFirstName") == w.A.first_name for p in periods.json().get("items", periods.json() if isinstance(periods.json(), list) else []))


def test_r3_k8_a_rate_of_zero_is_asked_again_even_where_there_is_no_rate_in_force(pg_session, clock):
    w, g, st = _world(pg_session, clock)
    r = client(w, "owner").put(f"{API}/org/payroll/rates/{w.B.id}", json={"valid_from": "2026-11", "percents": {"group": "0"}})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "RATE_UNUSUAL" and r.json()["detail"]["old"] is None and "ставки нет" in r.json()["detail"]["message"]
    ok = client(w, "owner").put(f"{API}/org/payroll/rates/{w.B.id}", json={"valid_from": "2026-11", "percents": {"group": "0"}, "confirm_unusual": True})
    assert ok.status_code == 200, ok.text
    assert client(w, "owner").put(f"{API}/org/payroll/rates/{w.B.id}", json={"valid_from": "2026-12", "percents": {"group": "0"}}).status_code == 200      # 0 -> 0 is no change: not asked again
