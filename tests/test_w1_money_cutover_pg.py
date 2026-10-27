"""W1 / А-14 (real PostgreSQL, no mocks): the cutover of the «Общий месяц» (``REGULAR_SCHEDULE_FROM`` = 01.11.2026) meets a revision of a series (S8) and one-off lessons.

Centre: «Общий месяц» (fixed billing day 1), one group Mon / Wed / Fri, price 600 000.  Student A joins 15.10.2026, student B joins 04.11.2026.
Then: a one-off lesson on 23.10 (Friday, inside A's October cell), a one-off on 06.11 (Friday, inside B's November cell), and a revision of the series of Monday:
first «с 20.11 — начало в 12:00» (a LATER revision), then «с 05.11 — вторник вместо понедельника» (S8: the change of the day is patched into the later revision too, so Mondays do not come back from 20.11).

Hand calculation (the charge of a partial cell is ``round_1000(price x lessons of the student in the cell / lessons of a full month)``, a half goes up):

* A, October (the cell starts 01.10, BEFORE the cutover: the old way, a one-off lesson is a lesson like any other, in the numerator and in the denominator).
  October 2026 by the pattern: Mon 5, 12, 19, 26 (4) + Wed 7, 14, 21, 28 (4) + Fri 2, 9, 16, 23, 30 (5) = 13.  A is in from 15.10: Fri 16, Mon 19, Wed 21, Fri 23, Mon 26, Wed 28, Fri 30 = 7.
  Without the one-off of 23.10: 600 000 x 7 / 13 = 323 076.9 -> 323 000.  With it: 600 000 x 8 / 14 = 342 857.1 -> 343 000.
* B, November (the cell starts 01.11, AFTER the cutover: the regular schedule only, a one-off lesson is in neither the numerator nor the denominator).
  Before the revision: Mon 2, 9, 16, 23, 30 (5) + Wed 4, 11, 18, 25 (4) + Fri 6, 13, 20, 27 (4) = 13; B is in from 04.11: all of them but Mon 2 = 12  ->  600 000 x 12 / 13 = 553 846.2 -> 554 000.
  The one-off of 06.11 changes nothing (not counted).  A one-off counted by mistake would give 600 000 x 13 / 14 = 557 142.9 -> 557 000.
  After the revision (Mondays become Tuesdays from 05.11, also from 20.11 by the patch of the later revision): Mon 2 (1) + Tue 10, 17, 24 (3) + Wed 4, 11, 18, 25 (4) + Fri 6, 13, 20, 27 (4) = 12;
  B's part from 04.11: 12 - Mon 2 = 11  ->  600 000 x 11 / 12 = 550 000 exactly.  A revision that did NOT reach the later one would bring Mondays 23.11 and 30.11 back (the old
  fall-back of a field, ДЫРА-1): 14 in the month, 13 for B -> 600 000 x 13 / 14 = 557 142.9 -> 557 000; the test sees 550 000 and no lesson on those Mondays.
* A, October after the revision and the one-off of 06.11: still 343 000 (the revision begins on 05.11, the one-off lies in November: a closed cell is not rewritten).
"""
from datetime import datetime

from src.models.auth import StudentGroup
from tests.ops_helpers import clock, needs_pg, new_student, pg_session  # noqa: F401  (fixtures)
from tests.payf_helpers import API, build_org, centre, client, statement

pytestmark = needs_pg


def cell(w, membership, first_day):
    """The segment (cell) of the statement that begins on ``first_day`` (ISO): its charge and the lessons it was counted by."""
    w.db.expire_all()
    (row,) = [r for r in statement(w, membership)["rows"] if r["type"] == "charge" and r["details"]["segment"]["period_from"] == first_day]
    seg = row["details"]["segment"]
    return seg["charge"], seg["lessons"]


def one_off(w, group, day):
    r = client(w, "owner").post(f"{API}/org/schedule/occurrences", json={"groupId": str(group.id), "lessonDate": day, "startTime": "15:00", "endTime": "16:00"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def revise(w, body):
    r = client(w, "owner").put(f"{API}/org/schedule/lessons/lesson-mon/recurrence", json={"reason": "Изменение", **body})
    assert r.status_code == 200, r.text
    return r.json()


def test_the_cutover_a_one_off_lesson_and_a_revision_of_the_series_by_hand(pg_session, clock):
    w = build_org(pg_session)
    clock.set("2026-10-15 09:00")
    (g,), (a,), (ma,) = centre(w, joined=datetime(2026, 10, 15, 6, 0), billing_anchor="fixed_day", billing_day=1)
    # A's October cell without any one-off: 7 of 13
    assert cell(w, ma, "2026-10-01") == (323_000, {"in_part": 7, "in_period": 13})
    one_off(w, g, "2026-10-23")                                                       # an extra lesson inside A's cell, before the cutover: counted (the old way)
    assert cell(w, ma, "2026-10-01") == (343_000, {"in_part": 8, "in_period": 14})

    clock.set("2026-11-04 09:00")
    b = new_student(w.db, w.org, w.ids, "sB", "Борисов", "Тест", g, joined=datetime(2026, 11, 4, 6, 0))
    w.db.commit()
    mb = w.db.query(StudentGroup).filter_by(group_id=g.id, student_id=b.id).one()
    assert cell(w, mb, "2026-11-01") == (554_000, {"in_part": 12, "in_period": 13})   # the regular schedule: 12 of 13

    one_off(w, g, "2026-11-06")                                                       # an extra lesson inside B's cell, after the cutover: counted by nobody
    assert cell(w, mb, "2026-11-01") == (554_000, {"in_part": 12, "in_period": 13})
    assert cell(w, ma, "2026-10-01") == (343_000, {"in_part": 8, "in_period": 14})

    # S8: a LATER revision first (from 20.11 the lesson starts at 12:00), then the change of the day from 05.11 - it is patched into the later revision as well
    revise(w, {"effectiveFrom": "2026-11-20", "startTime": "12:00", "endTime": "13:00"})
    out = revise(w, {"effectiveFrom": "2026-11-05", "dayOfWeek": 2})
    assert out["laterRevisions"] == [{"effectiveFrom": "2026-11-20", "fields": ["dayOfWeek"]}]
    occ = lambda day: [o for o in client(w, "owner").get(f"{API}/org/schedule/occurrences", params={"date_from": day, "date_to": day}).json() if o["id"] == "lesson-mon"]
    assert occ("2026-11-02") != [] and occ("2026-11-09") == [] and occ("2026-11-23") == [] and occ("2026-11-30") == []           # Mondays are gone from 05.11 for good
    assert [(o["startTime"], o["dayOfWeek"]) for o in occ("2026-11-24")] == [("12:00", 2)] and occ("2026-11-10")[0]["startTime"] == "10:00"

    clock.set("2026-11-20 12:00")
    assert cell(w, mb, "2026-11-01") == (550_000, {"in_part": 11, "in_period": 12})   # 11 of 12 by the revised regular schedule, the one-off of 06.11 is not in it
    assert cell(w, ma, "2026-10-01") == (343_000, {"in_part": 8, "in_period": 14})    # A's closed October cell: not rewritten by the one-off of 06.11 nor by the revision of 05.11
