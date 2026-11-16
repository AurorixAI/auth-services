"""Money-2 (I-5): the immutable money rows are compared row by row with the baseline of the last night (PostgreSQL).

Every distortion is made the way a bug or a hand with access to the database makes it - a bare SQL ``UPDATE`` / ``DELETE`` under the feet of the code - and must be found by ID of the row, whatever else
happened to the student that night.  The chains of honest life live in ``test_money2_chains_pg.py``.  The clock is the real one."""
from datetime import date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from src.auth import money_reconcile as mr
from src.auth import money_reconcile_rows as mrows
from src.models.auth import (
    MembershipOpeningBalance, MoneyReconcileCentre, MoneyReconcileFinding, MoneyReconcileRun, MoneyRowBaseline, OrgMoneyAudit, PayrollPayout, PayrollRate, StudentPayment, StudentRefund,
    StudentWriteoff,
)

from tests.money1_helpers import sheet, tashkent  # noqa: F401
from tests.pg_world import business_today, ensure_billing, make_group, make_user, make_member, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_payments_service_pg import mid, pay
from tests.test_pay2_api_pg import fixed_today  # noqa: E402,F401  (fixture)

pytestmark = needs_pg


def night(world, **kw):
    """One night of the run over THIS centre only; returns the result of ``mr.run``."""
    return mr.run(world.db, apply=True, only=[world.org.id], **kw)


def i5(result, world):
    """The keys (``kind:row id:code``) of the I-5 findings of this centre."""
    return sorted(i["key"] for i in result["items"] if i["org"] == str(world.org.id) and i["invariant"] == "I-5")


def i2(result, world):
    return sorted(i["key"] for i in result["items"] if i["org"] == str(world.org.id) and i["invariant"] == "I-2")


def sql(world, statement, **params):
    """A bare statement in the database - the way the code under test never writes."""
    world.db.execute(text(statement), params)
    world.db.commit()


def pid_of(answer):
    return answer["payment"]["id"] if "payment" in answer else answer["id"]


# ============================================================================================================ the rows of every kind (inserted BEFORE the first night: they are in the baseline)
def _void_fields(world, voided):
    return {"voided_at": datetime.utcnow(), "voided_by": world.owner.id, "void_reason": "Подложено в тест"} if voided else {}


def make_payment(world, voided=False):
    m = mid(world, world.students[0])
    row = StudentPayment(org_id=world.org.id, membership_id=m, student_id=world.students[0].id, group_id=world.group.id, amount=450_000, paid_on=business_today(), method="cash", kind="payment",
                         created_by=world.owner.id, **_void_fields(world, voided))
    world.db.add(row)
    world.db.commit()
    return row.id


def make_refund(world, voided=False):
    row = StudentRefund(org_id=world.org.id, membership_id=mid(world, world.students[0]), student_id=world.students[0].id, group_id=world.group.id, amount=50_000, effective_on=business_today(),
                        method="cash", reason="Тест", created_by=world.owner.id, **_void_fields(world, voided))
    world.db.add(row)
    world.db.commit()
    return row.id


def make_writeoff(world, voided=False):
    row = StudentWriteoff(org_id=world.org.id, membership_id=mid(world, world.students[1]), student_id=world.students[1].id, group_id=world.group.id, amount=10_000, effective_on=business_today(),
                          reason="Тест", created_by=world.owner.id, **_void_fields(world, voided))
    world.db.add(row)
    world.db.commit()
    return row.id


def make_opening(world, voided=False):
    row = MembershipOpeningBalance(org_id=world.org.id, membership_id=mid(world, world.students[2]), student_id=world.students[2].id, group_id=world.group.id, debt_amount=100_000,
                                   debt_on=business_today() - timedelta(days=30), source="manual", created_by=world.owner.id, **_void_fields(world, voided))
    world.db.add(row)
    world.db.commit()
    return row.id


