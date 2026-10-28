"""#157 A1 / A2 (PostgreSQL): what a TEACHER reads in the journal «Замены» - not the name of another requester / the substitute of somebody else's record, not the name of the
administrator, not the keys of the machinery - and what NOBODY reads (the snapshot of an event is cut to a white list; LESSONS 45: no money in the events).

Sources: substitutions-page-ux.md Ц1 / Ц2 / Ц13, P157_API.md §5-6."""
import json
from uuid import UUID

import pytest

from src.auth.lesson_substitutions import SNAPSHOT_KEYS, _MANAGER_ONLY, _PREVIOUS_KEYS
from src.auth.ops_rules import short_name
from src.models.auth import LessonSubstitutionEvent, LessonSubstitutionRequest as Req
from tests.b2_helpers import API, build_d2, clock, decide, events_of, needs_pg, pg_session, req, requests_of  # noqa: F401  (fixtures)
from tests.ops_helpers import client
from tests.test_ops_b2_decide_pg import L, Sync, one  # noqa: F401
from tests.test_w1_cancel_pg import restore, sheet

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"
DAY = "2026-10-26"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")                        # Wednesday
    return build_d2(pg_session)


@pytest.fixture
def sync(monkeypatch):
    return Sync(monkeypatch)


def chain_of_two(w):
    """A asks (secret reason), the administrator gives the lesson to D, D asks in his turn: the record of D points to the record of A."""
    first = one(w, "A", "L1", DAY, reason="sick", comment="Секретная причина А")
    assert decide(w, "admin", first["id"], decision="substitute", teacherId=str(w.D.id)).status_code == 200
    second = req(w, "D", [(w.G1, "lesson-l1", DAY)], reason="personal", comment="Свой комментарий D")
    assert second.status_code == 201, second.text
    return first, second.json()["created"][0]


def names_of(w, *keys):
    return [short_name(getattr(w, k)) for k in keys]


def test_a1_a_teacher_does_not_read_the_name_of_the_requester_before_him_in_any_answer(w, sync):
    first, second = chain_of_two(w)
    a_name, d_name = short_name(w.A), short_name(w.D)
    assert second["previous"] is None                                                              # the answer of POST (create)
    detail = client(w, "D").get(f"{SUB}/{second['id']}").json()
    assert detail["item"]["previous"] is None and a_name not in json.dumps(detail, ensure_ascii=False)
    mine = client(w, "D").get(f"{SUB}/mine").json()
    mine_second = [i for i in mine["items"] if i["id"] == second["id"]][0]
    assert mine_second["previous"] is None and "Секретная причина А" not in json.dumps(mine, ensure_ascii=False)
    # the manager still reads the whole history
    seen = client(w, "admin").get(f"{SUB}/{second['id']}").json()
    assert seen["item"]["previous"]["id"] == first["id"] and seen["item"]["previous"]["teacherName"] == a_name and d_name in json.dumps(seen, ensure_ascii=False)
    listed = [i for i in client(w, "admin").get(SUB, params={"status": "all"}).json()["items"] if i["id"] == second["id"]][0]
    assert listed["previous"]["teacherName"] == a_name


def test_a1_his_own_previous_record_stays_visible_to_him(w):
    first = one(w, "A", "L1", DAY, comment="Первый")
    assert client(w, "A").post(f"{SUB}/{first['id']}/withdraw").status_code == 200
    again = req(w, "A", [(w.G1, "lesson-l1", DAY)], comment="Второй").json()["created"][0]
    assert again["previous"]["id"] == first["id"]                                                  # the same teacher: his own earlier request
    assert client(w, "A").get(f"{SUB}/{again['id']}").json()["item"]["previous"]["id"] == first["id"]


