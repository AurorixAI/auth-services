"""INCOME-2 golden fixtures: the answers of the endpoints for the hand-calculated centre of ``income2_world`` (ids replaced by names).

    INCOME2_WRITE_FIXTURES=1 pytest tests/test_income2_fixtures_pg.py     # writes tests/fixtures/income2/*.json

Without the variable the test compares the live answers with the files (a drift of the API or of a figure fails here)."""
import json
import os
import re
from pathlib import Path

from tests.income2_helpers import income2_world, names_of
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401
from tests.test_pay2_api_pg import API, fixed_today, owner  # noqa: F401

pytestmark = needs_pg
FIX = Path(__file__).resolve().parent / "fixtures" / "income2"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# file -> (model, path, params)
OCT = {"from": "2026-10-01", "to": "2026-10-31"}
CASES = {
    "I01_income_month_october": ("IncomeOverview", "income", OCT),
    "I02_income_day_today_cash": ("IncomeOverview", "income", {"from": "2026-10-21", "to": "2026-10-21"}),
    "I03_income_week": ("IncomeOverview", "income", {"from": "2026-10-12", "to": "2026-10-18"}),
    "I04_income_september_closed": ("IncomeOverview", "income", {"from": "2026-09-01", "to": "2026-09-30"}),
    "I05_income_group_filter": ("IncomeOverview", "income", {**OCT, "group_id": "{group:B}"}),
    "I06_income_day_group_filter": ("IncomeOverview", "income", {"from": "2026-10-21", "to": "2026-10-21", "group_id": "{group:B}"}),
    "J01_journal_page1": ("PaymentsJournal", "journal", {**OCT, "size": 5}),
    "J02_journal_page2": ("PaymentsJournal", "journal", {**OCT, "size": 5, "page": 2}),
    "J03_journal_cash": ("PaymentsJournal", "journal", {**OCT, "method": "cash"}),
    "J04_journal_voided_and_replaced": ("PaymentsJournal", "journal", {**OCT, "status": "voided"}),
    "J05_journal_search": ("PaymentsJournal", "journal", {**OCT, "q": "алиев"}),
    "J06_journal_kind_advance": ("PaymentsJournal", "journal", {**OCT, "kind": "advance"}),
    "J07_journal_kind_current_cash_day": ("PaymentsJournal", "journal", {"from": "2026-10-21", "to": "2026-10-21", "kind": "current", "method": "cash"}),
    "J08_journal_kind_current_page2": ("PaymentsJournal", "journal", {**OCT, "kind": "current", "size": 3, "page": 2}),
    "J09_journal_status_active": ("PaymentsJournal", "journal", {**OCT, "status": "active", "size": 5}),
    "J10_journal_status_cancelled": ("PaymentsJournal", "journal", {**OCT, "status": "cancelled"}),
    "J11_journal_status_replaced": ("PaymentsJournal", "journal", {**OCT, "status": "replaced"}),
}
ERRORS = {
    "RANGE_TOO_LONG": ("income", {"from": "2025-10-01", "to": "2026-10-21"}),
    "INVALID_PERIOD": ("income", {"from": "2026-10-01", "to": "2026-10-02", "period": "day"}),
    "EXPORT_TOO_LONG": ("journal/export", {"from": "2026-10-01", "to": "2026-10-31"}),          # the 20 000-row cap is lowered to 3 for this one call
}


def normalise(node, names):
    if isinstance(node, dict):
        return {k: normalise(v, names) for k, v in node.items()}
    if isinstance(node, list):
        return [normalise(v, names) for v in node]
    if isinstance(node, str):
        node = names.get(node, node)
        assert not UUID_RE.search(node), f"an id without a name: {node}"
    return node


def test_the_endpoints_reproduce_the_golden_fixtures(world, fixed_today, monkeypatch):
    seeded = income2_world(world)
    names = names_of(world, seeded)
    by_name = {v: k for k, v in names.items()}
    c = owner(world)
    write = os.environ.get("INCOME2_WRITE_FIXTURES") == "1"
    index, errors = {}, {}
    for stem, (model, path, params) in CASES.items():
        params = {k: (by_name[v[1:-1]] if isinstance(v, str) and v.startswith("{") else v) for k, v in params.items()}
        r = c.get(f"{API}/org/payments/{path}", params=params)
        assert r.status_code == 200, (stem, r.text)
        got = normalise(r.json(), names)
        index[stem] = model
        target = FIX / f"{stem}.json"
        if write:
            target.write_text(json.dumps(got, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            assert json.loads(target.read_text(encoding="utf-8")) == got, stem
    from src.auth import payments_income

    monkeypatch.setattr(payments_income, "MAX_EXPORT_ROWS", 3)
    for code, (path, params) in ERRORS.items():
        r = c.get(f"{API}/org/payments/{path}", params=params)
        assert r.status_code == 422 and r.json()["detail"]["code"] == code
        errors[code] = {"status": 422, "body": r.json()}
    if write:
        (FIX / "index.json").write_text(json.dumps({"models": index}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (FIX / "errors.json").write_text(json.dumps(errors, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        assert json.loads((FIX / "errors.json").read_text(encoding="utf-8")) == errors
        assert json.loads((FIX / "index.json").read_text(encoding="utf-8"))["models"] == index
