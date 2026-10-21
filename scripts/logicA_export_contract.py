#!/usr/bin/env python3
"""Export the LOGIC-A subset of the OpenAPI schema (the frozen contract of pack «деньги» for the front).

    python scripts/logicA_export_contract.py [out.json]     # default: tests/fixtures/logicA/logicA_contract.json
    python scripts/logicA_export_contract.py --check        # exit 1 when the file on disk differs

Only the payment operations below and the schemas they reference are exported.  ``tests/test_logicA_contract.py`` runs the
``--check`` so that the API cannot drift from the file the front engineer works against.
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

DEFAULT_OUT = ROOT / "tests" / "fixtures" / "logicA" / "logicA_contract.json"
PREFIX = "/api/v1/auth"

OPERATIONS = [
    # opening balance / pause / leave / lesson type (logica_router)
    ("get", "/org/memberships/{membership_id}/opening-balance"),
    ("put", "/org/memberships/{membership_id}/opening-balance"),
    ("post", "/org/opening-balances/{opening_id}/void"),
    ("get", "/org/memberships/{membership_id}/pauses"),
    ("post", "/org/memberships/{membership_id}/pauses"),
    ("patch", "/org/pauses/{pause_id}"),
    ("post", "/org/pauses/{pause_id}/void"),
    ("get", "/org/groups/{group_id}/students/{student_id}/leave-preview"),
    ("post", "/org/groups/{group_id}/students/{student_id}/leave"),
    ("get", "/org/students/{student_id}/archive-preview"),
    ("post", "/org/students/{student_id}/archive"),
    ("get", "/org/groups/{group_id}/lesson-types"),
    ("put", "/org/groups/{group_id}/lesson-type"),
    # changed payment endpoints
    ("put", "/org/settings/payments"),
    ("get", "/org/payments/summary"),
    ("get", "/org/payments/status"),
    ("get", "/org/students/{student_id}/memberships"),
    ("put", "/org/groups/{group_id}/price"),
    ("put", "/org/memberships/{membership_id}/price-override"),
    ("delete", "/org/memberships/{membership_id}/price-override/{row_id}"),
    ("post", "/org/groups/{group_id}/students/{student_id}"),
    # payroll
    ("get", "/org/payroll/settings"),
    ("put", "/org/payroll/settings"),
    ("get", "/org/payroll/months/{month}"),
    ("post", "/org/payroll/months/{month}/calculate"),
    ("post", "/org/payroll/sheets"),
    ("get", "/org/payroll/sheets/{sheet_id}"),
    ("patch", "/org/payroll/sheets/{sheet_id}"),
    ("get", "/org/payroll/sheets/{sheet_id}/diff"),
    ("get", "/org/payroll/payouts"),
    ("post", "/org/payroll/sheets/{sheet_id}/payouts"),
    ("post", "/org/payroll/payouts/{payout_id}/void"),
]


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
    from src.api.main_v2 import app  # the whole application (importing the routers one by one trips a circular import)

    full = app.openapi()
    paths: dict = {}
    for method, path in OPERATIONS:
        op = full["paths"][PREFIX + path][method]
        op = {k: v for k, v in op.items() if k != "operationId"}
        paths.setdefault(path, {})[method] = op
    needed: set = set()
    _refs(paths, needed)
    schemas = full["components"]["schemas"]
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
        "info": {"title": "ALGO auth - LOGIC-A contract", "version": "logicA-1"},
        "servers": [{"url": PREFIX}],
        "paths": paths,
        "components": {"schemas": {name: schemas[name] for name in sorted(needed)}},
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
