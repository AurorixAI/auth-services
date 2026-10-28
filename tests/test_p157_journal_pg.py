"""#157 A3-A11 (PostgreSQL): the journal «Замены» tells the whole fate of a record - a day off that moves the kind of a cancellation (and gives it back), a substitution a day off took
off, a lesson given back, a chain of three records with ONE history, the one outcome of a row, the activity time and the closing trigger in the list.

Sources: substitutions-page-ux.md Ц3 / Ц4 / Ц5 / Ц6 / Ц8 / Ц9 / Ц10 / Ц11 / Ц12, P157_API.md."""
import json
from datetime import datetime, timedelta
from uuid import UUID

import pytest

from src.auth.ops_rules import short_name
from src.models.auth import GroupAttendance, LessonSubstitutionEvent, LessonSubstitutionRequest as Req
from tests.b2_helpers import API, build_d2, clock, decide, events_of, needs_pg, pg_session, req, requests_of, row_of  # noqa: F401  (fixtures)
from tests.ops_helpers import client
from tests.test_ops_b2_decide_pg import L, Sync, one  # noqa: F401
from tests.test_w1_cancel_pg import restore, sheet

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"
DAYS = f"{API}/org/days-off"
DAY = "2026-10-26"
NEXT = "2026-11-02"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")                        # Wednesday
    return build_d2(pg_session)


@pytest.fixture
def sync(monkeypatch):
    return Sync(monkeypatch)


def decided(w, who="owner", **params):
    return client(w, who).get(SUB, params={"status": "decided", "dateFrom": "2026-10-01", **params}).json()["items"]


def of_lesson(items, day, lesson_group):
    return [i for i in items if i["lesson"]["date"] == day and i["group"]["id"] == str(lesson_group.id)]


def day_off(w, day, reason="Праздник"):
    r = client(w, "owner").post(DAYS, json={"dateFrom": day, "dateTo": day, "reason": reason})
    assert r.status_code in (200, 201), r.text
    return r.json()


def void(w, made):
    r = client(w, "owner").post(f"{DAYS}/{made['dayOff']['id']}/void")
    assert r.status_code == 200, r.text
    return r.json()


# ============================================================================================================ A3
def test_a3_a_day_off_turns_a_cancellation_into_a_holiday_and_its_cancellation_turns_it_back_each_leaves_a_record(w, sync):
    sheet(w, "admin", w.G1, "lesson-l1", NEXT, reason="По ошибке")                                    # «по вине центра» by hand
    made = day_off(w, NEXT, "Хайит")
    assert [x["lessonId"] for x in made["converted"]] == ["lesson-l1"]
    kinds = [i for i in of_lesson(decided(w), NEXT, w.G1) if (i["decision"] or {}).get("closedReason") == "KIND_CHANGED"]
    assert [i["trigger"] for i in kinds] == ["day_off"] and kinds[0]["decision"]["by"]["id"] == str(w.owner.id)         # the author is the owner who made the day off
    void(w, made)
    kinds = [i for i in of_lesson(decided(w), NEXT, w.G1) if (i["decision"] or {}).get("closedReason") == "KIND_CHANGED"]
    assert sorted(i["trigger"] for i in kinds) == ["day_off", "day_off_void"]                          # TWO records: «Вид отмены изменил выходной» - there and back
    detail = [(i, client(w, "owner").get(f"{SUB}/{i['id']}").json()) for i in kinds]
    changes = sorted(((e["snapshot"]["cancellationKind"]["from"], e["snapshot"]["cancellationKind"]["to"]) for i, d in detail for e in d["events"] if e["action"] == "closed" and e["requestId"] == i["id"]))
    assert changes == [("centre", "holiday"), ("holiday", "centre")]
    assert all("Хайит" in i["comment"] for i in kinds)
    # the teacher of the lesson does not read the kind nor the words about the deduction (Ц2)
    mine = [i for i in client(w, "A").get(f"{SUB}/mine").json()["items"] if i["id"] in {k["id"] for k in kinds}]
    assert mine and all(i["comment"] is None for i in mine)
    assert "holiday" not in json.dumps([client(w, "A").get(f"{SUB}/{i['id']}").json() for i in mine])


