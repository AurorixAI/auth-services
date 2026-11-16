"""Money-2, the fourth (closing) round after the independent check (R3d, 17.11): Д1 - a group with a price by an administrator, one place for the 403 of the money rules (PostgreSQL)."""
from datetime import date

import pytest

from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.ops_helpers import exams  # noqa: F401  (fixture: exam-service stub)
from tests.test_money2_fix_r3_pg import admin_client
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, settings_block, stay  # noqa: F401  (fixture)

pytestmark = needs_pg


# ============================================================================================================ Д1: the first price of a new group has no past


def test_d1_an_administrator_with_the_money_right_creates_a_group_with_a_price(world, fixed_today):
    from src.models.auth import Group, GroupPrice

    admin = admin_client(world)
    r = admin.post(f"{API}/org/groups", json={"name": "С ценой админа", "price": 450_000})
    assert r.status_code == 201, r.text
    world.db.expire_all()
    group = world.db.query(Group).filter_by(org_id=world.org.id, name="С ценой админа").one()
    assert [p.amount for p in world.db.query(GroupPrice).filter_by(group_id=group.id)] == [450_000]


def test_d1_the_owner_still_creates_a_group_with_a_price(world, fixed_today):
    r = owner(world).post(f"{API}/org/groups", json={"name": "С ценой владельца", "price": 300_000})
    assert r.status_code == 201, r.text


# ============================================================================================================ Д1: the refusals of the rules are 403, never 500

BACKDATE, OPENING, RESTORE = "BACKDATE_OWNER_ONLY", "OPENING_OWNER_ONLY", "RESTORE_OWNER_ONLY"
OLD_PAUSE = {"first_day": "2026-11-12", "last_day": "2026-11-14", "reason": "Болезнь"}


def _p4(world, fixed_today):
    from tests.test_money2_fix_r3_pg import world_p4

    a, s0, old, e8, e7 = world_p4(world, fixed_today)
    return a, s0, old, e8, e7, admin_client(world)


def _v4(world, fixed_today):
    from tests.test_money2_fix_r3b_pg import world_v4

    a, s0, admin = world_v4(world, fixed_today)
    return a, s0, admin


def _launched(world, fixed_today):
    from tests.test_money2_fix_r3c_pg import world_g1

    return world_g1(world, fixed_today, "done")


def _old_pause(world, a):
    return owner(world).post(f"{API}/org/memberships/{membership(world, world.students[2], a).id}/pauses", json={"first_day": "2026-11-05", "last_day": "2026-11-08", "reason": "Каникулы"}).json()["pause"]["id"]


# the inventory: every road that changes a price / a pause / an opening balance / a payment and can be refused by the rules of the age, of the launch of the accounting or of the restore
def road_payment_replace(w, ft):
    a, s0, old, e8, e7, admin = _p4(w, ft)
    return BACKDATE, admin.post(f"{API}/org/payments/{old['id']}/replace", json={"amount": 1_000_000, "paid_on": old["paid_on"], "method": "cash", "reason": "Правка"})


def road_payment_replace_preview(w, ft):
    a, s0, old, e8, e7, admin = _p4(w, ft)
    return BACKDATE, admin.post(f"{API}/org/payments/{old['id']}/replace", params={"dry_run": "true"}, json={"amount": 1_000_000, "paid_on": old["paid_on"], "method": "cash", "reason": "Правка"})


def road_payment_void(w, ft):
    a, s0, old, e8, e7, admin = _p4(w, ft)
    return BACKDATE, admin.post(f"{API}/org/payments/{old['id']}/void", json={"reason": "Внесли по ошибке"})


def road_payment_quote_replace(w, ft):
    a, s0, old, e8, e7, admin = _p4(w, ft)
    return BACKDATE, admin.get(f"{API}/org/memberships/{membership(w, s0, a).id}/payments/preview", params={"amount": 1_000_000, "paid_on": old["paid_on"], "replace_payment_id": old["id"]})


def road_payment_restore(w, ft):
    a, s0, old, e8, e7, admin = _p4(w, ft)
    assert owner(w).post(f"{API}/org/payments/{e7['id']}/void", json={"reason": "Внесли по ошибке"}).status_code == 200
    return RESTORE, admin.post(f"{API}/org/payments/{e7['id']}/restore", json={"reason": "Отменили по ошибке"})


def road_payment_restore_preview(w, ft):
    a, s0, old, e8, e7, admin = _p4(w, ft)
    assert owner(w).post(f"{API}/org/payments/{e7['id']}/void", json={"reason": "Внесли по ошибке"}).status_code == 200
    return RESTORE, admin.post(f"{API}/org/payments/{e7['id']}/restore", params={"dry_run": "true"}, json={"reason": "Отменили по ошибке"})


def road_pause_create(w, ft):
    a, s0, admin = _v4(w, ft)
    return BACKDATE, admin.post(f"{API}/org/memberships/{membership(w, s0, a).id}/pauses", json=OLD_PAUSE)


def road_pause_create_preview(w, ft):
    a, s0, admin = _v4(w, ft)
    return BACKDATE, admin.post(f"{API}/org/memberships/{membership(w, s0, a).id}/pauses", params={"dry_run": "true"}, json=OLD_PAUSE)


