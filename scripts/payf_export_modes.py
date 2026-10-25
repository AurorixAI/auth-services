#!/usr/bin/env python3
"""Export the names of the ways to count a payment (``payments_texts.MODE_NAMES``) as the frozen contract with the front.

    python scripts/payf_export_modes.py            # writes tests/fixtures/payf/mode_names.json
    python scripts/payf_export_modes.py --check    # exit 1 when the file on disk differs

The front keeps a copy of this file (``algo-front/tests/fixtures/payf-mode-names.json``) and a test that ``PAYMENT_MODELS`` says the same words."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("APP_ENV", "testing")

from src.auth import payments_texts as tx  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "payf" / "mode_names.json"


def document() -> str:
    return json.dumps([{"key": key, "title": title, "hint": hint} for key, (title, hint) in tx.MODE_NAMES.items()], ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(0 if OUT.exists() and OUT.read_text(encoding="utf-8") == document() else 1)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(document(), encoding="utf-8")
    print(f"{OUT}")
