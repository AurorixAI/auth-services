"""LOGIC-B2 frozen contract: the golden fixtures of the chains 1-9 validate against the models and are reproducible by scripts/logicb2_gen_fixtures.py; the exported
OpenAPI subset is the file the front engineer works against.  Drift fails here: change the SPEC first, then regenerate."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "logicb2"
SKIP = {"index.json", "logicb2_contract.json", "matrix_ids.json"}


def _run(script, *args):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / script), *args], capture_output=True, text=True, cwd=str(ROOT))


def test_fixtures_are_reproducible_from_the_spec_chains():
    r = _run("logicb2_gen_fixtures.py", "--check")
    assert r.returncode == 0, r.stdout + r.stderr


def test_every_fixture_has_an_index_entry():
    index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
    assert {p.name for p in FIXTURES.glob("*.json")} - SKIP == set(index)


def test_fixtures_validate_and_round_trip():
    from src.auth import substitution_schemas as S

    index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
    for name, model in index.items():
        content = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        if model != "-":
            assert getattr(S, model).model_validate(content).model_dump(mode="json") == content, name


def test_error_bodies_carry_a_catalogued_code():
    from src.auth import ops_rules, substitution_rules

    known = {**ops_rules.ERROR_CATALOGUE, **substitution_rules.ERROR_CATALOGUE}
    index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
    for name, model in index.items():
        content = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        if model == "-" and isinstance(content, dict) and "body" in content:
            code = content["body"]["detail"]["code"]
            assert code in known and known[code][0] == content["status"], (name, code)


def test_openapi_subset_is_the_frozen_contract():
    r = _run("logicb2_export_contract.py", "--check")
    assert r.returncode == 0, "logicb2_contract.json differs from the API: change the SPEC first, then regenerate"
    data = json.loads((FIXTURES / "logicb2_contract.json").read_text(encoding="utf-8"))
    assert sum(len(v) for v in data["paths"].values()) >= 13 and data["x-errors"] and data["x-additive"]
