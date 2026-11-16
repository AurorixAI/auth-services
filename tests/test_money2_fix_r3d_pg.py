"""Money-2, the fourth (closing) round after the independent check (R3d, 17.11): Д1 - a group with a price by an administrator, one place for the 403 of the money rules (PostgreSQL)."""
from datetime import date

from src.auth import payment_status as ps

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


# ============================================================================================================ Д2: «closed» is the human's «Готово», not the automatic one


def world_d2(world, fixed_today, state="pending"):
    """Three students in A since 01.09 with no opening (3 «не заполнено» rows), accounting start 01.11; the launch is ``state``; ``admin`` has the right to money."""
    from tests.test_opening_bulk_pg import centre, enrolled

    centre(world)
    rows = [enrolled(world, date(2026, 9, 1), s) for s in world.students]
    settings_block(world, openings_state=state)
    return rows, admin_client(world)


def summary_of(client):
    r = client.get(f"{API}/org/opening-balances/summary")
    assert r.status_code == 200, r.text
    return r.json()


def test_d2_an_administrator_fills_the_last_row_the_state_is_done_but_the_administrator_still_corrects(world, fixed_today):
    from tests.test_opening_bulk_pg import apply, it, live_openings, settings_of

    (a, b, c), admin = world_d2(world, fixed_today)
    apply(world, [it(world, a, paid_until="2026-10-31"), it(world, b, paid_until="2026-11-30")], client=admin)
    out = apply(world, [it(world, c, paid_until="2026-10-31")], client=admin)
    assert out["state"] == "done" and settings_of(world)["openings_state_by"] == "auto"
    s = summary_of(admin)
    assert s["state"] == "done" and s["can_set_opening"] is True and s["launch_closed"] is False
    assert owner(world).get(f"{API}/org/opening-balances/summary").json()["launch_closed"] is False
    # the typo of the last row is corrected by the same administrator: the card and the table
    url = f"{API}/org/memberships/{c.id}/opening-balance"
    r = admin.put(url, json={"paid_until": "2026-11-15"})
    assert r.status_code == 200, r.text
    assert apply(world, [it(world, c, paid_until="2026-10-31")], client=admin)["changed"] == 1
    assert len(live_openings(world)) == 3


