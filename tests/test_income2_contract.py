"""INCOME-2 frozen contract: the golden fixtures validate against the response models, and the exported OpenAPI subset is the file the front
engineer works against (the live answers equal the fixtures in tests/test_income2_fixtures_pg.py)."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "income2"


def _models():
    from src.auth import payments_income_schemas

    index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))["models"]
    return {FIXTURES / f"{stem}.json": getattr(payments_income_schemas, model) for stem, model in index.items()}


def test_every_fixture_has_a_model():
    covered = {p.name for p in _models()}
    skip = {"index.json", "errors.json", "income2_contract.json"}
    assert not ({p.name for p in FIXTURES.glob("*.json")} - covered - skip)
    assert len(covered) >= 10


def test_fixtures_validate_and_round_trip():
    for path, model in _models().items():
        data = json.loads(path.read_text(encoding="utf-8"))
        parsed = model.model_validate(data)
        assert parsed.model_dump(mode="json", by_alias=True) == data, path.name


def test_error_fixture_bodies():
    from src.auth.payments_schemas import PayError

    for code, item in json.loads((FIXTURES / "errors.json").read_text(encoding="utf-8")).items():
        assert item["body"]["detail"]["code"] == code
        assert PayError.model_validate(item["body"]).detail.code == code


def test_openapi_subset_is_the_frozen_contract():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "income2_export_contract.py"), "--check"],
                       capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 0, "income2_contract.json differs from the API: change the spec first, then regenerate"
    data = json.loads((FIXTURES / "income2_contract.json").read_text(encoding="utf-8"))
    assert set(data["paths"]) == {"/org/payments/income", "/org/payments/journal", "/org/payments/journal/export"}
    assert {"IncomeOverview", "PaymentsJournal", "MonthCollection", "JournalRow", "IncomeCash", "CollectionRow"} <= set(data["components"]["schemas"])
    income_op = data["paths"]["/org/payments/income"]["get"]
    assert {p["name"] for p in income_op["parameters"]} == {"from", "to", "period", "method", "group_id", "teacher_id"}
    assert [e["code"] for e in income_op["x-errors"] if e.get("code")] == ["INVALID_PERIOD", "RANGE_TOO_LONG"]


def test_the_older_contracts_do_not_know_the_new_endpoints():
    """The PAY-2 / LOGIC-A subsets stay exactly as they were frozen: INCOME-2 adds endpoints and schemas of its own."""
    for name in ("pay2/pay2_contract.json", "logicA/logicA_contract.json"):
        text = (ROOT / "tests" / "fixtures" / name).read_text(encoding="utf-8")
        assert "/org/payments/income" not in text and "/org/payments/journal" not in text
