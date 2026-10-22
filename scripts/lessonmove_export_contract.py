#!/usr/bin/env python3
"""Export the contract of #124 (the lesson sheet: move a lesson without a request + the precheck of «Отменить урок»).

    python scripts/lessonmove_export_contract.py [out.json]     # default: tests/fixtures/lessonmove/lessonmove_contract.json
    python scripts/lessonmove_export_contract.py --check        # exit 1 when the file on disk differs

Three operations (+ the additive ``movedFrom`` fields, ``MovedFrom``) of ``substitution_router`` and the schemas they reference; ``x-errors`` = the codes with their texts.  The frozen ``logicB2_contract.json`` is not touched.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("SECRET_KEY", "ci-tests-only-secret-at-least-32-characters")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://x:x@localhost/x")  # never connected

DEFAULT_OUT = ROOT / "tests" / "fixtures" / "lessonmove" / "lessonmove_contract.json"
PREFIX = "/api/v1/auth"
OPERATIONS = [
    ("post", "/org/groups/{group_id}/lesson-move"),
    ("get", "/org/groups/{group_id}/lesson-move-check"),
    ("get", "/org/groups/{group_id}/lesson-exam-binding"),
]


def _errors():
    from src.auth import substitution_rules as R

    def known(code):
        status, message = {**R.DIRECT_MOVE_ERRORS, **R.ERROR_CATALOGUE}[code]
        return {"status": status, "code": code, "message": message}

    shared = [
        {"status": 403, "message": "Нет доступа (только владелец и администратор центра)"},
        {"status": 404, "code": "GROUP_NOT_FOUND", "message": "Группа не найдена"},
        {"status": 404, "code": "LESSON_NOT_FOUND", "message": "Урок не найден"},
    ]
    move = [
        *shared, {"status": 409, "code": "GROUP_ARCHIVED", "message": "Группа в архиве: действие недоступно"},
        {"status": 409, "code": "LESSON_CANCELLED", "message": "Урок отменён"},
        known("LESSON_ALREADY_STARTED"), known("LESSON_CHANGED"), known("SUBSTITUTION_ALREADY_DECIDED"), known("GROUP_BUSY"),
        {"status": 409, "code": "TEACHER_BUSY", "message": "У преподавателя в это время другой урок (conflicts, total)"},
        {"status": 409, "code": "LESSON_HAS_LIVE_EXAM", "message": "Нельзя перенести урок с активным экзаменом (текст exam-service)"},
        {"status": 503, "code": "EXAM_SERVICE_UNAVAILABLE", "message": "Не удалось проверить экзамены группы. Повторите попытку."},
        known("REASON_REQUIRED"), known("MOVE_FIELDS_REQUIRED"), known("MOVE_IN_PAST"), known("MOVE_TOO_FAR"), known("MOVE_SAME_SLOT"),
        known("LESSON_HAS_NO_TEACHER"),
        {"status": 422, "code": "REASON_TOO_LONG", "message": "Причина не длиннее 500 символов"},
    ]
    return {("post", "/org/groups/{group_id}/lesson-move"): move, ("get", "/org/groups/{group_id}/lesson-move-check"): [*shared, known("MOVE_FIELDS_REQUIRED")],
            ("get", "/org/groups/{group_id}/lesson-exam-binding"): shared}


def _refs(node, found):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value.startswith("#/components/schemas/"):
                found.add(value.rsplit("/", 1)[1])
            else:
                _refs(value, found)
    elif isinstance(node, list):
        for item in node:
            _refs(item, found)


def contract() -> dict:
    from src.api.main_v2 import app

    full = app.openapi()
    errors = _errors()
    paths: dict = {}
    for method, path in OPERATIONS:
        op = {k: v for k, v in full["paths"][PREFIX + path][method].items() if k != "operationId"}
        op["x-errors"] = errors[(method, path)]
        paths.setdefault(path, {})[method] = op
    needed: set = set()
    _refs(paths, needed)
    schemas = full["components"]["schemas"]
    todo = list(needed)
    while todo:
        inner: set = set()
        _refs(schemas[todo.pop()], inner)
        for n in inner - needed:
            needed.add(n)
            todo.append(n)
    from src.auth.substitution_schemas import MOVED_FROM_ADDITIVE, MovedFrom

    schemas["MovedFrom"] = MovedFrom.model_json_schema()          # the endpoints type ``movedFrom`` as a free object: the shape is documented here
    needed.add("MovedFrom")
    return {"openapi": full["openapi"], "info": {"title": "ALGO auth - lesson sheet moves (#124)", "version": "lessonmove-2"}, "servers": [{"url": PREFIX}],
            "paths": paths, "components": {"schemas": {name: schemas[name] for name in sorted(needed)}}, "x-additive": MOVED_FROM_ADDITIVE}


def main() -> int:
    text = json.dumps(contract(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if "--check" in sys.argv:
        return 0 if DEFAULT_OUT.exists() and DEFAULT_OUT.read_text(encoding="utf-8") == text else 1
    out = Path(next((a for a in sys.argv[1:] if not a.startswith("--")), DEFAULT_OUT))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"{len(text)} bytes -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