# ============================================================================================================ A4
def kinds_of_events(w, request_id, action):
    return [e for e in events_of(w, UUID(request_id)) if e.action == action]


def test_a4_a_substitution_a_day_off_took_off_says_so_and_the_voided_day_off_marks_it_not_restored(w, sync):
    item = one(w, "A", "L1", NEXT)
    assert decide(w, "admin", item["id"], decision="substitute", teacherId=str(w.D.id)).status_code == 200
    made = day_off(w, NEXT, "Хайит")
    (cleared,) = kinds_of_events(w, item["id"], "assignment_cleared")
    assert cleared.snapshot["trigger"] == "cancel" and cleared.snapshot["dayOffId"] == made["dayOff"]["id"]            # the lesson was cancelled by the day off
    out = void(w, made)
    assert [x["code"] for x in out["warnings"]] == ["SUBSTITUTE_NOT_RESTORED"]
    (voided,) = kinds_of_events(w, item["id"], "day_off_voided")
    assert voided.snapshot == {"dayOffId": made["dayOff"]["id"], "reopened": False}
    seen = client(w, "owner").get(f"{SUB}/{item['id']}").json()
    assert [e["action"] for e in seen["events"]][-2:] == ["assignment_cleared", "day_off_voided"]


def test_a4_a_hand_cancellation_on_a_day_off_date_is_not_marked_as_the_day_offs(w, sync):
    item = one(w, "A", "L1", NEXT)
    assert decide(w, "admin", item["id"], decision="substitute", teacherId=str(w.D.id)).status_code == 200
    sheet(w, "admin", w.G1, "lesson-l1", NEXT, reason="Вручную")
    (cleared,) = kinds_of_events(w, item["id"], "assignment_cleared")
    assert cleared.snapshot["trigger"] == "cancel" and "dayOffId" not in cleared.snapshot


def test_a4_a_request_the_day_off_closed_learns_the_day_off_is_gone_with_and_without_a_new_one(w, sync):
    item = one(w, "A", "L1", NEXT)
    made = day_off(w, NEXT, "Хайит")
    old = requests_of(w)[0]
    assert (old.status, old.closed_reason) == ("closed", "LESSON_CANCELLED")
    (closing,) = kinds_of_events(w, item["id"], "closed")
    assert closing.snapshot["trigger"] == "day_off" and closing.snapshot["dayOffId"] == made["dayOff"]["id"]
    void(w, made)
    reqs = requests_of(w)
    assert sorted((r.status, r.origin) for r in reqs) == [("closed", "teacher"), ("pending", "system")]                # reopened: a new request waits
    (voided,) = kinds_of_events(w, item["id"], "day_off_voided")
    assert voided.snapshot["reopened"] is True and voided.snapshot["dayOffId"] == made["dayOff"]["id"]
    # the second day off on another lesson: the requester stops leading it before the day off is voided -> nothing is reopened, but the old record still says the day off is gone
    item2 = one(w, "B", "L4", NEXT)
    made2 = day_off(w, NEXT, "Второй")
    w.db.query(type(w.G3)).filter_by(id=w.G3.id).update({"teacher_id": w.C.id})
    w.db.commit()
    void(w, made2)
    (voided2,) = kinds_of_events(w, item2["id"], "day_off_voided")
    assert voided2.snapshot["reopened"] is False
    assert [r.status for r in requests_of(w) if str(r.group_id) == str(w.G3.id)] == ["closed"]                          # nothing was opened for the lesson of B


