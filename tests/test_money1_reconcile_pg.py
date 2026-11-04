"""Money-1 A7: the nightly reconciliation of I-1 .. I-4 - the pure checks, the loader over a real centre, the run (dry run by default), the letter and the console route (PostgreSQL).

The test of the lessons-package of I-2 uses a real payment; the divergences are made the way a real defect would be: a row changed under the code's feet (SQL), a sheet whose sum does not add up."""
from datetime import date, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.auth import money_reconcile as mr
from src.models.auth import GroupAttendance, MoneyReconcileFinding, MoneyReconcileRun, MoneyReconcileSnapshot, PayrollSheet, StudentPayment

from tests.console_helpers import API as CONSOLE_API, cw  # noqa: F401  (fixture)
from tests.money1_helpers import hold_lesson, now_4_nov, sheet, tashkent  # noqa: F401  (fixtures)
from tests.pg_world import ensure_billing, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_payments_service_pg import pay

pytestmark = needs_pg
ORG = uuid4()


# ------------------------------------------------------------------------------------------------------------ the pure checks (no database)
def test_i1_an_account_that_does_not_add_up_is_a_finding_and_one_that_does_is_not():
    good = SimpleNamespace(reconciles=True)
    bad = SimpleNamespace(reconciles=False, charged=1_000, paid=400, transferred_in=0, written_off=0, recalculated=0, refunded=0, transferred_out=0, debt=700, advance=0)
    assert mr.check_identity(ORG, "m1", good) is None
    found = mr.check_identity(ORG, "m1", bad)
    assert (found.invariant, found.subject_key, found.expected, found.actual) == ("I-1", "membership:m1", 600, 700)


def test_i2_paid_until_that_moved_without_a_record_is_a_finding_the_first_night_and_an_explained_move_are_not():
    before, now = (date(2026, 11, 10), None, 450_000), (date(2026, 12, 10), None, 900_000)
    assert mr.check_paid_until(ORG, "m1", None, now, False) is None                # the first night only takes the snapshot
    assert mr.check_paid_until(ORG, "m1", before, now, True) is None               # a payment explains it
    assert mr.check_paid_until(ORG, "m1", before, before, False) is None           # it did not move
    found = mr.check_paid_until(ORG, "m1", before, now, False)
    assert found.invariant == "I-2" and found.details["covered_through"] == ["2026-11-10", "2026-12-10"] and found.details["paid"] == [450_000, 900_000]


def test_i3_a_sheet_whose_total_is_not_the_sum_of_its_lines_or_that_pays_a_lesson_not_held_is_a_finding():
    lines = [{"amount": 100_000, "marks": [{"date": "2026-10-05", "lesson_id": "s1", "state": "counted"}], "group_id": "g1"}]
    assert mr.check_sheet_total(ORG, "sh1", "2026-10", lines, 100_000) is None
    found = mr.check_sheet_total(ORG, "sh1", "2026-10", lines, 130_000)
    assert (found.invariant, found.expected, found.actual) == ("I-3", 100_000, 130_000)
    marks = mr.paid_marks(lines)
    assert marks == [("g1", "2026-10-05", "s1")]
    assert mr.check_paid_lessons(ORG, "sh1", "2026-10", marks, {("g1", "2026-10-05", "s1"): "completed"}) is None
    cancelled = mr.check_paid_lessons(ORG, "sh1", "2026-10", marks, {("g1", "2026-10-05", "s1"): "cancelled"})
    assert cancelled.actual == 1 and cancelled.details["what"] == "not_held"
    assert mr.check_paid_lessons(ORG, "sh1", "2026-10", marks, {}).actual == 1                      # a lesson that is not there at all


def test_i4_a_closed_month_with_open_lessons_is_a_finding():
    assert mr.check_unresolved(ORG, "t1", "2026-10", []) is None
    found = mr.check_unresolved(ORG, "t1", "2026-10", ["2026-10-28|g1", "2026-10-29|g1"])
    assert (found.invariant, found.actual, found.subject_key) == ("I-4", 2, "closed-open-lessons:t1:2026-10")


