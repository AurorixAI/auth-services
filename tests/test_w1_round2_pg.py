"""W1 round 2 (28.10): S2-1 (the money of a teacher is the owner's), #155 (lines of the day off, the group form), #156 («уроки ведёт только преподаватель»).  PostgreSQL.

Sources: artifacts/analysis/W1_FIX2_SPEC.md, only-teachers-teach.md (§5, §10, §11); LESSONS 45, 46, 47, 48, 49."""
from datetime import date

import pytest

from src.models.auth import (
    Group, GroupAudit, LessonSubstitutionEvent, LessonSubstitutionRequest as Req, LessonTeacherAssignment, OrganizationMember, PayrollRate, PayrollSheet,
)
from src.auth.ops_rules import short_name
from tests.b2_helpers import API, build_d2, clock, decide, events_of, needs_pg, pg_session, req, requests_of  # noqa: F401  (fixtures)
from tests.ops_helpers import client, history_row, new_group, new_slot
from tests.test_ops_b2_decide_pg import L, one, sub_body

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"
LT = f"{API}/org/groups/{{gid}}/lesson-teacher"
M = f"{API}/org/members/{{mid}}"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")                        # Wednesday
    return build_d2(pg_session)


def mid(w, key):
    return str(w.ids("member_" + key))


def put_teacher(w, who, day, teacher, lesson="lesson-l1", group=None):
    return client(w, who).put(LT.format(gid=(group or w.G1).id), json={"lessonDate": day, "lessonId": lesson, "teacherId": str(getattr(w, teacher).id), "reason": "Болезнь"})


# ============================================================================================================ S2-1: the money of a teacher
def test_s2_1_the_owner_sees_the_sheet_and_its_payout_state_the_admin_only_the_month(w):
    for teacher_id in (w.A.id, w.D.id):
        w.db.add(PayrollSheet(org_id=w.org.id, teacher_id=teacher_id, period_month=date(2026, 10, 1), source_fingerprint="x", total_amount=100, created_by=w.owner.id,
                              updated_by=w.owner.id))
    w.db.commit()
    mine = put_teacher(w, "owner", "2026-10-12", "D")                      # a lesson A held: A's and D's sheets of October become «data changed»
    assert mine.status_code == 200, mine.text
    sheets = mine.json()["payrollSheetsAffected"]
    assert len(sheets) == 2 and all(set(s) == {"sheet_id", "teacher_id", "month", "status"} and s["status"] == "unpaid" for s in sheets)
    theirs = put_teacher(w, "admin", "2026-10-19", "D")
    assert theirs.status_code == 200, theirs.text
    assert theirs.json()["payrollSheetsAffected"] == [{"month": "2026-10"}]          # no sheet, no teacher, no status: the toast «Ведомость за октябрь изменится» only
    assert "sheet_id" not in theirs.text and "unpaid" not in theirs.text and str(w.A.id) not in str(theirs.json()["payrollSheetsAffected"])


def test_s2_2_the_payroll_locked_code_is_gone(w):
    from src.auth import ops_rules, payroll_hooks

    assert "PAYROLL_LOCKED" not in ops_rules.ERROR_CATALOGUE
    assert not hasattr(payroll_hooks, "PayrollLocked") and not hasattr(payroll_hooks, "assert_lesson_teacher_change_allowed")


# ============================================================================================================ #155
def test_155_7_the_group_form_cannot_unassign_the_teacher_and_a_null_class_clears_it(w):
    cl = client(w, "admin")
    r = cl.put(f"{API}/org/groups/{w.G1.id}", json={"teacher_id": None})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "TEACHER_REQUIRED" and r.json()["detail"]["message"] == "Выберите преподавателя"
    w.db.expire_all()
    assert w.db.get(Group, w.G1.id).teacher_id == w.A.id
    assert w.db.get(Group, w.G3.id).class_level == 8
    r = cl.put(f"{API}/org/groups/{w.G3.id}", json={"class_level": None})                # explicit null clears
    assert r.status_code == 200 and r.json()["class_level"] is None, r.text
    w.db.expire_all()
    assert w.db.get(Group, w.G3.id).class_level is None
    audit = w.db.query(GroupAudit).filter_by(group_id=w.G3.id, action="updated").one()
    assert audit.snapshot == {"class_level": [8, None]}
    r = cl.put(f"{API}/org/groups/{w.G3.id}", json={"name": "8-В новая"})              # a field that is not sent stays as it is
    assert r.status_code == 200
    w.db.expire_all()
    assert w.db.get(Group, w.G3.id).class_level is None and w.db.get(Group, w.G3.id).teacher_id == w.B.id


