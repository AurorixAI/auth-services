"""LOGIC-A frozen contract: the golden fixtures validate against the response models, are reproducible from the spec chains
(scripts/logicA_gen_fixtures.py: an independent hand calculator, every figure asserted against the spec), and the exported
OpenAPI subset is the file the front engineer works against."""
import fnmatch
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "logicA"


def _models():
    from src.auth import payments_schemas, payroll_schemas

    index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
    payroll = set(index["payroll_models"])
    out = {}
    for path in sorted(FIXTURES.glob("A*.json")):
        for pattern, model in index["models"].items():
            if fnmatch.fnmatch(path.stem, pattern):
                out[path] = getattr(payroll_schemas if model in payroll else payments_schemas, model)
    return out


def test_every_fixture_has_a_model():
    covered = {p.name for p in _models()}
    skip = {"index.json", "errors.json", "logicA_contract.json"}
    missing = {p.name for p in FIXTURES.glob("*.json")} - covered - skip
    assert not missing, missing


def test_fixtures_validate_and_round_trip():
    for path, model in _models().items():
        data = json.loads(path.read_text(encoding="utf-8"))
        parsed = model.model_validate(data)
        assert parsed.model_dump(mode="json", by_alias=True) == data, path.name


def test_fixtures_are_reproducible_from_the_spec_chains():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "logicA_gen_fixtures.py"), "--check"],
                       capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 0, r.stdout + r.stderr


def test_error_fixture_bodies():
    from src.auth.payments_schemas import LogicAError

    for code, item in json.loads((FIXTURES / "errors.json").read_text(encoding="utf-8")).items():
        assert item["body"]["detail"]["code"] == code
        if code != "DUPLICATE_PAYOUT":
            assert LogicAError.model_validate(item["body"]).detail.code == code


def test_openapi_subset_is_the_frozen_contract():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "logicA_export_contract.py"), "--check"],
                       capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 0, "logicA_contract.json differs from the API: change the spec first, then regenerate"
    data = json.loads((FIXTURES / "logicA_contract.json").read_text(encoding="utf-8"))
    assert "/org/memberships/{membership_id}/opening-balance" in data["paths"]
    assert {"OpeningSaved", "PauseSaved", "LeavePreview", "PayoutState", "ImpactMoney", "GroupLessonTypeChanged"} <= set(
        data["components"]["schemas"])
