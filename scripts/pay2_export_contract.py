#!/usr/bin/env python3
"""Export the PAY-2 subset of the OpenAPI schema (the frozen contract for the front).

    python scripts/pay2_export_contract.py [out.json]     # default: tests/fixtures/pay2/pay2_contract.json
    python scripts/pay2_export_contract.py --check        # exit 1 when the file on disk differs

Only the payment operations below and the schemas they reference are exported.  ``tests/test_pay2_contract.py`` runs the
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

DEFAULT_OUT = ROOT / "tests" / "fixtures" / "pay2" / "pay2_contract.json"
PREFIX = "/api/v1/auth"

OPERATIONS = [
    ("get", "/org/settings/payments"),
    ("put", "/org/settings/payments"),
    ("get", "/org/payments/summary"),
    ("get", "/org/payments/status"),
    ("get", "/org/students/{student_id}/memberships"),
    ("get", "/org/memberships/{membership_id}/payments"),
    ("get", "/org/memberships/{membership_id}/payments/preview"),
    ("get", "/org/memberships/{membership_id}/payments/quote"),
    ("post", "/org/memberships/{membership_id}/payments"),
    ("post", "/org/payments/{payment_id}/replace"),
    ("post", "/org/payments/{payment_id}/void"),
    ("get", "/org/payments/{payment_id}/receipt"),
    ("post", "/org/memberships/{membership_id}/writeoffs"),
    ("post", "/org/writeoffs/{writeoff_id}/void"),
    ("post", "/org/memberships/{membership_id}/refunds"),
    ("post", "/org/refunds/{refund_id}/void"),
    ("get", "/org/memberships/{membership_id}/statement"),
    ("get", "/org/memberships/{membership_id}/statement/export"),
    ("get", "/org/payments/receipts"),
    ("get", "/org/payments/receipts/items"),
    ("get", "/org/payments/balances"),
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
    from fastapi import FastAPI

    from src.auth.payments_router import router

    app = FastAPI(title="ALGO auth - PAY-2 payments contract", version="pay2-1")
    app.include_router(router, prefix="/api/v1")
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
        "info": {"title": "ALGO auth - PAY-2 payments contract", "version": "pay2-1"},
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
