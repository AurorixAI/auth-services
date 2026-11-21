"""PAY-TAILS: the ANSWERS of package Б for the screens (``tests/fixtures/paytails/*.json``): the real answers of the routes of the product on worked-out worlds, with the ids and the moments made stable.  The files are
the contract the front builds its stubs on; this test fails when a file differs from what the server says (write them again with ``PAYTAILS_FIXTURES_WRITE=1`` after a deliberate change)."""
import json
import os
import re
from datetime import date
from pathlib import Path

from tests.money1b_helpers import add, fresh_student, set_schedule, slot
from tests.pg_world import ensure_billing, give_group_price, make_client, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, settings_block  # noqa: F401  (fixtures)
from tests.test_pay_tails_advance_pg import build as advance_world
from tests.test_pay_tails_golden_pg import get
from tests.test_pay_tails_money_pg import TTF, forgive, leave, lp, om, pay
from tests.test_pay_tails_second_group_pg import om_pair
from tests.test_pay_tails_upcoming_pg import world_om

pytestmark = needs_pg
D = date
ROOT = Path(__file__).resolve().parent / "fixtures" / "paytails"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MOMENT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
SKIP = {"created_at", "updated_at", "issued_at", "voided_at", "recorded_at"}
DISPLAY = re.compile(r"^[A-Z]+\d*-(\d+)$")


def stable(node, seen=None):
    """The same answer with every uuid replaced by ``id-N`` (in order of appearance) and every moment by a fixed one."""
    seen = {} if seen is None else seen
    if isinstance(node, dict):
        return {k: ("2026-11-21T07:00:00" if k in SKIP and isinstance(v, str) else f"AL-{DISPLAY.match(v).group(1)}" if k == "display_id" and isinstance(v, str) and DISPLAY.match(v) else stable(v, seen)) for k, v in node.items()}
    if isinstance(node, list):
        return [stable(x, seen) for x in node]
    if isinstance(node, str) and UUID.match(node):
        return seen.setdefault(node, f"id-{len(seen) + 1}")
    if isinstance(node, str) and MOMENT.match(node):
        return "2026-11-21T07:00:00"
    return node


def check(name: str, answer):
    path = ROOT / f"{name}.json"
    got = json.loads(json.dumps(stable(answer), ensure_ascii=False, sort_keys=True))
    if os.environ.get("PAYTAILS_FIXTURES_WRITE") == "1":
        ROOT.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(got, ensure_ascii=False, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == got, name


def test_the_advance_tab_with_the_left_ones_and_the_group_band(world, fixed_today):
    g, n = advance_world(world, fixed_today)
    check("advance_tab_all", get(world, "/org/payments/status", bucket="advance", limit=50))
    check("advance_tab_left_only", get(world, "/org/payments/status", bucket="advance", limit=50, left_only="true"))
    check("group_left_open", get(world, "/org/payments/status", bucket="left_open", group_id=str(g.id), limit=50))
    check("summary_with_advance", get(world, "/org/payments/summary"))


def test_the_leaving_and_the_archive_windows_with_recalc_and_others(world, fixed_today):
    a, b, s = om_pair(world, fixed_today)
    r = owner(world).get(f"{API}/org/groups/{a.id}/students/{s.id}/leave-preview")
    check("leave_preview_recalc_others", r.json())
    r = owner(world).get(f"{API}/org/students/{s.id}/archive-preview")
    check("archive_preview_recalc", r.json())
    r = owner(world).post(f"{API}/org/groups/{a.id}/students/{s.id}/leave", json={})
    check("leave_result_recalc_others", r.json())


def test_the_leaving_of_a_student_who_paid_ahead_and_was_forgiven(world, fixed_today):
    g, s = om(world, fixed_today)
    pay(world, s, g, amount=600_000, paid_on=D(2026, 11, 3))
    pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 3))
    fixed_today(D(2026, 11, 11))
    check("leave_preview_paid_ahead", owner(world).get(f"{API}/org/groups/{g.id}/students/{s.id}/leave-preview").json())


def test_the_transfer_and_the_adding_with_a_forecast(world, fixed_today):
    a, b, s = om_pair(world, fixed_today)
    t = fresh_student(world, "Перевод")
    fixed_today(D(2026, 11, 2))
    add(world, t, group=a, mode="add")
    fixed_today(D(2026, 11, 20))
    r = owner(world).post(f"{API}/org/groups/{b.id}/students/{t.id}", params={"mode": "transfer", "dry_run": "true"}, json={"from_group_id": str(a.id)})
    check("transfer_probe_others", r.json())


def test_the_adding_with_a_forecast_single_and_bulk(world, fixed_today):
    g = world_om(world, fixed_today)
    one, two = fresh_student(world, "Один"), fresh_student(world, "Два")
    check("enrol_upcoming_probe", add(world, one, group=g, mode="add", dry_run=True))
    r = owner(world).post(f"{API}/org/groups/{g.id}/students/bulk", params={"mode": "add", "dry_run": "true"}, json={"student_ids": [str(one.id), str(two.id)]})
    check("enrol_upcoming_bulk_probe", r.json())
    add(world, one, group=g, mode="add")
    check("statement_forecast", get(world, f"/org/memberships/{membership(world, one, g).id}/statement"))
    check("status_row_starts", get(world, "/org/payments/status", bucket="active", limit=50))


def test_the_words_of_a_state_and_the_receipts(world, fixed_today):
    g, s = lp(world, fixed_today)
    t = fresh_student(world, "Прощён")
    u = fresh_student(world, "Закрыт")
    for x in (t, u):
        add(world, x, group=g, mode="add")
    done = pay(world, s, g, amount=420_000, paid_on=D(2026, 11, 1))
    forgive(world, t, g, 420_000)
    check("status_words_paid_and_forgiven", get(world, "/org/payments/status", bucket="active", limit=50))
    part = pay(world, u, g, amount=100_000, paid_on=D(2026, 11, 1))
    pid = lambda made: made["payment"]["id"] if "payment" in made else made["id"]
    check("receipt_closed_period", owner(world).get(f"{API}/org/payments/{pid(done)}/receipt").json())
    check("receipt_partial_payment", owner(world).get(f"{API}/org/payments/{pid(part)}/receipt").json())
    fixed_today(D(2026, 11, 21))
    forgive(world, u, g, 220_000)
    leave(world, g, u)
    check("receipt_closed_membership", owner(world).get(f"{API}/org/payments/{pid(part)}/receipt").json())
    check("statement_settled_with_used_forgiveness", get(world, f"/org/memberships/{membership(world, u, g).id}/statement"))
    check("statement_forgiven_until", get(world, f"/org/memberships/{membership(world, t, g).id}/statement"))
    fixed_today(D(2026, 11, 22))
    check("status_debtors_closed", get(world, "/org/payments/status", bucket="debtors", limit=50))


def test_a_membership_that_never_charged_anything(world, fixed_today):
    g, s = lp(world, fixed_today, accounting="2026-12-01")
    fixed_today(D(2026, 11, 5))
    leave(world, g, s)
    check("statement_no_charges", get(world, f"/org/memberships/{membership(world, s, g).id}/statement"))
