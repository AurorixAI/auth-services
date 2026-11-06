"""Money-1 («Урока не было»): ``POST /org/groups/{id}/attendance/reconcile/cancel?dry_run=true`` says what the cancellation does to money and writes nothing (PostgreSQL).

The world is the one of #154 / PAY-F (``five``): October, price 600 000, five students who paid the period 06.10-05.11, the setting «вычитать уроки, отменённые центром» is on, the lesson of 26.10 is cancelled by
the centre (46 000 deducted from each).  A missed lesson of the group (28.10, never started) is cancelled «по вине центра» (46 000 more from each of the five = 230 000) or as «Праздник, выходной» (nothing).
The numbers of the preview are the numbers of the state after the real call (I-5), the refusals are the refusals of the real call, and an exam-service that WRITES (the release) is never asked by a preview."""
from datetime import date

import pytest

from src.auth import exam_release, money_effects as me

from tests.money1_helpers import changed_tables, db_fingerprint
from tests.ops_helpers import clock, closed_sheet, grant_money_right, needs_pg, pg_session  # noqa: F401  (fixtures)
from tests.payf_helpers import API, client
from tests.payf_helpers import statement
from tests.test_p154_money_pg import build_org, deducted, five

pytestmark = needs_pg
NB = "\u00a0"
WED = {"lessonDate": "2026-10-28", "lessonId": "lesson-wed", "cancellationReason": "Преподаватель заболел"}


def world(pg_session, clock, now="2026-10-30 12:00"):
    w = build_org(pg_session)
    g, students, ms = five(w, clock)
    clock.set(now)
    return w, g, students, ms, f"{API}/org/groups/{g.id}/attendance/reconcile/cancel"


def post(w, who, url, body, dry=False):
    return client(w, who).post(url, params={"dry_run": "true"} if dry else None, json=body)


def as_state_after(answer):
    """What a preview and the real call must agree on (not the flag that says which of them it is)."""
    effects = {k: v for k, v in answer["moneyEffects"].items() if k != "dryRun"}
    return {"deductions": answer.get("deductions"), "effects": effects, "warnings": answer["warnings"]}


# ================================================================================================ the numbers: the preview is the state after
@pytest.mark.parametrize("kind, each", [("centre", 46_000), ("holiday", 0)], ids=["po-vine-centra", "prazdnik-vykhodnoi"])
def test_the_preview_is_the_state_after_for_both_kinds_and_writes_nothing(pg_session, clock, kind, each):
    w, g, students, ms, url = world(pg_session, clock)
    body = {**WED, "cancellationKind": kind}
    assert [deducted(w, m) for m in ms] == [46_000] * 5
    before = db_fingerprint(w.db)
    dry = post(w, "owner", url, body, dry=True)
    assert dry.status_code == 200, dry.text
    assert changed_tables(before, db_fingerprint(w.db)) == []                                  # nothing is written: no row, no audit, no marker of the release
    assert [deducted(w, m) for m in ms] == [46_000] * 5
    seen = dry.json()
    assert seen["dryRun"] is True and seen["moneyEffects"]["dryRun"] is True and seen["cancelled"] is True
    assert sorted(d["amount"] for d in seen["deductions"]) == [each] * 5 * bool(each) and all(d["name"] for d in seen["deductions"])
    assert seen["moneyEffects"]["deductions"] == seen["deductions"]
    assert seen["moneyEffects"]["text"] == (f"Ученикам — перерасчёт: долг уменьшится сразу на {each * 5:,} сум (5 учеников)".replace(",", NB).replace(" сум", NB + "сум") if each else "Ученикам перерасчёта не будет")
    def balance():
        w.db.expire_all()
        return sum(statement(w, m)["summary"]["advance"] - statement(w, m)["summary"]["debt"] for m in ms)

    account_before = balance()
    real = post(w, "owner", url, body)
    assert real.status_code == 200, real.text
    assert balance() - account_before == each * 5                                               # the debt is lowered AT ONCE (it is not "the next payment"): the sentence of the dialog is true
    done = real.json()
    assert done["dryRun"] is False and done["moneyEffects"]["dryRun"] is False
    assert as_state_after(done) == as_state_after(seen) | {"effects": {**as_state_after(seen)["effects"], "text": done["moneyEffects"]["text"]}}
    assert [deducted(w, m) for m in ms] == [46_000 + each] * 5                                  # what the confirmation said is what the statements say
    assert (done["moneyEffects"]["text"] == "Ученикам перерасчёта нет") is (not each)
    assert done["deductions"] == seen["deductions"]


def test_a_second_preview_after_the_cancellation_is_the_refusal_of_the_second_real_call(pg_session, clock):
    w, g, students, ms, url = world(pg_session, clock)
    assert post(w, "owner", url, WED).status_code == 200
    again = {**WED, "cancellationReason": "Другая причина"}
    dry, real = post(w, "owner", url, again, dry=True), post(w, "owner", url, again)
    assert (dry.status_code, dry.json()) == (real.status_code, real.json()) and dry.status_code == 409 and dry.json()["detail"]["code"] == "ALREADY_CANCELLED"


