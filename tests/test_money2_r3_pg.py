"""Money-2, the round after the independent review (R3, 16.11): the holes of the row-by-row check I-5 (PostgreSQL).

Rule of these tests (LESSONS 105/115/116): a row that must have a «trace» is made THE WAY OF THE PRODUCT (a route), not by an insert - an insert has no line of the journal of its creation, so the
test could not see a hole that the creation line opens.  The distortion itself is a bare SQL statement under the feet of the code."""
from datetime import timedelta

import pytest

from src.models.auth import MembershipOpeningBalance, MoneyReconcileFinding, OrgMoneyAudit

from tests.pg_world import business_today, ensure_billing, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_money2_rows_pg import CANCEL, i2, i5, night, sql
from tests.test_payments_service_pg import mid
from tests.test_pay2_api_pg import API, fixed_today, owner  # noqa: F401  (fixture)

pytestmark = needs_pg


def opening_by_product(world, student=None, amount=250_000):
    """An opening balance made through the route of the product (it writes ``opening_balance_set`` with ``opening_id`` = the NEW row)."""
    student = student or world.students[2]
    on = (business_today() - timedelta(days=76)).isoformat()
    r = owner(world).put(f"{API}/org/memberships/{mid(world, student)}/opening-balance", json={"debt_amount": amount, "debt_on": on})
    assert r.status_code == 200, r.text
    return world.db.query(MembershipOpeningBalance).filter_by(org_id=world.org.id, membership_id=mid(world, student), voided_at=None).one().id


def test_r1_an_opening_made_by_the_product_and_cancelled_by_a_bare_update_has_no_line_of_cancellation(world):
    """The line ``opening_balance_set`` names the NEW row (the trace of the creation): it must not be taken for the trace of a cancellation of that row."""
    ensure_billing(world)
    night(world)
    row_id = opening_by_product(world)
    assert i5(night(world), world) == []
    sql(world, f"UPDATE membership_opening_balances SET {CANCEL} WHERE id = :id", id=row_id, owner=world.owner.id)
    assert f"opening:{row_id}:no_journal" in i5(night(world), world), "the debt went away without a line of cancellation"


def test_r1_an_opening_cancelled_by_the_product_and_one_replaced_by_the_product_leave_no_finding(world):
    ensure_billing(world)
    night(world)
    first = opening_by_product(world)
    night(world)
    second = opening_by_product(world, amount=300_000)                      # replaces the first one: the line ``opening_balance_set`` names it in ``voided_opening_id``
    assert second != first and i5(night(world), world) == []
    assert owner(world).post(f"{API}/org/opening-balances/{second}/void", json={"reason": "Ошибка ввода"}).status_code == 200
    assert i5(night(world), world) == []


# ============================================================================================================ Р2: accepting one finding accepts only that one
from tests.test_money2_rows_pg import make_payment, pid_of, script_main, tamper  # noqa: E402
from tests.test_payments_service_pg import pay  # noqa: E402


def accept(world, monkeypatch, key, reason="Разобрали с центром"):
    return script_main(world, monkeypatch, "--accept", key, "--reason", reason, *ACTOR_ARGS(world))


def ACTOR_ARGS(world):  # noqa: N802  (the author of an acceptance is mandatory since Р5/Р8; filled in there)
    return ()


def test_r2_accepting_changed_leaves_no_journal_of_the_same_row(world, monkeypatch):
    ensure_billing(world)
    row = pid_of(pay(world, world.students[0], amount=450_000))
    night(world)
    sql(world, "UPDATE student_payments SET amount = 900000, voided_at = now(), voided_by = :o, void_reason = 'Подложено в тест' WHERE id = :id", id=row, o=world.owner.id)
    assert i5(night(world), world) == sorted([f"payment:{row}:changed", f"payment:{row}:no_journal"])
    assert accept(world, monkeypatch, f"payment:{row}:changed") == 0
    assert i5(night(world), world) == [f"payment:{row}:no_journal"], "the cancellation without a line of the journal is NOT accepted by accepting the edit"
    assert accept(world, monkeypatch, f"payment:{row}:no_journal") == 0
    assert i5(night(world), world) == []


