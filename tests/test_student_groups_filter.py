"""N13 P1.7: «в группах» / «без группы» split of the student list, ID search, dashboard total (PostgreSQL).

The «Ученики» page shows two tabs whose counts must add up to the dashboard «Всего учеников»;
archived students are left out of both (the dashboard excludes them too).
"""
from datetime import datetime, timezone

from src.models.auth import Student, StudentGroup

from tests.pg_world import (  # noqa: F401  (fixtures)
    make_group, make_student, needs_pg, pg_session, world,
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


def _by_name(items):
    return {s.first_name: s for s in items}


def test_in_a_group_means_an_active_membership_in_a_live_group(world):
    """The three edge states: stale primary group, archived group only, membership without a primary group."""
    w = world
    # 1. students.group_id points at a group but there is no active membership: not in a group
    stale = _loose(w, "Висящий", "Групповой", status="active")
    stale.group_id = w.group.id
    # 2. the only membership is in an archived group: not in a group (can be enrolled again)
    old_group = make_group(w.db, w.org, w.teacher, "Старая")
    archived_member = make_student(w.db, w.org, old_group, "Архивгруппа", "Участник", "N13-020")
    archived_member.status = "active"
    old_group.status = "archived"
    # 3. an active membership but no primary group: in a group
    member_only = make_student(w.db, w.org, w.group, "Безосновной", "Участник", "N13-021")
    member_only.group_id = None
    # 4. a stale primary group plus a live membership elsewhere: in that other group
    other = make_group(w.db, w.org, w.teacher, "Вторая")
    two = make_student(w.db, w.org, w.group, "Перенесён", "Тихо", "N13-022")
    two.group_id = old_group.id                                    # primary points at the archived group
    w.db.add(StudentGroup(student_id=two.id, group_id=other.id, is_active=True))
    # 5. a foreign group never counts for this centre (corrupt data must not hide the student)
    foreign = _loose(w, "Чужое", "Членство", status="active")
    w.db.add(StudentGroup(student_id=foreign.id, group_id=w.foreign_group.id, is_active=True))
    w.db.commit()

    grouped = _by_name(_page(w, has_group=True, exclude_status="archived")[0])
    ungrouped = _by_name(_page(w, has_group=False, exclude_status="archived")[0])
    assert set(ungrouped) == {"Висящий", "Архивгруппа", "Чужое"}
    assert {"Безосновной", "Перенесён", "Алия", "Бек", "Дана"} == set(grouped)
    assert set(grouped).isdisjoint(ungrouped)
    # the list says the same thing as the filter: groups_count counts live groups only, current_group_id names one
    everyone = _by_name(_page(w, exclude_status="archived")[0])
    assert (everyone["Висящий"].groups_count, everyone["Висящий"].current_group_id) == (0, None)
    assert (everyone["Архивгруппа"].groups_count, everyone["Архивгруппа"].current_group_id) == (0, None)
    assert (everyone["Безосновной"].groups_count, everyone["Безосновной"].current_group_id) == (1, str(w.group.id))
    assert everyone["Безосновной"].group_id is None
    assert (everyone["Перенесён"].groups_count, everyone["Перенесён"].current_group_id) == (2, str(w.group.id))
    assert (everyone["Алия"].groups_count, everyone["Алия"].current_group_id) == (1, str(w.group.id))
    assert (everyone["Чужое"].groups_count, everyone["Чужое"].current_group_id) == (0, None)
    # and the dashboard total still equals the two tabs together
    assert w.svc.get_org_stats(str(w.org.id))["total_students"] == len(grouped) + len(ungrouped)


def test_current_group_prefers_the_live_primary_group(world):
    w = world
    second = make_group(w.db, w.org, w.teacher, "Вторая")
    w.db.add(StudentGroup(student_id=w.students[0].id, group_id=second.id, is_active=True,
                          joined_at=datetime(2020, 1, 1)))          # earlier than the primary membership
    w.db.commit()
    first = _by_name(_page(w, search="Алия")[0])["Алия"]
    assert first.groups_count == 2 and first.current_group_id == str(w.group.id)   # primary wins over "earliest"


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


def test_percent_and_underscore_in_a_search_are_plain_characters(world):
    w = world
    odd = _loose(w, "Анна_Мария", "Сто%", display_id="X_1-100%")
    w.db.commit()
    assert [s.first_name for s in _page(w, search="%")[0]] == ["Анна_Мария"]
    assert [s.first_name for s in _page(w, search="_")[0]] == ["Анна_Мария"]
    assert [s.first_name for s in _page(w, search="Сто%")[0]] == ["Анна_Мария"]
    assert [s.first_name for s in _page(w, search="а_М")[0]] == ["Анна_Мария"]
    assert _page(w, search="Ан_а")[1] == 0            # "_" is not "any one character"
    assert _page(w, search="Ка%ова")[1] == 0          # "%" is not "anything"
    assert [s.first_name for s in _page(w, search="X_1-100%")[0]] == ["Анна_Мария"]
    assert _page(w, search="X_1")[1] == 1 and _page(w, search="X%1")[1] == 0
    assert _page(w, search="N13_00")[1] == 0          # the ID's "-" is not "_"
    assert _page(w, search="\\")[1] == 0             # a backslash is a plain character too
    assert str(odd.id) in _ids(_page(w, search="1-100%")[0])
