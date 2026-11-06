"""Money-1 pack B8 (PostgreSQL): an excuse of a student who pays by the MONTH is the right to make up the missed lesson (the owner, 04.11: «денег не возвращаем; ученик не теряет занятие»).

A package gets the lesson back (the pack: ``students``); a month gives nothing to give back - the right to a make-up is the fact the answer names (``money_effects.makeup``); the money stays."""
from src.models.auth import AttendanceExcuse

from tests.pg_world import excused_lesson, make_client, needs_pg, payments_block, pg_session, world  # noqa: F401  (fixtures)

pytestmark = needs_pg
API = "/api/v1/auth"


def one_excuse(w):
    return w.db.query(AttendanceExcuse).filter_by(org_id=w.org.id).one()


def test_b8_the_confirmed_excuse_of_a_month_student_says_what_he_can_do_and_promises_nothing_else(world):
    w = world
    excused_lesson(w, complete=True)
    excuse = one_excuse(w)
    url = f"{API}/org/attendance/excuses/{excuse.id}"
    owner = make_client(w, user=w.owner)
    dry = owner.post(f"{url}/review", params={"dry_run": True}, json={"decision": "confirmed"})
    assert dry.status_code == 200 and dry.json()["status"] == "pending"                                  # a dry run writes nothing
    effects = dry.json()["money_effects"]
    text = "Ученик может отработать пропущенный урок на дополнительном уроке или в другой группе"           # R3 №15: no «вправе», no term, no record of a right
    assert effects["makeup"] == {"studentId": str(excuse.student_id), "name": effects["makeup"]["name"], "text": text}
    assert effects["students"] == [] and effects["text"] == text
    assert "вправе" not in effects["text"] and "право" not in effects["text"].lower()
    done = owner.post(f"{url}/review", json={"decision": "confirmed"})
    assert done.status_code == 200 and done.json()["money_effects"]["makeup"]["text"] == text
    reset = owner.post(f"{url}/reset", params={"dry_run": True})                                         # the withdrawal of the confirmation says nothing about a make-up
    assert "makeup" not in reset.json()["money_effects"]


def test_b8_a_failure_of_the_make_up_sentence_does_not_cost_the_decision_on_the_excuse(world, monkeypatch):
    """R3 code №5: the reading of the account is outside the decision - it fails, the excuse is confirmed all the same, there is no ``makeup`` block."""
    from src.auth import money_effects

    w = world
    excused_lesson(w, complete=True)
    excuse = one_excuse(w)

    def boom(*args, **kwargs):
        raise RuntimeError("the ledger is down")

    monkeypatch.setattr(money_effects, "calendar_mode_students", boom)
    done = make_client(w, user=w.owner).post(f"{API}/org/attendance/excuses/{excuse.id}/review", json={"decision": "confirmed"})
    assert done.status_code == 200 and done.json()["status"] == "confirmed" and "makeup" not in done.json()["money_effects"]
    w.db.expire_all()
    assert one_excuse(w).status == "confirmed"


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