# ============================================================================================================ A10
def test_a10_the_record_of_a_cancellation_learns_that_the_lesson_is_back(w, sync, clock):
    item = one(w, "A", "L1", NEXT)
    assert decide(w, "admin", item["id"], decision="cancel", reason="Преподаватель болен", cancellationKind="centre").status_code == 200
    clock.set("2026-10-21 14:01")                                                      # the records of one lesson are ordered by their time: the frozen clock would tie them
    restore(w, "admin", w.G1, "lesson-l1", NEXT, reason="По ошибке отменили")
    (restored,) = kinds_of_events(w, item["id"], "lesson_restored")
    assert restored.actor_id == w.admin.id and restored.snapshot == {"reason": "По ошибке отменили"}
    detail = client(w, "owner").get(f"{SUB}/{item['id']}").json()
    own = [e for e in detail["events"] if e["requestId"] == item["id"]]
    assert own[-1]["action"] == "lesson_restored" and own[-1]["snapshot"] == {"reason": "По ошибке отменили"}
    teacher_view = client(w, "A").get(f"{SUB}/{item['id']}").json()
    own = [e for e in teacher_view["events"] if e["requestId"] == item["id"]]
    assert own[-1] == {**own[-1], "action": "lesson_restored", "snapshot": {}, "actor": {"role": "admin"}}
    # cancel again, restore again: each cancellation record gets ITS event, the older one keeps its own
    clock.set("2026-10-21 14:02")
    sheet(w, "admin", w.G1, "lesson-l1", NEXT, reason="Опять")
    clock.set("2026-10-21 14:03")
    restore(w, "admin", w.G1, "lesson-l1", NEXT, reason="И опять вернули")
    again = [r for r in requests_of(w) if r.status == "cancelled" and r.id != UUID(item["id"])]
    assert len(again) == 1 and len(kinds_of_events(w, str(again[0].id), "lesson_restored")) == 1 and len(kinds_of_events(w, item["id"], "lesson_restored")) == 1


# ============================================================================================================ A5 / A6 / A7
def put(w, who, lesson_group, lesson, day, teacher, reason="Другой преподаватель"):
    r = client(w, who).put(f"{API}/org/groups/{lesson_group.id}/lesson-teacher", json={"lessonDate": day, "lessonId": lesson, "teacherId": str(teacher.id), "reason": reason})
    assert r.status_code == 200, r.text
    return r


def three_records(w, clock):
    """A asks, the administrator gives the lesson to D (r1); then to C (r2: no request, «Заменил администратор»); then back to D (r3)."""
    first = one(w, "A", "L1", DAY, comment="Температура")
    clock.set("2026-10-21 14:10")
    assert decide(w, "admin", first["id"], decision="substitute", teacherId=str(w.D.id)).status_code == 200
    clock.set("2026-10-21 14:20")
    put(w, "admin", w.G1, "lesson-l1", DAY, w.C)
    clock.set("2026-10-21 14:30")
    put(w, "owner", w.G1, "lesson-l1", DAY, w.D)
    by_time = sorted(requests_of(w), key=lambda r: r.created_at)
    assert len(by_time) == 3
    return [str(r.id) for r in by_time]


def test_a5_a_chain_of_three_records_is_one_history_with_the_pairs_folded(w, sync, clock):
    r1, r2, r3 = three_records(w, clock)
    mid = client(w, "owner").get(f"{SUB}/{r2}").json()
    assert [c["id"] for c in mid["chain"]] == [r1] and [n["id"] for n in mid["next"]] == [r3] and mid["item"]["id"] == r2 and mid["truncated"] is False
    first = client(w, "owner").get(f"{SUB}/{r1}").json()
    assert first["chain"] == [] and [n["id"] for n in first["next"]] == [r2, r3]                          # followers of followers
    last = client(w, "owner").get(f"{SUB}/{r3}").json()
    assert [c["id"] for c in last["chain"]] == [r1, r2] and last["next"] == []
    for body in (mid, first, last):                                                                         # the same history from every record
        assert [(e["requestId"], e["action"]) for e in body["events"]] == [(e["requestId"], e["action"]) for e in mid["events"]]
    events = mid["events"]
    assert all(e["requestId"] in (r1, r2, r3) for e in events)
    assert [e["at"] for e in events] == sorted(e["at"] for e in events)
    substituted = [e for e in events if e["action"] == "substituted"]
    assert [e["requestId"] for e in substituted] == [r1]                                                    # the two later «substituted» are folded into the «taken off» of the record before
    cleared = [e for e in events if e["action"] == "assignment_cleared"]
    assert [(e["requestId"], e.get("mergedRequestId"), e["snapshot"]["trigger"]) for e in cleared] == [(r1, r2, "put"), (r2, r3, "put")]
    assert [e["requestId"] for e in events if e["action"] == "requested"] == [r1, r2, r3]


