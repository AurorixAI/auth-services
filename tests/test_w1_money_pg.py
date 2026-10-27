"""W1: the money invariants of the one cancellation road (PostgreSQL) - the recalculation «по вине центра» is the same as before wherever it must be, and gone where it must be gone.

Sources: lesson-move-cancel-dayoff.md §8 №33-35, И6, Д19, Д8; substitutions.md D18, INV-6; WAVE1_SUMMARY A2 / A3.  The deduction is derived by ``run_ledger`` from the cancelled rows of
kind ``centre`` (the setting «вычитать» on): a holiday, a move and a lesson that was given back never deduct."""
import pytest

from tests.b2_helpers import API, req, decide, requests_of  # noqa: F401
from tests.ops_helpers import clock, needs_pg, pg_session  # noqa: F401  (fixtures)
from tests.payf_helpers import build_org, cancel, client, statement
from tests.test_payf_recalc_pg import LINE, kind_of, month_world

pytestmark = needs_pg


def deductions(w, m):
    return [r for r in statement(w, m)["rows"] if r["type"] == "recalc"]


def test_a_sheet_cancellation_of_the_centres_fault_deducts_as_before(pg_session, clock):
    w = build_org(pg_session)
    g, s, m = month_world(w, clock)
    cancel(w, g, "lesson-wed", "2026-10-21")                                              # the usual cancellation, kind by default
    (line,) = deductions(w, m)
    assert line["details"]["recalc"]["text"] == LINE and kind_of(w, g, "2026-10-21", "lesson-wed") == "centre"
    assert statement(w, m)["summary"]["recalculated"] == 46_000 and statement(w, m)["summary"]["reconciles"] is True
    # the record of the journal «Замены» is not money: the statement is the same whatever records exist
    assert [r.status for r in requests_of(w)] == ["cancelled"]


def test_a_cancellation_on_a_date_of_a_live_day_off_is_a_holiday_and_deducts_nothing(pg_session, clock):
    """Д19: the lesson the day off could not cancel (it was past) is cancelled by hand as «по вине центра» - the system makes it a holiday: no deduction."""
    w = build_org(pg_session)
    g, s, m = month_world(w, clock)
    made = client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": "2026-10-21", "dateTo": "2026-10-21", "reason": "Хайит"})
    assert made.status_code == 201 and made.json()["cancelled"] == [] and [x["lessonId"] for x in made.json()["blocked"]] == ["lesson-wed"]
    r = cancel(w, g, "lesson-wed", "2026-10-21")
    assert [x["code"] for x in r.json()["warnings"]] == ["CANCELLED_AS_HOLIDAY"]
    assert kind_of(w, g, "2026-10-21", "lesson-wed") == "holiday"
    assert deductions(w, m) == [] and statement(w, m)["summary"]["recalculated"] == 0


def test_the_decision_cancel_takes_the_kind_the_administrator_chose(pg_session, clock):
    """D18: the same request, two kinds: «по вине центра» (the default) deducts, «праздник» does not."""
    w = build_org(pg_session)
    g, s, m = month_world(w, clock)
    mine = [req(w, "A", [(g, lesson, day)]).json()["created"][0]["id"] for lesson, day in (("lesson-mon", "2026-10-26"), ("lesson-wed", "2026-10-28"))]
    assert decide(w, "admin", mine[0], decision="cancel", reason="Преподаватель болен").json()["result"]["cancellationKind"] == "centre"
    assert decide(w, "admin", mine[1], decision="cancel", reason="Праздник", cancellationKind="holiday").json()["result"]["cancellationKind"] == "holiday"
    clock.set("2026-10-29 12:00")
    lines = deductions(w, m)
    assert [x["details"]["recalc"]["text"] for x in lines] == ["−46 000 сум: урок 26.10 отменён центром"]


def test_a_lesson_given_back_deducts_nothing_any_more(pg_session, clock):
    w = build_org(pg_session)
    g, s, m = month_world(w, clock)                                                       # today Sunday 25.10
    cancel(w, g, "lesson-wed", "2026-10-28")
    clock.set("2026-10-29 12:00")
    assert [x["details"]["recalc"]["text"] for x in deductions(w, m)] == ["−46 000 сум: урок 28.10 отменён центром"]
    clock.set("2026-10-25 12:00")
    r = client(w, "owner").post(f"{API}/org/groups/{g.id}/lessons/restore", json={"lessonId": "lesson-wed", "lessonDate": "2026-10-28", "reason": "Отменили по ошибке"})
    assert r.status_code == 200 and r.json()["previousKind"] == "centre", r.text
    clock.set("2026-10-29 12:00")
    assert deductions(w, m) == [] and statement(w, m)["summary"]["recalculated"] == 0 and statement(w, m)["summary"]["reconciles"] is True


def test_a_move_stays_neutral_for_the_money_and_so_does_its_restore_refusal(pg_session, clock):
    """A move has no kind (neutral); to give a MOVED original back is refused and changes nothing."""
    w = build_org(pg_session)
    g, s, m = month_world(w, clock)
    moved = client(w, "owner").post(f"{API}/org/groups/{g.id}/lesson-move", json={"lessonId": "lesson-wed", "lessonDate": "2026-10-28", "newDate": "2026-10-29", "startTime": "12:00",
                                                                                 "endTime": "13:00", "reason": "Перенос"})
    assert moved.status_code == 200, moved.text
    assert kind_of(w, g, "2026-10-28", "lesson-wed") is None
    assert client(w, "owner").post(f"{API}/org/groups/{g.id}/lessons/restore", json={"lessonId": "lesson-wed", "lessonDate": "2026-10-28", "reason": "Верните"}).status_code == 409
    clock.set("2026-10-30 12:00")
    assert deductions(w, m) == []