def test_155_1_the_plan_of_the_cancel_of_a_day_off_has_the_time_of_every_lesson(w, clock):
    cl = client(w, "admin")
    made = cl.post(f"{API}/org/days-off", json={"dateFrom": "2026-10-26", "dateTo": "2026-10-27", "reason": "Праздник"})
    assert made.status_code in (200, 201), made.text
    day_off_id = made.json()["dayOff"]["id"] if "dayOff" in made.json() else made.json()["id"]
    plan = cl.post(f"{API}/org/days-off/{day_off_id}/void", params={"dry_run": "true"})
    assert plan.status_code == 200, plan.text
    body = plan.json()
    lines = body["restored"] + body["kept"]
    assert lines and all(l["startTime"] and l["endTime"] for l in lines), lines
    assert {(l["groupName"], l["date"], l["startTime"]) for l in body["restored"]} >= {("9-А", "2026-10-26", "10:00"), ("7-Б", "2026-10-27", "10:00")}
    assert body["examsNote"] == "Если с этих уроков снимались экзамены — назначьте их заново"


# ============================================================================================================ #156: nobody but a teacher leads
def test_156_owner_and_admin_are_not_a_substitute_a_group_teacher_a_receiver_of_a_rate_or_a_handover_target(w):
    for who in ("owner", "admin"):
        r = put_teacher(w, "owner", "2026-10-26", who)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "TARGET_NOT_TEACHER", r.text                     # a substitute
        item = one(w, "A", "L1", "2026-10-26") if who == "owner" else None
        if item:
            for target in ("owner", "admin"):
                d = decide(w, "admin", item["id"], **sub_body(w, target))
                assert d.status_code == 422 and d.json()["detail"]["code"] == "TARGET_NOT_TEACHER"                     # the decision of a request
        g = client(w, "admin").put(f"{API}/org/groups/{w.G3.id}", json={"teacher_id": str(getattr(w, who).id)})
        assert g.status_code == 400 and g.json()["detail"] == "Выбранный сотрудник не преподаватель центра"             # the main teacher
        c = client(w, "admin").post(f"{API}/org/groups", json={"name": f"Новая-{who}", "teacher_id": str(getattr(w, who).id)})
        assert c.status_code == 400, c.text                                                                              # a new group
        h = client(w, "owner").post(M.format(mid=mid(w, "B")) + "/handover", json={"assignments": [{"groupId": str(w.G3.id), "teacherId": str(getattr(w, who).id)}]})
        assert h.status_code == 422 and h.json()["detail"]["code"] == "TARGET_NOT_TEACHER", h.text                       # a hand-over target
        rate = client(w, "owner").put(f"{API}/org/payroll/rates/{getattr(w, who).id}", json={"valid_from": "2026-09", "percents": {"group": "50"}})
        assert rate.status_code == 422 and rate.json()["detail"]["code"] == "RATE_RECEIVER_NOT_TEACHER"
    assert w.db.query(PayrollRate).filter(PayrollRate.teacher_id.in_((w.owner.id, w.admin.id))).count() == 0
    cands = client(w, "owner").get(f"{API}/org/groups/{w.G1.id}/lesson-teacher-candidates", params={"lessonId": "lesson-l1", "lessonDate": "2026-10-26"}).json()["candidates"]
    assert {c["orgRole"] for c in cands} == {"teacher"} and str(w.owner.id) not in {c["teacherId"] for c in cands} and str(w.admin.id) not in {c["teacherId"] for c in cands}
    pending = one(w, "A", "L3", "2026-10-27")
    assert {c["teacherId"] for c in client(w, "admin").get(f"{SUB}/{pending['id']}/candidates").json()["candidates"]} == {str(getattr(w, k).id) for k in ("B", "C", "D")}


