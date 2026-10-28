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
    detail = [client(w, "owner").get(f"{SUB}/{i['id']}").json() for i in kinds]
    changes = sorted(((e["snapshot"]["cancellationKind"]["from"], e["snapshot"]["cancellationKind"]["to"]) for d in detail for e in d["events"] if e["action"] == "closed"))
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
    assert detail["events"][-1]["action"] == "lesson_restored" and detail["events"][-1]["snapshot"] == {"reason": "По ошибке отменили"}
    teacher_view = client(w, "A").get(f"{SUB}/{item['id']}").json()
    assert teacher_view["events"][-1] == {**teacher_view["events"][-1], "action": "lesson_restored", "snapshot": {}, "actor": {"role": "admin"}}
    # cancel again, restore again: each cancellation record gets ITS event, the older one keeps its own
    clock.set("2026-10-21 14:02")
    sheet(w, "admin", w.G1, "lesson-l1", NEXT, reason="Опять")
    clock.set("2026-10-21 14:03")
    restore(w, "admin", w.G1, "lesson-l1", NEXT, reason="И опять вернули")
    again = [r for r in requests_of(w) if r.status == "cancelled" and r.id != UUID(item["id"])]
    assert len(again) == 1 and len(kinds_of_events(w, str(again[0].id), "lesson_restored")) == 1 and len(kinds_of_events(w, item["id"], "lesson_restored")) == 1
