#!/usr/bin/env python3
"""Export the LOGIC-B2 ("Замены") subset of the OpenAPI schema (the frozen contract for the front).

    python scripts/logicb2_export_contract.py [out.json]   # default: tests/fixtures/logicb2/logicb2_contract.json
    python scripts/logicb2_export_contract.py --check      # exit 1 when the file on disk differs

The substitution routes with every schema they reference, ``x-errors`` (the B2 error catalogue), ``x-additive`` (the additive fields of existing endpoints).
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

DEFAULT_OUT = ROOT / "tests" / "fixtures" / "logicb2" / "logicb2_contract.json"
PREFIX = "/api/v1/auth"

OPERATIONS = [
    ("get", "/org/substitutions/my-lessons"),
    ("post", "/org/substitutions"),
    ("get", "/org/substitutions/mine"),
    ("get", "/org/substitutions/colleagues"),
    ("post", "/org/substitutions/withdraw"),
    ("post", "/org/substitutions/decide-bulk"),
    ("get", "/org/substitutions/summary"),
    ("get", "/org/substitutions"),
    ("get", "/org/substitutions/{request_id}"),
    ("post", "/org/substitutions/{request_id}/withdraw"),
    ("get", "/org/substitutions/{request_id}/candidates"),
    ("get", "/org/substitutions/{request_id}/move-check"),
    ("post", "/org/substitutions/{request_id}/decide"),
    ("get", "/org/groups/{group_id}/lesson-access"),
]
ERROR_MODELS = {"default": "OpsErrorResponse", "SUBSTITUTION_ALREADY_DECIDED": "SubstitutionAlreadyDecidedDetail"}
EXTRA_MODELS = ["SubstitutionRequestMark", "SubstitutionOutcome", "StudentNotice", "RequestClosed", "LessonRosterStudent", "SubstitutionAlreadyDecidedDetail"]


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

    from src.auth import ops_schemas, substitution_rules, substitution_schemas
    from src.auth.router import router

    app = FastAPI(title="ALGO auth - LOGIC-B2 substitutions contract", version="logicb2-1")
    app.include_router(router, prefix="/api/v1")
    full = app.openapi()
    paths: dict = {}
    for method, path in OPERATIONS:
        op = full["paths"][PREFIX + path][method]
        op = {k: v for k, v in op.items() if k not in ("operationId",)}
        paths.setdefault(path, {})[method] = op
    schemas = dict(full["components"]["schemas"])
    extra = set(EXTRA_MODELS) | {v for v in ERROR_MODELS.values()}
    for name in sorted(extra):
        model = getattr(substitution_schemas, name, None) or getattr(ops_schemas, name)
        js = model.model_json_schema(ref_template="#/components/schemas/{model}")
        for def_name, def_schema in js.pop("$defs", {}).items():
            schemas.setdefault(def_name, def_schema)
        schemas[name] = js
    needed: set = set(extra)
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
        "info": {"title": "ALGO auth - LOGIC-B2 substitutions contract", "version": "logicb2-1"},
        "servers": [{"url": PREFIX}],
        "paths": paths,
        "components": {"schemas": {name: schemas[name] for name in sorted(needed)}},
        "x-errors": {
            code: {"status": status, "message": message, "model": ERROR_MODELS.get(code, ERROR_MODELS["default"])}
            for code, (status, message) in sorted(substitution_rules.ERROR_CATALOGUE.items())
        },
        "x-additive": substitution_schemas.ADDITIVE_FIELDS,
        "x-labels": {"reasons": substitution_schemas.REASON_LABELS, "closedReasons": substitution_schemas.CLOSED_REASON_TEXT},
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