def test_a1_a_teacher_reads_the_author_of_the_events_by_the_role_only(w, sync):
    first = one(w, "A", "L1", DAY)
    assert decide(w, "admin", first["id"], decision="substitute", teacherId=str(w.D.id)).status_code == 200
    assert client(w, "owner").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": DAY, "lessonId": "lesson-l1", "teacherId": str(w.C.id), "reason": "Другой"}).status_code == 200
    mine = client(w, "A").get(f"{SUB}/{first['id']}")
    assert mine.status_code == 200
    body = mine.json()
    text = json.dumps(body, ensure_ascii=False)
    for who in ("admin", "owner"):                                                                 # neither the name nor the id of the administrator / the owner
        assert short_name(getattr(w, who)) not in text and str(getattr(w, who).id) not in text
    assert body["events"] and all(e["actor"] is None or set(e["actor"]) == {"role"} for e in body["events"])
    assert {e["actor"]["role"] for e in body["events"] if e["actor"]} >= {"teacher", "admin"}
    assert body["item"]["decision"]["by"] == {"id": None, "name": None, "role": "admin"}
    assert body["item"]["clearedBy"] == {"id": None, "name": None, "role": "owner"}
    listed = client(w, "A").get(f"{SUB}/mine").json()["items"]
    assert all(short_name(w.admin) not in json.dumps(i, ensure_ascii=False) and short_name(w.owner) not in json.dumps(i, ensure_ascii=False) for i in listed)
    # the manager keeps the names
    seen = client(w, "owner").get(f"{SUB}/{first['id']}").json()
    assert seen["item"]["decision"]["by"]["name"] == short_name(w.admin)
    assert {e["actor"]["name"] for e in seen["events"] if e["actor"]} >= {short_name(w.A), short_name(w.admin), short_name(w.owner)}
    assert all(set(e["actor"]) == {"id", "name", "role"} for e in seen["events"] if e["actor"])


def test_a1_his_own_name_is_his_in_a_decision_he_made_himself(w, sync):
    """A teacher who moves his own lesson is the author of the decision: ``decision.by`` is himself (not masked)."""
    r = client(w, "A").post(f"{API}/org/groups/{w.G1.id}/lesson-move", json={"lessonId": "lesson-l1", "lessonDate": DAY, "newDate": "2026-10-28", "startTime": "12:00", "endTime": "13:30", "reason": "Перенос"})
    assert r.status_code == 200, r.text
    item = r.json()["item"]
    assert item["decision"]["by"]["name"] == short_name(w.A) and item["decision"]["by"]["id"] == str(w.A.id)


def collect_keys(w):
    w.db.expire_all()
    out = {}
    for e in w.db.query(LessonSubstitutionEvent).filter_by(org_id=w.org.id).all():
        out.setdefault(e.action, set()).update((e.snapshot or {}).keys())
    return out


def test_a1_the_snapshot_of_an_event_is_cut_to_the_white_list_for_everybody(w, sync):
    """The limiter (LESSONS 45): whatever a future writer puts into a snapshot - a sum, a rate, an id of the machinery - never reaches a screen."""
    first = one(w, "A", "L1", DAY, comment="Причина")
    rogue = {"payEstimate": 123456, "amount": 5000, "percent": 30, "rate": 1, "batchId": "x", "assignmentId": "y", "previousRequestId": "z", "removedTeacherId": "u", "secret": "!"}
    for e in events_of(w, UUID(first["id"])):
        e.snapshot = {**(e.snapshot or {}), **rogue}
    w.db.commit()
    for who in ("owner", "admin", "A"):
        body = client(w, who).get(f"{SUB}/{first['id']}").json()
        assert body["events"], who
        for e in body["events"]:
            assert set(e["snapshot"]) <= set(SNAPSHOT_KEYS[e["action"]]), (who, e["action"], e["snapshot"])
            assert not set(e["snapshot"]) & set(rogue)
        text = json.dumps(body)
        assert "123456" not in text and "payEstimate" not in text and "5000" not in text