def make_transfer(world, voided=False):
    """A real transfer of the first student into another group (the link, the periods, the carried money)."""
    from tests.money1b_helpers import transfer
    from tests.pg_world import give_group_price
    from src.models.auth import MembershipTransfer

    give_group_price(world.db, world.group, 300_000)
    other = make_group(world.db, world.org, world.teacher, "Вторая")
    give_group_price(world.db, other, 400_000)
    world.db.commit()
    transfer(world, world.students[0], other)
    return world.db.query(MembershipTransfer).filter_by(org_id=world.org.id).one().id


def make_sheet(world):
    month = date.today().replace(day=1) - timedelta(days=40)
    return sheet(world, world.teacher, month.replace(day=1), tashkent(2026, 10, 30), total=475_000)


def make_payout(world, voided=False):
    from src.models.auth import PayrollSheet

    row = world.db.query(PayrollSheet).filter_by(org_id=world.org.id).first() or make_sheet(world)
    payout = PayrollPayout(org_id=world.org.id, sheet_id=row.id, teacher_id=row.teacher_id, period_month=row.period_month, amount=200_000, paid_on=business_today(), method="cash",
                           sheet_revision=row.revision, sheet_total=int(row.total_amount), created_by=world.owner.id, **_void_fields(world, voided))
    world.db.add(payout)
    world.db.commit()
    return payout.id


def make_rate(world, voided=False):
    row = PayrollRate(org_id=world.org.id, teacher_id=world.teacher.id, lesson_type="group", percent=50, valid_from=date(2026, 1, 1), created_by=world.owner.id)
    world.db.add(row)
    world.db.commit()
    return row.id


# kind -> (builder, table, SQL that changes a field of the fingerprint, SQL that moves the row to what it must not belong to)
KINDS = {
    "payment": (make_payment, "student_payments", "amount = 900000", "membership_id = (SELECT id FROM student_groups WHERE student_id = :other AND group_id = :group)"),
    "refund": (make_refund, "student_refunds", "amount = 70000", "membership_id = (SELECT id FROM student_groups WHERE student_id = :other AND group_id = :group)"),
    "writeoff": (make_writeoff, "student_writeoffs", "amount = 12000", "membership_id = (SELECT id FROM student_groups WHERE student_id = :other AND group_id = :group)"),
    "opening": (make_opening, "membership_opening_balances", "debt_amount = 100001", "membership_id = (SELECT id FROM student_groups WHERE student_id = :other AND group_id = :group)"),
    "transfer": (make_transfer, "membership_transfers", "at = at + interval '1 day'", "to_membership_id = (SELECT id FROM student_groups WHERE student_id = :other AND group_id = :group)"),
    "payout": (make_payout, "payroll_payouts", "amount = 250000", "teacher_id = :teacher2"),
    "rate": (make_rate, "payroll_rates", "percent = 99", "teacher_id = :outsider"),
}
VOIDABLE = ("payment", "refund", "writeoff", "opening", "payout")
CANCEL = "voided_at = now(), voided_by = :owner, void_reason = 'Подложено в тест'"


def tamper(world, kind, row_id, change):
    """One bare statement on the row of ``kind``."""
    _, table, _, _ = KINDS[kind]
    outsider = make_user(world.db, "teacher", "Чужой")
    world.db.commit()
    other = world.students[0] if kind == "writeoff" else world.students[1]                      # a student whose membership is not the row's own
    params = {"id": row_id, "owner": world.owner.id, "other": other.id, "group": world.group.id, "teacher2": world.teacher2.id, "outsider": outsider.id}
    sql(world, f"UPDATE {table} SET {change} WHERE id = :id", **params)