def test_a5_the_order_is_by_time_then_by_the_place_in_the_chain_when_the_times_are_equal(w, sync, clock):
    r1, r2, r3 = three_records(w, clock)
    stamp = datetime(2026, 10, 21, 9, 0, 0)
    for e in events_of(w):
        e.created_at = stamp                                                                                # all the same instant (a frozen clock)
    w.db.commit()
    events = client(w, "owner").get(f"{SUB}/{r3}").json()["events"]
    order = {r1: 0, r2: 1, r3: 2}
    assert [order[e["requestId"]] for e in events] == sorted(order[e["requestId"]] for e in events)


def test_a5_a_teacher_reads_his_own_records_of_the_history_only(w, sync, clock):
    first = one(w, "A", "L1", DAY, comment="Секретная причина А")
    assert decide(w, "admin", first["id"], decision="substitute", teacherId=str(w.D.id)).status_code == 200
    clock.set("2026-10-21 14:20")
    second = req(w, "D", [(w.G1, "lesson-l1", DAY)], reason="personal", comment="Свой комментарий D").json()["created"][0]
    a_view = client(w, "A").get(f"{SUB}/{first['id']}").json()
    assert a_view["next"] == [] and {e["requestId"] for e in a_view["events"]} == {first["id"]} and "Свой комментарий D" not in json.dumps(a_view, ensure_ascii=False)
    d_view = client(w, "D").get(f"{SUB}/{second['id']}").json()
    assert d_view["chain"] == [] and {e["requestId"] for e in d_view["events"]} == {second["id"]} and "Секретная причина А" not in json.dumps(d_view, ensure_ascii=False)
    assert client(w, "A").get(f"{SUB}/{second['id']}").status_code == 403                                   # not his record
    both = client(w, "owner").get(f"{SUB}/{second['id']}").json()
    assert [c["id"] for c in both["chain"]] == [first["id"]] and {e["requestId"] for e in both["events"]} == {first["id"], second["id"]}


def test_a5_a_chain_longer_than_ten_says_it_is_cut(w, sync):
    from uuid import uuid4

    first = one(w, "A", "L1", DAY)
    base = requests_of(w)[0]
    prev, ids = base, [base]
    stamp = base.created_at
    for n in range(12):
        stamp = stamp + timedelta(minutes=1)
        row = Req(org_id=base.org_id, group_id=base.group_id, lesson_id=base.lesson_id, lesson_date=base.lesson_date, start_time=base.start_time, end_time=base.end_time, starts_at=base.starts_at,
                  requester_id=base.requester_id, origin="teacher", batch_id=uuid4(), reason_code="sick", status="withdrawn", decided_at=stamp, decided_by=base.requester_id, decided_via="withdraw",
                  previous_request_id=prev.id, created_at=stamp, updated_at=stamp)
        w.db.add(row)
        w.db.flush()
        ids.append(row)
        prev = row
    w.db.query(Req).filter_by(id=base.id).update({"status": "withdrawn", "decided_at": base.created_at, "decided_by": base.requester_id, "decided_via": "withdraw"})
    w.db.commit()
    tail = client(w, "owner").get(f"{SUB}/{ids[-1].id}").json()
    assert len(tail["chain"]) == 10 and tail["truncated"] is True and tail["chain"][-1]["id"] == str(ids[-2].id)       # the ten nearest, the oldest first
    head = client(w, "owner").get(f"{SUB}/{ids[0].id}").json()
    assert len(head["next"]) == 10 and head["truncated"] is True and head["next"][0]["id"] == str(ids[1].id)
    short = client(w, "owner").get(f"{SUB}/{ids[6].id}").json()
    assert len(short["chain"]) == 6 and len(short["next"]) == 6 and short["truncated"] is False


