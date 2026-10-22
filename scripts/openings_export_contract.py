#!/usr/bin/env python3
"""Export the OPENING-BULK subset of the OpenAPI schema (the frozen contract of «Остатки на начало» for the front).

    python scripts/openings_export_contract.py [out.json]     # default: tests/fixtures/openings/openings_contract.json
    python scripts/openings_export_contract.py --check        # exit 1 when the file on disk differs

Only the operations below and the schemas they reference are exported.  ``tests/test_openings_contract.py`` runs the ``--check`` so that the API
cannot drift from the file the front engineer works against.
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

DEFAULT_OUT = ROOT / "tests" / "fixtures" / "openings" / "openings_contract.json"
PREFIX = "/api/v1/auth"

OPERATIONS = [
    ("get", "/org/opening-balances"),
    ("get", "/org/opening-balances/summary"),
    ("post", "/org/opening-balances/batch/preview"),
    ("post", "/org/opening-balances/batch"),
    ("post", "/org/opening-balances/state"),
    ("get", "/org/opening-balances/export"),
    ("post", "/org/opening-balances/import/preview"),
]
ACCESS = (403, "", "Нет доступа к оплатам")
FOREIGN = (404, "NOT_FOUND", "Членство не найдено (чужая организация = несуществующее)")
# the codes of the operations with their texts (the front shows ``detail.message`` as the server wrote it)
ERRORS = {
    ("get", "/org/opening-balances"): [ACCESS, (404, "", "Группа не найдена"),
                                       (422, "TOO_MANY_ROWS", "Слишком много учеников — выберите группу")],
    ("get", "/org/opening-balances/summary"): [ACCESS],
    ("post", "/org/opening-balances/batch/preview"): [ACCESS, FOREIGN,
                                                      (422, "TOO_MANY_ITEMS", "Не больше 2000 строк за один раз — сохраните по группам"),
                                                      (422, "EMPTY_BATCH", "Нечего проверять: список строк пуст")],
    ("post", "/org/opening-balances/batch"): [
        ACCESS, FOREIGN,
        (409, "STALE_ITEMS", "Кто-то уже изменил эти строки — обновите таблицу"),
        (409, "PREVIEW_STALE", "Данные изменились после проверки — проверьте ещё раз"),
        (409, "IDEMPOTENCY_MISMATCH", "Этот ключ уже использован для другого пакета"),
        (409, "CONCURRENT_EDIT", "Другой пользователь меняет эти данные — повторите через секунду"),
        (422, "BATCH_INVALID", "В пакете есть строки с ошибкой — ничего не записано"),
        (422, "REASON_REQUIRED", "У учеников уже есть оплаты: укажите причину изменения остатка (от 3 до 500 символов)"),
        (422, "TOO_MANY_ITEMS", "Не больше 2000 строк за один раз — сохраните по группам")],
    ("post", "/org/opening-balances/state"): [ACCESS],
    ("get", "/org/opening-balances/export"): [ACCESS, (404, "", "Группа не найдена"),
                                              (422, "TOO_MANY_ROWS", "Слишком много учеников для файла — выберите группу")],
    ("post", "/org/opening-balances/import/preview"): [
        ACCESS,
        (422, "FILE_TOO_LARGE", "Файл слишком большой (макс. 5 МБ)"),
        (422, "FILE_UNREADABLE", "Не удалось прочитать файл. Загрузите .xlsx, скачанный из ALGO"),
        (422, "SHEET_MISSING", "В файле нет листа «Остатки». Скачайте актуальный файл"),
        (422, "TOO_MANY_ROWS", "В файле больше 5000 строк"),
        (422, "TOO_MANY_CHANGES", "В файле больше 2000 изменений — загрузите файл по группам")],
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
        "info": {"title": "ALGO auth - OPENING-BULK contract", "version": "openings-1"},
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