def test_156_suggesting_a_colleague_offers_only_teachers_and_refuses_the_rest(w):
    assert {i["role"] for i in client(w, "A").get(f"{SUB}/colleagues").json()} == {"teacher"}
    for who in ("owner", "admin"):
        r = req(w, "A", [L(w, "L1", "2026-10-26")], suggested=who)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "TARGET_NOT_TEACHER", r.text


def test_156_a_rate_goes_to_the_one_who_led_lessons(w):
    """The rule: «the rate = who led lessons».  A former teacher (now an administrator) who held lessons gets it, an administrator who never did does not; the history reads for both."""
    c = client(w, "owner")
    body = {"valid_from": "2026-09", "percents": {"group": "50"}}
    # D is a teacher: ok.  He becomes an administrator without any lesson -> no new rate, history stays readable
    assert c.put(f"{API}/org/payroll/rates/{w.D.id}", json=body).status_code == 200
    assert c.put(M.format(mid=mid(w, "D")) + "/role", params={"role": "admin"}).status_code == 200
    r = c.put(f"{API}/org/payroll/rates/{w.D.id}", json={"valid_from": "2026-10", "percents": {"group": "60"}})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "RATE_RECEIVER_NOT_TEACHER"
    assert c.get(f"{API}/org/payroll/rates/{w.D.id}/history").status_code == 200 and len(c.get(f"{API}/org/payroll/rates/{w.D.id}/history").json()) == 1
    # A held lessons (the history of the world) and was made an administrator after the hand-over of his groups: the new rate is still allowed - he is paid for what he held
    assert client(w, "owner").post(M.format(mid=mid(w, "A")) + "/handover", json={"assignments": [{"groupId": str(w.G1.id), "teacherId": str(w.C.id)},
                                                                                              {"groupId": str(w.G2.id), "teacherId": str(w.B.id)}]}).status_code == 200
    assert c.put(M.format(mid=mid(w, "A")) + "/role", params={"role": "admin"}).status_code == 200
    assert c.put(f"{API}/org/payroll/rates/{w.A.id}", json={"valid_from": "2026-10", "percents": {"group": "40"}}).status_code == 200


