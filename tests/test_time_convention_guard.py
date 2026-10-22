"""TIME_CONVENTIONS guard (LOGIC-B O13): ``group_attendance.started_at / completed_at`` are TASHKENT wall time, ``created_at`` / ``updated_at``,
``joined_at`` / ``left_at`` and ``group_audit.created_at`` are UTC.  Arithmetic or a comparison that mixes the two groups is forbidden in
``src/auth``; the only sanctioned way is ``business_clock.attendance_started_utc / attendance_completed_utc`` (that module is the exception)."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ATTENDANCE_NAMES = {"GroupAttendance", "att", "attendance", "row", "record", "existing", "lesson_row", "active_lesson", "a"}
TASHKENT = {"started_at", "completed_at"}
UTC_ATTRS = {"created_at", "updated_at", "joined_at", "left_at", "utcnow", "utc_now"}
EXEMPT = {"business_clock.py"}


def _is_tashkent_attendance(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr in TASHKENT:
            base = sub.value
            name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", None)
            if name in ATTENDANCE_NAMES:
                return True
    return False


def _is_utc(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr in UTC_ATTRS:
            return True
        if isinstance(sub, ast.Name) and sub.id in UTC_ATTRS:
            return True
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr in {"now", "utcnow"}:
            owner = ast.unparse(sub.func.value)
            if owner in {"func", "datetime"} and (owner == "func" or "timezone.utc" in ast.unparse(sub)):
                return True
    return False


def violations(source: str, filename: str = "<src>"):
    found = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        sides = []
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Sub, ast.Add)):
            sides = [node.left, node.right]
        elif isinstance(node, ast.Compare):
            sides = [node.left, *node.comparators]
        if len(sides) < 2:
            continue
        if any(_is_tashkent_attendance(s) for s in sides) and any(_is_utc(s) for s in sides):
            found.append(f"{filename}:{node.lineno}: {ast.unparse(node)[:120]}")
    return found


def test_the_guard_catches_a_mixed_comparison():
    assert violations("x = att.completed_at - att.created_at")
    assert violations("x = GroupAttendance.started_at >= func.now()")
    assert violations("x = row.started_at < joined_at")
    assert violations("x = attendance.completed_at > membership.left_at")
    assert not violations("x = att.completed_at - att.started_at")                       # both Tashkent
    assert not violations("x = att.created_at - membership.joined_at")                   # both UTC
    assert not violations("x = business_clock.attendance_started_utc(att) >= membership.joined_at")


def test_no_module_mixes_the_two_time_groups():
    bad = []
    for path in sorted((ROOT / "src" / "auth").rglob("*.py")):
        if path.name in EXEMPT:
            continue
        bad += violations(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)))
    assert not bad, "Tashkent and UTC columns mixed (use src/auth/business_clock.py):\n" + "\n".join(bad)
