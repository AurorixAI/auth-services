"""Money-1 R3 #3 #16 #18: the nightly reconciliation checks the CALCULATION and does not cry wolf (PostgreSQL).

Every honest chain of the R3-деньги report must give 0 findings (strict: the list is compared with ``[]``, after a check that the run really looked at something); every distortion they injected
must be found - exactly once, by the invariant that owns it.  The clock is the real one: the records of the database are stamped by its own ``now()``."""
from datetime import date

import pytest

from src.auth import money_reconcile as mr
from src.models.auth import (
    AttendanceExcuse, GroupAttendance, MoneyReconcileCentre, MoneyReconcileFinding, MoneyReconcileRun, MoneyReconcileSnapshot, StudentPayment, StudentRefund, StudentWriteoff,
)

from tests.money1_helpers import hold_lesson, lesson, sheet, tashkent  # noqa: F401
from tests.ops_helpers import grant_money_right
from tests.pg_world import business_today, ensure_billing, make_client, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_logicA_payroll_pg import API, g_pay_world, owner, save_sheet
from tests.test_payments_service_pg import mid, pay, set_settings
from tests.test_pay2_api_pg import fixed_today  # noqa: E402,F401  (fixture)

pytestmark = needs_pg


def night(world, **kw):
    """One night of the run over THIS centre only; returns the result of ``mr.run``."""
    return mr.run(world.db, apply=True, only=[world.org.id], **kw)


def mine(result, world, invariant=None):
    return [i for i in result["items"] if i["org"] == str(world.org.id) and (invariant is None or i["invariant"] == invariant)]


def package_centre(w):
    set_settings(w, period_mode="lessons", lessons_per_payment=12)
    ensure_billing(w)


# ============================================================================================================ I-2 on a package centre (НЕТОЧНОСТЬ-1)
def test_r3_16_honest_events_of_a_package_centre_give_no_i2_finding_and_a_silent_change_of_money_gives_exactly_one(world):
    package_centre(world)
    s0 = world.students[0]
    pay(world, s0, amount=450_000)                                                                  # one package of 12 lessons
    first = night(world)
    assert world.db.query(MoneyReconcileSnapshot).filter_by(org_id=world.org.id).count() == 3        # the run really looked at the three memberships
    assert mine(first, world, "I-2") == []
    for day in (date(2026, 10, 28), date(2026, 10, 29)):                                            # a held lesson moves "paid until" of a package account - the old check cried 3 + 1 times
        hold_lesson(world, day)
        assert mine(night(world), world, "I-2") == [], day
    excuse = world.db.query(AttendanceExcuse)
    from src.auth.attendance_excuses import AttendanceExcuseService  # noqa: F401  (the review below goes through the route)
    row = world.db.query(GroupAttendance).filter_by(group_id=world.group.id, lesson_date="2026-10-29").one()
    row.records = {**row.records, str(s0.id): "excused"}                                             # a mark turned into an excuse and confirmed
    world.db.add(AttendanceExcuse(org_id=world.org.id, attendance_id=row.id, student_id=s0.id, status="confirmed", reason="Справка", reason_by=world.owner.id, reason_at=row.created_at,
                                  reviewed_by=world.owner.id, reviewed_at=row.created_at))
    world.db.commit()
    assert mine(night(world), world, "I-2") == []
    set_settings(world, period_mode="lessons", lessons_per_payment=12, billing_anchor="fixed_day", billing_day=5)       # the owner changes the settings of the centre
    from src.models.auth import Organization
    world.db.query(Organization).filter_by(id=world.org.id).update({"updated_at": __import__("datetime").datetime.utcnow()})
    world.db.commit()
    assert mine(night(world), world, "I-2") == []
    # a SILENT change of the sum of a payment: no record explains it
    paid = world.db.query(StudentPayment).filter_by(org_id=world.org.id).one()
    world.db.query(StudentPayment).filter_by(id=paid.id).update({"amount": 900_000})
    world.db.commit()
    third = night(world)
    (hit,) = mine(third, world, "I-2")
    assert hit["key"] == f"membership:{mid(world, s0)}"
    assert excuse is not None


def test_r3_16_a_calendar_centre_stays_quiet_through_a_payment_a_day_off_and_a_price_change(world):
    ensure_billing(world)
    s0 = world.students[0]
    pay(world, s0, amount=450_000)
    night(world)
    pay(world, s0, amount=450_000)                                                                  # a payment moves "paid until" - a record explains it
    assert mine(night(world), world, "I-2") == []
    from tests.pg_world import give_group_price
    give_group_price(world.db, world.group, 500_000, valid_from=date(2026, 12, 1))                    # a price that starts later
    assert mine(night(world), world, "I-2") == []
    r = owner(world).post(f"{API}/org/days-off", json={"dateFrom": "2026-12-10", "dateTo": "2026-12-10", "reason": "Праздник"})
    assert r.status_code == 201, r.text
    assert mine(night(world), world, "I-2") == []                                                    # a day off moves the coverage of the centre


