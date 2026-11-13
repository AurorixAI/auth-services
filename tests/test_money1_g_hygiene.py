"""Money-1 / Г, Р-14 (R3 М12): imports that have no reason to be lazy stand at the top of the file (a lazy import is for a real cycle only, and says so)."""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "auth"


def _lazy_imports(path: Path, module: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for func in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        for node in ast.walk(func):
            if isinstance(node, ast.ImportFrom) and (node.module == module or any(a.name == module for a in node.names)):
                found.append((func.name, node.lineno))
    return found


def test_the_excuse_service_imports_money_effects_at_the_top():
    assert _lazy_imports(SRC / "attendance_excuses.py", "money_effects") == []         # before: three imports inside methods (review_bulk, _probe, _effects)
