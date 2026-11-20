"""TIME-TESTS guard (server): which clock stamps a row, and who may touch the switch of the stand clock.

* The tables that the business rules read (money, schedule, attendance, journals, memberships) stamp ``created_at`` / ``updated_at`` / ``joined_at`` / ``changed_at`` / ``occurred_at`` with
  the BUSINESS clock (``business_clock.utc_now``) - never with the clock of the database (``func.now()``) or ``datetime.now`` in a ``default`` / ``onupdate``; a ``server_default`` is only the fallback of a raw insert.
* The real-clock islands (users and sessions, diagnostic invites and results, the platform, support and analytics models) keep the database clock.
* ``set_clock`` is called from nowhere in ``src`` but the clock module itself; the name of the environment is judged by ``is_dev_env()`` only.
"""
import ast
import re
from pathlib import Path

from src.auth import business_clock as bc
from src.models import analytics, auth, platform, support
from src.models.base import Base

SRC = Path(__file__).resolve().parents[1] / "src"
TIME_COLUMNS = {"created_at", "updated_at", "joined_at", "changed_at", "occurred_at", "started_at", "taken_at", "first_seen_at"}
# the real clock on purpose (TIME-TESTS 6.10: sessions, throttling, diagnostic invitations are islands of real time)
AUTH_REAL_CLOCK_CLASSES = {"User", "UserSession", "UserSessionToken", "DiagnosticInvite", "StudentDiagnosticResult"}


def _auth_business_tables():
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if cls.__module__ == auth.__name__ and cls.__name__ not in AUTH_REAL_CLOCK_CLASSES:
            yield cls


def test_business_tables_stamp_their_time_columns_with_the_business_clock():
    checked = 0
    for cls in _auth_business_tables():
        for column in cls.__table__.columns:
            if column.name not in TIME_COLUMNS:
                continue
            checked += 1
            for kind in ("default", "onupdate"):
                holder = getattr(column, kind)
                if holder is None:
                    continue
                arg = getattr(holder, "arg", None)
                assert getattr(arg, "__name__", None) == "utc_now" and arg.__module__ == bc.__name__, f"{cls.__name__}.{column.name}.{kind} is not the business clock: {arg!r}"
            if column.name in {"created_at", "changed_at", "occurred_at"}:
                assert column.default is not None, f"{cls.__name__}.{column.name} has no Python default (the database clock would stamp it)"
    assert checked > 40                                                  # the guard really looked at the money / schedule / journal tables


def test_the_main_tables_are_in_the_first_group():
    names = {c.__name__ for c in _auth_business_tables()}
    for expected in ("GroupAudit", "Group", "Organization", "GroupAttendance", "GroupScheduleOverride", "GroupScheduleOneOffLesson", "OrgDayOff", "StudentGroup", "OrganizationMember",
                     "StudentGroupPeriod", "PayrollSheet", "PayrollPayout", "LessonTeacherAssignment", "LessonSubstitutionRequest", "LessonSubstitutionEvent", "GroupPrice",
                     "MembershipPriceOverride", "StudentPayment", "StudentWriteoff", "StudentRefund", "MembershipTransfer", "OrgMoneyAudit", "MoneyReconcileSnapshot"):
        assert expected in names, expected
    assert not names & AUTH_REAL_CLOCK_CLASSES


def test_the_real_clock_islands_keep_the_database_clock():
    for cls in (auth.User, auth.DiagnosticInvite):
        assert cls.__table__.c.created_at.default is not None and "now" in str(cls.__table__.c.created_at.default.arg).lower()
    for module in (platform, support, analytics):
        for mapper in Base.registry.mappers:
            cls = mapper.class_
            if cls.__module__ == module.__name__:
                for column in cls.__table__.columns:
                    holder = column.default
                    assert holder is None or getattr(getattr(holder, "arg", None), "__module__", "") != bc.__name__, f"{cls.__name__}.{column.name} was moved to the business clock"


def _src_files():
    return [p for p in SRC.rglob("*.py")]


def test_set_clock_is_called_nowhere_in_src_but_its_own_module_and_set_offset_only_by_the_dev_router():
    offenders = []
    for path in _src_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name == "set_clock" and path.name != "business_clock.py":
                offenders.append(f"{path.name}:{node.lineno} set_clock")
            if name == "set_offset" and path.name not in ("business_clock.py", "dev_router.py"):
                offenders.append(f"{path.name}:{node.lineno} set_offset")
    assert not offenders, offenders


def test_the_name_of_the_environment_is_judged_by_one_function():
    """Nobody compares APP_ENV with "development" by hand any more: main_v2, invites, config and the clock use ``startup_guard.is_dev_env``."""
    pattern = re.compile(r"""(APP_ENV[^\n]{0,60}==\s*["']development["'])|(app_env\s*==\s*["']development["'])""")
    offenders = [f"{p.relative_to(SRC)}:{n}" for p in _src_files() if p.name != "startup_guard.py"      # the one definition itself
                 for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if pattern.search(line)]
    assert not offenders, offenders


def test_the_redis_of_the_clock_is_asked_only_by_the_clock_module():
    offenders = [p.name for p in _src_files() if p.name != "business_clock.py" and "algo:test:clock_offset_seconds" in p.read_text(encoding="utf-8")]
    assert not offenders
