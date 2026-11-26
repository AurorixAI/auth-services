"""LESSON-RULES С0: the rules of a centre - one mechanism (``centre_policy``): the two keys of the lessons, the journal, the owner's alone, the window read in ONE place,
the access of a substitute / a former teacher = max(7, window) and NOT the window (SPEC 6.11, 7.1).  The expectation is worked out by hand in the comment BEFORE the call.

World: today Wed 21.10.2026 14:00 (``build_d2``): G7-like lessons are not needed - the window is probed on the group G1 (9-А, teacher A, lesson-l1 on Mondays 10:00, lesson-l2 on
Wednesdays 16:00).  A lesson is «too old for a teacher» when its date < today - window."""
import pytest

from src.auth import centre_policy as cp
from src.auth.money_journal_actions import POLICY_CHANGED
from src.models.auth import OrgMoneyAudit

from tests.b2_helpers import API, build_d2, client, clock, decide, needs_pg, pg_session  # noqa: F401  (fixtures)
from tests.ops_helpers import grant_money_right
from tests.test_ops_b2_decide_pg import one, sub_body  # noqa: F401

pytestmark = needs_pg
ORG = f"{API}/org"
RULES = f"{ORG}/settings/lessons"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")
    world = build_d2(pg_session)
    grant_money_right(world)                       # the administrator of these tests has the money right of the owner (Money-1 A4)
    return world


def put(w, who="owner", **body):
    return client(w, who).put(RULES, json=body)


def reconcile(w, who, group, lesson, day):
    return client(w, who).post(f"{ORG}/groups/{group.id}/attendance/reconcile", json={"lessonDate": day, "lessonId": lesson})


# ---------------------------------------------------------------------------------------------------- the keys
def test_the_defaults_are_seven_days_and_a_confirmation(w):
    """Nothing stored: window 7, confirmation yes; the keys carry their allowed values and words."""
    got = client(w, "owner").get(RULES).json()
    assert (got["teacher_lesson_window_days"], got["past_cancel_needs_confirmation"]) == (7, True)
    items = {i["key"]: i for i in got["items"]}
    assert items["teacher_lesson_window_days"]["allowed"] == [1, 3, 7, 14, 30] and items["teacher_lesson_window_days"]["default"] == 7
    assert items["past_cancel_needs_confirmation"]["allowed"] == [True, False] and items["past_cancel_needs_confirmation"]["words"] == "да"
    assert items["past_cancel_needs_confirmation"]["title"] == ("Отмену прошедшего урока преподавателем или администратором без доступа к деньгам "
                                                                 "подтверждает владелец или администратор с доступом к деньгам")


def test_every_position_of_every_key_is_stored_read_and_journaled(w):
    """Each allowed window value 1/3/14/30 (7 is the default) and each position of the confirmation is accepted; 5 / 0 / 31 / a string are 422 and change nothing;
    the journal has one line per REAL change in the words «было → стало»: 7→1, 1→3, 3→7 ... (the same value again writes nothing)."""
    seen = 7
    for value in (1, 3, 7, 14, 30):
        r = put(w, teacher_lesson_window_days=value)
        assert r.status_code == 200 and r.json()["teacher_lesson_window_days"] == value, r.text
        assert client(w, "owner").get(RULES).json()["teacher_lesson_window_days"] == value
        seen = value
    for bad in (0, 5, 31, -7):
        r = put(w, teacher_lesson_window_days=bad)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "POLICY_VALUE_INVALID", (bad, r.text)
    assert put(w, teacher_lesson_window_days="семь").status_code == 422
    assert client(w, "owner").get(RULES).json()["teacher_lesson_window_days"] == seen == 30
    for flag in (False, True):
        r = put(w, past_cancel_needs_confirmation=flag)
        assert r.status_code == 200 and r.json()["past_cancel_needs_confirmation"] is flag
    assert put(w, past_cancel_needs_confirmation="maybe").status_code == 422
    # the unknown field is refused, the empty body changes nothing
    assert put(w, teacher_lesson_window_days_extra=3).status_code == 422
    n = len(client(w, "owner").get(f"{RULES}/history").json()["items"])
    assert put(w).status_code == 200 and put(w, teacher_lesson_window_days=30).status_code == 200      # nothing / the same value: no line
    items = client(w, "owner").get(f"{RULES}/history").json()["items"]
    assert len(items) == n
    # hand count: window 7→1, 1→3, 3→7, 7→14, 14→30 = 5 lines; the confirmation: да→нет, нет→да = 2 lines; 7 lines in all
    assert n == 7
    texts = [i["text"] for i in items]
    assert any(t.endswith("7 → 14") for t in texts) and any(t.endswith("да → нет") for t in texts) and all("вычет" not in t.lower() for t in texts)
    assert all(i["actor_name"] for i in items)


