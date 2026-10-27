"""W1 S10: «Выходной день центра» without its holes (PostgreSQL).

Sources: lesson-move-cancel-dayoff.md Д4 / Д6 / Д7 / Д11 / Д20 / Д23 / Д26 / Д28 and §8 №25-26, 28, 32; schedule-series-edit-stop.md А-11 / ДЫРА-10.
One live day off per range (and under a lock), nothing started or past is cancelled by it, voiding one of two overlapping days off hands the lessons to the other,
the «not finished» mark lives on the server, a group back from the archive meets the live days off."""
from datetime import date
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError

from src.auth.days_off import DayOffRequest, DaysOffService
from src.auth.service import AuthService
from src.models.auth import GroupAttendance, LessonSubstitutionEvent, OrgDayOff
from tests.b2_helpers import API, build_d2, clock, needs_pg, pg_session, requests_of, row_of, run_parallel  # noqa: F401  (fixtures)
from tests.ops_helpers import client, new_group, new_slot
from tests.test_ops_b2_decide_pg import Sync  # noqa: F401

pytestmark = needs_pg
DAYS = f"{API}/org/days-off"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")                        # Wednesday
    return build_d2(pg_session)


@pytest.fixture
def sync(monkeypatch):
    return Sync(monkeypatch)


def give(w, first="2026-10-28", last=None, reason="Хайит", who="owner", expect=201):
    r = client(w, who).post(DAYS, json={"dateFrom": first, "dateTo": last or first, "reason": reason})
    assert r.status_code == expect, r.text
    return r.json()


def live(w):
    w.db.expire_all()
    return w.db.query(OrgDayOff).filter_by(org_id=w.org.id, voided_at=None).all()


# ===================================================================================== Д26: one live day off per range
def test_a_day_off_of_the_same_range_is_given_once_and_continued(w):
    first = give(w)
    again = give(w, expect=201)
    assert again["dayOff"]["id"] == first["dayOff"]["id"] and len(live(w)) == 1
    assert [x["lessonId"] for x in first["cancelled"]] == ["lesson-l2"] and again["cancelled"] == [] and again["alreadyCancelled"] == 1


def test_the_same_range_with_another_reason_is_refused(w):
    give(w)
    r = give(w, reason="Совсем другое", expect=409)
    assert r["detail"]["code"] == "DAY_OFF_EXISTS" and "Хайит" in r["detail"]["message"]
    assert len(live(w)) == 1
    assert give(w, first="2026-10-28", last="2026-10-29", reason="Другое")["dayOff"]["id"]            # an overlapping range is another range (Д4 handles the rest)
    assert len(live(w)) == 2


def test_two_windows_at_once_give_one_day_off(w):
    w.db.commit()

    def create(svc):
        return DaysOffService(svc).create(str(w.org.id), str(w.owner.id), DayOffRequest(dateFrom=date(2026, 10, 28), dateTo=date(2026, 10, 28), reason="Хайит")).dayOff.id

    (a, ea), (b, eb) = run_parallel(w, create, create)
    assert ea is None and eb is None and a == b, (ea, eb)
    assert len(live(w)) == 1
    assert w.db.query(GroupAttendance).filter_by(group_id=w.G1.id, lesson_date="2026-10-28").count() == 1


def test_the_index_is_the_backstop_a_second_live_row_of_a_range_cannot_be_written(w):
    give(w)
    w.db.add(OrgDayOff(org_id=w.org.id, date_from=date(2026, 10, 28), date_to=date(2026, 10, 28), reason="Дубль", created_by=w.owner.id))
    with pytest.raises(IntegrityError):
        w.db.commit()
    w.db.rollback()
    row = live(w)[0]
    row.voided_at = row.created_at
    w.db.commit()
    w.db.add(OrgDayOff(org_id=w.org.id, date_from=date(2026, 10, 28), date_to=date(2026, 10, 28), reason="После отмены", created_by=w.owner.id))
    w.db.commit()                                                                                    # a voided one does not count