def road_pause_change(w, ft):
    a, s0, admin = _v4(w, ft)
    return BACKDATE, admin.patch(f"{API}/org/pauses/{_old_pause(w, a)}", json={"last_day": "2026-11-09"})


def road_pause_void(w, ft):
    a, s0, admin = _v4(w, ft)
    return BACKDATE, admin.post(f"{API}/org/pauses/{_old_pause(w, a)}/void", json={"reason": "Ошибка"})


def road_group_price(w, ft):
    a, s0, admin = _v4(w, ft)
    return BACKDATE, admin.put(f"{API}/org/groups/{a.id}/price", json={"amount": 310_000, "valid_from": "2026-11-12"})


def road_group_price_preview(w, ft):
    a, s0, admin = _v4(w, ft)
    return BACKDATE, admin.put(f"{API}/org/groups/{a.id}/price", params={"dry_run": "true"}, json={"amount": 310_000, "valid_from": "2026-11-12"})


def road_price_override(w, ft):
    a, s0, admin = _v4(w, ft)
    return BACKDATE, admin.put(f"{API}/org/memberships/{membership(w, s0, a).id}/price-override", json={"amount": 250_000, "reason": "Скидка", "valid_from": "2026-11-12"})


def road_opening_card_put(w, ft):
    a, s0, admin = _launched(w, ft)
    return OPENING, admin.put(f"{API}/org/memberships/{membership(w, s0, a).id}/opening-balance", json={"debt_amount": 50_000, "debt_on": "2026-11-02"})


def road_opening_card_preview(w, ft):
    a, s0, admin = _launched(w, ft)
    return OPENING, admin.put(f"{API}/org/memberships/{membership(w, s0, a).id}/opening-balance", params={"dry_run": "true"}, json={"debt_amount": 50_000, "debt_on": "2026-11-02"})


def road_opening_void(w, ft):
    from tests.test_opening_bulk_pg import live_openings

    a, s0, admin = _launched(w, ft)
    assert owner(w).put(f"{API}/org/memberships/{membership(w, s0, a).id}/opening-balance", json={"debt_amount": 50_000, "debt_on": "2026-11-02"}).status_code == 200
    return OPENING, admin.post(f"{API}/org/opening-balances/{live_openings(w)[0].id}/void", json={"reason": "Ошибка"})


def road_opening_batch_preview(w, ft):
    from tests.test_opening_bulk_pg import BASE, it

    a, s0, admin = _launched(w, ft)
    items = [it(w, membership(w, s0, a), debt_amount=70_000, debt_on="2026-11-02")]
    return OPENING, admin.post(f"{BASE}/batch/preview", json={"items": items, "reason": None})


def road_opening_import_preview(w, ft):
    from tests.test_opening_bulk_pg import BASE, export_xlsx

    a, s0, admin = _launched(w, ft)
    return OPENING, admin.post(f"{BASE}/import/preview", files={"file": ("ostatki.xlsx", export_xlsx(w).content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})


def road_student_added_with_opening(w, ft):
    from tests.test_money2_transfer_a_pg import groups3

    a, s0, admin = _launched(w, ft)
    b = groups3(w, ft)[1]
    return OPENING, admin.post(f"{API}/org/groups/{b.id}/students/{s0.id}", params={"mode": "add"}, json={"opening_balance": {"debt_amount": 40_000, "debt_on": "2026-11-19"}})


ROADS = [road_payment_replace, road_payment_replace_preview, road_payment_void, road_payment_quote_replace, road_payment_restore, road_payment_restore_preview,
         road_pause_create, road_pause_create_preview, road_pause_change, road_pause_void, road_group_price, road_group_price_preview, road_price_override,
         road_opening_card_put, road_opening_card_preview, road_opening_void, road_opening_batch_preview, road_opening_import_preview, road_student_added_with_opening]


@pytest.mark.parametrize("road", ROADS, ids=lambda f: f.__name__)
def test_d1_every_road_of_the_money_rules_answers_a_refusal_with_403_and_its_code_never_500(world, fixed_today, road):
    code, r = road(world, fixed_today)
    assert r.status_code == 403 and r.json()["detail"]["code"] == code, (road.__name__, r.status_code, r.text[:300])


@pytest.mark.parametrize("name", ["BackdateOwnerOnly", "OpeningOwnerOnly", "RestoreOwnerOnly"])
def test_d1_a_refusal_nobody_caught_is_403_with_its_code_from_the_one_handler(name):
    """The class of the mistake, not the single road: whatever router forgets ``except PaymentError``, the application answers the refusal as 403 with the code."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.auth import payments_service as ps_
    from src.auth.platform_errors import register

    app = FastAPI()
    register(app)

    @app.get("/boom")
    def boom():
        raise getattr(ps_, name)("текст отказа")

    r = TestClient(app).get("/boom")
    assert r.status_code == 403 and r.json()["detail"]["code"] == getattr(ps_, name).code and r.json()["detail"]["message"] == "текст отказа", r.text


def test_d1_the_real_application_has_the_handler():
    from src.api import main_v2
    from src.auth.payments_service import PaymentError

    assert PaymentError in main_v2.app.exception_handlers
