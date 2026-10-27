"""W1 R3 (c), the money side (PostgreSQL, no mocks): a hand-made «по вине центра» cancellation that a day off turned into a holiday gets its kind back when the day off is voided -
the deduction of the recalculation returns (read through the ledger / the statement), the reason is the author's own; a second covering day off keeps the holiday."""
import pytest

from tests.ops_helpers import clock, needs_pg, pg_session  # noqa: F401  (fixtures)
from tests.payf_helpers import API, build_org, cancel, client, statement
from tests.test_payf_recalc_pg import kind_of, month_world, row_of

pytestmark = needs_pg
DAYS = f"{API}/org/days-off"
DAY = "2026-10-28"                                                        # Wednesday, after the frozen «today» (25.10)


def recalc_days(w, m):
    return [r["date"] for r in statement(w, m)["rows"] if r["type"] == "recalc"]


def reason_of(w, g, day, lesson):
    from src.models.auth import GroupAttendance

    w.db.expire_all()
    return w.db.query(GroupAttendance).filter_by(group_id=g.id, lesson_date=day, lesson_id=lesson).one().cancellation_reason


def test_void_gives_back_the_centre_kind_and_the_deduction(pg_session, clock):
    w = build_org(pg_session)
    g, s, m = month_world(w, clock)
    cancel(w, g, "lesson-wed", DAY, reason="Свой повод")
    debt_cancelled = row_of(w, s)["debt"]
    assert recalc_days(w, m) == [DAY] and debt_cancelled < 600_000                               # the deduction exists
    made = client(w, "owner").post(DAYS, json={"dateFrom": DAY, "dateTo": DAY, "reason": "Хайит"}).json()
    assert [x["lessonId"] for x in made["converted"]] == ["lesson-wed"]
    assert kind_of(w, g, DAY, "lesson-wed") == "holiday" and recalc_days(w, m) == [] and row_of(w, s)["debt"] == 600_000       # the holiday: no deduction
    out = client(w, "owner").post(f"{DAYS}/{made['dayOff']['id']}/void").json()
    assert [(x["lessonId"], x["reason"]) for x in out["kept"] if x["lessonId"] == "lesson-wed"] == [("lesson-wed", "Ручная отмена: вид «по вине центра» возвращён")]
    assert kind_of(w, g, DAY, "lesson-wed") == "centre" and reason_of(w, g, DAY, "lesson-wed") == "Свой повод"
    assert recalc_days(w, m) == [DAY] and row_of(w, s)["debt"] == debt_cancelled                  # the deduction is back, the same as before the day off
    st = statement(w, m)
    assert st["summary"]["reconciles"] is True


def test_with_a_second_covering_day_off_the_lesson_stays_a_holiday_until_that_one_is_voided(pg_session, clock):
    w = build_org(pg_session)
    g, s, m = month_world(w, clock)
    cancel(w, g, "lesson-wed", DAY, reason="Свой повод")
    debt_cancelled = row_of(w, s)["debt"]
    first = client(w, "owner").post(DAYS, json={"dateFrom": DAY, "dateTo": DAY, "reason": "Первый"}).json()
    second = client(w, "owner").post(DAYS, json={"dateFrom": DAY, "dateTo": "2026-10-29", "reason": "Второй"}).json()
    out = client(w, "owner").post(f"{DAYS}/{first['dayOff']['id']}/void").json()
    assert [x["reason"] for x in out["kept"] if x["lessonId"] == "lesson-wed"] == ["Дата входит в другой выходной"]
    assert kind_of(w, g, DAY, "lesson-wed") == "holiday" and recalc_days(w, m) == [] and row_of(w, s)["debt"] == 600_000       # holiday under the covering one
    assert reason_of(w, g, DAY, "lesson-wed") == "Свой повод"
    client(w, "owner").post(f"{DAYS}/{second['dayOff']['id']}/void")
    assert kind_of(w, g, DAY, "lesson-wed") == "centre" and recalc_days(w, m) == [DAY] and row_of(w, s)["debt"] == debt_cancelled