def test_a1_the_kind_of_a_cancellation_and_the_reason_of_a_restore_are_the_managers_only(w, sync):
    item = one(w, "A", "L1", DAY, comment="Болею")
    assert decide(w, "admin", item["id"], decision="cancel", reason="Преподаватель болен", cancellationKind="centre").status_code == 200
    restore(w, "admin", w.G1, "lesson-l1", DAY, reason="Секретная причина возврата")
    cancelled = client(w, "owner").get(f"{SUB}/{item['id']}").json()
    assert [e["snapshot"].get("cancellationKind") for e in cancelled["events"] if e["action"] == "cancelled"] == ["centre"]
    mine_ids = [i["id"] for i in client(w, "A").get(f"{SUB}/mine").json()["items"]]
    assert item["id"] in mine_ids
    teacher_view = client(w, "A").get(f"{SUB}/{item['id']}").json()
    assert all("cancellationKind" not in e["snapshot"] for e in teacher_view["events"])
    for who, shown in (("owner", True), ("A", False)):
        body = client(w, who).get(f"{SUB}/{item['id']}").json()
        text = json.dumps(body, ensure_ascii=False)
        assert ("Секретная причина возврата" in text) is shown, who                                  # the manager reads it in the event, a teacher never (also not in the record of the return)
        back = [e for e in body["events"] if e["action"] == "lesson_restored"]
        assert len(back) == 1 and not [e for e in body["events"] if e["action"] == "closed" and e["snapshot"].get("closedReason") == "LESSON_RESTORED"]        # one event for the pair (R3 11)
        snap = back[0]["snapshot"]
        assert set(snap) <= set(SNAPSHOT_KEYS["lesson_restored"]) and ({"reason", "previous"} <= set(snap)) is shown
        if shown:
            assert set(snap["previous"]) <= set(_PREVIOUS_KEYS) and snap["previous"]["cancellationKind"] == "centre"


def test_a1_every_key_the_code_writes_is_on_the_list_or_cut_on_purpose(w, sync):
    """The writers of the events (a request, a decision of every kind, a substitution put and taken off, a cancel, a restore, a kind change, a reopen) leave keys; the keys that are NOT on the
    list are the machinery (ids of batches / assignments / previous records) - the test names them, so that a NEW key makes it fail until somebody decides."""
    first = one(w, "A", "L1", DAY, comment="x")
    decide(w, "admin", first["id"], decision="substitute", teacherId=str(w.D.id))
    client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": DAY, "lessonId": "lesson-l1", "teacherId": str(w.C.id), "reason": "Другой"})
    second = one(w, "B", "L4", "2026-10-26", comment="Другой")
    decide(w, "admin", second["id"], decision="reject", reason="Нет замены")
    third = one(w, "A", "L2", "2026-10-28")
    decide(w, "admin", third["id"], decision="reschedule", reason="Перенос", newDate="2026-10-29", startTime="12:00", endTime="13:30")
    fourth = one(w, "A", "L3", "2026-10-27")
    client(w, "A").post(f"{SUB}/{fourth['id']}/withdraw")
    sheet(w, "admin", w.G1, "lesson-l1", "2026-11-02", reason="Ошибка")
    client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/attendance/cancellation-kind", json={"lessonDate": "2026-11-02", "lessonId": "lesson-l1", "cancellationKind": "holiday", "reason": "Праздник"})
    restore(w, "admin", w.G1, "lesson-l1", "2026-11-02")
    machinery = {"batchId", "assignmentId", "suggestedTeacherId", "previousRequestId", "removedTeacherId", "version", "cancelledVersion"}
    written = collect_keys(w)
    assert {"requested", "substituted", "rejected", "rescheduled", "assignment_cleared", "withdrawn", "closed"} <= set(written)
    for action, keys in written.items():
        unknown = keys - set(SNAPSHOT_KEYS[action]) - machinery
        assert not unknown, f"{action}: the key(s) {sorted(unknown)} are written but neither on the white list nor in the machinery set - decide (LESSONS 45)"
    assert set(_MANAGER_ONLY) <= set(SNAPSHOT_KEYS)
    for action, hidden in _MANAGER_ONLY.items():
        assert set(hidden) <= set(SNAPSHOT_KEYS[action])


