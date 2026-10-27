#!/usr/bin/env python3
"""Deterministic test-file sharding for CI.

    ci_shard.py SHARD TOTAL            print the test files of shard SHARD (1-based)
    ci_shard.py --check TOTAL          assert every test file lands in exactly one shard
    ci_shard.py --rebuild JUNIT_XML... write .github/ci_test_durations.json from junit reports

Whole files are assigned (never single tests), so the order of tests inside a
file and file-level fixtures behave exactly as in a single full run. Files are
packed greedily, longest first, into the currently lightest shard, using the
measured seconds in .github/ci_test_durations.json. A file missing from the
JSON gets the median known duration (or its line count while the JSON is
absent), so a new test file is never lost - it is picked up by the glob.
"""
import json
import statistics
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DURATIONS = ROOT / ".github" / "ci_test_durations.json"


def test_files():
    found = {p for pat in ("test_*.py", "*_test.py") for p in (ROOT / "tests").rglob(pat)}
    return sorted(str(p.relative_to(ROOT)) for p in found)


def weights(files):
    known = json.loads(DURATIONS.read_text()) if DURATIONS.exists() else {}
    if known:
        default = statistics.median(known.values())
        return {f: float(known.get(f, default)) for f in files}
    return {f: float(len((ROOT / f).read_text().splitlines())) for f in files}


def shards(total):
    files = test_files()
    w = weights(files)
    bins = [[] for _ in range(total)]
    load = [0.0] * total
    for f in sorted(files, key=lambda f: (-w[f], f)):
        i = min(range(total), key=lambda k: (load[k], k))
        bins[i].append(f)
        load[i] += w[f]
    return [sorted(b) for b in bins], load


def rebuild(reports):
    seconds = {}
    for report in reports:
        for case in ET.parse(report).getroot().iter("testcase"):
            parts = case.get("classname", "").split(".")
            for n in range(len(parts), 0, -1):
                path = "/".join(parts[:n]) + ".py"
                if (ROOT / path).is_file():
                    seconds[path] = seconds.get(path, 0.0) + float(case.get("time", 0))
                    break
    DURATIONS.write_text(json.dumps({k: round(v, 1) for k, v in sorted(seconds.items())}, indent=1) + "\n")
    print(f"{len(seconds)} files, {sum(seconds.values()):.0f}s total")


if __name__ == "__main__":
    if sys.argv[1] == "--rebuild":
        rebuild(sys.argv[2:])
    elif sys.argv[1] == "--check":
        total = int(sys.argv[2])
        bins, load = shards(total)
        flat = [f for b in bins for f in b]
        assert sorted(flat) == test_files() and len(flat) == len(set(flat)), "shards do not partition tests"
        print(f"ok: {len(flat)} files in {total} shards; weights " + ", ".join(f"{x:.0f}" for x in load))
    else:
        shard, total = int(sys.argv[1]), int(sys.argv[2])
        print(" ".join(shards(total)[0][shard - 1]))