def test_r2_a_new_row_with_two_findings_of_its_arrival_is_accepted_one_by_one(world, monkeypatch):
    ensure_billing(world)
    night(world)
    sql(world, "INSERT INTO student_payments (id, org_id, membership_id, student_id, group_id, amount, paid_on, method, kind, created_by, created_at, currency, partial_period, voided_at, voided_by, void_reason) "
               "VALUES (gen_random_uuid(), :org, :m, :s, :g, 100000, current_date, 'cash', 'payment', :u, now(), 'UZS', false, now(), :u, 'Подложено в тест')",
        org=world.org.id, m=mid(world, world.students[0]), s=world.students[0].id, g=world.group.id, u=world.owner.id)
    from src.models.auth import StudentPayment

    row = world.db.query(StudentPayment).filter(StudentPayment.org_id == world.org.id).one().id
    assert i5(night(world), world) == sorted([f"payment:{row}:untraced", f"payment:{row}:no_journal"])
    assert accept(world, monkeypatch, f"payment:{row}:untraced") == 0
    assert i5(night(world), world) == [f"payment:{row}:no_journal"]
    assert accept(world, monkeypatch, f"payment:{row}:no_journal") == 0
    assert i5(night(world), world) == []


# ============================================================================================================ Р3: the race between a finding and its acceptance
def test_r3_the_row_moved_after_the_finding_the_acceptance_is_refused_and_the_baseline_is_untouched(world, monkeypatch, capsys):
    from src.models.auth import MoneyRowBaseline

    ensure_billing(world)
    row = pid_of(pay(world, world.students[0], amount=450_000))
    night(world)
    sql(world, "UPDATE student_payments SET amount = 200 WHERE id = :id", id=row)
    assert i5(night(world), world) == [f"payment:{row}:changed"]
    base = world.db.get(MoneyRowBaseline, ("payment", row)).fingerprint
    sql(world, "UPDATE student_payments SET amount = 900 WHERE id = :id", id=row)                  # between the night and the acceptance
    assert accept(world, monkeypatch, f"payment:{row}:changed") == 4
    assert "Запись изменилась после находки" in capsys.readouterr().err
    world.db.expire_all()
    assert world.db.get(MoneyRowBaseline, ("payment", row)).fingerprint == base
    assert world.db.query(OrgMoneyAudit).filter_by(org_id=world.org.id, action="money_reconcile_accept").count() == 0
    assert i5(night(world), world) == [f"payment:{row}:changed"]                                   # the night sees the row as it is now ...
    assert accept(world, monkeypatch, f"payment:{row}:changed") == 0                               # ... and then the acceptance is of what the night saw


def test_r3_a_finding_that_no_night_has_recorded_yet_is_not_accepted(world, monkeypatch, capsys):
    ensure_billing(world)
    row = pid_of(pay(world, world.students[0], amount=450_000))
    night(world)
    sql(world, "UPDATE student_payments SET amount = 200 WHERE id = :id", id=row)
    assert accept(world, monkeypatch, f"payment:{row}:changed") == 4
    assert "ещё не фиксировала ночная проверка" in capsys.readouterr().err


# ============================================================================================================ Р4: a second substitution under the same key is a new finding
def test_r4_a_second_edit_of_a_row_that_already_has_a_finding_is_new_and_the_letter_says_was_now(world, monkeypatch):
    sent = []
    from src.mail import sender

    monkeypatch.setattr(sender, "deliver", lambda msg, cfg=None: sent.append(msg) or True)
    monkeypatch.setenv("MONEY_RECONCILE_EMAIL", "money-team@example.test")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test"), monkeypatch.setenv("SMTP_USER", "u"), monkeypatch.setenv("SMTP_PASSWORD", "p")
    ensure_billing(world)
    row = pid_of(pay(world, world.students[0], amount=450_000))
    night(world)
    key = f"payment:{row}:changed"
    sql(world, "UPDATE student_payments SET amount = 200 WHERE id = :id", id=row)
    one = night(world, mail=True)
    assert key in i5(one, world) and one["mail"] == "sent" and len(sent) == 1
    same = night(world, mail=True)                                                                  # nobody touched the row: the same finding is not new, no letter
    assert same["new"] == 0 and same["mail"] == "skipped" and len(sent) == 1
    sql(world, "UPDATE student_payments SET amount = 900 WHERE id = :id", id=row)                   # a second substitution under the SAME key
    two = night(world, mail=True)
    found = world.db.query(MoneyReconcileFinding).filter_by(run_id=two["runId"], subject_key=key).one()
    assert found.is_new is True and two["new"] >= 1 and two["mail"] == "sent" and len(sent) == 2
    assert found.details["restated"]["wasAmount"] == 200 and found.details["restated"]["nowAmount"] == 900
    assert "правка повторная: было в прошлой находке 200 → стало 900" in sent[1].get_body(preferencelist=("plain",)).get_content()


# ============================================================================================================ Р5: a re-baseline does not wipe open findings without naming them
from uuid import uuid4  # noqa: E402

