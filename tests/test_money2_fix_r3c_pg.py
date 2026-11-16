"""Money-2, the third round after the independent check (R3c, 17.11): Г1 - ONE rule for the opening balance (PostgreSQL), Г2 - the small things.

Г1: while the launch of the accounting of the centre is not finished (``payments.openings_state`` = none / pending) the owner and an administrator with the right to money put, change and cancel
an opening balance (no check of the age); when it is finished (done / skipped) - the owner only (403 ``OPENING_OWNER_ONLY``).  Every road of the opening balance asks the same function."""
from datetime import date

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