@pytest.mark.parametrize("state", ["done", "skipped"])
def test_d2_the_owner_pressed_the_button_the_administrator_is_refused(world, fixed_today, state):
    from tests.test_opening_bulk_pg import apply, it, live_openings

    (a, b, c), admin = world_d2(world, fixed_today)
    assert owner(world).post(f"{API}/org/opening-balances/state", json={"state": state}).status_code == 200
    s = summary_of(admin)
    assert s["state"] == state and s["can_set_opening"] is False and s["launch_closed"] is True
    assert owner(world).get(f"{API}/org/opening-balances/summary").json()["can_set_opening"] is True
    r = admin.put(f"{API}/org/memberships/{a.id}/opening-balance", json={"paid_until": "2026-10-31"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "OPENING_OWNER_ONLY", r.text
    assert live_openings(world) == []
    apply(world, [it(world, a, paid_until="2026-10-31")], client=admin, expect=403)


def test_d2_an_administrator_who_pressed_the_button_closes_it_too(world, fixed_today):
    (a, b, c), admin = world_d2(world, fixed_today)
    assert admin.post(f"{API}/org/opening-balances/state", json={"state": "done"}).status_code == 200
    s = summary_of(admin)
    assert s["launch_closed"] is True and s["can_set_opening"] is False


def test_d2_the_repeat_of_a_batch_after_the_owner_pressed_done_gets_the_saved_answer_not_403(world, fixed_today):
    import uuid

    from tests.test_opening_bulk_pg import BASE, it, preview, settings_of

    (a, b, c), admin = world_d2(world, fixed_today)
    items = [it(world, a, paid_until="2026-10-31"), it(world, b, paid_until="2026-11-30"), it(world, c, paid_until="2026-10-31")]
    body = {"items": items, "reason": None, "idempotency_key": str(uuid.uuid4()), "digest": preview(world, items, client=admin)["digest"]}
    first = admin.post(f"{BASE}/batch", json=body)
    assert first.status_code == 200 and first.json()["replayed"] is False, first.text
    assert owner(world).post(f"{BASE}/state", json={"state": "done"}).status_code == 200
    assert settings_of(world)["openings_state_by"] != "auto"
    again = admin.post(f"{BASE}/batch", json=body)                              # the network broke after the answer: the same body, the same key
    assert again.status_code == 200 and again.json()["replayed"] is True and again.json()["batch_id"] == first.json()["batch_id"], again.text
    other = admin.post(f"{BASE}/batch", json={**body, "idempotency_key": str(uuid.uuid4())})      # a NEW key is a new write: refused
    assert other.status_code == 403 and other.json()["detail"]["code"] == "OPENING_OWNER_ONLY", other.text


# ============================================================================================================ Д3: the owner may take the launch back (pending)


def journal_of(client):
    r = client.get(f"{API}/org/settings/payments/history")
    assert r.status_code == 200, r.text
    return r.json()["items"]


def test_d3_the_owner_returns_the_launch_and_the_administrator_puts_the_openings_again(world, fixed_today):
    (a, b, c), admin = world_d2(world, fixed_today)
    assert owner(world).post(f"{API}/org/opening-balances/state", json={"state": "skipped"}).status_code == 200
    assert admin.put(f"{API}/org/memberships/{a.id}/opening-balance", json={"paid_until": "2026-10-31"}).status_code == 403
    r = owner(world).post(f"{API}/org/opening-balances/state", json={"state": "pending"})
    assert r.status_code == 200 and r.json()["state"] == "pending" and r.json()["launch_closed"] is False, r.text
    assert admin.get(f"{API}/org/opening-balances/summary").json()["can_set_opening"] is True
    assert admin.put(f"{API}/org/memberships/{a.id}/opening-balance", json={"paid_until": "2026-10-31"}).status_code == 200


def test_d3_an_administrator_cannot_return_the_launch(world, fixed_today):
    (a, b, c), admin = world_d2(world, fixed_today)
    assert owner(world).post(f"{API}/org/opening-balances/state", json={"state": "done"}).status_code == 200
    r = admin.post(f"{API}/org/opening-balances/state", json={"state": "pending"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "OPENING_OWNER_ONLY" and r.json()["detail"]["message"] == "Вернуть запуск учёта может владелец", r.text
    assert summary_of(admin)["state"] == "done"


@pytest.mark.parametrize("bad", ["none", "closed", "", "PENDING"])
def test_d3_other_words_are_still_422(world, fixed_today, bad):
    (a, b, c), admin = world_d2(world, fixed_today)
    assert owner(world).post(f"{API}/org/opening-balances/state", json={"state": bad}).status_code == 422


def test_d3_the_journal_of_the_settings_names_all_three_transitions(world, fixed_today):
    (a, b, c), admin = world_d2(world, fixed_today)
    assert admin.post(f"{API}/org/opening-balances/state", json={"state": "done"}).status_code == 200
    assert owner(world).post(f"{API}/org/opening-balances/state", json={"state": "pending"}).status_code == 200
    assert owner(world).post(f"{API}/org/opening-balances/state", json={"state": "skipped"}).status_code == 200
    rows = [i for i in journal_of(owner(world)) if i["action"] == "openings_state_changed"]
    assert [i["details"]["after"] for i in rows] == ["skipped", "pending", "done"]                       # the newest first
    assert [i["details"]["before"] for i in rows] == ["pending", "done", "pending"]
    assert all(i["actor_id"] and i["text"].startswith("Запуск учёта") for i in rows)
    assert rows[1]["details"]["before"] == "done" and "возвращён" in rows[1]["text"]


@pytest.mark.parametrize("state,by_owner,role,closed,can_reopen", [
    ("pending", False, "owner", False, False),
    ("done", False, "owner", True, True),
    ("skipped", False, "owner", True, True),
    ("done", False, "admin", True, False),
    ("pending", False, "admin", False, False),
])
def test_d3_can_reopen_launch_by_role_and_state(world, fixed_today, state, by_owner, role, closed, can_reopen):
    (a, b, c), admin = world_d2(world, fixed_today)
    if state != "pending":
        assert owner(world).post(f"{API}/org/opening-balances/state", json={"state": state}).status_code == 200
    client = owner(world) if role == "owner" else admin
    for url in (f"{API}/org/opening-balances/summary", f"{API}/org/opening-balances"):
        d = client.get(url).json()
        assert d["launch_closed"] is closed and d["can_reopen_launch"] is can_reopen, (url, d["launch_closed"], d["can_reopen_launch"])


def test_d3_the_answer_of_the_state_carries_can_reopen_launch(world, fixed_today):
    (a, b, c), admin = world_d2(world, fixed_today)
    d = owner(world).post(f"{API}/org/opening-balances/state", json={"state": "done"}).json()
    assert d["launch_closed"] is True and d["can_reopen_launch"] is True and d["can_set_opening"] is True
    d = owner(world).post(f"{API}/org/opening-balances/state", json={"state": "pending"}).json()
    assert d["launch_closed"] is False and d["can_reopen_launch"] is False


# ============================================================================================================ Д4: the journal of the settings of an EMPTY centre tells the truth

EMPTY_TEXT = "В центре ещё нет учеников и оплат — способ оплаты действует с начала учёта"


def test_d4_an_empty_centre_the_journal_and_the_trial_say_from_the_start(world, fixed_today):
    from tests.test_money2_fix_r3c_pg import empty_centre

    settings_block(world, period_mode="calendar", billing_anchor="fixed_day", billing_day=1)
    empty_centre(world)
    c = owner(world)
    dry = c.put(f"{API}/org/settings/payments", params={"dry_run": True}, json={"billing_anchor": "payment_date"})
    assert dry.status_code == 200 and dry.json()["regime_impact"]["text"] == EMPTY_TEXT, dry.text
    assert c.put(f"{API}/org/settings/payments", json={"billing_anchor": "payment_date"}).status_code == 200
    (row,) = [i for i in journal_of(c) if i["action"] == "billing_regime_changed"]
    assert row["details"]["from"] == "min" and row["text"].endswith(EMPTY_TEXT) and row["text"].count(EMPTY_TEXT) == 1 and "следующего периода" not in row["text"], row


def test_d4_a_centre_with_students_keeps_the_old_words_from_today(world, fixed_today):
    settings_block(world, period_mode="calendar", billing_anchor="fixed_day", billing_day=1)
    r = owner(world).put(f"{API}/org/settings/payments", json={"billing_anchor": "payment_date"})
    assert r.status_code == 200
    (row,) = [i for i in journal_of(owner(world)) if i["action"] == "billing_regime_changed"]
    assert row["details"]["from"] == ps.business_today().isoformat() and "с начала его следующего периода" in row["text"] and EMPTY_TEXT not in row["text"]


# ============================================================================================================ Д5: the small things


def test_d5_the_import_after_the_closed_launch_warns_with_the_text_of_the_rule(world, fixed_today):
    from tests.test_logicA_money_pg import centre, import_workbook

    centre(world, 600_000)
    settings_block(world, openings_state="done")
    admin = admin_client(world)
    data = import_workbook([["Шесть", "Ученик", 7, "", "Импорт-группа", "", date(2026, 10, 31), None, None]])
    pv = admin.post(f"{API}/org/import/preview", files={"file": ("x.xlsx", data)}).json()
    messages = [i["message"] for i in pv["issues"]]
    assert pv["ok"] and pv["counts"]["openings"] == 0
    assert any(m.startswith("Учёт запущен — остаток на начало вносит и меняет владелец") for m in messages) and not any("Нет прав вносить остатки" in m for m in messages), messages


def test_d5_the_put_of_an_opening_asks_the_rule_once(world, fixed_today, monkeypatch):
    from src.auth.logica_service import OpeningService

    (a, b, c), admin = world_d2(world, fixed_today)
    calls = []
    real = OpeningService.check_opening_allowed
    monkeypatch.setattr(OpeningService, "check_opening_allowed", lambda self, *args, **kw: (calls.append(1), real(self, *args, **kw))[1])
    assert admin.put(f"{API}/org/memberships/{a.id}/opening-balance", json={"paid_until": "2026-10-31"}).status_code == 200
    assert len(calls) == 1
