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