def test_156_the_role_change_teacher_to_admin_needs_the_groups_handed_over_and_takes_the_substitutions_off(w, clock):
    cl = client(w, "owner")
    impact = cl.get(M.format(mid=mid(w, "D")) + "/removal-impact", params={"intent": "role_change"}).json()
    assert impact["canChangeRole"] is True and impact["groups"] == []
    # D: an archived group of his, a substitution on a lesson of A, a request of A that suggested him and a request of A decided with him
    archived = new_group(w.db, w.org, w.ids, "Ea", "Архивная", w.D, [new_slot("lesson-e", 3, "12:00", "13:00", "2026-10-28")])
    w.db.commit()
    assert client(w, "admin").post(f"{API}/org/groups/{archived.id}/archive", json={"reason": "Пауза"}).status_code == 200
    first = one(w, "A", "L1", "2026-11-02")
    assert decide(w, "admin", first["id"], **sub_body(w, "D")).status_code == 200                        # A's lesson of 02.11 is D's
    waiting = one(w, "A", "L3", "2026-10-27", suggested="D")                                              # A asked for 27.10 and suggested D
    assert [(x.start_time, x.status) for x in requests_of(w, "pending")] == [("10:00", "pending")]
    # (1) without the hand-over: refused, the archived group counts, the plain removal-impact does not list it
    impact = cl.get(M.format(mid=mid(w, "D")) + "/removal-impact", params={"intent": "role_change"}).json()
    assert [(g["name"], g["status"]) for g in impact["groups"]] == [("Архивная", "archived")] and impact["canChangeRole"] is False
    assert cl.get(M.format(mid=mid(w, "D")) + "/removal-impact").json()["groups"] == [] and cl.get(M.format(mid=mid(w, "D")) + "/removal-impact").json()["canChangeRole"] is False
    refused = cl.put(M.format(mid=mid(w, "D")) + "/role", params={"role": "admin"})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "MEMBER_HAS_ASSIGNMENTS", refused.text
    assert [g["name"] for g in refused.json()["detail"]["groups"]] == ["Архивная"] and refused.json()["detail"]["canChangeRole"] is False
    w.db.expire_all()
    assert w.db.query(OrganizationMember).filter_by(user_id=w.D.id).one().org_role == "teacher"          # nothing changed
    assert w.db.query(LessonTeacherAssignment).filter_by(teacher_id=w.D.id, cancelled_at=None).count() == 1
    # (2) the archived group goes to C (the same hand-over as for a dismissal) ...
    h = cl.post(M.format(mid=mid(w, "D")) + "/handover", json={"assignments": [{"groupId": str(archived.id), "teacherId": str(w.C.id)}]})
    assert h.status_code == 200 and h.json()["handedOver"][0]["toTeacherId"] == str(w.C.id), h.text
    # ... then the role changes: the substitution is taken off, the request that suggested him gets the event, the request decided with him is reopened
    ok = cl.put(M.format(mid=mid(w, "D")) + "/role", params={"role": "admin"})
    assert ok.status_code == 200 and ok.json()["new_role"] == "admin" and ok.json()["releasedSubstitutions"] == 1, ok.text
    w.db.expire_all()
    assert w.db.query(LessonTeacherAssignment).filter_by(teacher_id=w.D.id, cancelled_at=None).count() == 0
    cleared = [e for e in events_of(w) if e.action == "assignment_cleared"]
    assert [e.snapshot["trigger"] for e in cleared] == ["role_changed"]
    assert str(cleared[0].request_id) == first["id"]
    audit = w.db.query(GroupAudit).filter_by(group_id=w.G1.id, action="lesson_teacher_cleared").one()
    assert audit.snapshot["reason"] == "Роль изменена: замена снята"
    assert [e.action for e in events_of(w, waiting["id"])][-1] == "suggestion_unavailable"
    listed = {i["id"]: i for i in client(w, "admin").get(SUB, params={"status": "pending"}).json()["items"]}
    assert listed[waiting["id"]]["suggestedTeacher"]["active"] is False                                   # «available» = active AND a teacher
    reopened = [r for r in requests_of(w, "pending") if r.previous_request_id and str(r.previous_request_id) == first["id"]]
    assert len(reopened) == 1 and reopened[0].origin == "system"
    assert reopened[0].comment == f"Заменяющий {short_name(w.D)} больше не ведёт уроки. Нужно новое решение."          # the events are not shown anywhere: the reason is the comment (R3 3)
    assert any(e.action == "reopened" and e.snapshot.get("cause") == "role_changed" for e in events_of(w, reopened[0].id))
    # admin -> teacher returns nothing taken off (the cancellation is an event, LESSONS 47) and asks nothing
    back = cl.put(M.format(mid=mid(w, "D")) + "/role", params={"role": "teacher"})
    assert back.status_code == 200 and "releasedSubstitutions" not in back.json()
    w.db.expire_all()
    assert w.db.query(LessonTeacherAssignment).filter_by(teacher_id=w.D.id, cancelled_at=None).count() == 0


def test_156_a_dismissal_keeps_its_reason_and_trigger(w):
    """The common function: a dismissal still says "Сотрудник удалён" / ``member_removed`` (the new parameter does not change it)."""
    assert put_teacher(w, "admin", "2026-11-02", "D").status_code == 200
    assert client(w, "owner").delete(M.format(mid=mid(w, "D"))).status_code == 200
    audit = w.db.query(GroupAudit).filter_by(group_id=w.G1.id, action="lesson_teacher_cleared").one()
    assert audit.snapshot["reason"] == "Сотрудник удалён: замена снята"