from src.models.auth import MoneyReconcileRun, MoneyRowBaseline  # noqa: E402
from tests.test_money2_rows_pg import staff_actor  # noqa: E402


def rebaseline(world, monkeypatch, *extra, reason=None, actor="staff"):
    reason = reason or f"Восстановление из копии {uuid4()}"
    args = ["--rebaseline", "--reason", reason, "--org", str(world.org.id)]
    if actor == "staff":
        args += ["--actor", staff_actor(world)]
    elif actor:
        args += ["--actor", actor]
    return script_main(world, monkeypatch, *args, *extra), reason


def test_r5_a_rebaseline_over_an_open_finding_is_refused_and_leaves_the_baseline_and_no_trace(world, monkeypatch, capsys):
    ensure_billing(world)
    row = pid_of(pay(world, world.students[0], amount=450_000))
    night(world)
    sql(world, "UPDATE student_payments SET amount = 200 WHERE id = :id", id=row)
    assert i5(night(world), world) == [f"payment:{row}:changed"]
    old = world.db.get(MoneyRowBaseline, ("payment", row)).fingerprint
    code, reason = rebaseline(world, monkeypatch)
    assert code == 4
    err = capsys.readouterr().err
    assert f"payment:{row}:changed" in err and "--with-open-findings" in err
    world.db.expire_all()
    assert world.db.get(MoneyRowBaseline, ("payment", row)).fingerprint == old
    assert world.db.query(MoneyReconcileRun).filter(MoneyReconcileRun.note.like("%" + reason + "%")).count() == 0
    assert world.db.query(OrgMoneyAudit).filter_by(org_id=world.org.id, action="money_reconcile_rebaseline").count() == 0
    assert i5(night(world), world) == [f"payment:{row}:changed"], "the finding is still there"


def test_r5_with_the_flag_the_findings_and_the_number_of_rows_that_differ_are_named_in_the_run_and_the_journal(world, monkeypatch):
    ensure_billing(world)
    row = pid_of(pay(world, world.students[0], amount=450_000))
    night(world)
    sql(world, "UPDATE student_payments SET amount = 200 WHERE id = :id", id=row)
    i5(night(world), world)
    code, reason = rebaseline(world, monkeypatch, "--with-open-findings")
    assert code == 0
    world.db.expire_all()
    run = world.db.query(MoneyReconcileRun).filter(MoneyReconcileRun.note.like("%" + reason + "%")).one()
    assert run.status == "rebaselined" and f"payment:{row}:changed" in run.note and "расходящихся строк 1" in run.note
    line = world.db.query(OrgMoneyAudit).filter_by(org_id=world.org.id, action="money_reconcile_rebaseline").one()
    assert line.details["findings"] == [f"payment:{row}:changed"] and line.details["divergentRows"] == 1 and line.details["divergent"] == [f"payment:{row}"]
    assert str(line.actor_id) and i5(night(world), world) == []


@pytest.mark.parametrize("who", [None, "garbage", "ordinary", "inactive-staff"])
def test_r5_a_rebaseline_without_an_author_who_is_a_user_of_the_platform_is_refused(world, monkeypatch, capsys, who):
    ensure_billing(world)
    pay(world, world.students[0], amount=450_000)
    night(world)
    actor = {None: None, "garbage": "nobody@example.test", "ordinary": str(world.owner.id), "inactive-staff": None}[who]
    if who == "inactive-staff":
        actor = staff_actor(world, active=False)
    code, reason = rebaseline(world, monkeypatch, actor=actor)
    assert code == 4 and "REFUSED" in capsys.readouterr().err
    world.db.expire_all()
    assert world.db.query(MoneyReconcileRun).filter(MoneyReconcileRun.note.like("%" + reason + "%")).count() == 0
    assert world.db.query(OrgMoneyAudit).filter_by(org_id=world.org.id, action="money_reconcile_rebaseline").count() == 0


def test_r5_a_clean_rebaseline_goes_through_with_the_author_in_the_journal_and_its_own_status(world, monkeypatch):
    ensure_billing(world)
    pay(world, world.students[0], amount=450_000)
    night(world)
    code, reason = rebaseline(world, monkeypatch)
    assert code == 0
    world.db.expire_all()
    assert world.db.query(MoneyReconcileRun).filter(MoneyReconcileRun.note.like("%" + reason + "%")).one().status == "rebaselined"
    assert world.db.query(OrgMoneyAudit).filter_by(org_id=world.org.id, action="money_reconcile_rebaseline").one().actor_id is not None