# ============================================================================================ Д20: a started lesson is never touched
def test_a_lesson_that_was_started_is_never_cancelled_by_the_cancellation_itself(w, clock):
    """The apply reads the journal once; the cancellation of each lesson re-reads its row under the lock: a started lesson is refused there, not trusted to the snapshot."""
    clock.set("2026-10-26 09:30")
    made = give(w, "2026-10-26", reason="Праздник")
    w.db.query(GroupAttendance).filter_by(group_id=w.G1.id, lesson_date="2026-10-26").delete()          # as if the apply had not reached the lesson yet
    w.db.commit()
    clock.set("2026-10-26 10:05")
    assert client(w, "A").post(f"{API}/org/groups/{w.G1.id}/attendance/start", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"}).status_code == 200
    svc = DaysOffService(AuthService(w.db))
    found, _rows = svc._lessons(str(w.org.id), date(2026, 10, 26), date(2026, 10, 26), w.G1.id)
    (group, occurrence), = found
    why = svc._cancel(str(w.org.id), str(w.owner.id), group, occurrence, "Праздник", UUID(made["dayOff"]["id"]))
    assert why and "начат" in why
    row = row_of(w, w.G1, "lesson-l1", "2026-10-26")
    assert (row.status, row.version, row.cancellation_kind) == ("in_progress", 1, None)
    # and the step of the apply itself lists it (the lesson began: past or going on)
    cancelled, blocked, _, _, down = svc._run(str(w.org.id), str(w.owner.id), UUID(made["dayOff"]["id"]), "Праздник", found, {})
    assert cancelled == [] and [b.lessonId for b in blocked] == ["lesson-l1"] and not down


# ============================================================================================ Д23 / Д11: the past only warns
def test_lessons_that_are_past_or_going_on_are_listed_not_cancelled(w, clock):
    plan = client(w, "owner").post(f"{DAYS}/preview", json={"dateFrom": "2026-10-19", "dateTo": "2026-10-22"}).json()
    assert sorted(x["lessonId"] for x in plan["cancellable"]) == ["lesson-l2", "lesson-l5"]            # today 16:00 and Thursday 16:00 - ahead
    by = {x["lessonId"]: x["reason"] for x in plan["blocked"]}
    assert "проведён" in by["lesson-l1"] and "прошёл" in by["lesson-l3"]                              # held (a row) / past with no row
    made = give(w, "2026-10-19", "2026-10-22", reason="Хайит")
    assert sorted(x["lessonId"] for x in made["cancelled"]) == ["lesson-l2", "lesson-l5"] and {x["lessonId"] for x in made["blocked"]} == {"lesson-l1", "lesson-l3", "lesson-l4"}
    assert row_of(w, w.G2, "lesson-l3", "2026-10-20") is None                                         # the unclosed lesson stays for the teacher to close
    # «now»: a lesson that is going on without a start in the journal is not cancelled either (Д11)
    clock.set("2026-10-28 16:30")
    made2 = give(w, "2026-10-28", reason="Сегодня")
    assert made2["cancelled"] == [] and [x["lessonId"] for x in made2["blocked"]] == ["lesson-l2"] and "отмените вручную, если не состоялся" in made2["blocked"][0]["reason"]


# ============================================================================================ Д4: two days off that overlap
def test_voiding_one_of_two_overlapping_days_off_hands_the_lessons_to_the_other(w):
    first = give(w, "2026-10-28", "2026-10-29", reason="Первый")
    second = give(w, "2026-10-28", "2026-10-30", reason="Второй")
    assert {x["lessonId"] for x in first["cancelled"]} == {"lesson-l2", "lesson-l5"}
    assert second["alreadyCancelled"] == 2 and second["cancelled"] == []
    out = client(w, "owner").post(f"{DAYS}/{first['dayOff']['id']}/void").json()
    assert out["restored"] == [] and {x["reason"] for x in out["kept"]} == {"Дата входит в другой выходной"}
    w.db.expire_all()
    assert {str(r.day_off_id) for r in w.db.query(GroupAttendance).filter_by(org_id=w.org.id, status="cancelled")} == {second["dayOff"]["id"]}       # nobody is a zombie of the voided one
    out2 = client(w, "owner").post(f"{DAYS}/{second['dayOff']['id']}/void").json()
    assert {x["lessonId"] for x in out2["restored"]} == {"lesson-l2", "lesson-l5"} and out2["kept"] == []
    assert w.db.query(GroupAttendance).filter_by(org_id=w.org.id, status="cancelled").count() == 0


def test_the_dry_run_of_a_void_changes_nothing(w):
    first = give(w, "2026-10-28", "2026-10-29", reason="Первый")
    give(w, "2026-10-28", "2026-10-30", reason="Второй")
    client(w, "owner").post(f"{DAYS}/{first['dayOff']['id']}/void", params={"dry_run": True})
    w.db.expire_all()
    assert {str(r.day_off_id) for r in w.db.query(GroupAttendance).filter_by(org_id=w.org.id, status="cancelled")} == {first["dayOff"]["id"]}


# ============================================================================================ Д6: «not finished» lives on the server
def test_an_unfinished_day_off_is_marked_on_the_server_until_it_is_applied(w, sync):
    sync.mode = "down"
    made = give(w, "2026-10-28", reason="Хайит")
    assert made["complete"] is False and made["dayOff"]["incomplete"] is True
    listed = client(w, "owner").get(DAYS).json()                                                      # a reloaded panel
    assert [(d["reason"], d["incomplete"]) for d in listed] == [("Хайит", True)]
    assert row_of(w, w.G1, "lesson-l2", "2026-10-28") is None
    sync.mode = "ok"
    done = client(w, "owner").post(f"{DAYS}/{made['dayOff']['id']}/apply").json()
    assert done["complete"] is True and done["dayOff"]["incomplete"] is False and [x["lessonId"] for x in done["cancelled"]] == ["lesson-l2"]
    assert client(w, "owner").get(DAYS).json()[0]["incomplete"] is False
    assert client(w, "owner").post(f"{DAYS}/{made['dayOff']['id']}/apply").json()["cancelled"] == []   # idempotent


def test_a_finished_day_off_is_not_marked_and_a_voided_one_never_is(w, sync):
    sync.mode = "down"
    made = give(w, "2026-10-28", reason="Хайит")
    client(w, "owner").post(f"{DAYS}/{made['dayOff']['id']}/void")
    assert [d["incomplete"] for d in client(w, "owner").get(DAYS).json()] == [False]


# ============================================================================================ Д7: a group back from the archive
def test_a_group_that_returns_from_the_archive_meets_the_live_day_off(w):
    empty = new_group(w.db, w.org, w.ids, "E", "Пустая", w.D, [new_slot("lesson-e", 3, "12:00", "13:00", "2026-10-28")])
    w.db.commit()
    assert client(w, "admin").post(f"{API}/org/groups/{empty.id}/archive", json={"reason": "Пауза"}).status_code == 200
    made = give(w, "2026-10-28", reason="Хайит")
    assert {x["groupName"] for x in made["cancelled"]} == {"9-А"}                                      # the archived group is not in the plan
    assert row_of(w, empty, "lesson-e", "2026-10-28") is None
    assert client(w, "admin").post(f"{API}/org/groups/{empty.id}/restore", json={}).status_code == 200
    row = row_of(w, empty, "lesson-e", "2026-10-28")
    assert (row.status, row.cancellation_kind, str(row.day_off_id)) == ("cancelled", "holiday", made["dayOff"]["id"])


# ============================================================================================ S2 meets the day off
def test_the_day_off_releases_the_substitutes_of_its_lessons_and_makes_no_record_of_its_own(w):
    assert client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher",
                                  json={"lessonDate": "2026-10-28", "lessonId": "lesson-l2", "teacherId": str(w.D.id), "reason": "Болезнь"}).status_code == 200
    before = len(requests_of(w))                                                                       # the record of the substitution itself
    made = give(w, "2026-10-28", reason="Хайит")
    assert "lesson-l2" in {x["lessonId"] for x in made["cancelled"]}
    assert client(w, "D").get(f"{API}/org/substitutions/mine").json()["assignedToMe"] == []
    assert [e.snapshot["trigger"] for e in w.db.query(LessonSubstitutionEvent).filter_by(org_id=w.org.id, action="assignment_cleared")] == ["cancel"]
    assert len(requests_of(w)) == before                                                               # the day off is the journal of its lessons: no «Замены» record per lesson


