"""N13 P1.7: «в группах» / «без группы» split of the student list, ID search, dashboard total (PostgreSQL).

The «Ученики» page shows two tabs whose counts must add up to the dashboard «Всего учеников»;
archived students are left out of both (the dashboard excludes them too).
"""
from datetime import datetime, timezone

from src.models.auth import Student, StudentGroup

from tests.pg_world import (  # noqa: F401  (fixtures)
    make_student, needs_pg, pg_session, world,
)

pytestmark = needs_pg


def _loose(w, first, last, status="new", display_id=None):
    student = Student(org_id=w.org.id, first_name=first, last_name=last, class_level=9,
                      status=status, display_id=display_id)
    w.db.add(student)
    w.db.flush()
    return student


def _page(w, **kw):
    return w.svc.list_students_page(str(w.org.id), actor_id="admin", is_org_admin=True, limit=500, **kw)


def _ids(items):
    return {str(s.id) for s in items}


def test_group_tabs_partition_the_non_archived_students_and_match_the_dashboard_total(world):
    w = world
    # in a group: 3 (fixture). Without a group: a new one, an active one, one whose membership ended.
    new = _loose(w, "Новый", "Безгруппов")
    active = _loose(w, "Активный", "Безгруппов", status="active", display_id="N13-010")
    left = make_student(w.db, w.org, w.group, "Ушёл", "Из-группы", "N13-011")
    membership = w.db.query(StudentGroup).filter_by(student_id=left.id).one()
    membership.is_active = False
    membership.left_at = datetime.now(timezone.utc)
    left.group_id = None
    # archived students: one with a group, one without - in neither tab.
    archived_loose = _loose(w, "Архив", "Безгруппов", status="archived")
    archived_in_group = make_student(w.db, w.org, w.group, "Архив", "Вгруппе", "N13-012")
    archived_in_group.status = "archived"
    w.db.commit()

    grouped, n_grouped = _page(w, has_group=True, exclude_status="archived")
    ungrouped, n_ungrouped = _page(w, has_group=False, exclude_status="archived")

    assert _ids(grouped) == {str(s.id) for s in w.students}
    assert _ids(ungrouped) == {str(new.id), str(active.id), str(left.id)}
    assert _ids(grouped).isdisjoint(_ids(ungrouped))
    assert (n_grouped, n_ungrouped) == (3, 3)
    # «В группах» + «Без группы» == «Всего учеников» of the dashboard (archived excluded from both)
    assert w.svc.get_org_stats(str(w.org.id))["total_students"] == n_grouped + n_ungrouped == 6
    # archived ones are reachable only on request
    assert _page(w, status="archived")[1] == 2
    assert str(archived_loose.id) not in _ids(grouped) | _ids(ungrouped)
    assert str(archived_in_group.id) not in _ids(grouped) | _ids(ungrouped)
    # the filter does not leak another centre
    assert str(w.foreign_student.id) not in _ids(grouped) | _ids(ungrouped)


def test_ungrouped_filter_combines_with_class_and_search(world):
    w = world
    nine = _loose(w, "Девятый", "Свободный")
    eight = _loose(w, "Восьмой", "Свободный")
    eight.class_level = 8
    w.db.commit()
    items, total = _page(w, has_group=False, exclude_status="archived", class_level=9)
    assert _ids(items) == {str(nine.id)} and total == 1
    items, total = _page(w, has_group=False, exclude_status="archived", search="Свободный")
    assert _ids(items) == {str(nine.id), str(eight.id)} and total == 2
    # a student in a group is found by search without the group filter (the drawer's search mode)
    items, _ = _page(w, exclude_status="archived", search="Каримова")
    assert _ids(items) == {str(w.students[0].id)}


def test_search_finds_a_student_by_public_id_but_a_plain_word_never_matches_ids(world):
    w = world
    items, total = _page(w, search="N13-002")
    assert [s.first_name for s in items] == ["Бек"] and total == 1
    assert _page(w, search="n13-00")[1] == 3                      # partial ID, any case
    items, _ = _page(w, search="Юсупов N13-002")                  # name + ID: every word must match
    assert [s.first_name for s in items] == ["Бек"]
    assert _page(w, search="Каримова N13-002")[1] == 0            # words of two different students
    # no digit - names only: «N» is in every ID of the centre but is not a name
    assert _page(w, search="N")[1] == 0
    assert _page(w, search="N13-900")[1] == 0                      # another centre's ID is invisible


def test_dashboard_total_excludes_archived_students(world):
    w = world
    before = w.svc.get_org_stats(str(w.org.id))["total_students"]
    gone = _loose(w, "Бывший", "Ученик", status="archived")
    w.db.commit()
    stats = w.svc.get_org_stats(str(w.org.id))
    assert stats["total_students"] == before == 3
    gone.status = "new"
    w.db.commit()
    assert w.svc.get_org_stats(str(w.org.id))["total_students"] == before + 1