# ============================================================================================================ I-1 (НЕТОЧНОСТЬ-3)
def test_r3_18_i1_the_injected_distortions_of_the_account_are_found_and_an_honest_chain_is_not(world):
    ensure_billing(world)
    s0, s1, _ = world.students
    pay(world, s0, amount=450_000)
    pay(world, s1, amount=450_000)
    assert [f for f in mr.reconcile_org(world.db, world.org.id) if f.invariant == "I-1"] == []        # honest
    # a refund of 5 000 000 against a payment of 450 000 (inserted under the feet of the code)
    m0 = mid(world, s0)
    world.db.add(StudentRefund(org_id=world.org.id, membership_id=m0, student_id=s0.id, group_id=world.group.id, amount=5_000_000, effective_on=date(2026, 11, 1), method="cash",
                               reason="Подложено", created_by=world.owner.id))
    # a forgiveness of 99 000 000 against a debt of about 450 000
    m1 = mid(world, s1)
    world.db.add(StudentWriteoff(org_id=world.org.id, membership_id=m1, student_id=s1.id, group_id=world.group.id, amount=99_000_000, effective_on=date(2026, 11, 1),
                                 reason="Подложено", created_by=world.owner.id))
    world.db.commit()
    keys = sorted(f.subject_key for f in mr.reconcile_org(world.db, world.org.id) if f.invariant == "I-1")
    assert f"membership:{m0}:outflow" in keys and f"membership:{m1}:writeoff" in keys               # BOTH injected defects (before the round: one of two)


def test_r3_18_i1_every_path_of_the_account_has_its_pure_check():
    from types import SimpleNamespace

    ledger = SimpleNamespace(reconciles=True, charged=450_000, paid=450_000, transferred_in=0, written_off=0, recalculated=0, refunded=0, transferred_out=0, debt=0, advance=0,
                             statement=(SimpleNamespace(balance_after=0),))
    org = __import__("uuid").uuid4()
    assert mr.check_statement(org, "m", ledger) is None and mr.check_rows(org, "m", ledger, mr.RowSums(paid=450_000)) == []
    bad_statement = SimpleNamespace(**{**vars(ledger), "statement": (SimpleNamespace(balance_after=-10),)})
    assert mr.check_statement(org, "m", bad_statement).details["what"] == "statement"
    (paid,) = mr.check_rows(org, "m", ledger, mr.RowSums(paid=400_000))                                 # the ledger counts 450 000, the rows say 400 000
    assert (paid.details["what"], paid.expected, paid.actual) == ("paid_rows", 400_000, 450_000)
    (refund,) = mr.check_rows(org, "m", ledger, mr.RowSums(paid=450_000, refunded=10))
    assert refund.details["what"] == "refund_rows"
    out = SimpleNamespace(**{**vars(ledger), "refunded": 500_000})
    assert [f.details["what"] for f in mr.check_rows(org, "m", out, mr.RowSums(paid=450_000, refunded=500_000))] == ["outflow_exceeds_inflow"]
    assert [f.details["what"] for f in mr.check_rows(org, "m", ledger, mr.RowSums(paid=450_000, written_off=99_000_000))] == ["writeoff_exceeds_charged"]
    assert mr.check_rows(org, "m", ledger, mr.RowSums(paid=450_000, written_off=450_000)) == []          # forgiving everything that was charged is lawful