# ------------------------------------------------------------------------------------------------------------ over a real centre
def test_a_sheet_with_a_wrong_total_and_a_paid_lesson_that_was_cancelled_are_found(world, now_4_nov):
    day, lid = hold_lesson(world, date(2026, 10, 28))
    row = sheet(world, world.teacher, date(2026, 10, 1), tashkent(2026, 10, 30), total=100_000)       # saved MID-month: open
    lines = [{"amount": 100_000, "group_id": str(world.group.id), "marks": [{"date": day.isoformat(), "lesson_id": lid, "state": "counted"}]}]
    row.lines, row.total_amount = lines, 250_000                                                     # the sum of the lines is 100 000, the sheet says 250 000
    world.db.commit()
    found = {(f.invariant, f.subject_key.split(":")[0]) for f in mr.reconcile_org(world.db, world.org.id)}
    assert ("I-3", "sheet-total") in found and ("I-3", "sheet-lessons") not in found                # the lesson is held: fine
    world.db.query(GroupAttendance).filter_by(group_id=world.group.id, lesson_date=day.isoformat()).update({"status": "cancelled"})
    world.db.commit()
    found = {(f.invariant, f.subject_key.split(":")[0]) for f in mr.reconcile_org(world.db, world.org.id)}
    assert ("I-3", "sheet-lessons") not in found                                                     # R3 #3: an OPEN month's sheet is a stage - the check of the lessons is for closed months only
    world.db.query(GroupAttendance).filter_by(group_id=world.group.id, lesson_date=day.isoformat()).update({"status": "completed"})
    row.updated_at = tashkent(2026, 11, 2)                                                           # saved after the month ended: closed
    world.db.commit()
    found = {(f.invariant, f.subject_key.split(":")[0]) for f in mr.reconcile_org(world.db, world.org.id)}
    assert ("I-3", "sheet-lessons") not in found
    world.db.query(GroupAttendance).filter_by(group_id=world.group.id, lesson_date=day.isoformat()).update({"status": "cancelled"})
    world.db.commit()
    found = {(f.invariant, f.subject_key.split(":")[0]) for f in mr.reconcile_org(world.db, world.org.id)}
    assert ("I-3", "sheet-lessons") in found                                                         # paid for a lesson that is not held


def test_a_closed_month_with_a_lesson_still_going_on_is_found_and_an_open_month_is_not(world, now_4_nov):
    from tests.money1_helpers import lesson
    from src.auth.schemas import StartAttendanceRequest

    day, lid = lesson(date(2026, 10, 28))
    world.svc.reconcile_missed_group_attendance(str(world.org.id), str(world.group.id), StartAttendanceRequest(lessonDate=day, lessonId=lid), reconciled_by=str(world.admin.id), is_org_admin=True)
    row = sheet(world, world.teacher, date(2026, 10, 1), tashkent(2026, 10, 30))                    # mid-month: open
    assert [f for f in mr.reconcile_org(world.db, world.org.id) if f.invariant == "I-4"] == []
    row.updated_at = tashkent(2026, 11, 2)                                                           # saved after the month: closed, the lesson is still «идёт»
    world.db.commit()
    (found,) = [f for f in mr.reconcile_org(world.db, world.org.id) if f.invariant == "I-4"]
    assert found.month == "2026-10" and found.actual >= 31 and found.subject_id == str(world.teacher.id)      # R3 #2: the lesson going on AND every never-started lesson of October (a lesson every day here)


def test_paid_until_that_moves_under_the_feet_of_the_journal_is_found_and_a_new_payment_is_not(world):
    """(the real clock: the records of the database are stamped by its own ``now()``)"""
    ensure_billing(world)
    s0 = world.students[0]
    pay(world, s0, amount=450_000)
    first = mr.run(world.db, apply=True, only=[world.org.id])                                                             # night 1: the snapshots
    assert first["findings"] == 0 or all(i["invariant"] != "I-2" for i in first["items"])
    assert world.db.query(MoneyReconcileSnapshot).filter_by(org_id=world.org.id).count() == 3
    pay(world, s0, amount=450_000)                                                                   # a lawful move: a payment (a record that explains it)
    second = mr.run(world.db, apply=True, only=[world.org.id])
    assert all(not (i["invariant"] == "I-2" and i["org"] == str(world.org.id)) for i in second["items"])
    mine = world.db.query(StudentPayment).filter_by(org_id=world.org.id).order_by(StudentPayment.created_at.desc()).first()
    world.db.query(StudentPayment).filter_by(id=mine.id).update({"amount": 1_350_000})              # a silent change in the database: no record explains the move
    world.db.commit()
    third = mr.run(world.db, apply=True, only=[world.org.id])
    ours = [i for i in third["items"] if i["org"] == str(world.org.id) and i["invariant"] == "I-2"]
    assert len(ours) == 1 and ours[0]["key"].startswith("membership:")


