"""Money-2, the third round after the independent check (R3c, 17.11): Г1 - ONE rule for the opening balance (PostgreSQL), Г2 - the small things.

Г1: while the launch of the accounting of the centre is not finished (``payments.openings_state`` = none / pending) the owner and an administrator with the right to money put, change and cancel
an opening balance (no check of the age); when it is finished (done / skipped) - the owner only (403 ``OPENING_OWNER_ONLY``).  Every road of the opening balance asks the same function."""
from datetime import date

from src.auth import payment_status as ps

import pytest

from tests.money1b_helpers import add
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.ops_helpers import exams  # noqa: F401  (fixture: exam-service stub)
from tests.test_money2_fix_r3b_pg import world_v4
from tests.test_money2_transfer_a_pg import groups3
from tests.test_opening_bulk_pg import BASE, apply, export_xlsx, import_xlsx, it, ledger_of, live_openings, preview
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, settings_block, stay  # noqa: F401  (fixture)

pytestmark = needs_pg

STATES = ["none", "pending", "done", "skipped"]
MESSAGE = "Учёт запущен — остаток на начало вносит и меняет владелец"


def launched(state):
    return state in ("done", "skipped")


def forbid(r):
    assert r.status_code == 403 and r.json()["detail"]["code"] == "OPENING_OWNER_ONLY" and r.json()["detail"]["message"] == MESSAGE, r.text


def world_g1(world, fixed_today, state):
    """Today Friday 20.11.2026; S0, S1, S2 are in A since 02.11; the launch of the accounting is in ``state``."""
    a, s0, admin = world_v4(world, fixed_today)
    settings_block(world, openings_state=state)
    return a, s0, admin


DEBT = {"debt_amount": 50_000, "debt_on": "2026-11-02"}


# ============================================================================================================ Г1: the card of the student


@pytest.mark.parametrize("state", STATES)
def test_g1_card_put_replace_and_cancel_by_the_state_of_the_launch(world, fixed_today, state):
    # the age of the opening is NOT checked any more (the debt is from 02.11, 18 days back: the old rule would refuse the administrator in every state); only the state of the launch decides
    a, s0, admin = world_g1(world, fixed_today, state)
    url = f"{API}/org/memberships/{membership(world, s0, a).id}/opening-balance"
    if launched(state):
        forbid(admin.put(url, json=DEBT))
        forbid(admin.put(url, params={"dry_run": "true"}, json=DEBT))
        assert live_openings(world) == []
        assert owner(world).put(url, json=DEBT).status_code == 200                                         # the owner always
        opening_id = live_openings(world)[0].id
        forbid(admin.put(url, json={"debt_amount": 60_000, "debt_on": "2026-11-02", "reason": "Уточнение"}))
        forbid(admin.put(url, json={"reason": "Снять"}))
        forbid(admin.post(f"{API}/org/opening-balances/{opening_id}/void", json={"reason": "Ошибка"}))
        assert [o.debt_amount for o in live_openings(world)] == [50_000]
        assert owner(world).put(url, json={"debt_amount": 60_000, "debt_on": "2026-11-02", "reason": "Уточнение"}).status_code == 200
        assert owner(world).post(f"{API}/org/opening-balances/{[o for o in live_openings(world)][0].id}/void", json={"reason": "Ошибка"}).status_code == 200
    else:
        assert admin.put(url, params={"dry_run": "true"}, json=DEBT).status_code == 200
        assert admin.put(url, json=DEBT).status_code == 200
        assert admin.put(url, json={"debt_amount": 60_000, "debt_on": "2026-11-02", "reason": "Уточнение"}).status_code == 200
        assert admin.post(f"{API}/org/opening-balances/{live_openings(world)[0].id}/void", json={"reason": "Ошибка"}).status_code == 200
        assert live_openings(world) == []


