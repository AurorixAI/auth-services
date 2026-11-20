"""Guard: the leave / archive preview fixtures of logicA carry real money (a misspelt LeaveMoney field is silently
ignored by the model and left ``unpaid: 0`` - the fixtures once hid 347 000 and 129 000 that way)."""
import json
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures" / "logicA"
FILES = sorted(FIX.glob("A*_leave_*.json")) + sorted(FIX.glob("A*_archive_*.json"))
PREVIEWS = [f for f in FILES if "memberships" in json.loads(f.read_text(encoding="utf-8"))]
EXPECTED_UNPAID = {"A09": 0, "A11": 347_000, "A12": 129_000}


def test_the_leave_previews_are_found():
    assert {f.name[:3] for f in PREVIEWS} == set(EXPECTED_UNPAID)


@pytest.mark.parametrize("path", PREVIEWS, ids=lambda p: p.name[:3])
def test_unpaid_is_the_sum_of_its_named_parts_and_no_old_debt_field(path):
    for item in json.loads(path.read_text(encoding="utf-8"))["memberships"]:
        money = item["money"]
        assert "debt" not in money
        assert money["unpaid"] == money["debt_amount"] + money["due_amount"]
        assert money["unpaid"] == EXPECTED_UNPAID[path.name[:3]]