# ============================================================================================================ (а) (б) (в) (г) (д) for every kind
@pytest.mark.parametrize("kind", list(KINDS))
def test_a_row_edited_in_place_is_found_by_its_id_every_night_until_it_is_accepted_and_it_is_never_new_again(world, kind):
    ensure_billing(world)
    builder, _, change, _ = KINDS[kind]
    row_id = builder(world)
    first = night(world)
    assert i5(first, world) == [], "the first night takes the baseline and compares nothing"
    tamper(world, kind, row_id, change)
    key = f"{kind}:{row_id}:changed"
    one = night(world)
    assert key in i5(one, world) and one["new"] >= 1
    two = night(world)                                                                              # the baseline of the row is NOT refreshed: the same finding stands
    assert key in i5(two, world)
    assert [f.is_new for f in world.db.query(MoneyReconcileFinding).filter_by(run_id=two["runId"], invariant="I-5", subject_key=key)] == [False]


@pytest.mark.parametrize("kind", list(KINDS))
def test_b_a_row_deleted_from_the_table_is_found(world, kind):
    ensure_billing(world)
    builder, table, _, _ = KINDS[kind]
    row_id = builder(world)
    night(world)
    sql(world, f"DELETE FROM {table} WHERE id = :id", id=row_id)
    assert f"{kind}:{row_id}:deleted" in i5(night(world), world)


@pytest.mark.parametrize("kind", VOIDABLE)
def test_c_a_cancelled_row_that_is_live_again_or_whose_cancellation_changed_is_found(world, kind):
    ensure_billing(world)
    builder, table, _, _ = KINDS[kind]
    row_id = builder(world, voided=True)
    night(world)
    sql(world, f"UPDATE {table} SET void_reason = 'Переписано' WHERE id = :id", id=row_id)
    assert f"{kind}:{row_id}:reopened" in i5(night(world), world)                                      # the fields of the cancellation were changed
    sql(world, f"UPDATE {table} SET voided_at = NULL, voided_by = NULL, void_reason = NULL WHERE id = :id", id=row_id)
    assert f"{kind}:{row_id}:reopened" in i5(night(world), world)                                      # the cancellation was taken off


@pytest.mark.parametrize("kind", ["transfer"])
def test_c_a_transfer_is_never_cancelled_by_the_product_so_a_cancelled_one_is_found(world, kind):
    ensure_billing(world)
    builder, table, _, _ = KINDS[kind]
    row_id = builder(world)
    night(world)
    sql(world, f"UPDATE {table} SET {CANCEL} WHERE id = :id", id=row_id, owner=world.owner.id)
    assert f"{kind}:{row_id}:reopened" in i5(night(world), world)


@pytest.mark.parametrize("kind", VOIDABLE)
def test_d_a_row_cancelled_by_a_bare_update_has_no_line_in_the_journal_and_is_found(world, kind):
    ensure_billing(world)
    builder, table, _, _ = KINDS[kind]
    row_id = builder(world)
    night(world)
    tamper(world, kind, row_id, CANCEL)
    assert f"{kind}:{row_id}:no_journal" in i5(night(world), world)


@pytest.mark.parametrize("kind", list(KINDS))
def test_e_a_row_that_points_into_a_membership_a_centre_or_a_sheet_that_is_not_its_own_is_found(world, kind):
    ensure_billing(world)
    builder, _, _, wrong = KINDS[kind]
    row_id = builder(world)
    night(world)
    tamper(world, kind, row_id, wrong)
    assert f"{kind}:{row_id}:foreign" in i5(night(world), world)


def test_e_a_payment_that_replaces_or_carries_a_payment_of_another_membership_is_foreign(world):
    ensure_billing(world)
    a = make_payment(world)
    night(world)
    b = make_payment(world)
    sql(world, "UPDATE student_payments SET replaces_payment_id = :a WHERE id = :b", a=a, b=b)
    sql(world, "UPDATE student_payments SET membership_id = (SELECT id FROM student_groups WHERE student_id = :s AND group_id = :g) WHERE id = :a", a=a, s=world.students[1].id, g=world.group.id)
    keys = i5(night(world), world)
    assert f"payment:{a}:foreign" in keys                                                              # the payment of ANOTHER student: its membership says one thing, it says another
    assert f"payment:{b}:foreign" in keys                                                              # a correction of a payment of a membership that is not its own


