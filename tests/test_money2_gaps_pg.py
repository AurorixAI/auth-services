"""Money-2: what the night did NOT check is said out loud (counters, the status of the run, the summary line), and the letter carries the findings of I-5 with the kind, the id, the centre and the sums
(PostgreSQL).  «Nothing found» is not «checked» (LESSONS 133)."""
from datetime import date

import pytest

from src.auth import money_reconcile as mr
from src.auth.payroll_service import PayrollService
from src.models.auth import MoneyReconcileCentre, MoneyReconcileRun

from tests.money1_helpers import sheet, tashkent
from tests.pg_world import ensure_billing, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_money2_rows_pg import baseline_of, make_payment, night, script_main, sql, tamper  # noqa: F401

pytestmark = needs_pg


def closed_sheet(world):
    """A saved sheet of a CLOSED month: the nightly drift check recalculates it."""
    return sheet(world, world.teacher, date(2026, 8, 1), tashkent(2026, 9, 2), total=0)          # before the accounting start: no lesson of it is unresolved (I-4)


def test_a_membership_without_a_snapshot_is_counted_on_its_first_night_only(world):
    ensure_billing(world)
    first = night(world)
    assert first["notChecked"] == {"membership_no_snapshot": 3}
    run = world.db.get(MoneyReconcileRun, first["runId"])
    assert run.not_checked == {"membership_no_snapshot": 3} and run.status == "clean", "a first night is not an alarm"
    second = night(world)
    assert second["notChecked"] == {} and world.db.get(MoneyReconcileRun, second["runId"]).not_checked is None


def test_a_sheet_the_payroll_cannot_price_makes_the_run_partial_not_clean_and_the_counter_and_the_console_say_so(world, monkeypatch, capsys):
    ensure_billing(world)
    closed_sheet(world)
    night(world)
    monkeypatch.setattr(PayrollService, "_fresh_total", lambda self, org_id, sheet, seed: None)          # no rate / a manual price: there is nothing to compare with
    result = night(world)
    assert result["notChecked"] == {"payroll_no_fresh": 1} and result["failed"] == [] and result["findings"] == 0
    run = world.db.get(MoneyReconcileRun, result["runId"])
    assert run.status == "partial" and run.not_checked == {"payroll_no_fresh": 1}
    assert [c.status for c in world.db.query(MoneyReconcileCentre).filter_by(run_id=run.id)] == ["ok"]
    assert script_main(world, monkeypatch, "--org", str(world.org.id)) == 0
    out = capsys.readouterr().out
    assert " not_checked=1" in out.splitlines()[0] and "not checked: payroll_no_fresh=1" in out
    from src.auth.platform_reconcile_router import _run_out

    assert _run_out(run)["notChecked"] == {"payroll_no_fresh": 1} and _run_out(run)["status"] == "partial"        # the console shows what the run did not check


def test_an_exception_of_the_recalculation_of_a_month_or_a_sheet_is_counted_and_does_not_hide_the_other_checks(world, monkeypatch):
    ensure_billing(world)
    closed_sheet(world)
    night(world)
    real_fresh = PayrollService._fresh_total

    def boom(*a, **k):
        raise RuntimeError("the payroll could not read the month")

    monkeypatch.setattr(PayrollService, "_fresh_total", boom)
    one = night(world)
    assert one["notChecked"] == {"payroll_error": 1} and one["failed"] == []
    assert world.db.get(MoneyReconcileRun, one["runId"]).status == "partial"
    monkeypatch.setattr(PayrollService, "_fresh_total", real_fresh)
    monkeypatch.setattr(PayrollService, "_collect", boom)
    two = night(world)
    assert two["notChecked"] == {"payroll_error": 1} and world.db.get(MoneyReconcileRun, two["runId"]).status == "partial"


def test_a_centre_that_is_not_active_is_counted_and_a_run_with_findings_stays_findings_not_partial(world, monkeypatch):
    ensure_billing(world)
    payment = make_payment(world)
    closed_sheet(world)
    night(world)
    world.other_org.status = "suspended"
    world.db.commit()
    monkeypatch.setattr(PayrollService, "_fresh_total", lambda self, org_id, sheet, seed: None)
    tamper(world, "payment", payment, "amount = 900000")
    result = mr.run(world.db, apply=True, only=[world.org.id, world.other_org.id])
    assert result["notChecked"] == {"centre_not_active": 1, "payroll_no_fresh": 1} and result["centres"] == 1
    run = world.db.get(MoneyReconcileRun, result["runId"])
    assert run.status == "findings" and run.not_checked == {"centre_not_active": 1, "payroll_no_fresh": 1}
    world.other_org.status = "active"
    world.db.commit()


def test_a_clean_run_has_no_counters_and_the_status_clean(world):
    ensure_billing(world)
    night(world)
    result = night(world)
    run = world.db.get(MoneyReconcileRun, result["runId"])
    assert run.status == "clean" and run.not_checked is None and result["notChecked"] == {}


def test_the_letter_names_every_finding_of_i5_with_the_kind_the_id_the_centre_and_the_sum_before_and_after(world, monkeypatch):
    from src.mail import sender

    sent = []
    monkeypatch.setattr(sender, "deliver", lambda msg, cfg=None: sent.append(msg) or True)
    monkeypatch.setenv("MONEY_RECONCILE_EMAIL", "money-team@example.test")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test"), monkeypatch.setenv("SMTP_USER", "u"), monkeypatch.setenv("SMTP_PASSWORD", "p")
    ensure_billing(world)
    payment = make_payment(world)
    night(world)
    tamper(world, "payment", payment, "amount = 900000")
    sql(world, "DELETE FROM student_payments WHERE id = (SELECT id FROM student_payments WHERE org_id = :o AND id <> :p LIMIT 1)", o=world.org.id, p=payment)
    result = night(world, mail=True)
    assert result["mail"] == "sent" and len(sent) == 1
    body = str(sent[0].get_body(preferencelist=("plain",)).get_content()) + str(sent[0])
    assert f"I-5 · центр {str(world.org.id)[:8]} · payment" in body and str(payment) in body
    assert "запись изменена на месте" in body and "было 450000 → стало 900000" in body
    assert "Построчная сверка записей денег (I-5)" in body
    two = night(world, mail=True)                                                                    # the finding stands and is not new: no second letter
    assert two["mail"] == "skipped" and len(sent) == 1