@pytest.mark.parametrize("state", ["none", "done"])
def test_g1_the_hole_an_administrator_cannot_wipe_the_debt_of_two_months_with_a_paid_until_after_the_launch(world, fixed_today, state):
    # S0 is in A since 02.11 and owes the whole of 02-20.11 (nothing paid); «оплачено до» 30.11 closes every day from the first one = the debt is wiped.  After «Готово» the administrator gets 403 and the debt is the same
    a, s0, admin = world_g1(world, fixed_today, state)
    url = f"{API}/org/memberships/{membership(world, s0, a).id}/opening-balance"
    debt_before = ledger_of(world, membership(world, s0, a)).debt
    assert debt_before > 0
    r = admin.put(url, json={"paid_until": "2026-11-30"})
    if launched(state):
        forbid(r)
        assert ledger_of(world, membership(world, s0, a)).debt == debt_before and live_openings(world) == []
    else:
        assert r.status_code == 200, r.text
        assert ledger_of(world, membership(world, s0, a)).debt < debt_before


# ============================================================================================================ Г1: the table of the centre, the file


@pytest.mark.parametrize("state", STATES)
def test_g1_the_table_preview_and_apply_by_the_state_of_the_launch(world, fixed_today, state):
    a, s0, admin = world_g1(world, fixed_today, state)
    m = membership(world, s0, a)
    items = [it(world, m, debt_amount=70_000, debt_on="2026-11-02")]
    if launched(state):
        forbid(admin.post(f"{BASE}/batch/preview", json={"items": items, "reason": None}))
        digest = preview(world, items)["digest"]                                                          # the owner's preview
        r = admin.post(f"{BASE}/batch", json={"items": items, "reason": None, "idempotency_key": "7d1f8b8e-0c36-4b0f-a0a1-3a0c9d6f1b11", "digest": digest})
        forbid(r)
        assert live_openings(world) == []
        apply(world, items)                                                                               # the owner
        assert [o.debt_amount for o in live_openings(world)] == [70_000]
        items2 = [it(world, m, clear=True)]
        digest2 = preview(world, items2, reason="Снять")["digest"]
        forbid(admin.post(f"{BASE}/batch", json={"items": items2, "reason": "Снять", "idempotency_key": "7d1f8b8e-0c36-4b0f-a0a1-3a0c9d6f1b12", "digest": digest2}))
        assert len(live_openings(world)) == 1
    else:
        apply(world, items, client=admin)
        assert [o.debt_amount for o in live_openings(world)] == [70_000]


