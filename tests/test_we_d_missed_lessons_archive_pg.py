"""Wave E, E-D, R3 В2 (SV-13): «was he in the class at the lesson» is ONE rule everywhere - the snapshot of the roster (``_attendance_roster_at``) and the search for missed
lessons (``group_period_spans`` + ``membership_covers_instant``) both ask the archive the same question.  A student the archive held, with a stale open membership (what an
archiving older than N13 left), gave a «missed lesson» that could not be done: the roster of that day was empty and the teacher was refused."""
from datetime import datetime, timedelta

from src.auth.service import lesson_start_as_naive_utc, membership_covers_instant
from src.auth.student_archive import ARCHIVED
from src.models.auth import Student
from tests.pg_world import ENROLLED_SINCE, business_today, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_attendance_open_actions import _missed_for, _noon, _weekday_group

pytestmark = needs_pg


def _archive_stale(w, group, moment):
    student = w.db.query(Student).filter(Student.group_id == group.id).one()
    student.status, student.archived_at = ARCHIVED, moment
    w.db.commit()


def test_a_student_the_archive_held_gives_no_missed_lesson_after_the_archiving(world):
    today = business_today()
    group = _weekday_group(world, "Старый архив", [(ENROLLED_SINCE, None)])
    archived = datetime(today.year, today.month, today.day, 6, 0) - timedelta(days=10)
    _archive_stale(world, group, archived)
    dates = _missed_for(world, group, _noon(today))
    archived_day = (archived + timedelta(hours=5)).date()
    assert dates and dates[-1] <= archived_day.isoformat()          # on the old code: every weekday up to today (a lesson nobody could mark)
    # the same answer as the snapshot builder for every lesson day of the last two weeks
    for back in range(0, 14):
        day = today - timedelta(days=back)
        start = lesson_start_as_naive_utc(day, 10 * 60)
        assert bool(world.svc._attendance_roster_at(group.id, start)) == (day.isoformat() in dates), day


def test_a_student_archived_after_the_lesson_still_makes_it_a_missed_lesson(world):
    today = business_today()
    group = _weekday_group(world, "Новый архив", [(ENROLLED_SINCE, None)])
    _archive_stale(world, group, datetime(today.year, today.month, today.day, 6, 0) + timedelta(days=1))      # the archiving is still to come
    assert _missed_for(world, group, _noon(today))[-1] == today.isoformat()


def test_a_missing_stamp_is_an_unknown_moment_and_is_not_excluded(world):
    group = _weekday_group(world, "Без штампа", [(ENROLLED_SINCE, None)])
    _archive_stale(world, group, None)
    assert _missed_for(world, group, _noon(business_today()))[-1] == business_today().isoformat()


def test_the_python_twin_asks_the_archive_like_the_sql_rule():
    start = datetime(2026, 10, 1, 5, 0)
    assert membership_covers_instant(None, None, start, None)
    assert membership_covers_instant(None, None, start, start + timedelta(seconds=1))
    assert not membership_covers_instant(None, None, start, start)                 # archived exactly at the start: not there (student_not_archived_at: ``archived_at > instant``)
    assert not membership_covers_instant(None, None, start, start - timedelta(days=1))
