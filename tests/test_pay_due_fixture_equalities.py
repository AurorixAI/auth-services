"""PAY-DUE 17.1 / 17.12: the equalities of the statement (10.11) hold in EVERY statement of the frozen fixtures (PAY-2, logicA, PAY-F, income2), not in six scenarios only."""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent / "fixtures"


def statements():
    out = []
    for path in sorted(ROOT.glob("*/*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("rows"), list) and isinstance(data.get("summary"), dict) and "open_parts" in json.dumps(data.get("rows", [])[:1] or [{}]):
            out.append(pytest.param(data, id=f"{path.parent.name}/{path.name}"))
    return out


CASES = statements()


def test_the_fixtures_hold_statements():
    assert len(CASES) >= 3


@pytest.mark.parametrize("st", CASES)
def test_the_open_sums_of_the_rows_are_the_cards_and_the_money_balances(st):
    s = st["summary"]
    sums = {"due": 0, "debt": 0, "advance": 0, "paid": 0, "forgiven": 0}
    for row in st["rows"]:
        for part in row["open_parts"]:
            sums[part["name"]] += part["amount"]
        assert (row.get("open_amount") or 0) == sum(p["amount"] for p in row["open_parts"])
    assert (sums["due"], sums["debt"], sums["advance"]) == (s["due_amount"], s["debt_amount"], s["advance"])
    assert s["unpaid"] == s["debt_amount"] + s["due_amount"]
    assert s["charged"] - s["paid"] - s["transferred_in"] - s["written_off"] - s["recalculated"] + s["refunded"] + s["transferred_out"] == s["debt_amount"] + s["due_amount"] - s["advance"]
