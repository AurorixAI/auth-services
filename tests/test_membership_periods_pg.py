"""ROADMAP #59: a student who returns to a group keeps the time he spent there before (PostgreSQL).

``student_groups`` stays ONE row per student and group (the payments hang on it); ``student_group_periods`` keeps
every stay.  The row's ``joined_at`` / ``left_at`` are the current / last stay, so the code that only knows them
keeps working; "was he a member at instant X" asks the periods.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event

from src.auth import membership_periods as mp
from src.auth import service as service_module
from src.auth import student_archive as archive_module
from src.auth.membership_periods import check_periods, close_period, membership_period_violations, open_period
from src.auth.service import lesson_start_as_naive_utc
from src.models.auth import Student, StudentGroup, StudentGroupPeriod, StudentPayment

from tests.pg_world import (  # noqa: F401  (fixtures)
    business_today, make_client, make_group, needs_pg, pg_session, world,
)
from tests.test_attendance_open_actions import _missed_for, _noon, _weekday_group

pytestmark = needs_pg

API = "/api/v1/auth"
STUDENTS = f"{API}/org/students"
D = datetime


# ---- helpers --------------------------------------------------------------------------------------------


class Clock:
    """The service reads the time through ``datetime.now(...)``; the story needs September, October, November."""

    def __init__(self):
        self.now = D(2026, 9, 1, 12, 0)  # naive UTC

    def at(self, month, day, hour=12):
        self.now = D(2026, month, day, hour, 0)
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()

    class FakeDatetime(D):
        @classmethod
        def now(cls, tz=None):
            base = c.now.replace(tzinfo=timezone.utc)
            return base.astimezone(tz) if tz is not None else c.now

    for module in (service_module, archive_module, mp):
        monkeypatch.setattr(module, "datetime", FakeDatetime)
    return c


def newcomer(w, name="Возвращенец", group=None):
    """A student with NO membership yet (``group`` is only where he will be put)."""
    student = Student(org_id=w.org.id, first_name=name, last_name="Периодов", class_level=9, status="new")
    w.db.add(student)
    w.db.commit()
    return student


def add(w, group, student, mode="add"):
    result = w.svc.add_student_to_group(
        str(w.org.id), str(group.id), str(student.id), mode=mode, actor_id=str(w.owner.id)
    )
    w.db.expire_all()
    return result


def remove(w, group, student):
    w.svc.remove_student_from_group(str(w.org.id), str(group.id), str(student.id))
    w.db.expire_all()


def membership(w, student, group):
    w.db.expire_all()
    return w.db.query(StudentGroup).filter_by(student_id=student.id, group_id=group.id).one()


def stays(w, student, group):
    """[(joined_at, left_at), ...] oldest first, straight from the table."""
    m = membership(w, student, group)
    rows = (
        w.db.query(StudentGroupPeriod)
        .filter_by(membership_id=m.id)
        .order_by(StudentGroupPeriod.joined_at)
        .all()
    )
    return [(p.joined_at, p.left_at) for p in rows]


def world_membership_ids(w):
    return [
        row.id
        for row in w.db.query(StudentGroup.id)
        .join(Student, Student.id == StudentGroup.student_id)
        .filter(Student.org_id.in_([w.org.id, w.other_org.id]))
        .all()
    ]


def invariants(w):
    """Every invariant of every membership of the world (no period missing, no overlap, one open iff active ...)."""
    w.db.expire_all()
    check_periods(w.db, world_membership_ids(w))


def roster_ids(w, group, instant):
    return {r["student_id"] for r in w.svc._attendance_roster_at(group.id, instant)}


@pytest.fixture
def two(world):
    world.second = make_group(world.db, world.org, world.teacher2, "Вторая")
    world.db.commit()
    return world


# ---- the story: join Sep -> leave Sep 30 -> return Nov 1 ------------------------------------------------


def test_join_leave_return_gives_two_periods_on_the_same_membership_row(world, clock):
    s = newcomer(world)
    clock.at(9, 1)
    add(world, world.group, s)
    first = membership(world, s, world.group)
    assert stays(world, s, world.group) == [(D(2026, 9, 1, 12), None)]
    assert first.joined_at == D(2026, 9, 1, 12) and first.is_active is True
    invariants(world)

    clock.at(9, 30)
    remove(world, world.group, s)
    assert stays(world, s, world.group) == [(D(2026, 9, 1, 12), D(2026, 9, 30, 12))]
    left = membership(world, s, world.group)
    assert left.is_active is False and left.left_at == D(2026, 9, 30, 12) and left.joined_at == D(2026, 9, 1, 12)
    invariants(world)

    clock.at(11, 1)
    add(world, world.group, s)
    back = membership(world, s, world.group)
    assert back.id == first.id                                   # ONE row per student and group
    assert world.db.query(StudentGroup).filter_by(student_id=s.id, group_id=world.group.id).count() == 1
    assert stays(world, s, world.group) == [
        (D(2026, 9, 1, 12), D(2026, 9, 30, 12)),
        (D(2026, 11, 1, 12), None),
    ]
    # the columns of the row keep their meaning: the CURRENT stay
    assert back.joined_at == D(2026, 11, 1, 12) and back.left_at is None and back.is_active is True
    invariants(world)


def test_a_roster_at_a_lesson_start_follows_the_periods(world, clock):
    s = newcomer(world)
    clock.at(9, 1)
    add(world, world.group, s)
    clock.at(9, 30)
    remove(world, world.group, s)
    clock.at(11, 1)
    add(world, world.group, s)
    sid = str(s.id)

    september = lesson_start_as_naive_utc(D(2026, 9, 15).date(), 10 * 60)   # 10:00 Tashkent
    october = lesson_start_as_naive_utc(D(2026, 10, 15).date(), 10 * 60)
    november = lesson_start_as_naive_utc(D(2026, 11, 15).date(), 10 * 60)
    assert sid in roster_ids(world, world.group, september)
    assert sid not in roster_ids(world, world.group, october)               # the gap
    assert sid in roster_ids(world, world.group, november)
    # the edges: left_at excludes (the lesson that starts at that very instant is not his), joined_at includes
    assert sid in roster_ids(world, world.group, D(2026, 9, 30, 11, 59, 59))
    assert sid not in roster_ids(world, world.group, D(2026, 9, 30, 12, 0))
    assert sid not in roster_ids(world, world.group, D(2026, 11, 1, 11, 59, 59))
    assert sid in roster_ids(world, world.group, D(2026, 11, 1, 12, 0))
    # before the first day he was never there
    assert sid not in roster_ids(world, world.group, D(2026, 8, 31))
    # the other students of the group (joined 60 days before the REAL now) are not affected
    others = {str(x.id) for x in world.students}
    assert others <= roster_ids(world, world.group, datetime.utcnow())


def test_the_missed_lesson_pass_counts_every_stay(world):
    """A group whose only student was away for a while: a never-started lesson of his EARLIER stay is a missed one."""
    today = business_today()
    joined = D.combine(today - timedelta(days=25), D.min.time())
    group = _weekday_group(world, "С перерывом", [(joined, None)])
    m = world.db.query(StudentGroup).filter_by(group_id=group.id).one()
    close_period(m, D.combine(today - timedelta(days=15), D.min.time()))
    open_period(m, D.combine(today - timedelta(days=3), D.min.time()))
    world.db.commit()
    invariants(world)

    dates = _missed_for(world, group, _noon(today))
    assert (today - timedelta(days=20)).isoformat() in dates          # inside the first stay
    assert (today - timedelta(days=10)).isoformat() not in dates      # the gap: nobody was there
    assert (today - timedelta(days=1)).isoformat() in dates           # the second stay


# ---- payments stay on the membership row ----------------------------------------------------------------


def test_payments_stay_on_the_same_membership_row_across_a_return(world, clock):
    s = newcomer(world)
    clock.at(9, 1)
    add(world, world.group, s)
    m = membership(world, s, world.group)
    pay = StudentPayment(
        org_id=world.org.id, membership_id=m.id, student_id=s.id, group_id=world.group.id, amount=450000,
        paid_on=D(2026, 9, 1).date(), valid_from=D(2026, 9, 1).date(), valid_to=D(2026, 9, 30).date(),
        created_by=world.owner.id,
    )
    world.db.add(pay)
    world.db.commit()
    pay_id = pay.id
    clock.at(9, 30)
    remove(world, world.group, s)
    clock.at(11, 1)
    add(world, world.group, s)
    world.db.expire_all()
    kept = world.db.get(StudentPayment, pay_id)
    assert kept.membership_id == m.id == membership(world, s, world.group).id
    assert world.db.query(StudentGroupPeriod).filter_by(membership_id=m.id).count() == 2
    invariants(world)


# ---- archive and restore --------------------------------------------------------------------------------


def archive(w, student, expect=200):
    r = make_client(w, user=w.admin).post(f"{STUDENTS}/{student.id}/archive", json={"reason": "Переехал"})
    assert r.status_code == expect, r.text
    w.db.expire_all()
    return r.json()


def restore(w, student, group_ids=None):
    r = make_client(w, user=w.admin).post(
        f"{STUDENTS}/{student.id}/restore", json=None if group_ids is None else {"group_ids": [str(g) for g in group_ids]}
    )
    assert r.status_code == 200, r.text
    w.db.expire_all()
    return r.json()


def test_archive_closes_the_period_and_restore_opens_a_new_one_in_the_chosen_groups_only(two, clock):
    s = newcomer(two)
    clock.at(9, 1)
    add(two, two.group, s)
    add(two, two.second, s)
    clock.at(10, 5)
    archive(two, s)
    for group in (two.group, two.second):
        assert stays(two, s, group) == [(D(2026, 9, 1, 12), D(2026, 10, 5, 12))]
        m = membership(two, s, group)
        assert m.is_active is False and m.left_at == D(2026, 10, 5, 12)
    invariants(two)

    clock.at(11, 3)
    body = restore(two, s, [two.group.id])                       # back into ONE of the two groups
    assert [g["group_id"] for g in body["restored_groups"]] == [str(two.group.id)]
    assert stays(two, s, two.group) == [(D(2026, 9, 1, 12), D(2026, 10, 5, 12)), (D(2026, 11, 3, 12), None)]
    assert stays(two, s, two.second) == [(D(2026, 9, 1, 12), D(2026, 10, 5, 12))]   # not chosen: stays closed
    invariants(two)

    # a second round: the third stay in the first group
    clock.at(12, 1)
    archive(two, s)
    clock.at(12, 20)
    restore(two, s, [two.group.id, two.second.id])
    assert stays(two, s, two.group) == [
        (D(2026, 9, 1, 12), D(2026, 10, 5, 12)),
        (D(2026, 11, 3, 12), D(2026, 12, 1, 12)),
        (D(2026, 12, 20, 12), None),
    ]
    assert len(stays(two, s, two.second)) == 2
    invariants(two)


def test_restore_without_groups_opens_nothing(two, clock):
    s = newcomer(two)
    clock.at(9, 1)
    add(two, two.group, s)
    clock.at(10, 5)
    archive(two, s)
    clock.at(11, 3)
    restore(two, s)
    assert stays(two, s, two.group) == [(D(2026, 9, 1, 12), D(2026, 10, 5, 12))]
    assert membership(two, s, two.group).is_active is False
    invariants(two)


# ---- transfers ------------------------------------------------------------------------------------------


def test_transfer_a_to_b_and_back_keeps_every_stay(two, clock):
    s = newcomer(two)
    clock.at(9, 1)
    add(two, two.group, s)                                       # A
    clock.at(10, 1)
    add(two, two.second, s, mode="transfer")                     # A -> B
    assert stays(two, s, two.group) == [(D(2026, 9, 1, 12), D(2026, 10, 1, 12))]
    assert stays(two, s, two.second) == [(D(2026, 10, 1, 12), None)]
    invariants(two)
    clock.at(11, 1)
    add(two, two.group, s, mode="transfer")                      # B -> A
    assert stays(two, s, two.group) == [(D(2026, 9, 1, 12), D(2026, 10, 1, 12)), (D(2026, 11, 1, 12), None)]
    assert stays(two, s, two.second) == [(D(2026, 10, 1, 12), D(2026, 11, 1, 12))]
    invariants(two)
    # the rosters: A in September and November, B in October only
    a_sep, b_oct = D(2026, 9, 15, 5), D(2026, 10, 15, 5)
    assert str(s.id) in roster_ids(two, two.group, a_sep) and str(s.id) not in roster_ids(two, two.group, b_oct)
    assert str(s.id) in roster_ids(two, two.second, b_oct) and str(s.id) not in roster_ids(two, two.second, a_sep)
    assert str(s.id) in roster_ids(two, two.group, D(2026, 11, 15, 5))


def test_a_transfer_into_a_group_he_already_belongs_to_keeps_its_period(two, clock):
    s = newcomer(two)
    clock.at(9, 1)
    add(two, two.group, s)
    add(two, two.second, s)                                      # both, the first one primary
    clock.at(10, 1)
    add(two, two.group, s, mode="transfer")                      # the second one is closed, the first stays
    assert stays(two, s, two.group) == [(D(2026, 9, 1, 12), None)]                 # untouched
    assert stays(two, s, two.second) == [(D(2026, 9, 1, 12), D(2026, 10, 1, 12))]
    assert membership(two, s, two.group).transferred_at == D(2026, 10, 1, 12)
    invariants(two)


def test_a_transfer_to_the_same_single_group_changes_nothing(world, clock):
    s = newcomer(world)
    clock.at(9, 1)
    add(world, world.group, s)
    clock.at(10, 1)
    assert add(world, world.group, s, mode="transfer")["status"] == "unchanged"
    assert stays(world, s, world.group) == [(D(2026, 9, 1, 12), None)]
    invariants(world)


# ---- other write points ---------------------------------------------------------------------------------


def test_the_bulk_add_and_the_import_open_periods(world, clock):
    people = [newcomer(world, f"Массовый{i}") for i in range(3)]
    clock.at(9, 1)
    result = world.svc.bulk_add_students(
        str(world.org.id), str(world.group.id), [str(p.id) for p in people], mode="add", actor_id=str(world.owner.id)
    )
    assert result["added"] == 3
    for p in people:
        assert stays(world, p, world.group) == [(D(2026, 9, 1, 12), None)]
    invariants(world)


def test_a_membership_added_to_the_session_without_a_period_gets_one(world):
    s = newcomer(world)
    world.db.add(StudentGroup(student_id=s.id, group_id=world.group.id, is_active=True, joined_at=D(2026, 9, 3, 9)))
    world.db.commit()
    assert stays(world, s, world.group) == [(D(2026, 9, 3, 9), None)]
    other = newcomer(world, "Закрытый")
    world.db.add(
        StudentGroup(
            student_id=other.id, group_id=world.group.id, is_active=False,
            joined_at=D(2026, 9, 3, 9), left_at=D(2026, 9, 20, 9),
        )
    )
    world.db.commit()
    assert stays(world, other, world.group) == [(D(2026, 9, 3, 9), D(2026, 9, 20, 9))]
    invariants(world)


def test_the_repo_removal_closes_the_period(world, clock):
    from src.repositories.group_repo import StudentGroupRepo

    s = newcomer(world)
    clock.at(9, 1)
    add(world, world.group, s)
    clock.at(9, 20)
    assert StudentGroupRepo(world.db).remove_student(s.id, world.group.id) is True
    world.db.commit()
    assert stays(world, s, world.group) == [(D(2026, 9, 1, 12), D(2026, 9, 20, 12))]
    invariants(world)


def test_a_clock_that_stepped_back_never_overlaps_the_previous_stay(world, clock):
    s = newcomer(world)
    clock.at(9, 1)
    add(world, world.group, s)
    clock.at(9, 30)
    remove(world, world.group, s)
    clock.at(9, 29)                                              # the server clock went back a day
    add(world, world.group, s)
    assert stays(world, s, world.group) == [(D(2026, 9, 1, 12), D(2026, 9, 30, 12)), (D(2026, 9, 30, 12), None)]
    assert membership(world, s, world.group).joined_at == D(2026, 9, 30, 12)
    invariants(world)


def test_the_invariant_checker_sees_a_hand_made_mess(world):
    m = world.db.query(StudentGroup).filter_by(group_id=world.group.id).first()
    assert membership_period_violations(world.db, [m.id]) == []
    m.left_at = D(2030, 1, 1)                      # a bulk UPDATE of the membership alone ...
    m.is_active = False                            # ... leaves the open period behind
    world.db.flush()
    problems = membership_period_violations(world.db, [m.id])
    assert any("open period" in p for p in problems)
    world.db.rollback()


# ---- the API contract -----------------------------------------------------------------------------------


def _two_stays(w, student, group):
    """September stay, then a gap, then the current one - through the real helpers, at fixed instants."""
    m = membership(w, student, group)
    close_period(m, D(2026, 9, 30, 12))
    open_period(m, D(2026, 11, 1, 12))
    w.db.commit()
    return D(2026, 9, 1, 12)


PERIODS = [
    {"joined_at": "2026-09-01T12:00:00", "left_at": "2026-09-30T12:00:00"},
    {"joined_at": "2026-11-01T12:00:00", "left_at": None},
]


def test_the_student_profile_lists_the_periods_of_every_membership(two, clock):
    s = newcomer(two)
    clock.at(9, 1)
    add(two, two.group, s)
    add(two, two.second, s)
    clock.at(9, 30)
    remove(two, two.second, s)
    m = membership(two, s, two.group)
    close_period(m, D(2026, 9, 30, 12))
    open_period(m, D(2026, 11, 1, 12))
    two.db.commit()

    profile = make_client(two, user=two.admin).get(f"{STUDENTS}/{s.id}/profile").json()
    by_group = {h["group_id"]: h for h in profile["group_history"]}
    assert by_group[str(two.group.id)]["periods"] == PERIODS
    assert by_group[str(two.second.id)]["periods"] == [
        {"joined_at": "2026-09-01T12:00:00", "left_at": "2026-09-30T12:00:00"}
    ]
    # the old fields are unchanged: the CURRENT stay
    assert by_group[str(two.group.id)]["joined_at"] == "2026-11-01T12:00:00"
    assert by_group[str(two.group.id)]["left_at"] is None and by_group[str(two.group.id)]["is_active"] is True
    # the active groups of the card carry them too
    active = {g["group_id"]: g for g in profile["groups"]}
    assert set(active) == {str(two.group.id)} and active[str(two.group.id)]["periods"] == PERIODS
    assert active[str(two.group.id)]["joined_at"] == "2026-11-01T12:00:00"


def test_the_group_card_lists_the_periods_of_its_students(world, clock):
    s = newcomer(world)
    clock.at(9, 1)
    add(world, world.group, s)
    _two_stays(world, s, world.group)
    detail = make_client(world, user=world.admin).get(f"{API}/org/groups/{world.group.id}").json()
    by_id = {row["id"]: row for row in detail["students"]}
    assert by_id[str(s.id)]["periods"] == PERIODS and by_id[str(s.id)]["joined_at"] == "2026-11-01T12:00:00"
    for old in world.students:                                    # the others have one open stay
        periods = by_id[str(old.id)]["periods"]
        assert len(periods) == 1 and periods[0]["left_at"] is None
        assert periods[0]["joined_at"] == by_id[str(old.id)]["joined_at"]


def test_the_journal_lists_the_periods_of_a_former_student(world):
    from tests.test_memberships_pg import completed_lesson, journal

    s = world.students[0]
    completed_lesson(world, world.group)                          # the roster of that lesson is frozen with him in it
    remove(world, world.group, s)
    add(world, world.group, s)
    remove(world, world.group, s)                                 # two stays, the second one closed again
    entry = {h["id"]: h for h in journal(world, world.group).historicalStudents}[str(s.id)]
    assert len(entry["periods"]) == 2 and all(p["left_at"] for p in entry["periods"])
    assert entry["periods"][0]["joined_at"] < entry["periods"][0]["left_at"] <= entry["periods"][1]["joined_at"]
    assert entry["left_at"] == entry["periods"][1]["left_at"]     # the old field: the last stay
    invariants(world)


# ---- no N+1 ---------------------------------------------------------------------------------------------


def _count_statements(w, fn):
    statements = []

    def before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = w.db.get_bind()
    event.listen(engine, "before_cursor_execute", before)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", before)
    return statements


def test_reading_periods_is_one_query_however_many_memberships(world):
    gid, other_gid = world.group.id, world.foreign_group.id
    ids = [m.id for m in world.db.query(StudentGroup).filter(StudentGroup.group_id == gid).all()]
    ids += [uuid4() for _ in range(50)]
    assert len(_count_statements(world, lambda: mp.periods_by_membership(world.db, ids))) == 1
    spans = []
    statements = _count_statements(
        world, lambda: spans.append(mp.group_period_spans(world.db, [gid, other_gid], not_before=D(2020, 1, 1)))
    )
    assert len(statements) == 1 and gid in spans[0] and other_gid in spans[0]


def test_the_roster_at_an_instant_is_one_membership_query_plus_the_students(world):
    gid = world.group.id
    instant = datetime.utcnow()
    statements = _count_statements(world, lambda: world.svc._attendance_roster_at(gid, instant))
    assert len(statements) == 2
    assert "student_group_periods" in statements[0]


# ---- self-healing and the safety net --------------------------------------------------------------------------


def test_closing_an_active_membership_without_an_open_period_heals_it_and_warns(world, clock, caplog):
    """A hand-made SQL edit left the membership active with every period closed: closing it must not claim success
    while ``is_active`` stays true."""
    s = newcomer(world)
    clock.at(9, 1)
    add(world, world.group, s)
    clock.at(9, 20)
    remove(world, world.group, s)
    clock.at(10, 1)
    add(world, world.group, s)                                            # open again (Oct 1)
    m = membership(world, s, world.group)
    for stay in m.periods:
        if stay.left_at is None:
            stay.left_at = D(2026, 10, 3, 12)                             # ... and somebody closed the period by hand
    world.db.commit()
    assert any("open period" in p for p in membership_period_violations(world.db, [m.id]))

    clock.at(10, 5)
    with caplog.at_level("WARNING", logger="src.auth.membership_periods"):
        remove(world, world.group, s)
    healed = membership(world, s, world.group)
    assert healed.is_active is False and healed.left_at == D(2026, 10, 5, 12)
    assert stays(world, s, world.group) == [
        (D(2026, 9, 1, 12), D(2026, 9, 20, 12)),
        (D(2026, 10, 1, 12), D(2026, 10, 3, 12)),                         # the hand-closed one stays as it was
        (D(2026, 10, 3, 12), D(2026, 10, 5, 12)),                         # the healed stay: never starts before the previous ended
    ]
    assert healed.joined_at == D(2026, 10, 3, 12)                         # and the row mirrors its last period
    assert [r.getMessage() for r in caplog.records if str(healed.id) in r.getMessage() and r.levelname == "WARNING"]
    assert membership_period_violations(world.db, [healed.id]) == []


def test_closing_an_active_membership_with_no_period_at_all_heals_it_too(world, caplog):
    s = newcomer(world)
    m = StudentGroup(student_id=s.id, group_id=world.group.id, is_active=True, joined_at=D(2026, 9, 3, 9))
    world.db.add(m)
    world.db.commit()
    world.db.query(StudentGroupPeriod).filter_by(membership_id=m.id).delete()      # a row written outside the application
    world.db.commit()
    world.db.expire_all()
    m = world.db.get(StudentGroup, m.id)
    with caplog.at_level("WARNING", logger="src.auth.membership_periods"):
        period = close_period(m, D(2026, 9, 20, 9))
    world.db.commit()
    assert period is not None and (period.joined_at, period.left_at) == (D(2026, 9, 3, 9), D(2026, 9, 20, 9))
    assert m.is_active is False and m.left_at == D(2026, 9, 20, 9)
    assert any(str(m.id) in r.getMessage() for r in caplog.records)
    assert membership_period_violations(world.db, [m.id]) == []


def test_closing_a_membership_that_is_already_closed_changes_nothing_and_does_not_warn(world, caplog):
    s = newcomer(world)
    m = StudentGroup(student_id=s.id, group_id=world.group.id, is_active=False,
                     joined_at=D(2026, 9, 3, 9), left_at=D(2026, 9, 10, 9))
    world.db.add(m)
    world.db.commit()
    with caplog.at_level("WARNING", logger="src.auth.membership_periods"):
        assert close_period(m, D(2026, 12, 1)) is None
    assert m.left_at == D(2026, 9, 10, 9) and not caplog.records


def test_a_new_closed_membership_without_left_at_gets_one_from_the_net(world):
    s = newcomer(world)
    m = StudentGroup(student_id=s.id, group_id=world.group.id, is_active=False, joined_at=D(2026, 9, 3, 9))
    world.db.add(m)
    world.db.commit()
    world.db.refresh(m)
    assert m.left_at == D(2026, 9, 3, 9)                                  # the closing moment: the row mirrors its period
    assert stays(world, s, world.group) == [(D(2026, 9, 3, 9), D(2026, 9, 3, 9))]
    assert membership_period_violations(world.db, [m.id]) == []
    # a left_at BEFORE joined_at is lifted to it (a period never ends before it starts) and the row follows
    other = newcomer(world, "Обратный")
    n = StudentGroup(student_id=other.id, group_id=world.group.id, is_active=False,
                     joined_at=D(2026, 9, 3, 9), left_at=D(2026, 9, 1, 9))
    world.db.add(n)
    world.db.commit()
    world.db.refresh(n)
    assert n.left_at == D(2026, 9, 3, 9) and membership_period_violations(world.db, [n.id]) == []


def test_the_open_period_index_holds_on_sqlite_too():
    """The one-open-period rule is a partial unique index; the unit tests that run on SQLite get it as well."""
    from sqlalchemy import create_engine
    from sqlalchemy.exc import IntegrityError

    engine = create_engine("sqlite://")
    StudentGroupPeriod.__table__.create(engine)
    membership_id, other_id = uuid4(), uuid4()
    with engine.begin() as conn:
        conn.execute(StudentGroupPeriod.__table__.insert(), [
            {"id": uuid4(), "membership_id": membership_id, "joined_at": D(2026, 9, 1), "left_at": D(2026, 9, 5)},
            {"id": uuid4(), "membership_id": membership_id, "joined_at": D(2026, 10, 1), "left_at": None},
            {"id": uuid4(), "membership_id": other_id, "joined_at": D(2026, 10, 1), "left_at": None},
        ])
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(StudentGroupPeriod.__table__.insert(), [
                {"id": uuid4(), "membership_id": membership_id, "joined_at": D(2026, 11, 1), "left_at": None}])
