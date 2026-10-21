"""PAY-2 frozen contract: the golden fixtures validate against the response models, are reproducible from the design
examples (E1-E9), and the exported OpenAPI subset is the file the front engineer works against."""
import fnmatch
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "pay2"


def _models():
    from src.auth import payments_schemas

    index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
    out = {}
    for path in sorted(FIXTURES.glob("*.json")):
        stem = path.stem
        for pattern, model in index.items():
            if fnmatch.fnmatch(stem, pattern):
                out[path] = getattr(payments_schemas, model)
    return out


def test_every_fixture_has_a_model():
    covered = {p.name for p in _models()}
    skip = {"index.json", "errors.json", "pay2_contract.json"}
    missing = {p.name for p in FIXTURES.glob("*.json")} - covered - skip
    assert not missing, missing


def test_fixtures_validate_and_round_trip():
    for path, model in _models().items():
        data = json.loads(path.read_text(encoding="utf-8"))
        parsed = model.model_validate(data)
        assert parsed.model_dump(mode="json", by_alias=True) == data, path.name


def test_fixtures_are_reproducible_from_the_design_examples():
    """scripts/pay2_gen_fixtures.py recomputes every number by hand-calculator and asserts the figures of the design."""
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "pay2_gen_fixtures.py"), "--check"],
                       capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 0, r.stdout + r.stderr


def test_error_fixture_bodies():
    from src.auth.payments_schemas import PayError

    for code, item in json.loads((FIXTURES / "errors.json").read_text(encoding="utf-8")).items():
        assert PayError.model_validate(item["body"]).detail.code == code


def test_openapi_subset_is_the_frozen_contract():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "pay2_export_contract.py"), "--check"],
                       capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 0, "pay2_contract.json differs from the API: change the design first, then regenerate"
    data = json.loads((FIXTURES / "pay2_contract.json").read_text(encoding="utf-8"))
    assert "/org/memberships/{membership_id}/payments/preview" in data["paths"]
    assert {"PaymentPreview", "StatementOut", "ReceiptsReport", "WriteoffCreated", "RefundCreated"} <= set(
        data["components"]["schemas"])
