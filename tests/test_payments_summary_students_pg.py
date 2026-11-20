"""The payments summary counts DISTINCT students for the two names of an unpaid sum (PostgreSQL; PAY-DUE 10.2): ``debtors`` / ``due_students`` are students, ``buckets.debtors`` / ``buckets.to_pay`` are
memberships (rows); the number of a tab (``students`` of the page) is the number of the card and the sum of its rows is the sum of the card.  (The old ``due`` / ``expired`` / ``students_due`` /
``students_expired`` are gone: they named the paid-until status as a debt.)"""
from sqlalchemy import event

from src.models.auth import StudentGroup

from tests.pg_world import (  # noqa: F401  (fixtures)
    make_client, make_group, make_student, needs_pg, pg_session, world,
)
from tests.test_payments_service_pg import API, day, pay, status_page, summary

pytestmark = needs_pg


def second_group(w, *students):
    group = make_group(w.db, w.org, w.teacher2, "Вторая")
    w.db.commit()
    for student in students:
        w.svc.add_student_to_group(str(w.org.id), str(group.id), str(student.id), mode="add", money_right=True)
    return group


def scenario(w):
    """Six memberships of four students: s0 expired in both groups, s1 ends today in A and is expired in B,
    s2 paid, s3 ends today in A."""
    s0, s1, s2 = w.students
    s3 = make_student(w.db, w.org, w.group, "Новый", "Ученик", "N13-004")
    w.db.commit()
    b = second_group(w, s0, s1)
    pay(w, s0, paid_on=day(-30), valid_from=day(-30), valid_to=day(-1))
    pay(w, s0, group=b, paid_on=day(-30), valid_from=day(-30), valid_to=day(-2))
    pay(w, s1, paid_on=day(-30), valid_from=day(-30), valid_to=day(0))
    pay(w, s1, group=b, paid_on=day(-30), valid_from=day(-30), valid_to=day(-3))
    pay(w, s2, paid_on=day(-5), valid_from=day(-5), valid_to=day(25))
    pay(w, s3, paid_on=day(-30), valid_from=day(-30), valid_to=day(0))
    return s3, b


def test_a_student_in_two_groups_counts_once_in_the_students_and_twice_in_the_memberships(world):
    scenario(world)
    s = summary(world)
    assert (s["ends_today"], s["tracked_total"]) == (2, 6)
    debtors, to_pay = status_page(world, bucket="debtors"), status_page(world, bucket="to_pay")
    assert s["buckets"]["debtors"] == debtors["total"] and s["debtors"] == debtors["students"] and s["debt_total"] == debtors["amount_total"]
    assert s["buckets"]["to_pay"] == to_pay["total"] and s["due_students"] == to_pay["students"] and s["due_total"] == to_pay["amount_total"]
    assert debtors["total"] >= debtors["students"] >= 1             # a student who owes in two groups is two rows and one student
    assert len({r["student_id"] for r in debtors["items"]}) == debtors["students"]
    assert sum(r["debt_amount"] for r in debtors["items"]) == s["debt_total"]


def test_nobody_owes_gives_zeros(world):
    s = summary(world)
    assert (s["debt_total"], s["due_total"], s["debtors"], s["due_students"], s["due_nearest"]) == (0, 0, 0, 0, None)


def test_the_summary_costs_the_same_number_of_queries_however_many_students_there_are(world):
    s3, b = scenario(world)

    def measured():
        world.db.expire_all()
        statements, engine = [], world.db.get_bind()

        def record(conn, cursor, statement, *a):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            body = summary(world)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return body, len(statements)

    small, small_count = measured()
    for n in range(5, 11):
        extra = make_student(world.db, world.org, world.group, f"Имя{n}", f"Фамилия{n}", f"N13-0{n:02d}")
        world.db.commit()
        world.svc.add_student_to_group(str(world.org.id), str(b.id), str(extra.id), mode="add", money_right=True)
        pay(world, extra, paid_on=day(-30), valid_from=day(-30), valid_to=day(-1))
        pay(world, extra, group=b, paid_on=day(-30), valid_from=day(-30), valid_to=day(-1))
    big, big_count = measured()
    assert big["debtors"] == small["debtors"] + 6                  # six more students, two groups each: counted once
    assert big["buckets"]["debtors"] > small["buckets"]["debtors"]
    assert big_count == small_count, (small_count, big_count)


def test_the_permissions_of_the_summary_are_unchanged(world):
    """No new permission (the full matrix is test_n13_p2_permissions): the owner reads the new fields, a teacher and an
    admin without the payments switch are still refused."""
    scenario(world)
    url = f"{API}/org/payments/summary"
    owner = make_client(world, user=world.owner).get(url)
    assert owner.status_code == 200 and owner.json()["debtors"] >= 1
    assert make_client(world, user=world.teacher).get(url).status_code == 403
    assert make_client(world, user=world.admin).get(url).status_code == 403