# ============================================================================================================ the same on the night of every honest event (the old rule «something happened» let it pass)
def event_transfer(world):
    from tests.money1b_helpers import transfer
    from tests.test_money1_reconcile_round_pg import transfer_world

    _, other = transfer_world(world)
    transfer(world, world.students[0], other)


def event_leave(world):
    from tests.test_pay2_api_pg import API, owner

    assert owner(world).post(f"{API}/org/groups/{world.group.id}/students/{world.students[0].id}/leave", json={}).status_code == 200


def event_return(world):
    from tests.money1b_helpers import add

    event_leave(world)
    add(world, world.students[0], mode="add")


def event_pause(world):
    from tests.test_pay2_api_pg import API, owner

    body = {"first_day": (business_today() + timedelta(days=3)).isoformat(), "last_day": (business_today() + timedelta(days=9)).isoformat()}
    assert owner(world).post(f"{API}/org/memberships/{mid(world, world.students[0])}/pauses", json=body).status_code == 201


def event_writeoff(world):
    from tests.test_pay2_api_pg import API, owner

    r = owner(world).post(f"{API}/org/memberships/{mid(world, world.students[1])}/writeoffs", json={"all": True, "reason": "Договорённость с родителем"})
    assert r.status_code == 201, r.text


def event_refund(world):
    from tests.test_pay2_api_pg import API, owner

    pay(world, world.students[0], amount=1_500_000)
    r = owner(world).post(f"{API}/org/memberships/{mid(world, world.students[0])}/refunds", json={"amount": 50_000, "method": "cash", "reason": "Выбыл на неделю"})
    assert r.status_code == 201, r.text


def event_opening(world):
    from tests.test_pay2_api_pg import API, owner

    r = owner(world).put(f"{API}/org/memberships/{mid(world, world.students[2])}/opening-balance", json={"debt_amount": 250_000, "debt_on": (business_today() - timedelta(days=76)).isoformat()})
    assert r.status_code == 200, r.text


def event_price(world):
    from tests.pg_world import give_group_price

    give_group_price(world.db, world.group, 520_000, valid_from=business_today() + timedelta(days=40))


def event_payment(world):
    pay(world, world.students[0], amount=100_000)


EVENTS = {"transfer": event_transfer, "leave": event_leave, "return": event_return, "pause": event_pause, "forgiveness": event_writeoff, "refund": event_refund, "opening": event_opening,
          "price": event_price, "payment": event_payment}


@pytest.mark.parametrize("event", list(EVENTS))
def test_a_payment_edited_in_place_on_the_night_of_an_honest_event_is_found(world, event):
    ensure_billing(world)
    s0 = world.students[0]
    first = pay(world, s0, amount=450_000)
    pay(world, world.students[1], amount=450_000)
    night(world)
    EVENTS[event](world)
    sql(world, "UPDATE student_payments SET amount = 900000 WHERE id = :id", id=pid_of(first))
    result = night(world)
    assert f"payment:{pid_of(first)}:changed" in i5(result, world)
    assert f"membership:{mid(world, s0)}" in i2(result, world), "the sum of the payments moved by a sum no new / cancelled row explains"


# ============================================================================================================ the first night, the explicit acceptance and the explicit re-baseline
def script_main(world, monkeypatch, *argv):
    """The command of the deploy (``scripts.money_reconcile``) on the database of the test."""
    import importlib

    monkeypatch.setattr("src.core.database.engine", world.db.get_bind())
    return importlib.import_module("scripts.money_reconcile").main(list(argv))


def baseline_of(world, kind, row_id):
    world.db.expire_all()
    return world.db.get(MoneyRowBaseline, (kind, row_id))


