"""Money-2, the closing tail of the independent check (R3e, 17.11): E1 - the journal of the launch records the close by a human also over an automatic ``done``; E2 - the common handler of money refusals logs;
E4 - the fallback text of the journal of the mode in an empty centre."""
from datetime import date

import pytest

from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.ops_helpers import exams  # noqa: F401  (fixture: exam-service stub)
from tests.test_money2_fix_r3_pg import admin_client
from tests.test_money2_fix_r3d_pg import journal_of, world_d2
from tests.test_pay2_api_pg import API, fixed_today, owner  # noqa: F401  (fixture)

pytestmark = needs_pg


def _auto_done(world, fixed_today):
    from tests.test_opening_bulk_pg import apply, it, settings_of

    (a, b, c), admin = world_d2(world, fixed_today)
    apply(world, [it(world, a, paid_until="2026-10-31"), it(world, b, paid_until="2026-11-30"), it(world, c, paid_until="2026-10-31")], client=admin)
    assert settings_of(world)["openings_state"] == "done" and settings_of(world)["openings_state_by"] == "auto"
    return admin


def _state_rows(client):
    return [i for i in journal_of(client) if i["action"] == "openings_state_changed"]


@pytest.mark.parametrize("who", ["owner", "admin"])
def test_e1_a_human_closing_over_the_automatic_done_leaves_a_line_with_the_author(world, fixed_today, who):
    admin = _auto_done(world, fixed_today)
    assert _state_rows(owner(world)) == []                                                   # the automatic done has no line (as before)
    actor = owner(world) if who == "owner" else admin
    assert actor.post(f"{API}/org/opening-balances/state", json={"state": "done"}).status_code == 200
    (row,) = _state_rows(owner(world))
    assert row["actor_id"] == str((world.owner if who == "owner" else world.admin).id), row
    d = row["details"]
    assert d["before"] == "done" and d["after"] == "done" and d["before_by_human"] is False and d["after_by_human"] is True, d
    assert "заполнен автоматически" in row["text"] and row["text"].startswith("Запуск учёта"), row["text"]


def test_e1_the_repeat_of_done_over_a_state_closed_by_a_human_adds_no_line(world, fixed_today):
    (a, b, c), admin = world_d2(world, fixed_today)
    assert owner(world).post(f"{API}/org/opening-balances/state", json={"state": "done"}).status_code == 200
    assert admin.post(f"{API}/org/opening-balances/state", json={"state": "done"}).status_code == 200
    assert len(_state_rows(owner(world))) == 1


def test_e2_the_common_handler_of_money_refusals_writes_a_warning_without_bodies(caplog):
    """A refusal that reached the common handler was not caught by its router: it must be visible in the log (class, code, method, path - no body, no sums)."""
    import logging

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.auth import payments_service as ps_
    from src.auth.platform_errors import register

    app = FastAPI()
    register(app)

    @app.post("/boom")
    def boom():
        raise ps_.OpeningOwnerOnly("текст отказа")

    with caplog.at_level(logging.WARNING):
        r = TestClient(app).post("/boom", json={"amount": 123456789})
    assert r.status_code == 403
    rec = [x for x in caplog.records if x.levelno == logging.WARNING and "OpeningOwnerOnly" in x.getMessage()]
    assert len(rec) == 1, [x.getMessage() for x in caplog.records]
    msg = rec[0].getMessage()
    assert ps_.OpeningOwnerOnly.code in msg and "POST" in msg and "/boom" in msg and "123456789" not in msg and "текст отказа" not in msg, msg


def _fail_the_sums(monkeypatch):
    from src.auth.payments_service import PaymentService

    def boom(self, *a, **kw):
        raise RuntimeError("sums failed")

    monkeypatch.setattr(PaymentService, "_regime_audit", boom)


def test_e4_the_fallback_text_of_an_empty_centre_does_not_mention_the_sums(world, fixed_today, monkeypatch):
    from tests.test_money2_fix_r3c_pg import empty_centre
    from tests.test_money2_fix_r3d_pg import EMPTY_TEXT
    from tests.test_pay2_api_pg import settings_block

    settings_block(world, period_mode="calendar", billing_anchor="fixed_day", billing_day=1)
    empty_centre(world)
    _fail_the_sums(monkeypatch)
    assert owner(world).put(f"{API}/org/settings/payments", json={"billing_anchor": "payment_date"}).status_code == 200
    (row,) = [i for i in journal_of(owner(world)) if i["action"] == "billing_regime_changed"]
    assert row["details"]["sums_available"] is False and row["text"].endswith(EMPTY_TEXT) and "Суммы" not in row["text"], row["text"]


def test_e4_the_fallback_text_of_a_centre_with_students_still_says_so(world, fixed_today, monkeypatch):
    from tests.test_pay2_api_pg import settings_block

    settings_block(world, period_mode="calendar", billing_anchor="fixed_day", billing_day=1)
    _fail_the_sums(monkeypatch)
    assert owner(world).put(f"{API}/org/settings/payments", json={"billing_anchor": "payment_date"}).status_code == 200
    (row,) = [i for i in journal_of(owner(world)) if i["action"] == "billing_regime_changed"]
    assert row["text"].endswith("Суммы по ученикам не посчитаны"), row["text"]
