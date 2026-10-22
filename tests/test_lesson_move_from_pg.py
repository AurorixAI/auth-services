"""#124 (F1..F6): the new lesson that a move created knows where it came from (`movedFrom`): group week / teacher schedule, group journal, student card.
Both ways of moving (the decision on a teacher's request and the move from the lesson sheet) give the same field."""
import pytest

from src.models.auth import GroupAttendance

from tests.b2_helpers import API, client, clock, needs_pg, pg_session  # noqa: F401
from tests.test_lesson_move_direct_pg import BODY, move
from tests.test_ops_b2_decide_pg import one, sync, w  # noqa: F401
from tests.b2_helpers import decide

pytestmark = needs_pg
FROM = {"lessonId": "lesson-l2", "date": "2026-10-28", "startTime": "16:00", "endTime": "17:30"}


def occurrences(w, who, day="2026-10-29", teacher=None):
    params = {"date_from": day, "date_to": day, **({"teacher_id": teacher} if teacher else {})}
    return client(w, who).get(f"{API}/org/schedule/occurrences", params=params).json()


def make_move(w, path):
    if path == "request":
        item = one(w, "A", "L2", "2026-10-28")
        r = decide(w, "admin", item["id"], decision="reschedule", newDate="2026-10-29", startTime="16:00", endTime="17:30", reason=BODY["reason"])
    else:
        r = move(w)
    assert r.status_code == 200, r.text
    return r.json()["result"]["newLesson"]["id"]


@pytest.mark.parametrize("path", ["request", "sheet"])
def test_f1_the_schedule_marks_the_new_lesson_for_everybody_who_sees_it(w, sync, path):
    new_id = make_move(w, path)
    for who in ("admin", "owner", "A"):                                           # the manager and the teacher of the new lesson
        mine = [o for o in occurrences(w, who) if o["id"] == new_id]
        assert len(mine) == 1 and mine[0]["movedFrom"] == FROM, (who, mine)
        assert "reason" not in str(mine[0]["movedFrom"]).lower() and BODY["reason"] not in str(mine[0]["movedFrom"])
    others = [o for o in occurrences(w, "admin") if o["id"] != new_id]
    assert others and all(o["movedFrom"] is None for o in others)                # F5
    original = next(o for o in occurrences(w, "admin", "2026-10-28") if o["id"] == "lesson-l2")
    assert original["movedFrom"] is None and original["substitutionOutcome"]["kind"] == "rescheduled"


@pytest.mark.parametrize("path", ["request", "sheet"])
def test_f2_f3_the_journal_and_the_student_card(w, sync, path):
    new_id = make_move(w, path)
    # the new lesson is held: it appears in the journal of the group and in the card of the student
    from tests.ops_helpers import history_row

    history_row(w.db, w.org, w.G1, new_id, "2026-10-29", w.A, [w.s1, w.s2, w.s3], start="16:00", end="17:30")
    w.db.commit()
    journal = client(w, "admin").get(f"{API}/org/groups/{w.G1.id}/attendance", params={"month": "2026-10"}).json()
    lessons = {l["id"]: l for l in journal["lessons"]}
    assert lessons[new_id]["movedFrom"] == FROM
    assert all("movedFrom" not in l for k, l in lessons.items() if k != new_id)
    card = client(w, "admin").get(f"{API}/org/students/{w.s1.id}/attendance", params={"month": "2026-10"}).json()
    held = [l for l in card["lessons"] if l["lesson_id"] == new_id]
    assert len(held) == 1 and held[0]["movedFrom"] == FROM
    assert all("movedFrom" not in l for l in card["lessons"] if l["lesson_id"] != new_id)
    assert any(n["kind"] == "rescheduled" and n["movedTo"]["lessonId"] == new_id for n in card["notices"])      # the original's notice still says where it went


def test_f4_a_second_move_points_to_the_first_new_lesson(w, sync):
    first = make_move(w, "sheet")
    r = client(w, "admin").post(f"{API}/org/groups/{w.G1.id}/lesson-move", json={"lessonId": first, "lessonDate": "2026-10-29", "newDate": "2026-10-30",
                                                                                 "startTime": "16:00", "endTime": "17:30", "reason": "Ещё раз"})
    assert r.status_code == 200, r.text
    second = r.json()["result"]["newLesson"]["id"]
    o = {x["id"]: x for x in occurrences(w, "admin", "2026-10-29")}
    assert o[first]["substitutionOutcome"]["kind"] == "rescheduled" and o[first]["movedFrom"] == FROM       # the first keeps its own origin
    assert {x["id"]: x for x in occurrences(w, "admin", "2026-10-30")}[second]["movedFrom"] == {"lessonId": first, "date": "2026-10-29", "startTime": "16:00", "endTime": "17:30"}


def test_f6_both_paths_give_the_same_value(w, sync):
    a = make_move(w, "request")
    got = [o for o in occurrences(w, "admin") if o["id"] == a][0]["movedFrom"]
    assert set(got) == {"lessonId", "date", "startTime", "endTime"} and got == FROM
