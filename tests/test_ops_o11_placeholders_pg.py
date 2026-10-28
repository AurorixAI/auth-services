"""LOGIC-B O11 - no invented data: a room nobody typed is null, no 10:00-11:30 for a slot without a time, a busy room is a WARNING.
Matrix rows [O11.R1] ... [O11.R9]."""
import logging

import pytest

from tests.ops_helpers import (  # noqa: F401  (fixtures)
    build_d0, clock, client, exams, needs_pg, new_group, new_slot, new_student, pg_session,
)

pytestmark = needs_pg
API = "/api/v1/auth/org"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")
    world = build_d0(pg_session)
    world.G5 = new_group(pg_session, world.org, world.ids, "G5", "10-А", world.C, [], class_level=10)
    world.G6 = new_group(pg_session, world.org, world.ids, "G6", "10-Б", world.D, [], class_level=10)
    pg_session.commit()
    return world


def body(group, **over):
    data = {"groupId": str(group.id), "dayOfWeek": 5, "startTime": "10:00", "endTime": "11:30", "room": None, "format": "offline"}
    data.update(over)
    return data


def test_r1_no_room_no_invented_room(w):
    """[O11.R1] A lesson without a room (and an online one) has room null in the answer, the schedule and the occurrences; "Кабинет 204" never appears."""
    c = client(w, "admin")
    a = c.post(f"{API}/schedule/lessons", json=body(w.G5)).json()
    b = c.post(f"{API}/schedule/lessons", json=body(w.G6, format="online", dayOfWeek=2)).json()
    assert a["room"] is None and b["room"] is None
    sched = c.get(f"{API}/schedule").json()
    assert {s["room"] for s in sched if s["groupId"] in (str(w.G5.id), str(w.G6.id))} == {None}
    occ = c.get(f"{API}/schedule/occurrences", params={"date_from": "2026-10-21", "date_to": "2026-11-05"}).json()
    assert all(o["room"] is None for o in occ if o["groupId"] in (str(w.G5.id), str(w.G6.id)))
    assert "Кабинет 204" not in c.get(f"{API}/schedule").text


def test_r2_r3_a_repeat_changes_nothing_and_an_empty_room_is_null(w):
    """[O11.R2] [O11.R3] room "" and "   " are stored as null; the same lesson twice is one slot (409)."""
    c = client(w, "admin")
    first = c.post(f"{API}/schedule/lessons", json=body(w.G5, room="   "))
    assert first.status_code == 201 and first.json()["room"] is None
    assert c.post(f"{API}/schedule/lessons", json=body(w.G5, room="")).status_code == 409
    w.db.refresh(w.G5)
    assert w.G5.schedule[0]["room"] is None


def test_r4_a_busy_room_is_a_warning_not_a_refusal(w):
    """[O11.R4] Two administrators create overlapping lessons of different groups in one room: both pass, the second is warned (case and spaces are ignored; only offline)."""
    c = client(w, "admin")
    assert c.post(f"{API}/schedule/lessons", json=body(w.G5, room="Каб 12")).json()["warnings"] == []
    second = c.post(f"{API}/schedule/lessons", json=body(w.G6, room="  каб   12 ", startTime="10:30", endTime="11:00"))
    assert second.status_code == 201, second.text
    assert second.json()["warnings"] == [{"code": "ROOM_BUSY", "message": "Кабинет «каб   12» в это время занят группой «10-А»"}] or \
        second.json()["warnings"][0]["code"] == "ROOM_BUSY" and "10-А" in second.json()["warnings"][0]["message"]
    online = c.post(f"{API}/schedule/lessons", json=body(w.G6, room="Каб 12", format="online", dayOfWeek=5, startTime="12:00", endTime="13:00"))
    assert online.json()["warnings"] == []                                     # online: the room means nothing
    one_off = c.post(f"{API}/schedule/occurrences", json={"groupId": str(w.G3.id), "lessonDate": "2026-10-23", "startTime": "10:40", "endTime": "10:50", "room": "каб 12", "purpose": "extra", "money": "paid"})
    assert one_off.status_code == 201 and one_off.json()["warnings"][0]["code"] == "ROOM_BUSY"


def test_r6_back_to_back_lessons_in_one_room_do_not_conflict(w):
    """[O11.R6] 10:00-11:30 and 11:30-13:00: half-open intervals."""
    c = client(w, "admin")
    c.post(f"{API}/schedule/lessons", json=body(w.G5, room="204"))
    r = c.post(f"{API}/schedule/lessons", json=body(w.G6, room="204", startTime="11:30", endTime="13:00"))
    assert r.status_code == 201 and r.json()["warnings"] == []


def test_r7_a_legacy_slot_without_a_time_is_skipped_and_logged(w, caplog):
    """[O11.R7]"""
    group = new_group(w.db, w.org, w.ids, "Gt", "Без времени", w.D, [{"id": "t0", "dayOfWeek": 1, "validFrom": "2026-10-01"},
                                                                      new_slot("t1", 3, "09:00", "10:00", "2026-10-01")])
    new_student(w.db, w.org, w.ids, "gt", "Ученик", "Безвременный", group)
    w.db.commit()
    with caplog.at_level(logging.INFO):
        occ = client(w, "admin").get(f"{API}/schedule/occurrences", params={"date_from": "2026-10-19", "date_to": "2026-10-25"}).json()
    assert [o["id"] for o in occ if o["groupId"] == str(group.id)] == ["t1"]
    assert "schedule_slot_without_time" in caplog.text
    sched = client(w, "admin").get(f"{API}/schedule").json()
    assert [s["id"] for s in sched if s["groupId"] == str(group.id)] == ["t1"]


def test_r8_teacher_and_admin_alike_and_a_transfer_warns_too(w, clock):
    """[O11.R8] A teacher creating in his own group gets the same answer; a one-day transfer / a new version of the schedule also warn about a busy room."""
    c = client(w, "admin")
    c.post(f"{API}/schedule/lessons", json=body(w.G5, room="7", dayOfWeek=4, startTime="18:00", endTime="19:00"))
    mine = client(w, "D").post(f"{API}/schedule/lessons", json=body(w.G6, room="7", dayOfWeek=4, startTime="18:30", endTime="19:30"))
    assert mine.status_code == 201, mine.text
    assert mine.json()["warnings"][0]["code"] == "ROOM_BUSY"
    lesson = mine.json()["id"]
    revised = client(w, "D").put(f"{API}/schedule/lessons/{lesson}/recurrence",
                                 json={"effectiveFrom": "2026-11-05", "reason": "Новое время", "startTime": "18:10", "endTime": "18:40"})
    assert revised.status_code == 200 and revised.json()["warnings"][0]["code"] == "ROOM_BUSY"