def test_156_restore_from_the_archive_with_a_former_teacher_says_he_no_longer_leads(w):
    arch = new_group(w.db, w.org, w.ids, "Eb", "Старая", w.D, [new_slot("lesson-f", 3, "12:00", "13:00", "2026-10-28")])
    w.db.commit()
    assert client(w, "admin").post(f"{API}/org/groups/{arch.id}/archive", json={"reason": "Пауза"}).status_code == 200
    w.db.query(OrganizationMember).filter_by(user_id=w.D.id).update({"org_role": "admin"})                    # legacy: an archived group of a man who is an administrator now
    w.db.commit()
    r = client(w, "admin").post(f"{API}/org/groups/{arch.id}/restore", json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "TEACHER_INACTIVE"
    assert r.json()["detail"]["message"] == "Преподаватель группы больше не ведёт уроки" and "работает в центре" not in r.text
    ok = client(w, "admin").post(f"{API}/org/groups/{arch.id}/restore", json={"teacherId": str(w.C.id)})
    assert ok.status_code == 200, ok.text


def test_156_a_move_of_a_lesson_of_a_group_without_a_teacher_says_where_to_assign_one(w):
    w.db.query(Group).filter_by(id=w.G3.id).update({"teacher_id": None})
    w.db.commit()
    r = client(w, "admin").post(f"{API}/org/groups/{w.G3.id}/lesson-move", json={"lessonId": "lesson-l4", "lessonDate": "2026-10-26", "newDate": "2026-10-27", "startTime": "12:00",
                                                                              "endTime": "13:30", "reason": "Перенос"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "LESSON_HAS_NO_TEACHER"
    assert r.json()["detail"]["message"] == "У группы нет преподавателя — назначьте его в карточке группы"


# ============================================================================================================ the one-time cleanup script (part 4)
def test_cleanup_script_dry_run_changes_nothing_and_apply_does_the_three_things(w, clock):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("only_teachers_cleanup", Path(__file__).resolve().parents[1] / "scripts" / "w1_only_teachers_cleanup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # legacy data (the old model): the owner leads a group (an archived one too), the admin has a future substitution, a request of A suggested the owner
    legacy = new_group(w.db, w.org, w.ids, "Lg", "Старая-владельца", w.owner, [new_slot("lesson-g", 3, "12:00", "13:00", "2026-10-28")])
    legacy_arch = new_group(w.db, w.org, w.ids, "Lh", "Архив-админа", w.admin, [new_slot("lesson-h", 4, "12:00", "13:00", "2026-10-29")])
    w.db.commit()
    assert client(w, "admin").post(f"{API}/org/groups/{legacy_arch.id}/archive", json={"reason": "Пауза"}).status_code == 200
    w.db.add(LessonTeacherAssignment(org_id=w.org.id, group_id=w.G1.id, lesson_id="lesson-l1", lesson_date="2026-11-02", teacher_id=w.admin.id, reason="Болезнь", set_by=w.owner.id))
    w.db.commit()
    suggested = one(w, "A", "L3", "2026-10-27")
    w.db.query(Req).filter_by(id=suggested["id"]).update({"suggested_teacher_id": w.owner.id})
    w.db.commit()
    rates_before = w.db.query(PayrollRate).filter_by(org_id=w.org.id).count()

    def mine(rows):
        return [r for r in rows if r["org_id"] == str(w.org.id)]

    rows = mine(module.cleanup(w.db))
    w.db.rollback()                                                                                        # the dry run
    assert len(rows) == 1 and rows[0]["released_substitutions"] == 1 and rows[0]["groups_unassigned"] == 1 and rows[0]["groups_archived_unassigned"] == 1 and rows[0]["suggestion_events"] == 1
    assert "@" not in str(rows) and w.owner.email not in str(rows)                                         # counters only: no names, no e-mails
    w.db.expire_all()
    assert w.db.get(Group, legacy.id).teacher_id == w.owner.id and w.db.query(LessonTeacherAssignment).filter_by(teacher_id=w.admin.id, cancelled_at=None).count() == 1
    applied = mine(module.cleanup(w.db))
    w.db.commit()
    assert applied == rows
    w.db.expire_all()
    assert w.db.get(Group, legacy.id).teacher_id is None and w.db.get(Group, legacy_arch.id).teacher_id is None and w.db.get(Group, legacy_arch.id).status == "archived"
    assert w.db.query(LessonTeacherAssignment).filter_by(teacher_id=w.admin.id, cancelled_at=None).count() == 0
    assert [e.action for e in events_of(w, suggested["id"])][-1] == "suggestion_unavailable"
    assert w.db.query(PayrollRate).filter_by(org_id=w.org.id).count() == rates_before                                                  # rates, past lessons, sheets stay
    again = mine(module.cleanup(w.db))
    w.db.rollback()
    assert again == []                                                                                      # idempotent: nothing left to do


# ============================================================================================================ R3 (28.10)
def test_r3_8_s2_1_the_decision_of_a_request_gives_the_admin_only_the_month(w, monkeypatch):
    """The second road of ``payrollSheetsAffected`` (``decide`` kind=substitute): the owner gets the sheets with the payout state, the admin the month only."""
    from src.auth.lesson_teachers import LessonTeachers  # noqa: F401

    for teacher_id in (w.A.id, w.D.id):
        w.db.add(PayrollSheet(org_id=w.org.id, teacher_id=teacher_id, period_month=date(2026, 10, 1), source_fingerprint="x", total_amount=100, created_by=w.owner.id,
                              updated_by=w.owner.id))
    first, second = one(w, "A", "L1", "2026-10-26"), one(w, "A", "L2", "2026-10-28")
    w.db.info["b2_skip"] = {(str(w.G1.id), "lesson-l1", "2026-10-26"), (str(w.G1.id), "lesson-l2", "2026-10-28")}          # the journal rows below do not close the requests
    for lesson_id, day in (("lesson-l1", "2026-10-26"), ("lesson-l2", "2026-10-28")):
        history_row(w.db, w.org, w.G1, lesson_id, day, w.A, [w.s1], status="completed")
    w.db.commit()
    from src.auth.lesson_substitutions import Substitutions

    original = Substitutions.closure_reason
    monkeypatch.setattr(Substitutions, "closure_reason", lambda self, r, g, book=None: (lambda out: (None, out[1]) if out[0] == "LESSON_STARTED" else out)(original(self, r, g, book)))
    mine = decide(w, "owner", first["id"], **sub_body(w, "D"))
    assert mine.status_code == 200, mine.text
    sheets = mine.json()["result"]["payrollSheetsAffected"]
    assert len(sheets) == 2 and all(set(x) == {"sheet_id", "teacher_id", "month", "status"} for x in sheets)
    theirs = decide(w, "admin", second["id"], **sub_body(w, "D"))
    assert theirs.status_code == 200, theirs.text
    assert theirs.json()["result"]["payrollSheetsAffected"] == [{"month": "2026-10"}]


def _two_groups_two_days_off(w):
    """A day off over 26-29.10 takes lessons of four groups on four days; D substitutes the lesson of 26.10 (a warning on the void)."""
    assert put_teacher(w, "admin", "2026-10-26", "D").status_code == 200
    made = client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": "2026-10-26", "dateTo": "2026-10-29", "reason": "Хайит"})
    assert made.status_code in (200, 201), made.text
    return made.json()["dayOff"]["id"]


def test_r3_2_the_warning_has_the_time_of_the_lesson_and_matches_its_line_in_the_list(w):
    day_off = _two_groups_two_days_off(w)
    out = client(w, "owner").post(f"{API}/org/days-off/{day_off}/void").json()
    (warning,) = out["warnings"]
    assert (warning["startTime"], warning["endTime"], warning["groupName"], warning["date"]) == ("10:00", "11:30", "9-А", "2026-10-26")
    assert warning["substituteName"] == short_name(w.D) and f"({short_name(w.D)})" in warning["message"]          # «Рахимов Т.», not the full «Рахимов Тимур»
    line = next(x for x in out["restored"] if x["lessonId"] == warning["lessonId"] and x["date"] == warning["date"])
    assert (line["startTime"], line["endTime"], line["groupName"]) == (warning["startTime"], warning["endTime"], warning["groupName"])      # the same line in both places (LESSONS 48)


def test_r3_8_examsnote_is_empty_when_nothing_comes_back(w):
    day_off = _two_groups_two_days_off(w)
    from src.models.auth import GroupAttendance

    w.db.query(GroupAttendance).filter_by(day_off_id=day_off).update({"version": 2})               # every lesson was changed after the day off: nothing comes back
    w.db.commit()
    out = client(w, "owner").post(f"{API}/org/days-off/{day_off}/void").json()
    assert out["restored"] == [] and out["examsNote"] == "" and all(x["reason"] for x in out["kept"])


def test_r3_4_the_void_reads_the_schedule_of_all_the_groups_in_one_preload(w, monkeypatch):
    day_off = _two_groups_two_days_off(w)
    from src.auth.service import AuthService

    calls = []
    original = AuthService._preload_schedule_data
    monkeypatch.setattr(AuthService, "_preload_schedule_data", lambda self, *a, **k: (calls.append(a), original(self, *a, **k))[1])
    out = client(w, "owner").post(f"{API}/org/days-off/{day_off}/void", params={"dry_run": "true"}).json()
    assert len({(x["groupName"], x["date"]) for x in out["restored"]}) >= 4                      # 2+ groups x 2+ days
    assert len({x["groupName"] for x in out["restored"]}) >= 2 and len({x["date"] for x in out["restored"]}) >= 2
    assert len(calls) == 1 and len(calls[0][0]) >= 2                                              # one preload for the groups of the rows
    assert all(x["startTime"] and x["endTime"] for x in out["restored"])


def test_r3_5_the_new_teacher_of_a_group_is_read_with_for_share(w):
    from sqlalchemy import event

    statements = []
    engine = w.db.get_bind()

    def spy(conn, cursor, statement, *a):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", spy)
    try:
        assert client(w, "admin").put(f"{API}/org/groups/{w.G3.id}", json={"teacher_id": str(w.C.id)}).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    shared = [x for x in statements if "FOR SHARE" in x and "organization_members" in x]
    assert shared, "the membership of the new teacher must be read FOR SHARE"
    first_group_lock = next(i for i, x in enumerate(statements) if "FROM groups" in x and "FOR UPDATE" in x)
    assert statements.index(shared[0]) < first_group_lock                                         # member row before the group row: the order of a dismissal / a change of the role


def test_r3_8_a_lesson_in_progress_blocks_the_role_change_and_missed_lessons_are_shown(w):
    cl = client(w, "owner")
    history_row(w.db, w.org, w.G4, "lesson-l5", "2026-10-15", w.D, [], status="in_progress", start="16:00")             # D (a substitute) has a lesson going on
    w.db.commit()
    refused = cl.put(M.format(mid=mid(w, "D")) + "/role", params={"role": "admin"})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "MEMBER_HAS_ASSIGNMENTS", refused.text
    assert [x["groupName"] for x in refused.json()["detail"]["inProgressLessons"]] == ["6-Г"] and refused.json()["detail"]["canChangeRole"] is False
    impact = cl.get(M.format(mid=mid(w, "A")) + "/removal-impact", params={"intent": "role_change"}).json()
    assert impact["missedLessons"] == {"count": 1} and impact["canChangeRole"] is False and {g["status"] for g in impact["groups"]} == {"active"}
