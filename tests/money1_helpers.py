"""Money-1 pack A: shared helpers (a frozen business clock, saved payroll sheets, a completed lesson)."""
from datetime import date, datetime

import pytest

from src.auth import business_clock as bc
from src.models.auth import PayrollSheet

from tests.pg_world import save


@pytest.fixture
def now_4_nov():
    """Wednesday 04.11.2026 12:00 Tashkent; the clock of the business package is frozen (``bc.set_clock``)."""
    moment = datetime(2026, 11, 4, 12, 0, tzinfo=bc.BUSINESS_TIMEZONE)
    bc.set_clock(lambda: moment)
    yield moment
    bc.set_clock(None)


def sheet(w, teacher, month: date, saved_at_utc: datetime, total: int = 100_000) -> PayrollSheet:
    """A saved payroll sheet whose last save happened at ``saved_at_utc`` (naive UTC, like ``updated_at``)."""
    row = PayrollSheet(org_id=w.org.id, teacher_id=teacher.id, period_month=month.replace(day=1), source_fingerprint="x", total_amount=total, created_by=w.owner.id,
                       updated_by=w.owner.id, created_at=saved_at_utc, updated_at=saved_at_utc, lines=[], percent_inputs={}, divisor_default=12)
    w.db.add(row)
    w.db.commit()
    return row


def tashkent(y, m, d, hh=12, mm=0) -> datetime:
    """A Tashkent wall time as the naive UTC instant the database stores."""
    return bc.business_wall_to_utc(datetime(y, m, d, hh, mm))


def lesson(day: date):
    return day, f"s{day.isoweekday()}"


def hold_lesson(w, day: date, marks=("present", "present", "present")):
    """Reconcile (open the journal of a past lesson) and complete it; returns ``(day, lesson_id)``."""
    from src.auth.schemas import StartAttendanceRequest

    day, lid = lesson(day)
    w.svc.reconcile_missed_group_attendance(str(w.org.id), str(w.group.id), StartAttendanceRequest(lessonDate=day, lessonId=lid), reconciled_by=str(w.admin.id), is_org_admin=True)
    save(w, day, lid, list(zip(w.students, marks)), status="completed")
    return day, lid


def reopen_now(db, org_id, teacher_id, month: str, actor_id, reason: str):
    """«Переоткрыть месяц» the way the screen does: with the revision of the sheet it saw (R3 #11)."""
    from datetime import date as _date

    from src.auth import closed_period
    from src.models.auth import PayrollSheet

    first = _date(int(month[:4]), int(month[5:7]), 1)
    revision = db.query(PayrollSheet.revision).filter(PayrollSheet.org_id == org_id, PayrollSheet.teacher_id == teacher_id, PayrollSheet.period_month == first).scalar()
    return closed_period.reopen_month(db, org_id, teacher_id, month, actor_id, reason, revision)


def db_fingerprint(db) -> dict:
    """``{table: (rows, md5 of every row as text)}`` of EVERY table of the schema: «in the database nothing was written» is one comparison of two of these, not a list of the tables somebody thought of
    (Money-1 round after the stage walk, D-4: a refusal that left a row behind is a defect too)."""
    from sqlalchemy import text

    from src.models.auth import Organization

    db.flush()
    out = {}
    for table in Organization.metadata.sorted_tables:
        name = f'"{table.name}"'
        rows, digest = db.execute(text(f"SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) FROM {name} t")).one()
        out[table.name] = (rows, digest)
    return out


def changed_tables(before: dict, after: dict) -> list:
    """The names of the tables whose rows differ between two ``db_fingerprint`` results (empty = nothing written)."""
    return sorted(name for name in before if before[name] != after.get(name))
