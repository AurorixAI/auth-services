"""Money-1 V7: ``scripts/payf_payroll_report.py`` - the report «у каких сохранённых ведомостей fresh изменится после PAY-F» (PostgreSQL).

The scenario of ``test_payf_recalc_pg::test_17``: the student paid 600 000, the centre cancelled a lesson (the deduction), the teacher held the lesson of 06.11.  The November sheet is SAVED under the
old rule (the lesson is «не оплачено»: nothing for it), then the new rule is switched on: the report must print exactly ONE line «было → станет» for that sheet and write nothing."""
import pytest

from src.auth import payments_service
from tests.ops_helpers import clock, history_row, needs_pg, pg_session  # noqa: F401  (fixtures)
from tests.payf_helpers import API, cancel, client
from tests.test_p154_money_pg import build_org, sheet
from tests.test_payf_recalc_pg import month_world
from tests.test_payroll_pg import set_rate

pytestmark = needs_pg


def _saved_under_the_old_rule(pg_session, clock, monkeypatch, cancelled=True):
    w = build_org(pg_session)
    g, s, m = month_world(w, clock, paid=600_000)
    set_rate(w, w.A, "group", "50")
    if cancelled:
        cancel(w, g, "lesson-wed", "2026-10-21")                                        # the deduction that PAY-F counts for the teacher
    clock.set("2026-11-06 12:00")
    history_row(w.db, w.org, g, "lesson-fri", "2026-11-06", w.A, [s], start="10:00", end="10:55")
    w.db.commit()
    monkeypatch.setattr(payments_service, "PAYROLL_COUNTS_DEDUCTION", False)               # the old rule: the lesson covered by the deduction is not paid
    calc = sheet(w, "2026-11")
    saved = client(w, "owner").post(f"{API}/org/payroll/sheets", json={"month": "2026-11", "teacher_id": str(w.A.id), "expected_fingerprint": calc["source_fingerprint"]})
    assert saved.status_code == 201, saved.text
    monkeypatch.setattr(payments_service, "PAYROLL_COUNTS_DEDUCTION", True)
    return w, int(saved.json()["total_amount"])


def test_the_report_prints_one_line_for_the_sheet_that_the_new_rule_changes_and_changes_nothing(pg_session, clock, monkeypatch, capsys):
    import src.core.database as database
    from scripts import payf_payroll_report as report
    from src.models.auth import PayrollSheet

    w, saved_total = _saved_under_the_old_rule(pg_session, clock, monkeypatch)
    monkeypatch.setattr(database, "engine", pg_session.get_bind())
    code = report.main(["--org", str(w.org.id), "--months", "3", "--dry-run"])
    out = capsys.readouterr().out
    lines = [x for x in out.splitlines() if " → станет " in x]
    assert len(lines) == 1 and code == 2, out                                             # exactly one sheet changes
    assert f"сохранено {saved_total})" in lines[0] and lines[0].startswith(f"{w.org.name} · 2026-11 · ") and f"было {saved_total} → станет " in lines[0]
    assert "sheets checked=1 changed=1 mode=read-only" in out
    pg_session.expire_all()
    assert pg_session.query(PayrollSheet).filter_by(org_id=w.org.id).one().total_amount == saved_total      # read only: the sheet is as it was


def test_the_report_is_silent_when_nothing_changes(pg_session, clock, monkeypatch, capsys):
    import src.core.database as database
    from scripts import payf_payroll_report as report

    w, _ = _saved_under_the_old_rule(pg_session, clock, monkeypatch, cancelled=False)    # no lesson was cancelled by the centre: the rules give the same sum
    monkeypatch.setattr(database, "engine", pg_session.get_bind())
    assert report.main(["--org", str(w.org.id), "--months", "3"]) == 0
    assert " → станет " not in capsys.readouterr().out