# ============================================================================================================ I-3 (НЕТОЧНОСТЬ-3)
def test_r3_18_i3_a_drift_of_a_closed_month_and_a_line_without_marks_are_found_an_honest_sheet_is_not(world, fixed_today):
    m, s1, s2 = g_pay_world(world)
    saved = save_sheet(world, m)                                                                     # 475 000, saved after the month ended: CLOSED
    assert saved["total_amount"] == 475_000
    assert [f for f in mr.reconcile_org(world.db, world.org.id) if f.invariant == "I-3"] == []       # honest: the fresh total is the saved one
    from tests.test_logicA_payroll_pg import student_group, _today

    r = owner(world).post(f"{API}/org/memberships/{student_group(world, s2).id}/payments", json={"amount": 300_000, "paid_on": _today().isoformat(), "method": "cash"})
    assert r.status_code == 201, r.text                                                              # a late payment: the data now says 600 000, the teacher was paid by 475 000
    (drift,) = [f for f in mr.reconcile_org(world.db, world.org.id) if f.invariant == "I-3"]
    assert (drift.details["what"], drift.expected, drift.actual, drift.details["changedBy"]) == ("drift", 600_000, 475_000, 125_000)
    # a self-consistent sheet with a line of 100 000 and no lesson behind it
    from src.models.auth import PayrollSheet

    row = world.db.query(PayrollSheet).filter_by(id=saved["id"]).one()
    lines = [{"amount": 100_000, "group_id": str(world.group.id), "line_key": "x", "marks": []}]
    row.lines, row.total_amount = lines, 100_000
    world.db.commit()
    kinds = sorted(f.details["what"] for f in mr.reconcile_org(world.db, world.org.id) if f.invariant == "I-3")
    assert "line_without_marks" in kinds                                                            # the form is fine (100 000 = 100 000), the calculation is not


# ============================================================================================================ the operation (m6)
def test_r3_3_two_runs_at_once_the_second_answers_busy_and_writes_nothing(world):
    from sqlalchemy import text

    engine = world.db.get_bind()
    engine = getattr(engine, "engine", engine)
    holder = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        assert holder.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": mr.LOCK_KEY}).scalar() is True
        before = world.db.query(MoneyReconcileRun).count()
        busy = mr.run(world.db, apply=True, only=[world.org.id])
        assert busy["busy"] is True and world.db.query(MoneyReconcileRun).count() == before
    finally:
        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": mr.LOCK_KEY})
        holder.close()
    assert "busy" not in night(world)                                                                # the lock was let go: the next night runs


def test_r3_3_a_centre_that_could_not_be_checked_is_named_and_does_not_make_old_findings_new_next_night(world, monkeypatch):
    sent = []
    from src.mail import sender

    monkeypatch.setattr(sender, "deliver", lambda msg, cfg=None: sent.append(msg) or True)
    monkeypatch.setenv("MONEY_RECONCILE_EMAIL", "money-team@example.test")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test"), monkeypatch.setenv("SMTP_USER", "u"), monkeypatch.setenv("SMTP_PASSWORD", "p")
    row = sheet(world, world.teacher, date(2026, 10, 1), tashkent(2026, 10, 30), total=100_000)
    row.total_amount = 999_000                                                                       # a finding that stands
    world.db.commit()
    one = night(world, mail=True)
    assert one["new"] >= 1 and len(sent) == 1
    real = mr.reconcile_org

    def broken(db, org_id, *a, **k):
        raise RuntimeError("the centre could not be read")

    monkeypatch.setattr(mr, "reconcile_org", broken)
    two = night(world, mail=True)                                                                    # the night the centre fails
    assert two["failed"] == [str(world.org.id)] and two["findings"] == 0
    run_two = world.db.get(MoneyReconcileRun, two["runId"])
    assert run_two.status == "failed" and run_two.error == "не удалось проверить центров: 1" and run_two.mail_status == "sent"      # a run with no findings is no silence: the letter says it
    assert "сбой проверки" in sent[-1]["Subject"] and len(sent) == 2
    assert [c.status for c in world.db.query(MoneyReconcileCentre).filter_by(run_id=two["runId"])] == ["failed"]
    monkeypatch.setattr(mr, "reconcile_org", real)
    three = night(world, mail=True)                                                                  # the centre is back: the finding of night 1 is the SAME finding, not a new one
    ours = world.db.query(MoneyReconcileFinding).filter_by(run_id=three["runId"], org_id=world.org.id).all()
    assert ours and not any(f.is_new for f in ours) and three["new"] == 0 and three["mail"] == "skipped" and len(sent) == 2


