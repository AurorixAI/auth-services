"""LOGIC-B frozen contract: the golden fixtures validate against the response models, are reproducible from the SPEC
chains (scripts/logicb_gen_fixtures.py recomputes every figure), and the exported OpenAPI subset is the file the front
engineer works against.  Drift of the API from the file fails here: change the SPEC first, then regenerate."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "logicb"


def _run(script, *args):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / script), *args], capture_output=True, text=True,
                          cwd=str(ROOT))


def test_fixtures_are_reproducible_from_the_spec_chains():
    r = _run("logicb_gen_fixtures.py", "--check")
    assert r.returncode == 0, r.stdout + r.stderr


def test_every_fixture_has_an_index_entry():
    index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
    files = {p.name for p in FIXTURES.glob("*.json")} - {"index.json", "logicb_contract.json", "matrix_ids.json"}
    assert files == set(index)


def test_fixtures_validate_and_round_trip():
    from src.auth import ops_schemas

    index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
    for name, model in index.items():
        content = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        if model == "-":
            if isinstance(content, dict) and set(content) == {"status", "body"} and isinstance(content["body"].get("detail"), dict):
                ops_schemas.OpsErrorResponse.model_validate(content["body"])
            continue
        parsed = getattr(ops_schemas, model).model_validate(content)
        exclude_none = model in {"ArchiveImpactOut", "VerifyStudentAsOfOut"}
        assert parsed.model_dump(mode="json", exclude_none=exclude_none) == content, name


def test_error_bodies_carry_a_catalogued_code():
    from src.auth.ops_rules import ERROR_CATALOGUE

    index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
    for name, model in index.items():
        content = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        if model == "-" and isinstance(content, dict) and "body" in content and isinstance(content["body"].get("detail"), dict):
            code = content["body"]["detail"]["code"]
            assert code in ERROR_CATALOGUE, (name, code)
            assert ERROR_CATALOGUE[code][0] == content["status"], (name, code)


def test_openapi_subset_is_the_frozen_contract():
    r = _run("logicb_export_contract.py", "--check")
    assert r.returncode == 0, "logicb_contract.json differs from the API: change the SPEC first, then regenerate"
    data = json.loads((FIXTURES / "logicb_contract.json").read_text(encoding="utf-8"))
    assert sum(len(v) for v in data["paths"].values()) >= 34 and data["x-errors"] and data["x-internal"]


def test_every_testable_row_of_the_scenario_matrices_has_a_marked_test():
    """The SPEC's matrices (``tests/fixtures/logicb/matrix_ids.json``) against the ``[<ITEM>.R<n>]`` markers of the tests (scripts/check_matrix_coverage.py)."""
    r = _run("check_matrix_coverage.py")
    assert r.returncode == 0, r.stdout + r.stderr
