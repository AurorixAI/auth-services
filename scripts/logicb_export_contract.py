#!/usr/bin/env python3
"""Export the LOGIC-B subset of the OpenAPI schema (the frozen contract for the front).

    python scripts/logicb_export_contract.py [out.json]    # default: tests/fixtures/logicb/logicb_contract.json
    python scripts/logicb_export_contract.py --check       # exit 1 when the file on disk differs

New routes and the changed old ones, with every schema they reference, plus the internal exam-service bodies
(``x-internal``).  ``tests/test_logicb_contract.py`` runs ``--check``: the API cannot drift from the file the front
engineer works against.
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

DEFAULT_OUT = ROOT / "tests" / "fixtures" / "logicb" / "logicb_contract.json"
PREFIX = "/api/v1/auth"

OPERATIONS = [
    # schedule (O1, O5, O11)
    ("post", "/org/schedule/lessons"),
    ("post", "/org/schedule/occurrences"),
    ("get", "/org/schedule"),
    ("get", "/org/schedule/occurrences"),
    ("get", "/org/teacher/dashboard/summary"),
    # journal and marks (O6, O7, O8, decision 7)
    ("get", "/org/attendance/open-actions"),
    ("get", "/org/groups/{group_id}/attendance"),
    ("post", "/org/groups/{group_id}/attendance"),
    ("post", "/org/groups/{group_id}/attendance/start"),
    ("post", "/org/groups/{group_id}/attendance/reconcile"),
    ("post", "/org/groups/{group_id}/attendance/reconcile/cancel"),
    # lesson teacher (O5)
    ("put", "/org/groups/{group_id}/lesson-teacher"),
    ("get", "/org/groups/{group_id}/lesson-teacher-candidates"),
    # group archive (O4)
    ("get", "/org/groups/{group_id}/archive-impact"),
    ("post", "/org/groups/{group_id}/archive"),
    ("post", "/org/groups/{group_id}/restore"),
    ("put", "/org/groups/{group_id}"),
    ("get", "/org/groups"),
    ("post", "/org/groups"),
    # staff (O2)
    ("get", "/org/members"),
    ("get", "/org/members/{member_id}/removal-impact"),
    ("post", "/org/members/{member_id}/handover"),
    ("delete", "/org/members/{member_id}"),
    ("post", "/org/members/{member_id}/restore"),
    # invitations (O10)
    ("post", "/org/invitations"),
    ("get", "/org/invitations"),
    ("get", "/invite/{code}"),
    ("post", "/invite/accept"),
    # import (O9)
    ("post", "/org/import/preview"),
    ("post", "/org/import/commit"),
    # students (O12, O15)
    ("post", "/org/students"),
    ("get", "/org/students/{student_id}"),
    ("put", "/org/students/{student_id}"),
    ("get", "/org/stats"),
]

# Internal bodies (exam-service <-> auth), not routes of this app: exported as plain JSON schemas.
INTERNAL = {
    "POST /internal/lesson-bindings/completion-check": ("LessonBindingRequest", "CompletionCheckOut"),
    "POST /internal/lesson-bindings/completed": ("LessonBindingRequest", "CompletedOut"),
    "POST /internal/group-bindings/archive-impact": ("GroupBindingRequest", "GroupArchiveImpactOut"),
    "POST /internal/group-bindings/archive-release": ("GroupBindingRequest", "GroupArchiveReleaseOut"),
    "GET /public/groups/{group_id}/verify-student?as_of=": (None, "VerifyStudentAsOfOut"),
}

ERROR_MODELS = {
    "default": "OpsErrorResponse",
    "TEACHER_BUSY": "TeacherBusyDetail",
    "ATTENDANCE_CONFLICT": "AttendanceConflictDetail",
    "LESSON_HAS_LIVE_EXAM": "LiveExamDetail",
    "GROUP_ARCHIVE_BLOCKED": "GroupArchiveBlockedDetail",
    "TEACHER_INACTIVE": "TeacherInactiveDetail",
    "LESSON_TEACHER_CHANGED": "LessonTeacherChangedDetail",
    "MEMBER_HAS_ASSIGNMENTS": "MemberHasAssignmentsDetail",
}


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
    import src.api.internal_router  # noqa: F401  (import order: avoids a pre-existing router cycle)
    from fastapi import FastAPI

    from src.auth import ops_schemas, ops_rules
    from src.auth.router import router

    app = FastAPI(title="ALGO auth - LOGIC-B operations contract", version="logicb-1")
    app.include_router(router, prefix="/api/v1")
    full = app.openapi()
    paths: dict = {}
    for method, path in OPERATIONS:
        op = full["paths"][PREFIX + path][method]
        op = {k: v for k, v in op.items() if k not in ("operationId",)}
        paths.setdefault(path, {})[method] = op
    # the error bodies and the internal bodies are not reachable from a route: pull them from the models
    extra_models = set(ERROR_MODELS.values())
    for req, resp in INTERNAL.values():
        extra_models.update(x for x in (req, resp) if x)
    schemas = dict(full["components"]["schemas"])
    for name in sorted(extra_models):
        model = getattr(ops_schemas, name)
        js = model.model_json_schema(ref_template="#/components/schemas/{model}")
        for def_name, def_schema in js.pop("$defs", {}).items():
            schemas.setdefault(def_name, def_schema)
        schemas[name] = js
    needed: set = set(extra_models)
    _refs(paths, needed)
    todo = list(needed)
    while todo:
        name = todo.pop()
        inner: set = set()
        _refs(schemas[name], inner)
        for n in inner - needed:
            needed.add(n)
            todo.append(n)
    return {
        "openapi": full["openapi"],
        "info": {"title": "ALGO auth - LOGIC-B operations contract", "version": "logicb-1"},
        "servers": [{"url": PREFIX}],
        "paths": paths,
        "components": {"schemas": {name: schemas[name] for name in sorted(needed)}},
        "x-errors": {
            code: {"status": status, "message": message, "model": ERROR_MODELS.get(code, ERROR_MODELS["default"])}
            for code, (status, message) in sorted(ops_rules.ERROR_CATALOGUE.items())
        },
        "x-internal": {
            key: {"request": req, "response": resp} for key, (req, resp) in INTERNAL.items()
        },
    }


def dumps(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    text = dumps(contract())
    if "--check" in sys.argv:
        return 0 if DEFAULT_OUT.exists() and DEFAULT_OUT.read_text(encoding="utf-8") == text else 1
    out = Path(next((a for a in sys.argv[1:] if not a.startswith("--")), DEFAULT_OUT))
    out.write_text(text, encoding="utf-8")
    print(f"{len(text)} bytes -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