def test_only_the_owner_changes_the_rules_and_the_page_of_the_payments_does_not_show_the_lines(w):
    for who in ("admin", "A"):
        assert client(w, who).get(RULES).status_code == 403 and put(w, who, teacher_lesson_window_days=3).status_code == 403
    assert put(w, teacher_lesson_window_days=3).status_code == 200
    pay = client(w, "owner").get(f"{ORG}/settings/payments/history").json()["items"]
    assert pay == [] or all(i["action"] != POLICY_CHANGED for i in pay)
    w.db.expire_all()
    assert w.db.query(OrgMoneyAudit).filter_by(org_id=w.org.id, action=POLICY_CHANGED).count() == 1


def test_a_parallel_write_of_the_payment_settings_and_of_the_lessons_loses_neither(w):
    """The two blocks live in one JSON of the organisation: each write locks the row and replaces ITS block.  Two writes in turn (one of each) leave both."""
    assert put(w, teacher_lesson_window_days=14).status_code == 200
    r = client(w, "owner").put(f"{ORG}/settings/payments", json={"pay_due_days": 4})
    assert r.status_code == 200, r.text
    assert put(w, past_cancel_needs_confirmation=False).status_code == 200
    pay = client(w, "owner").get(f"{ORG}/settings/payments").json()
    rules = client(w, "owner").get(RULES).json()
    assert pay["pay_due_days"] == 4 and (rules["teacher_lesson_window_days"], rules["past_cancel_needs_confirmation"]) == (14, False)


def test_versions_carry_the_day_and_a_key_without_a_default_reads_none(w):
    """The format of the storage: versions ``{from, value}``; the value on a day is the last version that started on or before it; a key without a default is ``None`` («не выбрано»)."""
    key = cp.PolicyKey("x", "lessons", None, (1, 2), "t", "h")
    versions = [{"from": "min", "value": 1}, {"from": "2026-11-01", "value": 2}]
    from datetime import date
    assert cp.value_of(versions, key, date(2026, 10, 31)) == 1 and cp.value_of(versions, key, date(2026, 11, 1)) == 2 and cp.value_of(versions, key) == 2
    assert cp.value_of(None, key) is None and cp.value_of([{"from": "2026-12", "value": 2}], key, date(2026, 11, 30)) is None and cp.value_of([{"from": "2026-12", "value": 2}], key, date(2026, 12, 1)) == 2
    assert cp.value_of([{"from": "min", "value": 99}], cp.KEYS["teacher_lesson_window_days"]) == 7          # a hand-edited value out of the range is the default, never an error of a page
    assert [cp.window_days_text(n) for n in (1, 3, 7, 14, 21, 30)] == ["1 день", "3 дня", "7 дней", "14 дней", "21 день", "30 дней"]


# ----------------------------------------------------------------------------------------- the window of the actions
@pytest.fixture
def g7(w, pg_session):
    """A group of teacher D with ONE Wednesday lesson since 01.08 (so old Wednesdays exist), one student."""
    from tests.ops_helpers import new_group, new_slot, new_student

    group = new_group(pg_session, w.org, w.ids, "G7", "Гр-7", w.D, [new_slot("lesson-wed", 3, "10:00", "11:00", "2026-08-01")])
    new_student(pg_session, w.org, w.ids, "g7", "Седьмой", "Ученик", group)
    pg_session.commit()
    return group