def test_a6_reading_the_detail_of_a_waiting_request_writes_nothing(w, sync):
    item = one(w, "A", "L1", DAY)
    w.db.query(Req).filter_by(id=UUID(item["id"])).update({"lesson_date": "2026-10-27"})                   # the lesson of the request is not on that date: the request is void, the list closes it
    w.db.commit()
    before = [(r.status, r.updated_at) for r in requests_of(w)]
    events_before = len(events_of(w))
    for who in ("admin", "A"):
        assert client(w, who).get(f"{SUB}/{item['id']}").status_code == 200
    assert [(r.status, r.updated_at) for r in requests_of(w)] == before and len(events_of(w)) == events_before        # GET detail did not close it, did not touch it
    assert client(w, "admin").get(SUB, params={"status": "pending"}).json()["total"] == 0                    # the list keeps the lazy pass
    assert [r.status for r in requests_of(w)] == ["closed"]


def test_a7_the_exam_service_is_asked_for_a_waiting_request_of_a_manager_only(w, sync, monkeypatch):
    from src.auth import exam_bridge

    calls = []
    monkeypatch.setattr(exam_bridge, "completion_check", lambda *a, **k: calls.append(a) or [])
    item = one(w, "A", "L1", DAY)
    assert client(w, "A").get(f"{SUB}/{item['id']}").json()["examBinding"] is None and calls == []             # a teacher does not need it
    assert client(w, "admin").get(f"{SUB}/{item['id']}").json()["examBinding"] == {"state": "none", "title": None, "canCancel": True} and len(calls) == 1
    assert decide(w, "admin", item["id"], decision="reject", reason="Нет замены").status_code == 200
    calls.clear()
    assert client(w, "admin").get(f"{SUB}/{item['id']}").json()["examBinding"] is None and calls == []        # decided: no call, no 5 s wait on a lying exam-service


# ============================================================================================================ A8 / A9
def test_a8_a_substitution_taken_off_just_now_lifts_its_row_and_the_window_counts_from_the_last_event(w, sync, clock):
    first = one(w, "A", "L1", DAY, comment="Первый")
    second = one(w, "B", "L4", DAY, comment="Второй")
    clock.set("2026-10-21 14:10")
    assert decide(w, "admin", first["id"], decision="substitute", teacherId=str(w.D.id)).status_code == 200
    clock.set("2026-10-21 14:20")
    assert decide(w, "admin", second["id"], decision="reject", reason="Нет замены").status_code == 200
    rows = client(w, "owner").get(SUB, params={"status": "decided"}).json()["items"]
    assert [i["id"] for i in rows] == [second["id"], first["id"]]                                               # by the decision: the newest first
    assert rows[0]["lastActivityAt"] == rows[0]["decision"]["at"]
    clock.set("2026-10-21 15:00")
    put(w, "admin", w.G1, "lesson-l1", DAY, w.C)                                                                 # the substitution of the FIRST record is taken over by C: its fate moves
    rows = client(w, "owner").get(SUB, params={"status": "decided"}).json()["items"]
    first_row = [i for i in rows if i["id"] == first["id"]][0]
    assert rows[0]["id"] != second["id"] and rows[0]["lastActivityAt"] >= first_row["lastActivityAt"] > first_row["decision"]["at"]
    assert [i["lastActivityAt"] for i in rows] == sorted((i["lastActivityAt"] for i in rows), reverse=True)    # the list is sorted by what the screen shows as «Когда»
    assert first_row["clearedAt"] is not None and first_row["lastActivityAt"] == first_row["clearedAt"]
    # «за 30 дней»: what happened in the last 30 days, not the date of the lesson
    clock.set("2026-11-25 12:00")
    assert client(w, "owner").get(SUB, params={"status": "decided"}).json()["items"] == []                      # the last event is 40 days old
    assert len(client(w, "owner").get(SUB, params={"status": "decided", "dateFrom": "2026-10-01"}).json()["items"]) == 3          # an explicit period is the period of the LESSONS, as before