# ------------------------------------------------------------------------------------------------------------ the run
def test_the_run_is_a_dry_run_by_default_and_stores_nothing(world, now_4_nov):
    before = world.db.query(MoneyReconcileRun).count()
    result = mr.run(world.db, only=[world.org.id])
    assert result["apply"] is False and "runId" not in result
    assert world.db.query(MoneyReconcileRun).count() == before and world.db.query(MoneyReconcileSnapshot).filter_by(org_id=world.org.id).count() == 0


def test_the_run_stores_the_findings_marks_the_known_ones_and_writes_to_the_team_only_about_new_ones(world, monkeypatch):
    sent = []
    from src.mail import sender

    monkeypatch.setattr(sender, "deliver", lambda msg, cfg=None: sent.append(msg) or True)
    monkeypatch.setenv("MONEY_RECONCILE_EMAIL", "money-team@example.test")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test"), monkeypatch.setenv("SMTP_USER", "u"), monkeypatch.setenv("SMTP_PASSWORD", "p")
    row = sheet(world, world.teacher, date(2026, 10, 1), tashkent(2026, 10, 30), total=100_000)
    row.total_amount = 999_000                                                                       # lines [] -> sum 0
    world.db.commit()
    first = mr.run(world.db, apply=True, mail=True, only=[world.org.id])
    assert first["new"] >= 1 and first["mail"] == "sent" and len(sent) == 1
    letter = sent[0]
    assert letter["To"] == "money-team@example.test" and "Сверка денег" in letter["Subject"]
    body = letter.get_body(preferencelist=("plain",)).get_content()
    assert world.teacher.last_name not in body and world.teacher.email not in body and "sheet-total" in body        # ids and sums, no names
    run_row = world.db.get(MoneyReconcileRun, first["runId"])
    assert run_row.status == "findings" and run_row.mail_status == "sent"
    second = mr.run(world.db, apply=True, mail=True, only=[world.org.id])                                                 # the same finding stands: it is in the console, not in the mailbox again
    ours = [f for f in world.db.query(MoneyReconcileFinding).filter_by(run_id=second["runId"], org_id=world.org.id)]
    assert ours and not any(f.is_new for f in ours) and second["mail"] == "skipped" and len(sent) == 1


def test_a_clean_night_writes_no_letter(world, monkeypatch):
    sent = []
    from src.mail import sender

    monkeypatch.setattr(sender, "deliver", lambda msg, cfg=None: sent.append(msg) or True)
    result = mr.run(world.db, apply=True, mail=True, only=[world.org.id])
    quiet = mr.run(world.db, apply=True, mail=True, only=[world.org.id])               # the same state again: whatever was found the first time is not new
    assert quiet["new"] == 0 and quiet["mail"] == "skipped" and len(sent) <= 1


# ------------------------------------------------------------------------------------------------------------ the console
def test_the_console_reads_the_runs_with_the_right_and_a_centre_cannot(cw):
    org = cw.make_centre()
    cw.db.add(MoneyReconcileRun(status="findings", centres_checked=1, findings_count=1, new_findings_count=1))
    cw.db.flush()
    run_row = cw.db.query(MoneyReconcileRun).order_by(MoneyReconcileRun.started_at.desc()).first()
    cw.db.add(MoneyReconcileFinding(run_id=run_row.id, org_id=org.org.id, invariant="I-1", subject_key="membership:m1", expected=600, actual=700))
    cw.db.commit()
    staff = cw.make_staff(["audit.view"])
    ok = cw.signed_in(staff).get(f"{CONSOLE_API}/money-reconcile")
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["latest"]["findings"] == 1 and body["findings"][0]["invariant"] == "I-1" and body["findings"][0]["orgId"] == str(org.org.id)
    assert cw.signed_in(cw.make_staff(["requests.view"])).get(f"{CONSOLE_API}/money-reconcile").status_code == 403
    assert cw.new_client().get(f"{CONSOLE_API}/money-reconcile").status_code in (401, 403)


def test_nothing_to_say_is_no_letter():
    assert mr._mail([], 0, [], "run") == "skipped"
    assert mr._mail([mr.Found(ORG, "I-1", "membership:m1")], 0, [], "run") == "skipped"          # found, but it stood last night too


def test_the_command_is_a_dry_run_by_default_and_prints_counters_and_ids_only(world, capsys):
    import importlib

    script = importlib.import_module("scripts.money_reconcile")
    before = world.db.query(MoneyReconcileRun).count()
    code = script.main([])
    out = capsys.readouterr().out
    assert out.startswith("mode=dry-run centres=") and "by invariant: I-1=" in out and code in (0, 2)
    assert "@" not in out                                                                       # no address, no e-mail of a person
    world.db.expire_all()
    assert world.db.query(MoneyReconcileRun).count() == before                                  # a dry run stores nothing
