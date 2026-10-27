"""LOGIC-B2 rights of the substitute (the lead's decision 21.10): access ONLY to the substitution lesson, 403 everywhere else; ``lesson-access`` for exam-service (D-1); ``mine`` / ``assignedToMe``."""
import pytest

from tests.b2_helpers import API, Golden, build_d2, client, clock, decide, fx, needs_pg, pg_session, req, requests_of  # noqa: F401  (fixtures)
from tests.test_ops_b2_decide_pg import L, one, sub_body  # noqa: F401

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"
B = f"{API}/org"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")
    return build_d2(pg_session)


def substituted(w):
    item = one(w, "A", "L1", "2026-10-26", reason="sick", comment="Температура", suggested="D")
    assert decide(w, "admin", item["id"], **sub_body(w, "D")).status_code == 200
    return item


def test_lesson_access_for_exam_service(w, clock):
    """D-1: ``GET /org/groups/{id}/lesson-access`` gives the group (name, grade) and ONLY the lessons of the substitution (golden); everybody else gets 403, also the group teacher and the managers (they use the group page)."""
    substituted(w)
    d = client(w, "D").get(f"{B}/groups/{w.G1.id}/lesson-access")
    assert d.status_code == 200, d.text
    g = Golden()
    assert not g(w, d.json(), "b2x_lesson_access.json"), g(w, d.json(), "b2x_lesson_access.json")
    assert "students" not in d.json() and "student_phone" not in d.text
    for who in ("A", "B", "C", "admin", "owner"):
        assert client(w, who).get(f"{B}/groups/{w.G1.id}/lesson-access").status_code == 403, who
    assert client(w, "D").get(f"{B}/groups/{w.G2.id}/lesson-access").status_code == 403           # another group
    # the assignment is taken off: the access goes at once; after the window it is over as well
    assert client(w, "admin").put(f"{B}/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "teacherId": None, "reason": "Передумали"}).status_code == 200
    assert client(w, "D").get(f"{B}/groups/{w.G1.id}/lesson-access").status_code == 403
    substituted_again = client(w, "admin").put(f"{B}/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "teacherId": str(w.D.id), "reason": "Снова"})
    assert substituted_again.status_code == 200
    clock.set("2026-11-03 10:00")                                                                  # 8 days after the lesson
    assert client(w, "D").get(f"{B}/groups/{w.G1.id}/lesson-access").status_code == 403


def test_mine_lists_the_requests_and_the_assigned_lessons(w, clock):
    """``GET /org/substitutions/mine``: A's requests and, for D, the future lessons he substitutes in (``assignedToMe``: who he replaces, no reason)."""
    g = Golden()
    clock.set("2026-10-21 14:00")
    substituted(w)
    clock.set("2026-10-21 14:00")
    d = client(w, "D").get(f"{SUB}/mine").json()
    assert d["items"] == [] and len(d["assignedToMe"]) == 1
    got = d["assignedToMe"][0]
    assert (got["groupName"], got["lessonId"], got["lessonDate"], got["startTime"], got["endTime"]) == ("9-А", "lesson-l1", "2026-10-26", "10:00", "11:30")
    assert got["replacing"] == {"id": str(w.A.id), "name": "Иванов И.", "role": "teacher"} and "reason" not in str(got) and "Температура" not in str(got)
    a = client(w, "A").get(f"{SUB}/mine").json()
    assert a["items"][0]["status"] == "substituted" and a["items"][0]["comment"] == "Температура" and a["assignedToMe"] == []


def test_the_substitute_reaches_nothing_but_his_lesson(w, clock):
    """Per endpoint (the lead's list: journal, lesson, students, payments, exams, substitutions): D is admitted to exactly the lesson of the substitution."""
    substituted(w)
    d, g = client(w, "D"), str(w.G1.id)
    assert d.get(f"{B}/groups/{g}").status_code == 403 and d.get(f"{B}/groups/{g}/stats").status_code == 403
    j = d.get(f"{B}/groups/{g}/attendance", params={"month": "2026-10"}).json()
    assert [x["instanceKey"] for x in j["lessons"]] == ["2026-10-26_lesson-l1"] and len(j["lessonRoster"]) == 3
    assert all(set(s) == {"id", "first_name", "last_name", "full_name", "display_id", "class_level"} for s in j["lessonRoster"])
    assert d.get(f"{B}/students/{w.s1.id}").status_code in (403, 404) and d.get(f"{B}/students/{w.s1.id}/profile").status_code in (403, 404)
    assert d.get(f"{B}/students/{w.s1.id}/attendance").status_code == 403
    assert d.get(f"{B}/payments/summary").status_code == 403 and d.get(f"{B}/groups/{g}/prices").status_code == 403
    assert d.get(f"{B}/payroll/months/2026-10").status_code == 403
    for path in ("", "/summary"):
        assert d.get(f"{SUB}{path}").status_code == 403
    assert d.post(f"{SUB}/decide-bulk", json={"ids": ["00000000-0000-0000-0000-000000000000"], "decision": "reject", "reason": "xxx"}).status_code == 403
    rid = str(requests_of(w)[0].id)
    assert d.get(f"{SUB}/{rid}").status_code == 403 and d.get(f"{SUB}/{rid}/candidates").status_code == 403
    assert d.post(f"{SUB}/{rid}/decide", json={"decision": "reject", "reason": "xxx"}).status_code == 403
    # what he may do: see the lesson in the schedule, ask for a replacement of it, work it
    assert [o["id"] for o in d.get(f"{B}/schedule/occurrences", params={"date_from": "2026-10-21", "date_to": "2026-11-20"}).json()] == ["lesson-l1"]
    assert req(w, "D", [L(w, "L1", "2026-10-26")]).status_code == 201