def test_the_first_night_takes_the_baseline_without_findings_says_so_and_the_second_night_compares(world, monkeypatch, capsys):
    ensure_billing(world)
    ids = [make_payment(world), make_refund(world), make_writeoff(world), make_opening(world), make_payout(world), make_rate(world)]
    first = night(world)
    assert i5(first, world) == [] and first["firstCentres"] == 1 and first["rows"] == first["baselined"] == len(ids)
    run = world.db.get(MoneyReconcileRun, first["runId"])
    assert run.note == f"эталон записей снят в первый раз: центров 1, записей {len(ids)}"
    assert world.db.query(MoneyRowBaseline).filter_by(org_id=world.org.id).count() == len(ids)
    assert world.db.query(MoneyReconcileCentre).filter_by(run_id=first["runId"], org_id=world.org.id).one().rows_checked is True
    second = night(world)
    assert second["firstCentres"] == 0 and second["baselined"] == 0 and second["rows"] == len(ids) and i5(second, world) == []


def test_the_summary_line_keeps_its_beginning_for_the_scripts_of_the_deploy_and_adds_the_counters(world, monkeypatch, capsys):
    ensure_billing(world)
    make_payment(world)
    assert script_main(world, monkeypatch, "--org", str(world.org.id)) == 0
    line = capsys.readouterr().out.splitlines()[0]
    assert line == "mode=dry-run centres=1 failed=0 findings=0 new=0 mail=off rows=1 baselined=1 first_centres=1 not_checked=3"
    assert script_main(world, monkeypatch, "--org", str(world.org.id)) == 0
    out = capsys.readouterr().out
    assert "not checked: membership_no_snapshot=3" in out and "baseline: the first night of 1 centres" in out        # a dry run writes nothing: it is the first night again


def test_an_empty_centre_on_its_first_night_is_not_taken_for_a_first_night_again_when_its_first_row_comes(world):
    ensure_billing(world)
    first = night(world)
    assert first["rows"] == 0 and first["firstCentres"] == 1
    row_id = make_refund(world)                                                                       # a refund of the second night: a NEW row is not a finding
    assert i5(night(world), world) == []
    tamper(world, "refund", row_id, "amount = 1")
    assert f"refund:{row_id}:changed" in i5(night(world), world)


def test_accept_takes_one_finding_with_a_reason_writes_the_journal_and_leaves_the_others(world, monkeypatch, capsys):
    ensure_billing(world)
    payment, refund = make_payment(world), make_refund(world)
    night(world)
    tamper(world, "payment", payment, "amount = 900000")
    tamper(world, "refund", refund, "amount = 70000")
    keys = i5(night(world), world)
    assert keys == sorted([f"payment:{payment}:changed", f"refund:{refund}:changed"])
    old = baseline_of(world, "payment", payment).fingerprint
    assert script_main(world, monkeypatch, "--accept", f"payment:{payment}:changed", "--reason", "Правка по заявке 17") == 0
    out = capsys.readouterr().out
    assert f"accepted key=payment:{payment}:changed" in out and old in out
    world.db.expire_all()
    line = world.db.query(OrgMoneyAudit).filter_by(org_id=world.org.id, action="money_reconcile_accept").one()
    assert line.details["key"] == f"payment:{payment}:changed" and line.details["reason"] == "Правка по заявке 17" and line.details["old_fingerprint"] == old
    assert line.details["new_fingerprint"] == baseline_of(world, "payment", payment).fingerprint != old
    after = night(world)
    assert i5(after, world) == [f"refund:{refund}:changed"], "the other finding stands"
    assert i2(after, world) == [], "the sum of the accepted payment is in the snapshot of the first night already"
    tamper(world, "payment", payment, "amount = 910000")                                              # an accepted row is watched again: the next edit is a new finding
    assert f"payment:{payment}:changed" in i5(night(world), world)