# ============================================================================================================ honest chains: 0 findings after EVERY event
@pytest.mark.parametrize("mode", ["calendar", "lessons"])
def test_r3_16_an_honest_life_of_the_accounts_gives_no_finding_after_any_event(world, mode):
    """A payment, a second one, the cancellation of the first, a replacement, a forgiveness and its cancellation, a refund, a pause, a new price of the group, a lesson cancelled by the centre
    (with the deduction on), a held lesson - and a night after EACH.  I-1 and I-2 stay silent on a calendar centre and on a package centre alike."""
    from datetime import timedelta

    from src.auth.schemas import ReconcileMissedLessonRequest
    from tests.pg_world import give_group_price

    if mode == "lessons":
        package_centre(world)
    else:
        ensure_billing(world)
    from tests.test_payments_service_pg import set_settings as _settings  # noqa: F401
    from tests.pg_world import payments_block

    payments_block(world.db, world.org, recalc_history=[{"from": "min", "on": True}])
    s0, s1, s2 = world.students
    owner_client = owner(world)
    seen = night(world)                                                                              # night 0: the snapshots
    assert world.db.query(MoneyReconcileSnapshot).filter_by(org_id=world.org.id).count() == 3        # it looked at the three memberships

    def quiet(label):
        result = night(world)
        assert [i for i in mine(result, world) if i["invariant"] in ("I-1", "I-2")] == [], label
        return result

    p1 = pay(world, s0, amount=450_000)
    quiet("payment")
    p2 = pay(world, s0, amount=300_000)
    quiet("second payment")
    voided = owner_client.post(f"{API}/org/payments/{p2['payment']['id'] if 'payment' in p2 else p2['id']}/void", json={"reason": "Внесли дважды"})
    assert voided.status_code == 200, voided.text
    quiet("void a payment")
    pid = p1["payment"]["id"] if "payment" in p1 else p1["id"]
    replaced = owner_client.post(f"{API}/org/payments/{pid}/replace", json={"amount": 500_000, "paid_on": business_today().isoformat(), "method": "cash", "reason": "Неверная сумма"})
    assert replaced.status_code == 201, replaced.text
    quiet("replace a payment")
    w = owner_client.post(f"{API}/org/memberships/{mid(world, s1)}/writeoffs", json={"all": True, "reason": "Договорённость с родителем"})
    if w.status_code == 201:
        quiet("forgiveness")
        assert owner_client.post(f"{API}/org/writeoffs/{w.json()['writeoff']['id']}/void", json={"reason": "Ошибка"}).status_code == 200
        quiet("cancel a forgiveness")
    refund = owner_client.post(f"{API}/org/memberships/{mid(world, s0)}/refunds", json={"amount": 50_000, "method": "cash", "reason": "Выбыл на неделю"})
    if refund.status_code == 201:
        quiet("refund")
    pause = owner_client.post(f"{API}/org/memberships/{mid(world, s2)}/pauses", json={"first_day": (business_today() + timedelta(days=3)).isoformat(), "last_day": (business_today() + timedelta(days=9)).isoformat()})
    if pause.status_code == 201:
        quiet("pause")
    give_group_price(world.db, world.group, 520_000, valid_from=business_today() + timedelta(days=40))
    quiet("a price that starts later")
    day, lid = date(2026, 10, 26), "s1"
    world.svc.cancel_missed_group_attendance(str(world.org.id), str(world.group.id), ReconcileMissedLessonRequest(lessonDate=day, lessonId=lid, cancellationReason="Нет света", cancellationKind="centre"),
                                             cancelled_by=str(world.admin.id), is_org_admin=True, is_org_manager=True)
    quiet("a lesson cancelled by the centre (the deduction)")
    hold_lesson(world, date(2026, 10, 28))
    quiet("a held lesson")
    assert seen is not None


def test_r3_16_a_package_account_is_compared_by_lessons_paid_not_by_the_date_that_moves_with_every_held_lesson():
    """Pure: the same ledger one night later, one more lesson held - the date of «paid until» moved by a day, the lessons paid for did not.  The old snapshot kept the date and cried."""
    from types import SimpleNamespace

    def ledger(mode, through, covered):
        return SimpleNamespace(mode=mode, covered_through=through, paid=450_000, lessons=SimpleNamespace(covered=covered) if mode == "lessons" else None)

    org = __import__("uuid").uuid4()
    night1, night2 = ledger("lessons", date(2026, 10, 28), 12), ledger("lessons", date(2026, 10, 29), 12)
    assert mr.coverage_of(night1) == (None, 12, 450_000) and mr.coverage_of(night1) == mr.coverage_of(night2)
    assert mr.check_paid_until(org, "m", mr.coverage_of(night1), mr.coverage_of(night2), False) is None          # not explained by anything - and still nothing to explain
    assert mr.coverage_of(ledger("calendar", date(2026, 11, 10), 12)) == (date(2026, 11, 10), None, 450_000)
    moved = mr.check_paid_until(org, "m", mr.coverage_of(ledger("calendar", date(2026, 11, 10), 0)), mr.coverage_of(ledger("calendar", date(2026, 12, 10), 0)), False)
    assert moved is not None                                                                                      # a calendar account that moved without a record: a finding
    assert mr.check_paid_until(org, "m", (None, 12, 450_000), (None, 12, 900_000), explained=True, paid_explained=False) is not None       # the coverage is explained, the gross money is not