def test_a2_what_an_administrator_wrote_on_his_own_action_is_not_in_the_teachers_records_but_the_text_of_a_reopened_one_is(w, sync):
    """A2: the record a substitution set from the lesson sheet / a move / a restore / a kind change made carries the reason of the ADMINISTRATOR in ``comment``: the teacher of the lesson
    (the requester of that system record) reads ``null`` there; the record the system REOPENED keeps its text (the explanation, LESSONS 46)."""
    assert client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": DAY, "lessonId": "lesson-l1", "teacherId": str(w.D.id), "reason": "Админская причина"}).status_code == 200
    sheet(w, "admin", w.G1, "lesson-l1", "2026-11-02", reason="Ошибка")
    client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/attendance/cancellation-kind", json={"lessonDate": "2026-11-02", "lessonId": "lesson-l1", "cancellationKind": "holiday", "reason": "Причина смены вида"})
    restore(w, "admin", w.G1, "lesson-l1", "2026-11-02", reason="Причина возврата")
    member = w.member("D")
    assert client(w, "owner").put(f"{API}/org/members/{member.id}/role", params={"role": "admin"}).status_code == 200            # D stops being a teacher: A's lesson is back to A, a new request is reopened
    items = client(w, "A").get(f"{SUB}/mine").json()["items"]
    triggered = [i for i in items if i["origin"] == "system" and i["trigger"]]
    assert {i["trigger"] for i in triggered} >= {"manager_assign", "manager_restore", "manager_reason_change"}
    assert all(i["comment"] is None for i in triggered)
    reopened = [i for i in items if i["origin"] == "system" and i["trigger"] is None]
    assert len(reopened) == 1 and "больше не ведёт уроки" in reopened[0]["comment"]
    text = json.dumps(items, ensure_ascii=False)
    for secret in ("Админская причина", "Причина смены вида", "Причина возврата"):
        assert secret not in text
        assert secret in json.dumps(client(w, "admin").get(SUB, params={"status": "decided", "dateFrom": "2026-10-01"}).json(), ensure_ascii=False)       # the manager reads them
    for i in triggered:                                                                                  # the same in the detail
        assert client(w, "A").get(f"{SUB}/{i['id']}").json()["item"]["comment"] is None


# ============================================================================================================ R3 fix round
def test_r3_the_substitute_reads_who_took_the_lesson_off_him_by_the_role_only(w, sync):
    """/mine ``clearedForMe``: D was the substitute; the administrator took it off, then another teacher cancelled a lesson of his - no name, no id of the author in either."""
    assert client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": DAY, "lessonId": "lesson-l1", "teacherId": str(w.D.id), "reason": "Болезнь"}).status_code == 200
    assert client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": DAY, "lessonId": "lesson-l1", "teacherId": None, "reason": "Снова основной"}).status_code == 200
    assert client(w, "owner").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": "2026-11-02", "lessonId": "lesson-l1", "teacherId": str(w.D.id), "reason": "Болезнь"}).status_code == 200
    sheet(w, "owner", w.G1, "lesson-l1", "2026-11-02", reason="Урока не будет")                                # the owner cancels the lesson D was to lead: D's substitution is taken off by the owner
    body = client(w, "D").get(f"{SUB}/mine").json()
    cleared = body["clearedForMe"]
    assert len(cleared) == 2 and {c["lessonDate"] for c in cleared} == {DAY, "2026-11-02"}
    assert all(c["clearedBy"] is not None and c["clearedBy"]["id"] is None and c["clearedBy"]["name"] is None for c in cleared)
    assert {c["clearedBy"]["role"] for c in cleared} == {"admin", "owner"}
    text = json.dumps(body, ensure_ascii=False)
    for who in ("admin", "owner"):
        assert short_name(getattr(w, who)) not in text and str(getattr(w, who).id) not in text, who
    # what the substitute is told about the lessons he will lead: the colleague he replaces (a teacher), never a manager
    assert client(w, "owner").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": "2026-11-09", "lessonId": "lesson-l1", "teacherId": str(w.D.id), "reason": "Болезнь"}).status_code == 200
    assigned = client(w, "D").get(f"{SUB}/mine").json()["assignedToMe"]
    assert assigned and all(a["replacing"]["role"] == "teacher" for a in assigned)
    assert short_name(w.admin) not in json.dumps(assigned, ensure_ascii=False) and short_name(w.owner) not in json.dumps(assigned, ensure_ascii=False)
    # the author keeps his own name when he is the viewer
    own = client(w, "A").get(f"{SUB}/mine").json()
    assert own["clearedForMe"] == []


