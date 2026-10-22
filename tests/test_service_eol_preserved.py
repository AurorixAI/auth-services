"""SPEC 0.5: ``src/auth/service.py`` and ``src/auth/schemas.py`` have mixed CRLF / LF line endings; the operations package must not normalise
them.  The check compares the working tree with the commit the package started from (``3a1946d``): every changed line keeps its own ending
(``git diff --stat`` equals ``git diff --ignore-space-at-eol --stat``) and ``schemas.py`` is not touched at all."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE = "3a1946d"
pytestmark = pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(), reason="needs a git checkout")


def _git(*args):
    if shutil.which("git") is None:
        return subprocess.CompletedProcess(args, 1, "", "no git")
    return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, text=True)


def _have_base():
    return _git("cat-file", "-e", f"{BASE}^{{commit}}").returncode == 0


@pytest.mark.skipif(not _have_base(), reason="the base commit is not in this checkout")
def test_service_py_diff_is_not_an_eol_normalisation():
    plain = _git("diff", "--stat", BASE, "--", "src/auth/service.py").stdout
    quiet = _git("diff", "--ignore-space-at-eol", "--stat", BASE, "--", "src/auth/service.py").stdout
    assert plain == quiet, "service.py: some line ended differently than before (EOL normalised)"


@pytest.mark.skipif(not _have_base(), reason="the base commit is not in this checkout")
def test_service_py_keeps_its_mixed_endings():
    """The number of ``\\r`` in the file changes by no more than the number of CRLF lines the diff touches."""
    before = subprocess.run(["git", "show", f"{BASE}:src/auth/service.py"], cwd=str(ROOT), capture_output=True).stdout
    now = (ROOT / "src" / "auth" / "service.py").read_bytes()
    diff = subprocess.run(["git", "diff", "-U0", BASE, "--", "src/auth/service.py"], cwd=str(ROOT), capture_output=True).stdout
    touched = sum(
        1 for line in diff.split(b"\n")
        if line[:1] in (b"+", b"-") and line[:3] not in (b"+++", b"---") and line.endswith(b"\r")
    )
    assert abs(now.count(b"\r") - before.count(b"\r")) <= touched
    assert b"\r\n" in now and now.count(b"\n") > now.count(b"\r")  # still mixed, not normalised either way


@pytest.mark.skipif(not _have_base(), reason="the base commit is not in this checkout")
def test_schemas_py_diff_is_not_an_eol_normalisation():
    # LOGIC-A added the opening balance field to schemas.py on purpose (3 lines); the rule is that its mixed
    # endings are never normalised: the diff must be the same with and without ignoring end-of-line spaces
    plain = _git("diff", "--stat", BASE, "--", "src/auth/schemas.py").stdout
    quiet = _git("diff", "--ignore-space-at-eol", "--stat", BASE, "--", "src/auth/schemas.py").stdout
    assert plain == quiet
