"""Money-1 pack B8 (PostgreSQL): an excuse of a student who pays by the MONTH is the right to make up the missed lesson (the owner, 04.11: «денег не возвращаем; ученик не теряет занятие»).

A package gets the lesson back (the pack: ``students``); a month gives nothing to give back - the right to a make-up is the fact the answer names (``money_effects.makeup``); the money stays."""
from src.models.auth import AttendanceExcuse

from tests.pg_world import excused_lesson, make_client, needs_pg, payments_block, pg_session, world  # noqa: F401  (fixtures)

pytestmark = needs_pg
API = "/api/v1/auth"


def one_excuse(w):
    return w.db.query(AttendanceExcuse).filter_by(org_id=w.org.id).one()


def test_b8_the_confirmed_excuse_of_a_month_student_is_the_right_to_a_make_up_and_the_money_stays(world):
    w = world
    excused_lesson(w, complete=True)
    excuse = one_excuse(w)
    url = f"{API}/org/attendance/excuses/{excuse.id}"
    owner = make_client(w, user=w.owner)
    dry = owner.post(f"{url}/review", params={"dry_run": True}, json={"decision": "confirmed"})
    assert dry.status_code == 200 and dry.json()["status"] == "pending"                                  # a dry run writes nothing
    effects = dry.json()["money_effects"]
    assert effects["makeup"] == {"studentId": str(excuse.student_id), "name": effects["makeup"]["name"], "right": True,
                                 "text": "Ученик вправе отработать пропущенный урок; деньги не возвращаются"}
    assert effects["students"] == [] and effects["text"] == "Ученик вправе отработать пропущенный урок; деньги не возвращаются"
    done = owner.post(f"{url}/review", json={"decision": "confirmed"})
    assert done.status_code == 200 and done.json()["money_effects"]["makeup"]["right"] is True
    reset = owner.post(f"{url}/reset", params={"dry_run": True})
    assert reset.json()["money_effects"]["makeup"] == {**reset.json()["money_effects"]["makeup"], "right": False, "text": "Право ученика на отработку этого урока снимается"}


def test_b8_a_rejected_excuse_gives_no_right_and_says_nothing(world):
    w = world
    excused_lesson(w, complete=True)
    excuse = one_excuse(w)
    dry = make_client(w, user=w.owner).post(f"{API}/org/attendance/excuses/{excuse.id}/review", params={"dry_run": True}, json={"decision": "rejected"})
    assert "makeup" not in dry.json()["money_effects"]


def test_b8_a_package_student_gets_the_lesson_back_in_his_pack_not_a_make_up(world):
    w = world
    payments_block(w.db, w.org, period_mode="lessons", lessons_per_payment=12, billing_history=[{"from": "min", "period_mode": "lessons", "lessons_per_payment": 12}])
    excused_lesson(w, complete=True)
    excuse = one_excuse(w)
    dry = make_client(w, user=w.owner).post(f"{API}/org/attendance/excuses/{excuse.id}/review", params={"dry_run": True}, json={"decision": "confirmed"})
    effects = dry.json()["money_effects"]
    assert "makeup" not in effects and [s["delta"] for s in effects["students"]] == [1]                  # the lesson goes back into the pack
