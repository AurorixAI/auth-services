#!/usr/bin/env python3
"""Export the INCOME-2 subset of the OpenAPI schema (the frozen contract of «Поступления» v2 for the front).

    python scripts/income2_export_contract.py [out.json]     # default: tests/fixtures/income2/income2_contract.json
    python scripts/income2_export_contract.py --check        # exit 1 when the file on disk differs

Only the three operations below and the schemas they reference are exported.  ``tests/test_income2_contract.py`` runs the ``--check`` so that
the API cannot drift from the file the front engineer works against.
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

DEFAULT_OUT = ROOT / "tests" / "fixtures" / "income2" / "income2_contract.json"
PREFIX = "/api/v1/auth"

OPERATIONS = [
    ("get", "/org/payments/income"),
    ("get", "/org/payments/journal"),
    ("get", "/org/payments/journal/export"),
]
ACCESS = (403, "", "Нет доступа к оплатам")
PERIOD = [
    (422, "INVALID_PERIOD", "Конец периода раньше начала | День — это один день | Неделя — это семь дней | "
                            "Месяц начинается с первого числа и не выходит за свой месяц"),
    (422, "RANGE_TOO_LONG", "Период не длиннее 366 дней"),
]
# the codes of the operations with their texts (the front shows ``detail.message`` as the server wrote it)
ERRORS = {
    ("get", "/org/payments/income"): [ACCESS, *PERIOD],
    ("get", "/org/payments/journal"): [ACCESS, *PERIOD],
    ("get", "/org/payments/journal/export"): [ACCESS, *PERIOD,
                                              (422, "EXPORT_TOO_LONG", "Слишком много оплат для файла — сузьте период или фильтры")],
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
    from src.api.main_v2 import app  # the whole application (importing the routers one by one trips a circular import)

    full = app.openapi()
    paths: dict = {}
    for method, path in OPERATIONS:
        op = full["paths"][PREFIX + path][method]
        op = {k: v for k, v in op.items() if k != "operationId"}
        op["x-errors"] = [{"status": status, **({"code": code} if code else {}), "message": message}
                          for status, code, message in ERRORS[(method, path)]]
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
        "info": {"title": "ALGO auth - INCOME-2 contract", "version": "income2-1"},
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
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"{len(text)} bytes -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