@pytest.mark.parametrize("state", STATES)
def test_g1_the_import_of_the_file_by_the_state_of_the_launch(world, fixed_today, state):
    a, s0, admin = world_g1(world, fixed_today, state)
    data = export_xlsx(world).content
    if launched(state):
        forbid(admin.post(f"{BASE}/import/preview", files={"file": ("ostatki.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}))
        assert import_xlsx(world, data)                                                                   # the owner reads the same file
    else:
        assert import_xlsx(world, data, client=admin)


# ============================================================================================================ Г1: the window of adding


@pytest.mark.parametrize("state", STATES)
def test_g1_the_window_of_adding_with_an_opening_balance_by_the_state_of_the_launch(world, fixed_today, state):
    a, s0, admin = world_g1(world, fixed_today, state)
    b = groups3(world, fixed_today)[1]
    body = {"opening_balance": {"debt_amount": 40_000, "debt_on": "2026-11-19"}}
    if launched(state):
        forbid_add(add(world, s0, group=b, mode="add", client=admin, expect=403, dry_run=True, **body))
        forbid_add(add(world, s0, group=b, mode="add", client=admin, expect=403, **body))
        assert live_openings(world) == [] and not membership_of(world, s0, b)
        add(world, s0, group=b, mode="add", **body)                                                       # the owner
        assert [o.debt_amount for o in live_openings(world)] == [40_000]
    else:
        add(world, s0, group=b, mode="add", client=admin, dry_run=True, **body)
        add(world, s0, group=b, mode="add", client=admin, **body)
        assert [o.debt_amount for o in live_openings(world)] == [40_000]


def forbid_add(answer):
    assert answer["detail"]["code"] == "OPENING_OWNER_ONLY" and answer["detail"]["message"] == MESSAGE, answer


def membership_of(world, student, group):
    from src.models.auth import StudentGroup

    world.db.expire_all()
    return world.db.query(StudentGroup).filter_by(student_id=student.id, group_id=group.id).first()


def test_g1_a_transfer_of_an_administrator_after_the_launch_carries_the_opening_balance_as_before(world, fixed_today):
    # the SYSTEM roads (the carry of a transfer, the cancellation of a leaving, the closing on leaving) do not ask the rule: the owner's opening of S0 goes with him when an administrator transfers him
    a, s0, admin = world_g1(world, fixed_today, "none")
    b = groups3(world, fixed_today)[1]
    url = f"{API}/org/memberships/{membership(world, s0, a).id}/opening-balance"
    assert owner(world).put(url, json=DEBT).status_code == 200
    settings_block(world, openings_state="done")
    add(world, s0, group=b, mode="transfer", client=admin)
    assert len(live_openings(world)) == 1


# ============================================================================================================ Г1: can_set_opening


@pytest.mark.parametrize("state", STATES)
def test_g1_can_set_opening_in_the_summary_and_the_table(world, fixed_today, state):
    a, s0, admin = world_g1(world, fixed_today, state)
    for client, expected in ((owner(world), True), (admin, not launched(state))):
        summary = client.get(f"{BASE}/summary")
        assert summary.status_code == 200 and summary.json()["can_set_opening"] is expected, summary.text
        table = client.get(BASE)
        assert table.status_code == 200 and table.json()["can_set_opening"] is expected, table.text


def test_g1_without_the_right_to_money_the_summary_is_closed(world, fixed_today):
    from tests.pg_world import make_client

    settings_block(world, openings_state="none")
    plain = make_client(world, user=world.admin, org_role="admin")          # no right to money granted: the whole route answers 403, the field never reaches him
    assert plain.get(f"{BASE}/summary").status_code == 403 and plain.get(BASE).status_code == 403


# ============================================================================================================ Г2: the small things


def set_member_role(world, user, role):
    from src.models.auth import OrganizationMember

    world.db.query(OrganizationMember).filter_by(org_id=world.org.id, user_id=user.id).update({"org_role": role})
    world.db.commit()


def test_g2_who_cancelled_a_payment_is_the_role_at_the_moment_of_the_cancellation_not_the_role_today(world, fixed_today):
    # (a) the owner cancelled, later he is an administrator: the cancellation is still the owner's - an administrator does not restore it (the journal line says «owner»)
    from tests.test_money2_fix_r3_pg import void_it
    from tests.test_money2_fix_r3b_pg import admin_client, world_p4

    a, s0, old, e8, e7 = world_p4(world, fixed_today)
    void_it(world, e7)
    set_member_role(world, world.owner, "admin")
    r = admin_client(world).post(f"{API}/org/payments/{e7['id']}/restore", json={"reason": "Отменили по ошибке"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "RESTORE_OWNER_ONLY", r.text


def test_g2_an_administrator_who_became_the_owner_later_did_not_cancel_as_an_owner(world, fixed_today):
    # (b) an administrator cancelled; he is the owner of the centre today: the cancellation was an administrator's - another administrator may restore it
    from tests.test_money2_fix_r3_pg import void_it
    from tests.test_money2_fix_r3b_pg import admin_client, world_p4

    a, s0, old, e8, e7 = world_p4(world, fixed_today)
    admin = admin_client(world)
    void_it(world, e7, client=admin)
    set_member_role(world, world.admin, "owner")
    r = admin.post(f"{API}/org/payments/{e7['id']}/restore", json={"reason": "Отменили по ошибке"})
    assert r.status_code == 201, r.text


# ============================================================================================================ Г3: the first change of the mode in an empty centre acts from the start


def empty_centre(world):
    """A centre with nobody in any group: no membership (its periods, payments, openings, transfers go with it by the cascade of the database)."""
    from sqlalchemy import text

    world.db.execute(text("TRUNCATE student_groups CASCADE"))
    world.db.commit()


def history_of(world):
    world.db.expire_all()
    return world.org.settings["payments"].get("billing_history")


def test_g3_an_empty_centre_gets_the_new_mode_from_the_start_the_history_is_one_entry(world, fixed_today):
    # a new centre starts with «Общий месяц»; nobody is in a group, nothing is paid: the owner chooses «Личный период» -> the history is ONE entry from the start, no «defer», no entry of the old mode
    settings_block(world, period_mode="calendar", billing_anchor="fixed_day", billing_day=1)
    empty_centre(world)
    c = owner(world)
    dry = c.put(f"{API}/org/settings/payments", params={"dry_run": True}, json={"billing_anchor": "payment_date"})
    assert dry.status_code == 200 and dry.json()["regime_impact"]["students_affected"] == 0, dry.text
    assert history_of(world) is None                                                                  # the trial wrote nothing
    assert c.put(f"{API}/org/settings/payments", json={"billing_anchor": "payment_date"}).status_code == 200
    (entry,) = history_of(world)
    assert entry["from"] == "min" and entry["billing_anchor"] == "payment_date" and entry["period_mode"] == "calendar" and "defer" not in entry
    # and back (still empty): a replacement again, not a second entry
    assert c.put(f"{API}/org/settings/payments", json={"billing_anchor": "fixed_day", "billing_day": 1}).status_code == 200
    (entry,) = history_of(world)
    assert entry["from"] == "min" and entry["billing_anchor"] == "fixed_day" and "defer" not in entry


def test_g3_a_centre_with_a_student_a_payment_or_an_opening_keeps_the_rule_from_today(world, fixed_today):
    # the same change when ANY of the three exists: from today, the old mode is the first entry («the past is not recounted»)
    settings_block(world, period_mode="calendar", billing_anchor="fixed_day", billing_day=1)
    # (a) a student in a group (the world has three)
    assert owner(world).put(f"{API}/org/settings/payments", json={"billing_anchor": "payment_date"}).status_code == 200
    first, last = history_of(world)[0], history_of(world)[-1]
    assert len(history_of(world)) == 2 and first["from"] == "min" and last["from"] == ps.business_today().isoformat() and last["defer"] is True


def test_g3_a_left_student_a_payment_and_an_opening_each_make_the_centre_not_empty(world, fixed_today):
    from src.auth.payments_service import PaymentService
    from tests.money1b_helpers import opening_until
    from tests.test_pay2_api_pg import seed_payment

    service = PaymentService(world.db)
    assert service._centre_is_empty(str(world.org.id)) is False                                        # three students in the group
    m = membership(world, world.students[0])
    m_id, student_id = m.id, m.student_id
    seed_payment(world, m, 100_000, date(2026, 11, 10))
    opening_until(world, m, date(2026, 11, 30))
    from src.models.auth import MembershipOpeningBalance, StudentPayment

    world.db.query(StudentPayment).delete()
    world.db.commit()
    assert service._centre_is_empty(str(world.org.id)) is False                                        # the opening alone
    world.db.query(MembershipOpeningBalance).delete()
    world.db.commit()
    assert service._centre_is_empty(str(world.org.id)) is False                                        # the memberships alone (also closed ones: is_active is not asked)
    empty_centre(world)
    assert service._centre_is_empty(str(world.org.id)) is True


def enrol_with_pay_from(world, before, change, populated):
    """Today Wednesday 21.10.2026 (the clock of the fixture); group on Tuesdays and Thursdays, 300 000 a month; the student S0 is written with «Оплата с» 11.10 (10 days ago).  Returns (debt, entries of the history)."""
    from datetime import timedelta

    from tests.money1b_helpers import add, set_schedule
    from tests.pg_world import give_group_price
    from tests.test_money2_fix_r3_pg import TUE_THU

    settings_block(world, accounting_start_date="2026-10-01", **before)
    set_schedule(world, TUE_THU, world.group)
    give_group_price(world.db, world.group, 300_000, valid_from=date(2026, 1, 1))
    s0, s1 = world.students[0], world.students[1]
    empty_centre(world)
    c = owner(world)
    if populated:
        add(world, s1, group=world.group, mode="add")                                                  # somebody is already in a group before the switch
    assert c.put(f"{API}/org/settings/payments", json=change).status_code == 200
    add(world, s0, group=world.group, mode="add", pay_from=(ps.business_today() - timedelta(days=10)).isoformat())
    debt = c.get(f"{API}/org/students/{s0.id}/memberships").json()[0]["debt"]
    return debt, history_of(world)


COMMON = {"period_mode": "calendar", "billing_anchor": "fixed_day", "billing_day": 1}


def test_g3_numbers_empty_centre_common_month_to_personal_period_charges_the_whole_period_from_the_day(world, fixed_today):
    # «Личный период»: the period 11.10-10.11 is charged whole = 300 000 (the common month never existed in this centre).  One entry of the history, from the start
    debt, history = enrol_with_pay_from(world, COMMON, {"billing_anchor": "payment_date"}, populated=False)
    assert debt == 300_000 and len(history) == 1 and history[0]["from"] == "min" and "defer" not in history[0]


def test_g3_numbers_the_same_with_a_student_already_in_a_group_the_old_rule_from_today(world, fixed_today):
    # the change acts from today (21.10), the student with «Оплата с» 11.10 is before it: the COMMON month is still his: 11.10-31.10 holds 6 lessons (13, 15, 20, 22, 27, 29.10) of the 9 of October
    # (1, 6, 8, 13, 15, 20, 22, 27, 29) -> 300 000 x 6 / 9 = 200 000; the history has two entries (the old mode from the start, the new one from today)
    debt, history = enrol_with_pay_from(world, COMMON, {"billing_anchor": "payment_date"}, populated=True)
    assert debt == 200_000 and [h["from"] for h in history] == ["min", ps.business_today().isoformat()] and history[-1]["defer"] is True


def test_g3_numbers_the_other_direction_an_empty_centre_without_an_explicit_mode_to_the_common_month(world, fixed_today):
    # an old centre without an entry reads as «payment date»; in an empty one the owner chooses «Общий месяц»: it acts from the start -> the share of the month 6 / 9 of 300 000 = 200 000, one entry
    debt, history = enrol_with_pay_from(world, {}, {"billing_anchor": "fixed_day", "billing_day": 1}, populated=False)
    assert debt == 200_000 and len(history) == 1 and history[0]["from"] == "min" and history[0]["billing_anchor"] == "fixed_day" and "defer" not in history[0]


@pytest.mark.parametrize("state", ["none", "done"])
def test_g1_the_file_of_students_reads_the_opening_columns_by_the_state_of_the_launch(world, fixed_today, state):
    from tests.test_logicA_money_pg import centre, import_workbook

    centre(world, 600_000)
    settings_block(world, openings_state=state)
    from tests.test_money2_wave_b_pg import _admin_with_the_right

    admin = _admin_with_the_right(world)
    data = import_workbook([["Шесть", "Ученик", 7, "", "Импорт-группа", "", date(2026, 10, 31), None, None]])
    pv = admin.post(f"{API}/org/import/preview", files={"file": ("x.xlsx", data)}).json()
    if state == "done":
        assert pv["ok"] and pv["counts"]["openings"] == 0 and any("Учёт запущен — остаток" in i["message"] for i in pv["issues"])
        assert owner(world).post(f"{API}/org/import/preview", files={"file": ("x.xlsx", data)}).json()["counts"]["openings"] == 1
    else:
        assert pv["ok"] and pv["counts"]["openings"] == 1
