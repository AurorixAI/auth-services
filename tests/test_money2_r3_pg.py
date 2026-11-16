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


def ACTOR_ARGS(world):  # noqa: N802  (the author of an acceptance is mandatory since Р8)
    from tests.test_money2_rows_pg import staff_actor

    return ("--actor", staff_actor(world))


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


# ============================================================================================================ Р6: «baseline without findings» must be true on the first night
from tests.test_money2_rows_pg import KINDS, baseline_of, make_payment, tamper  # noqa: E402


def test_r6_a_foreign_row_of_the_first_night_is_reported_and_is_in_the_baseline_and_the_second_night_only_repeats_it(world):
    ensure_billing(world)
    row = make_payment(world)
    tamper(world, "payment", row, KINDS["payment"][3])
    first = night(world)
    assert i5(first, world) == [f"payment:{row}:foreign"] and first["firstCentres"] == 1, "reported on the first night"
    assert baseline_of(world, "payment", row) is not None, "and the row is in the baseline all the same"
    second = night(world, mail=False)
    assert i5(second, world) == [f"payment:{row}:foreign"], "only the repeat of foreign: no untraced / no_journal"
    found = world.db.query(MoneyReconcileFinding).filter_by(run_id=second["runId"], subject_key=f"payment:{row}:foreign").one()
    assert found.is_new is False and i2(second, world) == []
    third = night(world)
    assert i5(third, world) == [f"payment:{row}:foreign"] and i2(third, world) == []


def test_r6_a_foreign_row_that_arrives_later_is_in_the_baseline_the_same_night_and_does_not_open_other_findings(world):
    ensure_billing(world)
    pay(world, world.students[0], amount=450_000)
    night(world)
    row = make_payment(world)                                      # arrives on the second night: no snapshot of a receipt (untraced) AND does not belong to its membership
    tamper(world, "payment", row, KINDS["payment"][3])
    keys = i5(night(world), world)
    assert f"payment:{row}:foreign" in keys and f"payment:{row}:untraced" in keys
    assert baseline_of(world, "payment", row) is None, "a row with a finding of a move (untraced) stays out of the baseline"


def test_r6_old_rows_that_exist_before_the_first_night_without_a_receipt_snapshot_or_a_journal_line_give_no_findings(world):
    ensure_billing(world)
    make_payment(world)                                            # an old payment: no snapshot of a receipt
    make_payment(world, voided=True)                               # an old cancellation: no line of the journal
    first = night(world)
    assert i5(first, world) == [] and first["baselined"] == 2
    assert i5(night(world), world) == [] and i5(night(world), world) == []


# ============================================================================================================ Р8: the small things
@pytest.mark.parametrize("who", [None, "ordinary"])
def test_r8_an_acceptance_needs_an_author_who_is_a_user_of_the_platform(world, monkeypatch, capsys, who):
    ensure_billing(world)
    row = pid_of(pay(world, world.students[0], amount=450_000))
    night(world)
    sql(world, "UPDATE student_payments SET amount = 200 WHERE id = :id", id=row)
    night(world)
    extra = [] if who is None else ["--actor", str(world.owner.id)]
    assert script_main(world, monkeypatch, "--accept", f"payment:{row}:changed", "--reason", "Разобрали с центром", *extra) == 4
    assert "REFUSED" in capsys.readouterr().err and world.db.query(OrgMoneyAudit).filter_by(org_id=world.org.id, action="money_reconcile_accept").count() == 0
    assert accept(world, monkeypatch, f"payment:{row}:changed") == 0
    line = world.db.query(OrgMoneyAudit).filter_by(org_id=world.org.id, action="money_reconcile_accept").one()
    assert line.actor_id is not None


def test_r8_the_functions_take_the_author_and_the_time_without_defaults_and_do_not_roll_back_for_the_caller(world):
    import inspect

    from src.auth import money_reconcile_rows as rows

    for fn in (rows.accept, rows.rebaseline):
        params = inspect.signature(fn).parameters
        assert params["actor_id"].default is inspect.Parameter.empty
    assert inspect.signature(rows.accept).parameters["at"].default is inspect.Parameter.empty
    marker = OrgMoneyAudit(org_id=world.org.id, actor_id=world.owner.id, action="settings_changed", details={})
    world.db.add(marker)
    world.db.flush()
    with pytest.raises(ValueError):
        rows.accept(world.db, "garbage", "Причина есть", actor_id=world.owner.id, at=business_today())
    assert marker in world.db and world.db.query(OrgMoneyAudit).filter_by(id=marker.id).count() == 1, "the caller owns the transaction: a refusal does not roll it back"


def test_r8_the_product_takes_the_names_of_the_journal_from_a_neutral_module_not_from_the_check():
    import src.auth.payroll_payouts as payouts
    from src.auth import money_journal_actions as names

    assert payouts.PAYOUT_CREATED is names.PAYOUT_CREATED and payouts.PAYOUT_VOIDED is names.PAYOUT_VOIDED
    source = open(payouts.__file__, encoding="utf-8").read()
    assert "money_reconcile_rows" not in source


def test_r8_the_baseline_is_not_rewritten_every_night_and_has_no_columns_of_the_last_night(world):
    ensure_billing(world)
    make_payment(world)
    night(world)
    assert not {"last_run_id", "last_confirmed_at"} & set(MoneyRowBaseline.__table__.columns.keys())
    from sqlalchemy import text as _text

    before = world.db.execute(_text("SELECT xmin::text FROM money_row_baseline WHERE org_id = :o"), {"o": world.org.id}).scalar()
    night(world)
    after = world.db.execute(_text("SELECT xmin::text FROM money_row_baseline WHERE org_id = :o"), {"o": world.org.id}).scalar()
    assert before == after, "a row that did not move is not written again"


# ============================================================================================================ the new payment of a correction is cancelled by a bare UPDATE
def test_a_payment_made_by_a_correction_of_the_product_and_cancelled_by_a_bare_update_has_no_line_of_cancellation(world):
    """A correction (= the old payment cancelled + a new one) writes ``payment_replace`` with ``payment_id`` = the OLD row: it is the trace of the cancellation of the old one, not of the new one."""
    ensure_billing(world)
    night(world)
    old = pid_of(pay(world, world.students[0], amount=450_000))
    replaced = owner(world).post(f"{API}/org/payments/{old}/replace", json={"amount": 500_000, "paid_on": business_today().isoformat(), "method": "cash", "reason": "Неверная сумма"})
    assert replaced.status_code == 201, replaced.text
    new = pid_of(replaced.json())
    assert new != old and i5(night(world), world) == []
    sql(world, f"UPDATE student_payments SET {CANCEL} WHERE id = :id", id=new, owner=world.owner.id)
    keys = i5(night(world), world)
    assert f"payment:{new}:no_journal" in keys, "the new payment went away without a line of cancellation"
    assert f"payment:{old}:no_journal" not in keys
