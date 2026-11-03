"""E7 / DU-03: the fallback cut-offs of auth-service (``diagnostic_level.py``) equal the one table of the level cut-offs, a copy of
diagnostic-service's ``tests/fixtures/level_thresholds_golden.json`` (diagnostic-service decides the level; auth only labels old rows by theta)."""
import json
import pathlib

import pytest

from src.auth.diagnostic_level import classify_theta, theta_to_category, theta_to_status

GOLDEN = json.loads((pathlib.Path(__file__).parent / "fixtures" / "level_thresholds_golden.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=[str(c["theta"]) for c in GOLDEN["cases"]])
def test_every_case_of_the_table(case):
    assert theta_to_category(case["theta"]) == case["category"]
    assert theta_to_status(case["theta"]) == case["status"]
    assert classify_theta(case["theta"]) == (case["category"], case["status"])
