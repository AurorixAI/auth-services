"""Wave E, E-D (SV-21): both readers of a month (the student's summary and the group journal) refuse and read it the same way (PostgreSQL)."""
import pytest

from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)

pytestmark = needs_pg


@pytest.mark.parametrize("bad", ["2026-1", "2026-13", "2026-12-01", "abc"])
def test_the_two_readers_refuse_a_bad_month_with_one_text(world, bad):
    w = world
    with pytest.raises(ValueError) as student_side:
        w.svc.get_student_attendance_summary(str(w.org.id), str(w.students[0].id), month=bad)
    with pytest.raises(ValueError) as group_side:
        w.svc.get_group_attendance(str(w.org.id), str(w.group.id), month=bad, actor_id=str(w.admin.id), is_org_admin=True)
    assert str(student_side.value) == str(group_side.value) == "month должен иметь формат YYYY-MM"


@pytest.mark.parametrize("month,first,last", [("2026-02", "2026-02-01", "2026-02-28"), ("2026-12", "2026-12-01", "2026-12-31")])
def test_the_two_readers_cut_the_same_month(world, month, first, last):
    w = world
    summary = w.svc.get_student_attendance_summary(str(w.org.id), str(w.students[0].id), month=month)
    journal = w.svc.get_group_attendance(str(w.org.id), str(w.group.id), month=month, actor_id=str(w.admin.id), is_org_admin=True)
    assert summary["period"] == {"month": month, "date_from": first, "date_to": last}
    assert journal.month == month