@pytest.mark.parametrize("argv, why", [
    (("--accept", "payment:00000000-0000-0000-0000-000000000000:changed"), "--reason is required"),
    (("--accept", "payment:00000000-0000-0000-0000-000000000000:changed", "--reason", "ok"), "Причина: от 3 до 500 символов"),
    (("--accept", "garbage", "--reason", "Причина есть"), "Ключ находки"),
    (("--accept", "payment:00000000-0000-0000-0000-000000000000:changed", "--reason", "Причина есть"), "Такой записи нет"),
    (("--rebaseline",), "--reason is required"),
])
def test_accept_and_rebaseline_refuse_without_a_reason_with_a_wrong_key_or_for_a_row_that_is_nobodys(world, monkeypatch, capsys, argv, why):
    ensure_billing(world)
    make_payment(world)
    night(world)
    assert script_main(world, monkeypatch, *argv) == 4
    assert why in capsys.readouterr().err


def test_accept_refuses_a_key_that_is_not_a_finding_right_now(world, monkeypatch, capsys):
    ensure_billing(world)
    payment = make_payment(world)
    night(world)
    assert script_main(world, monkeypatch, "--accept", f"payment:{payment}:changed", "--reason", "Принять нечего") == 4
    assert "Сейчас такой находки нет" in capsys.readouterr().err
    assert world.db.query(OrgMoneyAudit).filter_by(org_id=world.org.id, action="money_reconcile_accept").count() == 0


def test_accept_of_a_deleted_row_forgets_it_and_accept_of_a_foreign_row_is_remembered_for_that_finding_only(world, monkeypatch):
    ensure_billing(world)
    gone, strange = make_payment(world), make_refund(world)
    night(world)
    sql(world, "DELETE FROM student_payments WHERE id = :id", id=gone)
    tamper(world, "refund", strange, KINDS["refund"][3])
    keys = i5(night(world), world)
    assert f"payment:{gone}:deleted" in keys and f"refund:{strange}:foreign" in keys and f"refund:{strange}:changed" in keys
    for key in (f"payment:{gone}:deleted", f"refund:{strange}:foreign", f"refund:{strange}:changed"):
        assert script_main(world, monkeypatch, "--accept", key, "--reason", "Разобрали с центром") == 0
    assert baseline_of(world, "payment", gone) is None and baseline_of(world, "refund", strange).accepted == ["foreign"]
    assert i5(night(world), world) == []
    tamper(world, "refund", strange, "amount = 5")
    assert i5(night(world), world) == [f"refund:{strange}:changed"], "what was accepted as foreign stays accepted, a new edit is found"


def test_rebaseline_takes_the_rows_again_prints_rows_and_reason_and_keeps_them_in_the_run_and_in_the_journal(world, monkeypatch, capsys):
    ensure_billing(world)
    payment, refund, rate = make_payment(world), make_refund(world), make_rate(world)
    night(world)
    tamper(world, "payment", payment, "amount = 900000")
    sql(world, "DELETE FROM student_refunds WHERE id = :id", id=refund)
    assert len(i5(night(world), world)) == 2
    reason = f"Восстановление базы из копии {uuid4()}"
    assert script_main(world, monkeypatch, "--rebaseline", "--reason", reason, "--org", str(world.org.id)) == 0
    assert f"rebaseline centres=1 rows=2 reason='{reason}'" in capsys.readouterr().out
    world.db.expire_all()
    run = world.db.query(MoneyReconcileRun).filter(MoneyReconcileRun.note.like("%" + reason)).one()          # (the database is shared with other tests: the reason is unique)
    assert run.note == f"пересъёмка эталона: центров 1, записей 2; причина: {reason}" and run.centres_checked == 0 and run.status == "clean"
    line = world.db.query(OrgMoneyAudit).filter_by(org_id=world.org.id, action="money_reconcile_rebaseline").one()
    assert line.details["rows"] == 2 and line.details["reason"] == reason
    assert world.db.query(MoneyRowBaseline).filter_by(org_id=world.org.id).count() == 2
    assert i5(night(world), world) == [] and rate is not None
    from src.auth.platform_reconcile_router import latest

    assert latest(p=None, db=world.db, limit=10)["latest"]["id"] == str(world.db.query(MoneyReconcileRun).filter(MoneyReconcileRun.centres_checked > 0).order_by(MoneyReconcileRun.started_at.desc()).first().id)