def test_r3_a_teacher_reads_that_a_day_off_did_it_but_not_which_day_off(w, sync):
    item = one(w, "A", "L1", "2026-11-02")
    assert decide(w, "admin", item["id"], decision="substitute", teacherId=str(w.D.id)).status_code == 200
    made = client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": "2026-11-02", "dateTo": "2026-11-02", "reason": "Хайит"}).json()
    day_off_id = made["dayOff"]["id"]
    manager = client(w, "owner").get(f"{SUB}/{item['id']}").json()
    teacher = client(w, "A").get(f"{SUB}/{item['id']}").json()
    cleared_m = [e for e in manager["events"] if e["action"] == "assignment_cleared"][0]["snapshot"]
    cleared_t = [e for e in teacher["events"] if e["action"] == "assignment_cleared"][0]["snapshot"]
    assert cleared_m["dayOffId"] == day_off_id and cleared_m["byDayOff"] is True
    assert "dayOffId" not in cleared_t and cleared_t["byDayOff"] is True and day_off_id not in json.dumps(teacher)
    assert client(w, "owner").post(f"{API}/org/days-off/{day_off_id}/void").status_code == 200
    voided = [e for e in client(w, "A").get(f"{SUB}/{item['id']}").json()["events"] if e["action"] == "day_off_voided"][0]["snapshot"]
    assert "dayOffId" not in voided and voided["byDayOff"] is True and set(voided) <= set(SNAPSHOT_KEYS["day_off_voided"])
    pending = req(w, "A", [(w.G1, "lesson-l1", "2026-11-09")]).json()["created"][0]                    # a request closed by a day off on another date: its closing event
    made2 = client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": "2026-11-09", "dateTo": "2026-11-09", "reason": "Второй"}).json()
    closed = [e for e in client(w, "A").get(f"{SUB}/{pending['id']}").json()["events"] if e["action"] == "closed"][0]["snapshot"]
    assert closed["trigger"] == "day_off" and closed["byDayOff"] is True and "dayOffId" not in closed and made2["dayOff"]["id"] not in json.dumps(closed)
    closed_m = [e for e in client(w, "owner").get(f"{SUB}/{pending['id']}").json()["events"] if e["action"] == "closed"][0]["snapshot"]
    assert closed_m["dayOffId"] == made2["dayOff"]["id"]


def test_r3_a_record_of_another_teacher_does_not_exist_for_a_teacher(w, sync):
    first, second = chain_of_two(w)
    foreign = client(w, "A").get(f"{SUB}/{second['id']}")
    unknown = client(w, "A").get(f"{SUB}/00000000-0000-0000-0000-000000000000")
    assert foreign.status_code == 404 and foreign.json() == unknown.json() and unknown.json()["detail"]["code"] == "SUBSTITUTION_NOT_FOUND"
    assert client(w, "admin").get(f"{SUB}/{second['id']}").status_code == 200
    assert client(w, "owner", org="other").get(f"{SUB}/{second['id']}").status_code in (403, 404)


def test_r3_the_context_of_the_items_has_no_default_viewer():
    from src.auth.lesson_substitutions import Ctx

    with pytest.raises(TypeError):
        Ctx()
