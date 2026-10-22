"""The scenario matrix of logicA_spec §2 is covered: every cell has a marked test, a mapped existing test or a waiver with a reason."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_logicA_matrix_coverage_has_no_open_cell():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_matrix_coverage_a.py")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