def test_a9_the_closing_trigger_reaches_the_list(w, sync):
    one(w, "A", "L1", NEXT)
    made = day_off(w, NEXT, "Хайит")
    (closed,) = [i for i in decided(w) if i["decision"]["closedReason"] == "LESSON_CANCELLED"]
    assert closed["closedTrigger"] == "day_off" and closed["trigger"] is None                                  # how it ended is not how it began
    assert closed["decision"]["by"] is None                                                                  # ``via = system``: nobody decided it ...
    detail = client(w, "owner").get(f"{SUB}/{closed['id']}").json()
    ev = [e for e in detail["events"] if e["action"] == "closed"][0]
    assert ev["actor"]["id"] == str(w.owner.id) and ev["snapshot"]["trigger"] == "day_off"                      # ... the author of the closing is in the event
    pending = client(w, "owner").get(SUB, params={"status": "pending"}).json()["items"]
    assert pending == []
    void(w, made)
    reopened = client(w, "owner").get(SUB, params={"status": "pending"}).json()["items"]
    assert len(reopened) == 1 and reopened[0]["closedTrigger"] is None and reopened[0]["outcome"] == "pending"


# ============================================================================================================ A11
def test_a11_the_same_outcome_in_the_list_the_detail_mine_and_the_answer_of_the_decision(w, sync, clock):
    pending = one(w, "A", "L1", DAY)
    assert pending["outcome"] == "pending"
    second = one(w, "A", "L2", "2026-10-28")
    third = one(w, "B", "L4", DAY)
    answer = decide(w, "admin", pending["id"], decision="substitute", teacherId=str(w.D.id)).json()["item"]
    assert answer["outcome"] == "substituted"
    moved = decide(w, "admin", second["id"], decision="reschedule", reason="Перенос", newDate="2026-10-29", startTime="12:00", endTime="13:30").json()["item"]
    assert moved["outcome"] == "rescheduled"
    cancelled = decide(w, "admin", third["id"], decision="cancel", reason="Преподаватель болен", cancellationKind="centre").json()["item"]
    assert cancelled["outcome"] == "cancelled"
    clock.set("2026-10-21 14:30")
    restore(w, "admin", w.G3, "lesson-l4", DAY, reason="По ошибке")
    want = {pending["id"]: "substituted", second["id"]: "rescheduled", third["id"]: "lesson_restored"}                  # the cancellation was undone: its chip is «Урок возвращён»
    listed = {i["id"]: i["outcome"] for i in decided(w)}
    assert {k: listed[k] for k in want} == want
    assert {i["id"]: i["outcome"] for i in client(w, "A").get(f"{SUB}/mine").json()["items"]}.items() >= {pending["id"]: "substituted", second["id"]: "rescheduled"}.items()
    for rid, code in want.items():
        assert client(w, "owner").get(f"{SUB}/{rid}").json()["item"]["outcome"] == code
    clock.set("2026-10-21 14:40")
    put(w, "admin", w.G1, "lesson-l1", DAY, w.C)                                                                       # taken over by C
    assert client(w, "owner").get(f"{SUB}/{pending['id']}").json()["item"]["outcome"] == "substitution_replaced"
    clock.set("2026-10-21 14:50")
    client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": DAY, "lessonId": "lesson-l1", "teacherId": None, "reason": "Снова основной"})
    by_item = {i["id"]: i["outcome"] for i in decided(w)}
    assert by_item[pending["id"]] == "substitution_replaced"                                                           # r1's last fate is still «taken over by C» (C's own record carries the rest)
    assert "substitution_cleared" in by_item.values()                                                                  # C's record: the main teacher leads again