def test_a_new_payment_without_the_snapshot_of_a_receipt_is_found_once_per_night_and_does_not_make_the_sum_of_the_next_night_a_finding(world):
    ensure_billing(world)
    first = pay(world, world.students[0], amount=450_000)
    night(world)
    sql(world, "INSERT INTO student_payments (id, org_id, membership_id, student_id, group_id, amount, paid_on, method, kind, created_by, created_at, currency, partial_period) "
               "VALUES (gen_random_uuid(), :org, :m, :s, :g, 100000, current_date, 'cash', 'payment', :u, now(), 'UZS', false)",
        org=world.org.id, m=mid(world, world.students[0]), s=world.students[0].id, g=world.group.id, u=world.owner.id)
    stray = world.db.query(StudentPayment).filter(StudentPayment.org_id == world.org.id, StudentPayment.id != pid_of(first)).one().id
    one = night(world)
    assert f"payment:{stray}:untraced" in i5(one, world)
    assert i2(one, world) == [f"membership:{mid(world, world.students[0])}"], "the stray sum is not explained by a row WITHOUT a finding: the sum of the night is a finding too, once"
    pay(world, world.students[0], amount=50_000)                                                      # an honest payment the next day
    two = night(world)
    assert i5(two, world) == [f"payment:{stray}:untraced"] and i2(two, world) == [], "the finding stands; the stray row is in the snapshot already and must not spoil the sum of the honest payment"


def test_a_new_row_of_a_kind_that_has_no_receipt_is_not_a_finding_by_itself(world):
    ensure_billing(world)
    night(world)
    make_refund(world), make_writeoff(world), make_opening(world), make_rate(world)
    assert i5(night(world), world) == []


# ============================================================================================================ one view of the centre (the race: «the row is there, the trace is not yet»)
def test_a_payment_that_commits_while_the_centre_is_read_is_in_neither_path_of_the_night(world, monkeypatch):
    """Without ONE ``REPEATABLE READ`` transaction the rows of I-5 and the ledgers of I-2 are read at different moments: a payment made between them is in the ledger and not in the rows - a finding
    of I-2 for an honest payment.  A second connection commits the payment (with its receipt) right after the rows were read."""
    from sqlalchemy.orm import sessionmaker

    ensure_billing(world)
    pay(world, world.students[0], amount=450_000)
    night(world)
    real = mrows.evaluate
    other = sessionmaker(world.db.get_bind(), autoflush=False)()
    made = []

    def evaluate_then_a_payment_commits(db, org_id):
        verdict = real(db, org_id)
        if not made:
            from src.models.auth import PaymentReceiptSnapshot

            row = StudentPayment(org_id=world.org.id, membership_id=mid(world, world.students[0]), student_id=world.students[0].id, group_id=world.group.id, amount=100_000,
                                 paid_on=business_today(), method="cash", kind="payment", created_by=world.owner.id)
            other.add(row)
            other.flush()
            other.add(PaymentReceiptSnapshot(payment_id=row.id, org_id=world.org.id, snapshot={}))
            other.commit()
            made.append(row.id)
        return verdict

    monkeypatch.setattr(mrows, "evaluate", evaluate_then_a_payment_commits)
    racing = night(world)
    monkeypatch.setattr(mrows, "evaluate", real)
    other.close()
    assert made and i2(racing, world) == [] and i5(racing, world) == [], "one moment: the payment is in neither path yet"
    after = night(world)                                                                              # the next night sees it as a NEW row with its receipt and its sum
    assert i2(after, world) == [] and i5(after, world) == [] and after["failed"] == []
