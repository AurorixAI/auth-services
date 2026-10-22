"""#124 contract (lesson sheet: move without a request + the precheck of cancel): fixtures validate against the models, the OpenAPI subset is the file the front works against."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "lessonmove"


def test_fixtures_validate_and_error_bodies_carry_a_known_code():
    from src.auth import substitution_rules as R
    from src.auth import substitution_schemas as S

    index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
    assert {p.name for p in FIXTURES.glob("K*.json")} == set(index) and len(index) >= 10
    known = {**R.ERROR_CATALOGUE, **R.DIRECT_MOVE_ERRORS, "TEACHER_BUSY": (409, "")}
    for name, model in index.items():
        content = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        if model != "-":
            assert getattr(S, model).model_validate(content).model_dump(mode="json") == content, name
        else:
            code = content["body"]["detail"]["code"]
            assert code in known and known[code][0] == content["status"], (name, code)


def test_openapi_subset_is_the_frozen_contract():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "lessonmove_export_contract.py"), "--check"], capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 0, "lessonmove_contract.json differs from the API: change the spec first, then regenerate"
    data = json.loads((FIXTURES / "lessonmove_contract.json").read_text(encoding="utf-8"))
    assert set(data["paths"]) == {"/org/groups/{group_id}/lesson-move", "/org/groups/{group_id}/lesson-move-check", "/org/groups/{group_id}/lesson-exam-binding"}
    assert {"LessonMoveRequest", "DecideOut", "MoveCheckOut", "ExamBinding"} <= set(data["components"]["schemas"])
    codes = {e.get("code") for e in data["paths"]["/org/groups/{group_id}/lesson-move"]["post"]["x-errors"]}
    assert {"LESSON_CHANGED", "LESSON_HAS_NO_TEACHER", "SUBSTITUTION_ALREADY_DECIDED", "TEACHER_BUSY", "GROUP_BUSY"} <= codes


def test_the_frozen_b2_contract_does_not_know_the_new_codes():
    text = (ROOT / "tests" / "fixtures" / "logicb2" / "logicb2_contract.json").read_text(encoding="utf-8")
    assert "lesson-move" not in text and "LESSON_CHANGED" not in text
