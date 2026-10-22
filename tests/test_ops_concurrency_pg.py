"""LOGIC-B concurrency beyond the per-item tests (those carry their own races: O1.R4 parallel slots, O2.R4 remove vs start, O4.R4 archive vs start, O5.R4 two assignments,
O6.R4 remove-last-student vs start, O7.R4 two saves, O10.R2 two accepts, O9.R4 import lock): an enrolment racing an archive."""
import pytest

from src.models.auth import Group, StudentGroup

from tests.ops_helpers import (  # noqa: F401  (fixtures)
    build_d0, clock, code_of, exams, needs_pg, new_group, new_slot, new_student, pg_session, run_parallel,
)

pytestmark = needs_pg


def test_enrolment_and_archive_of_the_same_group_leave_a_consistent_state(pg_session, clock, exams):
    """[O4.R4] An administrator enrols a student while another archives the (empty) group: either the enrolment is first (the archive is blocked by ACTIVE_STUDENTS) or the archive is
    first (the enrolment is refused under the group lock: GROUP_ARCHIVED).  Never an archived group with an active member (INV-O4-1)."""
    from src.auth.group_lifecycle import GroupLifecycle
    from src.auth.ops_schemas import ArchiveGroupRequest

    clock.set("2026-10-22 10:00")
    w = build_d0(pg_session)
    group = new_group(w.db, w.org, w.ids, "Gr", "Гонка", w.D, [new_slot("lesson-f", 5, "15:00", "16:00", "2026-10-22")])
    guest = new_student(w.db, w.org, w.ids, "guest", "Гость", "Ученик", None, status="new")
    w.db.commit()
    org, gid, sid = str(w.org.id), str(group.id), str(guest.id)
    for _round in range(3):
        results = run_parallel(
            w,
            lambda svc: GroupLifecycle(svc).archive(org, gid, ArchiveGroupRequest(), actor_id=str(w.admin.id), actor_role="admin"),
            lambda svc: svc.add_student_to_group(org, gid, sid, mode="transfer", actor_id=str(w.admin.id)),
        )
        w.db.expire_all()
        status = w.db.get(Group, group.id).status
        members = w.db.query(StudentGroup).filter_by(group_id=group.id, is_active=True).count()
        assert not (status == "archived" and members), results
        if status == "archived":
            assert results[0][1] is None and results[1][1] is not None            # the enrolment lost
            from src.auth.schedule_rules import close_open_interval  # noqa: F401
            GroupLifecycle(w.svc).restore(org, gid, None, actor_id=str(w.admin.id), actor_role="admin")
        else:
            assert code_of(results[0][1]) == "GROUP_ARCHIVE_BLOCKED" and members == 1
            break
