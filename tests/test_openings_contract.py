"""OPENING-BULK frozen contract: the golden fixtures validate against the response models, the generator and the exported OpenAPI subset equal the
files on disk (the front engineer works against them; the live answers are compared with them in tests/test_opening_bulk_pg.py)."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "openings"


def _models():
    from src.auth import openings_schemas

    index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))["models"]
    return {FIXTURES / f"{stem}.json": getattr(openings_schemas, model) for stem, model in index.items()}


def test_every_fixture_has_a_model():
    covered = {p.name for p in _models()}
    skip = {"index.json", "errors.json", "excel.json", "openings_contract.json"}
    assert not ({p.name for p in FIXTURES.glob("*.json")} - covered - skip)
    assert len(covered) == 9


def test_fixtures_validate_and_round_trip():
    for path, model in _models().items():
        data = json.loads(path.read_text(encoding="utf-8"))
        assert model.model_validate(data).model_dump(mode="json", by_alias=True) == data, path.name


def test_error_fixture_bodies():
    from src.auth.openings_schemas import OpeningsError

    for code, item in json.loads((FIXTURES / "errors.json").read_text(encoding="utf-8")).items():
        assert item["body"]["detail"]["code"] == code
        assert OpeningsError.model_validate(item["body"]).detail.code == code


def test_the_generator_and_the_openapi_subset_are_the_frozen_contract():
    for script in ("openings_gen_fixtures.py", "openings_export_contract.py"):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / script), "--check"], capture_output=True, text=True, cwd=str(ROOT))
        assert r.returncode == 0, f"{script}: the files on disk differ from the code: change the spec first, then regenerate"
    data = json.loads((FIXTURES / "openings_contract.json").read_text(encoding="utf-8"))
    assert set(data["paths"]) == {
        "/org/opening-balances", "/org/opening-balances/summary", "/org/opening-balances/batch/preview", "/org/opening-balances/batch",
        "/org/opening-balances/state", "/org/opening-balances/export", "/org/opening-balances/import/preview"}
    params = {p["name"] for p in data["paths"]["/org/opening-balances"]["get"]["parameters"]}
    assert params == {"group_id", "missing_only", "q", "include_left"}
    batch = data["paths"]["/org/opening-balances/batch"]["post"]
    assert {"STALE_ITEMS", "PREVIEW_STALE", "BATCH_INVALID", "REASON_REQUIRED", "IDEMPOTENCY_MISMATCH"} <= {e["code"] for e in batch["x-errors"] if e.get("code")}
    schemas = data["components"]["schemas"]
    assert {"OpeningsList", "OpeningBatchPreview", "OpeningBatchApplied", "OpeningBatchItem", "OpeningsImportPreview"} <= set(schemas)
    assert set(schemas["OpeningBatchItem"]["properties"]) == {"membership_id", "version", "paid_until", "debt_amount", "debt_on", "lessons_left", "clear"}


def test_the_older_contracts_do_not_know_the_new_endpoints():
    for name in ("pay2/pay2_contract.json", "logicA/logicA_contract.json", "income2/income2_contract.json"):
        text = (ROOT / "tests" / "fixtures" / name).read_text(encoding="utf-8")
        assert "/org/opening-balances/batch" not in text and "/org/opening-balances/import" not in text
