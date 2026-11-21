"""PAY-TAILS Б7.8 step 5.1 on PostgreSQL: the MOMENT a lesson became cancelled by the centre reaches the input of the account (``CentreCancel.entered``) - from the journal of attendance, not from the day of the lesson and not from
``updated_at``; and the moment of the setting (``RecalcVersion.entered``).  The cancels go through the real endpoints (the hand cancellation, the change of its kind)."""
from datetime import datetime

import pytest

from src.auth import business_clock as bc
from src.auth import ledger_service as ls
from src.auth import payment_status as ps
from src.models.auth import GroupAttendance

from tests.ops_helpers import clock, needs_pg, pg_session  # noqa: F401  (fixtures)
from tests.payf_helpers import API, build_org, cancel, centre, client

from tests.test_payf_recalc_pg import month_world

pytestmark = needs_pg


def loaded(w, m):
    w.db.expire_all()
    loader = ls.LedgerLoader(w.db, str(w.org.id), ps.business_today())
    loader.load_facts([m.id])
    return loader.build_input(m.id)


def test_the_moment_of_a_cancel_is_the_record_of_the_journal_the_setting_is_the_moment_it_was_switched_on(pg_session, clock):
    w = build_org(pg_session)
    g, s, m = month_world(w, clock)                                        # the setting is switched on 06.10 (clock 06.10 09:00)
    clock.set("2026-10-25 12:00")
    cancel(w, g, "lesson-wed", "2026-10-21")                               # a lesson of the PAST cancelled today: the moment is today, not the day of the lesson
    cancelled_at = bc.utc_now()
    clock.set("2026-10-27 12:00")                                          # a later touch of the row must not move the moment
    w.db.query(GroupAttendance).filter_by(group_id=g.id, lesson_date="2026-10-21").update({"updated_at": bc.utc_now()})
    w.db.commit()
    inp = loaded(w, m)
    (c,) = inp.centre_cancels
    assert (str(c.day), c.entered) == ("2026-10-21", cancelled_at)
    (v,) = inp.recalc_versions
    assert (str(v.effective_from), v.on) == ("2026-10-06", True)
    assert v.entered == datetime(2026, 10, 6, 4, 0)                       # Б7.9 п. 3: the exact moment of the switch, 06.10 09:00 in Tashkent = 04:00 UTC (an entry without ``at`` keeps the start of its day: test_payf_ledger)


def test_a_holiday_turned_into_a_centre_cancel_has_the_moment_of_the_change(pg_session, clock):
    w = build_org(pg_session)
    g, s, m = month_world(w, clock)
    clock.set("2026-10-25 12:00")
    cancel(w, g, "lesson-wed", "2026-10-21", kind="holiday", reason="Хайит")
    assert loaded(w, m).centre_cancels == ()                               # a holiday is not a deduction
    clock.set("2026-10-26 15:30")
    r = client(w, "owner").put(f"{API}/org/groups/{g.id}/attendance/cancellation-kind", json={"lessonDate": "2026-10-21", "lessonId": "lesson-wed", "cancellationKind": "centre", "reason": "Это не праздник"})
    assert r.status_code == 200, r.text
    changed_at = bc.utc_now()
    clock.set("2026-10-28 10:00")
    (c,) = loaded(w, m).centre_cancels
    assert c.entered == changed_at


def test_a_cancel_made_again_after_the_lesson_was_given_back_has_the_last_moment(pg_session, clock):
    w = build_org(pg_session)
    g, s, m = month_world(w, clock)
    clock.set("2026-10-25 12:00")
    cancel(w, g, "lesson-wed", "2026-10-21")
    first = loaded(w, m).centre_cancels[0].entered
    clock.set("2026-10-26 12:00")
    r = client(w, "owner").put(f"{API}/org/groups/{g.id}/attendance/cancellation-kind", json={"lessonDate": "2026-10-21", "lessonId": "lesson-wed", "cancellationKind": "holiday", "reason": "Это праздник"})
    assert r.status_code == 200, r.text
    clock.set("2026-10-27 12:00")
    r = client(w, "owner").put(f"{API}/org/groups/{g.id}/attendance/cancellation-kind", json={"lessonDate": "2026-10-21", "lessonId": "lesson-wed", "cancellationKind": "centre", "reason": "Нет, это вина центра"})
    assert r.status_code == 200, r.text
    second = bc.utc_now()
    (c,) = loaded(w, m).centre_cancels
    assert c.entered == second and c.entered > first
