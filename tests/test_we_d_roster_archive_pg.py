"""Wave E, E-D (SV-13): the roster of a lesson counts an archived student by ONE rule - «was he in the class at that instant».  The current roster used to drop
an archived student by the status, the historical one never looked at the archive: a stale open membership of an old archiving (the invariant of N13 closes the
memberships, an older archiving did not) came back into the snapshot of a lesson reconciled later.  Both builders now share the row shape (``_roster_of``)."""
from datetime import timedelta

from src.auth.student_archive import ARCHIVED, StudentArchiveService
from tests.pg_world import ENROLLED_SINCE, make_student, needs_pg, pg_session, world  # noqa: F401  (fixtures)

pytestmark = needs_pg

ARCHIVED_AT = ENROLLED_SINCE + timedelta(days=10)
BEFORE, AFTER = ENROLLED_SINCE + timedelta(days=5), ENROLLED_SINCE + timedelta(days=20)


def _ids(roster):
    return {row["student_id"] for row in roster}


def _stale_archive(w, student):
    """Archived at ARCHIVED_AT with the membership still open (what an archiving older than N13 left)."""
    student.status, student.archived_at = ARCHIVED, ARCHIVED_AT
    w.db.commit()


def test_a_student_the_archive_held_at_that_instant_is_in_no_snapshot(world):
    w = world
    s1 = w.students[0]
    _stale_archive(w, s1)
    assert str(s1.id) not in _ids(w.svc._active_attendance_roster(w.group.id))        # now
    assert str(s1.id) not in _ids(w.svc._attendance_roster_at(w.group.id, AFTER))     # after the archiving: the same answer (fails on the old code: he came back)
    assert str(s1.id) in _ids(w.svc._attendance_roster_at(w.group.id, BEFORE))        # a lesson held before the archiving keeps him


def test_a_student_archived_after_the_lesson_stays_in_its_journal(world):
    """The real archiving (it closes the memberships): the snapshot of a lesson held before it keeps the student, the current one and a later one do not."""
    w = world
    s1 = w.students[0]
    StudentArchiveService(w.db).archive(str(w.org.id), str(s1.id), actor_id=str(w.owner.id), actor_role="owner")
    w.db.expire_all()
    assert str(s1.id) not in _ids(w.svc._active_attendance_roster(w.group.id))
    assert str(s1.id) in _ids(w.svc._attendance_roster_at(w.group.id, s1.archived_at - timedelta(seconds=1)))   # held before the archiving: in the journal
    assert str(s1.id) not in _ids(w.svc._attendance_roster_at(w.group.id, s1.archived_at + timedelta(seconds=1)))


def test_the_two_snapshots_have_one_row_shape(world):
    w = world
    now_row = w.svc._active_attendance_roster(w.group.id)[0]
    then_row = w.svc._attendance_roster_at(w.group.id, AFTER)[0]
    assert list(now_row) == list(then_row) == ["student_id", "first_name", "last_name", "full_name", "class_level", "display_id"]
