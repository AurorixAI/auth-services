#!/usr/bin/env python3
"""Every row of the scenario matrices of the LOGIC-B SPEC must have a test marked ``[<ITEM>.R<n>]`` (e.g. ``[O1.R4]``).

    python scripts/check_matrix_coverage.py                       # check: exit 1 when a testable row has no marker
    python scripts/check_matrix_coverage.py --update-from-spec F  # rewrite tests/fixtures/logicb/matrix_ids.json from the SPEC (logicB_spec.md)

A row whose scenario / expectation is "н/д", "—" or "нет нового" cannot be tested and is listed as waived.  ``D7`` is the decision 7 table.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IDS = ROOT / "tests" / "fixtures" / "logicb" / "matrix_ids.json"
IDS_B2 = ROOT / "tests" / "fixtures" / "logicb2" / "matrix_ids.json"
NA = re.compile(r"^(н/д|—|-|нет нового\.?)$", re.I)
SECTION = re.compile(r"^###\s+(O\d+|Решение 7)\b")
ROW = re.compile(r"^\|\s*(R[1-9])\s*\|(.*)\|\s*$")


def from_spec(path: Path) -> dict:
    out, section = {}, None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = SECTION.match(line)
        if m:
            section = "D7" if m.group(1).startswith("Решение") else m.group(1)
            continue
        if line.startswith("## "):
            section = None
        r = ROW.match(line)
        if r and section:
            cells = [c.strip() for c in r.group(2).split("|")]
            waived = any(NA.match(c) for c in cells if c) and len([c for c in cells if c]) <= 2 and all(NA.match(c) or not c for c in cells[:1] + cells[-1:])
            out[f"{section}.{r.group(1)}"] = {"testable": not waived}
    return out


SECTION_B2 = re.compile(r"^###\s+(S\d)\b")


def from_spec_b2(path: Path) -> dict:
    """LOGIC-B2: the rows ``R1..R9`` under the headings ``### S1 ...`` ``### S9 ...`` -> ``B2.S<n>.R<m>``."""
    out, section = {}, None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = SECTION_B2.match(line)
        if m:
            section = m.group(1)
            continue
        if line.startswith("## ") or line.startswith("### 1.1"):
            section = None
        r = ROW.match(line)
        if r and section:
            out[f"B2.{section}.{r.group(1)}"] = {"testable": True}
    return out


def markers() -> set:
    found = set()
    for path in (ROOT / "tests").rglob("*.py"):
        found.update(a or b for a, b in re.findall(r"\[([A-Z]\d+\.R[1-9])\]|\[(B2\.S\d\.R[1-9])\]", path.read_text(encoding="utf-8")))
    return found


def main() -> int:
    if "--update-from-spec" in sys.argv:
        spec = Path(sys.argv[sys.argv.index("--update-from-spec") + 1])
        if "logicB2" in spec.name:
            data, target = from_spec_b2(spec), IDS_B2
        else:
            data, target = from_spec(spec), IDS
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"{len(data)} rows -> {target}")
        return 0
    data = json.loads(IDS.read_text(encoding="utf-8"))
    if IDS_B2.exists():
        data.update(json.loads(IDS_B2.read_text(encoding="utf-8")))
    have = markers()
    missing = sorted(k for k, v in data.items() if v["testable"] and k not in have)
    waived = sorted(k for k, v in data.items() if not v["testable"])
    print(f"rows: {len(data)}, testable: {len(data) - len(waived)}, waived (n/a): {len(waived)}, missing: {len(missing)}")
    for k in missing:
        print("  missing:", k)
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