# ================================================================================================ the refusals of the real call
def test_the_closed_month_refuses_the_preview_as_it_refuses_the_real_call(pg_session, clock):
    w, g, students, ms, url = world(pg_session, clock, now="2026-11-03 12:00")
    closed_sheet(w, w.A, date(2026, 10, 1), saved="2026-11-02 12:00")                           # October of the teacher is saved after it ended: closed
    before = db_fingerprint(w.db)
    dry, real = post(w, "owner", url, WED, dry=True), post(w, "owner", url, WED)
    assert dry.status_code == real.status_code == 409 and dry.json() == real.json()
    detail = dry.json()["detail"]
    assert detail["code"] == "PERIOD_CLOSED" and detail["month"] == "2026-10" and "переоткройте месяц" in detail["message"]
    assert changed_tables(before, db_fingerprint(w.db)) == []


def test_the_rights_are_those_of_the_real_call_a_teacher_cannot_choose_the_holiday_and_has_no_money_in_the_answer(pg_session, clock):
    w, g, students, ms, url = world(pg_session, clock)
    holiday = {**WED, "cancellationKind": "holiday"}
    dry, real = post(w, "A", url, holiday, dry=True), post(w, "A", url, holiday)
    assert dry.status_code == real.status_code == 403 and dry.json() == real.json()            # «Праздник, выходной» is for an owner / an administrator
    mine = post(w, "A", url, WED, dry=True)                                                      # his own lesson, «по вине центра»: the preview is allowed, the sums are not his
    assert mine.status_code == 200 and "deductions" not in mine.json() and "ученик" not in mine.json()["moneyEffects"]["text"].lower()
    assert [deducted(w, m) for m in ms] == [46_000] * 5
    real_mine = post(w, "A", url, WED)
    assert real_mine.status_code == 200 and "moneyEffects" not in real_mine.json() and "deductions" not in real_mine.json()          # as before the preview existed
    assert [deducted(w, m) for m in ms] == [92_000] * 5


def test_an_administrator_without_the_money_right_previews_without_the_sums_of_the_students(pg_session, clock):
    w, g, students, ms, url = world(pg_session, clock)
    grant_money_right(w, False)
    seen = post(w, "admin", url, WED, dry=True)
    assert seen.status_code == 200 and "deductions" not in seen.json() and "deductions" not in seen.json()["moneyEffects"]
    grant_money_right(w, True)
    with_right = post(w, "admin", url, WED, dry=True).json()
    assert len(with_right["deductions"]) == 5 and sum(d["amount"] for d in with_right["deductions"]) == 230_000


# ================================================================================================ the payroll part of the block (the sheets of the teachers)
def test_the_sheets_of_the_teacher_are_named_was_to_be_for_the_owner_and_by_the_month_for_an_administrator(pg_session, clock, monkeypatch):
    """A cancellation «по вине центра» moves the pay only when the recalculation moves the cover of a held lesson of a saved month: the cover is faked here (1 lesson of October becomes paid), the sheets are
    measured by the real code of ``run_with_effects`` (the same as the payments use)."""
    w, g, students, ms, url = world(pg_session, clock)
    closed_sheet(w, w.A, date(2026, 10, 1), saved="2026-10-30 12:00")                           # saved in the middle of the month: open
    calls = {"n": 0}

    def cover(self, ids):
        calls["n"] += 1
        return {} if calls["n"] == 1 else {("m", date(2026, 10, 21), "lesson-wed"): (True, 1, "money")}

    def measure(self, into):
        for sheet_id in self.sheets:
            (self.before if into == "before" else self.after)[sheet_id] = 90_000 if into == "after" else 100_000

    monkeypatch.setattr(me.MembershipProbe, "_cover", cover)
    monkeypatch.setattr(me.SheetProbe, "measure", measure)
    owner_line = post(w, "owner", url, WED, dry=True).json()["moneyEffects"]["payroll"]
    assert [(x["month"], x["state"], x["freshBefore"], x["freshAfter"], x["changedBy"]) for x in owner_line] == [("2026-10", "open", 100_000, 90_000, -10_000)]
    calls["n"] = 0
    admin_line = post(w, "admin", url, WED, dry=True).json()["moneyEffects"]["payroll"]
    assert admin_line == [{"month": "2026-10", "state": "open"}]                                 # the administrator: the month and its state, no sums


# ================================================================================================ the preview never writes outside the database
def test_a_preview_never_asks_exam_service_to_release_and_the_real_call_asks_once(pg_session, clock, monkeypatch):
    """The mutation of the task: without the guard (``money_effects.is_preview``) the first pass of the real call and the preview would send the release of the exams of a lesson that is only being
    previewed - this test is red on that code (run by hand: the guard removed from ``AuthService.cancel_missed_group_attendance``)."""
    w, g, students, ms, url = world(pg_session, clock)
    asked = []

    def spy(svc, org_id, group_id=None, **kw):
        asked.append(group_id)
        return []

    monkeypatch.setattr(exam_release, "release_pending", spy)
    assert post(w, "owner", url, WED, dry=True).status_code == 200 and asked == []
    assert post(w, "owner", url, WED).status_code == 200 and len(asked) == 1