# ============================================================================================ А-11: a revision that meets a day off
def test_a_revision_that_turns_an_old_hand_cancellation_into_a_holiday_says_so(w, sync):
    """А-11: a «по вине центра» row that sits on a date of a live day off (a legacy one: the new cancellation would be a holiday already) is turned into a holiday by a revision of its
    series - the answer carries the warning and the deduction is not lost silently."""
    give(w, "2026-11-04", reason="Хайит")
    w.db.query(GroupAttendance).filter_by(group_id=w.G1.id, lesson_date="2026-11-04").delete()
    w.db.add(GroupAttendance(org_id=w.org.id, group_id=w.G1.id, lesson_date="2026-11-04", lesson_id="lesson-l2", status="cancelled", records={}, roster_snapshot=[],
                             cancellation_reason="Старая отмена", cancellation_kind="centre", marked_by=w.admin.id, version=1))
    w.db.commit()
    r = client(w, "admin").put(f"{API}/org/schedule/lessons/lesson-l2/recurrence", json={"effectiveFrom": "2026-11-02", "room": "Каб. 7", "reason": "Кабинет"})
    assert r.status_code == 200, r.text
    assert [x["code"] for x in r.json()["warnings"] if x["code"].startswith("DAY_OFF")] == ["DAY_OFF_CONVERTED"]
    assert "переведена в «выходной»" in [x for x in r.json()["warnings"] if x["code"] == "DAY_OFF_CONVERTED"][0]["message"]
    assert row_of(w, w.G1, "lesson-l2", "2026-11-04").cancellation_kind == "holiday"
