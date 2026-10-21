"""GET /org/group-prices - the table of the page «Оплаты -> Цены групп» (PostgreSQL)."""
from sqlalchemy import event

from src.models.auth import Group, MembershipPriceOverride, Organization, Student, StudentGroup

from tests.pg_world import (  # noqa: F401  (fixtures)
    EPOCH, give_group_price, give_price, make_client, make_group, make_member, make_org, make_student, make_user,
    needs_pg, pg_session, world,
)
from tests.test_group_price_pg import allow_admin, put_price
from tests.test_payments_service_pg import API, day, mid, owner as money_owner

pytestmark = needs_pg

URL = f"{API}/org/group-prices"


def listing(w, client=None, expect=200):
    r = (client or money_owner(w)).get(URL)
    assert r.status_code == expect, r.text
    return r.json()


def item(w, group=None, **kw):
    gid = str((group or w.group).id)
    (found,) = [i for i in listing(w, **kw)["items"] if i["group_id"] == gid]
    return found


def allow_admin_for(w, org):
    w.db.query(Organization).filter_by(id=org.id).update({"settings": {"payments": {"admin_can_manage": True}}})
    w.db.commit()


def test_an_org_without_groups_gets_an_empty_list(world):
    empty_org = make_org(world.db, "Пустой центр")
    lone_owner = make_user(world.db, "owner")
    make_member(world.db, empty_org, lone_owner, "owner")
    world.db.commit()
    world.other_org = empty_org
    out = listing(world, client=make_client(world, user=lone_owner, org="other"))
    assert out["items"] == [] and out["period_mode"] == "calendar" and out["price_label"] == "Оплата в месяц"


def test_a_group_without_a_price(world):
    out = listing(world)
    assert out["as_of"] and [i["name"] for i in out["items"]] == ["Группа"]
    row = out["items"][0]
    assert row["current"] is None and row["scheduled"] is None and row["scheduled_count"] == 0
    assert row["students_with_override"] == 0 and row["kind"] == "group"
    assert row["teacher_name"] == "Пётр Teacher" and row["teacher_id"] == str(world.teacher.id)


def test_current_and_scheduled_price(world):
    put_price(world, 400_000, valid_from=day(-60))
    put_price(world, 500_000, valid_from=day(-1))
    put_price(world, 520_000, valid_from=day(40))
    put_price(world, 560_000, valid_from=day(100))
    row = item(world)
    assert row["current"] == {"amount": 500_000, "valid_from": day(-1).isoformat()}
    assert row["scheduled"]["amount"] == 520_000 and row["scheduled"]["valid_from"] == day(40).isoformat()
    assert row["scheduled_count"] == 2
    cancel = money_owner(world).delete(f"{API}/org/groups/{world.group.id}/prices/{row['scheduled']['row_id']}")
    assert cancel.status_code == 200, cancel.text                                   # row_id is the one DELETE takes
    assert item(world)["scheduled"]["amount"] == 560_000


def test_a_price_that_starts_today_is_current_not_scheduled(world):
    give_group_price(world.db, world.group, 300_000, day(0))
    row = item(world)
    assert row["current"]["amount"] == 300_000 and row["scheduled"] is None


def test_only_scheduled_price_means_no_current(world):
    give_group_price(world.db, world.group, 300_000, day(10))
    row = item(world)
    assert row["current"] is None and row["scheduled"]["amount"] == 300_000


def test_individual_prices_are_counted_for_active_students_with_an_amount_today(world):
    s0, s1, s2 = world.students
    give_group_price(world.db, world.group, 500_000)
    give_price(world.db, world.db.get(StudentGroup, mid(world, s0)), 300_000)                    # counted
    m1 = world.db.get(StudentGroup, mid(world, s1))
    give_price(world.db, m1, 250_000, valid_from=day(-30))
    world.db.add(MembershipPriceOverride(membership_id=m1.id, amount=None, valid_from=day(-1), reason=None))   # ended
    world.db.commit()
    m2 = world.db.get(StudentGroup, mid(world, s2))
    give_price(world.db, m2, 200_000, valid_from=day(5))                                          # scheduled: not today
    assert item(world)["students_with_override"] == 1
    give_price(world.db, m2, 200_000, valid_from=day(0))
    assert item(world)["students_with_override"] == 2
    world.db.query(Student).filter_by(id=s0.id).update({"status": "archived"})                    # archived student
    world.db.commit()
    assert item(world)["students_with_override"] == 1
    world.db.query(StudentGroup).filter_by(id=m2.id).update({"is_active": False})                 # left the group
    world.db.commit()
    assert item(world)["students_with_override"] == 0


def test_archived_groups_are_left_out_and_other_orgs_are_isolated(world):
    second = make_group(world.db, world.org, world.teacher2, "Архивная")
    third = make_group(world.db, world.org, world.teacher2, "Без учителя")
    third.teacher_id = None
    world.db.commit()
    give_group_price(world.db, third, 111_000)
    give_group_price(world.db, world.foreign_group, 999_000)
    assert [i["name"] for i in listing(world)["items"]] == ["Архивная", "Без учителя", "Группа"]
    world.db.query(Group).filter_by(id=second.id).update({"status": "archived"})
    world.db.commit()
    items = listing(world)["items"]
    assert [i["name"] for i in items] == ["Без учителя", "Группа"]
    assert items[0]["teacher_name"] is None and items[0]["current"]["amount"] == 111_000
    assert str(world.foreign_group.id) not in {i["group_id"] for i in items}                   # nothing of the other org
    foreign = listing(world, client=make_client(world, user=world.foreign_admin, org="other"), expect=403)
    assert foreign["detail"]                                                                    # (no right to money there)
    allow_admin_for(world, world.other_org)
    other = listing(world, client=make_client(world, user=world.foreign_admin, org="other"))["items"]
    assert [i["name"] for i in other] == ["Чужая"] and other[0]["current"]["amount"] == 999_000


def test_the_kind_follows_the_lesson_type(world):
    world.db.query(Group).filter_by(id=world.group.id).update({"lesson_type": "individual"})
    world.db.commit()
    assert item(world)["kind"] == "individual"


def test_the_label_follows_the_billing_mode(world):
    from tests.test_payments_service_pg import set_settings

    set_settings(world, period_mode="lessons")
    out = listing(world)
    assert out["period_mode"] == "lessons" and out["price_label"] == "Стоимость пакета"


def test_admin_needs_the_right_to_money_teacher_never(world):
    admin = make_client(world, user=world.admin)
    assert listing(world, client=admin, expect=403)["detail"]
    allow_admin(world)
    assert listing(world, client=admin)["items"]
    teacher = make_client(world, user=world.teacher)
    assert listing(world, client=teacher, expect=403)["detail"]
    student_like = make_client(world, user=world.teacher2, role="student", org_role="student")
    assert listing(world, client=student_like, expect=403)["detail"]


def test_the_number_of_queries_does_not_depend_on_the_number_of_groups(world):
    def count():
        seen = []
        engine = world.db.get_bind()

        def hook(conn, cursor, statement, *a):
            seen.append(statement)

        event.listen(engine, "before_cursor_execute", hook)
        try:
            listing(world)
        finally:
            event.remove(engine, "before_cursor_execute", hook)
        return len(seen)

    client_warm = count()
    for n in range(6):
        g = make_group(world.db, world.org, world.teacher2, f"Г{n}")
        make_student(world.db, world.org, g, f"У{n}", "Тест", f"N13-5{n}")
        world.db.commit()
        give_group_price(world.db, g, 100_000 + n)
        give_group_price(world.db, g, 200_000, day(7))
    assert count() == client_warm