@pytest.mark.parametrize("window, day, passes", [
    # today Wed 21.10 14:00; the Wednesdays by hand: 21.10 = 0 d, 14.10 = 7 d, 07.10 = 14 d, 30.09 = 21 d, 23.09 = 28 d, 16.09 = 35 d.  A lesson is too old when age > window.
    (7, "2026-10-14", True), (7, "2026-10-07", False),          # 7 days passes, 14 does not (the old rule, unchanged by default)
    (1, "2026-10-21", True), (1, "2026-10-14", False),          # window 1: today only
    (3, "2026-10-21", True), (3, "2026-10-14", False),
    (14, "2026-10-07", True), (14, "2026-09-30", False),        # 14 days passes, 21 does not
    (30, "2026-09-23", True), (30, "2026-09-16", False),        # 28 days passes, 35 does not
])
def test_the_window_decides_what_a_teacher_may_reconcile(w, g7, window, day, passes):
    assert put(w, teacher_lesson_window_days=window).status_code == 200
    r = reconcile(w, "D", g7, "lesson-wed", day)
    if passes:
        assert r.status_code == 200, r.text
    else:
        d = r.json()["detail"]
        assert r.status_code == 403 and d["code"] == "LESSON_TOO_OLD" and d["windowDays"] == window, r.text
        assert d["message"] == f"Урок старше {window} дн. — его закрывает администратор или владелец" and "7 дней" not in d["message"]
        assert reconcile(w, "admin", g7, "lesson-wed", day).status_code == 200                      # the administrator is not bound by the window


def test_the_list_without_a_mark_names_the_window_of_the_centre(w, g7):
    """``requiresAdmin`` and ``windowDays`` of every lesson of the list come from the rule: window 3 - the lesson of today (0 d) is the teacher's, the one of 14.10 (7 d) the administrator's."""
    assert put(w, teacher_lesson_window_days=3).status_code == 200
    items = client(w, "D").get(f"{ORG}/attendance/open-actions").json()
    assert items and all(i["windowDays"] == 3 for i in items)
    by = {i["lessonDate"]: i for i in items if i["lessonId"] == "lesson-wed"}
    assert by["2026-10-21"]["requiresAdmin"] is False and by["2026-10-14"]["requiresAdmin"] is True


# -------------------------------------------------------------------- access = max(7, window), not the window (7.1)
def _substitute_d(w, clock):
    item = one(w, "A", "L1", "2026-10-26", reason="sick", comment="Температура", suggested="D")
    assert decide(w, "admin", item["id"], **sub_body(w, "D")).status_code == 200
    clock.set("2026-11-05 11:00")                  # 10 days after the lesson of 26.10


def test_window_14_lets_a_substitute_in_and_see_and_close_his_lesson_of_ten_days_ago(w, clock):
    """Today 05.11, lesson 26.10 = 10 days.  Window 7 (default): the access (7) is over - 403 on the access of exam-service, the group page and the reconcile.
    Window 14: access = max(7, 14) = 14 -> in; the lesson is in his journal; he reconciles it (200)."""
    _substitute_d(w, clock)
    d = client(w, "D")
    assert d.get(f"{ORG}/groups/{w.G1.id}/lesson-access").status_code == 403
    assert reconcile(w, "D", w.G1, "lesson-l1", "2026-10-26").status_code == 403
    assert put(w, teacher_lesson_window_days=14).status_code == 200
    assert d.get(f"{ORG}/groups/{w.G1.id}/lesson-access").status_code == 200
    j = d.get(f"{ORG}/groups/{w.G1.id}/attendance", params={"month": "2026-10"})
    assert j.status_code == 200 and "2026-10-26_lesson-l1" in [x["instanceKey"] for x in j.json()["lessons"]]
    assert reconcile(w, "D", w.G1, "lesson-l1", "2026-10-26").status_code == 200


def test_window_3_keeps_the_access_at_seven_days_and_limits_only_the_action(w, clock):
    """Window 3: today 28.10, lesson 26.10 = 2 days -> he closes it; today 31.10 (5 days): he still SEES it (access 7) but may not close it (LESSON_TOO_OLD, 3 days)."""
    item = one(w, "A", "L1", "2026-10-26", reason="sick", comment="Температура", suggested="D")
    assert decide(w, "admin", item["id"], **sub_body(w, "D")).status_code == 200
    assert put(w, teacher_lesson_window_days=3).status_code == 200
    clock.set("2026-10-31 11:00")
    d = client(w, "D")
    assert d.get(f"{ORG}/groups/{w.G1.id}/lesson-access").status_code == 200
    j = d.get(f"{ORG}/groups/{w.G1.id}/attendance", params={"month": "2026-10"})
    assert j.status_code == 200 and "2026-10-26_lesson-l1" in [x["instanceKey"] for x in j.json()["lessons"]]
    r = reconcile(w, "D", w.G1, "lesson-l1", "2026-10-26")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "LESSON_TOO_OLD" and r.json()["detail"]["windowDays"] == 3
    clock.set("2026-11-03 11:00")                  # 8 days: even the access is over
    assert d.get(f"{ORG}/groups/{w.G1.id}/lesson-access").status_code == 403
