"""INCOME-2: a deterministic seeded centre for the tests and the golden fixtures (payments / refunds / write-offs get a uuid5 of their name and the organisation).

Hand-calculated figures (today 21.10.2026, fixed billing day 1, accounting start 01.09.2026, full months, no lessons involved):
price A 500 000 (3 students), B 300 000 (2), C 400 000 (1); Sep and Oct are charged in full for everybody.
"""
from datetime import date, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from src.models.auth import Group, Student, StudentGroup, StudentPayment, StudentRefund, StudentWriteoff

from tests.pg_world import give_group_price, make_group, make_student
from tests.test_pay2_api_pg import settings_block, stay

D = date


def uid(org, name: str) -> UUID:
    """Deterministic per organisation (the test database keeps the rows of every test: two worlds must not share an id)."""
    return uuid5(NAMESPACE_URL, f"income2/{org.id}/{name}")


def membership_of(w, student, group):
    w.db.expire_all()
    return w.db.query(StudentGroup).filter_by(student_id=student.id, group_id=group.id).one()


def income2_world(w):
    """Three groups, six students, 11 live payments + 1 voided + 1 replaced, a refund, a write-off.  Returns a namespace-like dict."""
    db = w.db
    settings_block(w, accounting_start_date="2026-09-01", billing_anchor="fixed_day", billing_day=1)
    for user, first, last in ((w.owner, "Азиз", "Хакимов"), (w.admin, "Нодира", "Саидова"), (w.teacher, "Пётр", "Иванов"),
                              (w.teacher2, "Мария", "Ким")):
        user.first_name, user.last_name = first, last
    ga = w.group
    ga.name, ga.schedule = "Математика 9А", []
    gb = make_group(db, w.org, w.teacher2, "Английский 7Б")
    gc = make_group(db, w.org, w.teacher, "Физика 10В")
    gb.schedule = gc.schedule = []
    db.commit()
    for g, price in ((ga, 500_000), (gb, 300_000), (gc, 400_000)):
        give_group_price(db, g, price, valid_from=D(2026, 1, 1))
    a1, a2, a3 = w.students
    b1 = make_student(db, w.org, gb, "Мадина", "Рахимова", "INC-004")
    b2 = make_student(db, w.org, gb, "Тимур", "Алиев", "INC-005")
    c1 = make_student(db, w.org, gc, "Жасур", "Нуров", "INC-006")
    db.commit()
    mem = {"a1": (a1, ga), "a2": (a2, ga), "a3": (a3, ga), "b1": (b1, gb), "b2": (b2, gb), "c1": (c1, gc)}
    ms = {}
    for key, (student, group) in mem.items():
        m = membership_of(w, student, group)
        stay(w, m, D(2026, 8, 15))
        ms[key] = m
    pay = {}

    def payment(key, who, amount, day, hour, minute, method, receipt, by, **extra):
        m = ms[who]
        p = StudentPayment(
            id=uid(w.org, "pay:" + key), org_id=w.org.id, membership_id=m.id, student_id=m.student_id, group_id=m.group_id, amount=amount,
            paid_on=day, method=method, receipt_no=receipt, created_by=by.id, created_at=datetime(day.year, day.month, day.day, hour, minute),
            **extra)
        db.add(p)
        pay[key] = p
        return p

    o, a = w.owner, w.admin
    payment("a1-sep3", "a1", 500_000, D(2026, 9, 3), 5, 10, "cash", 1, o)
    payment("b2-sep5", "b2", 300_000, D(2026, 9, 5), 6, 20, "cash", 2, a)
    payment("c1-sep8", "c1", 400_000, D(2026, 9, 8), 7, 30, "card", 3, o)
    payment("a2-sep10", "a2", 300_000, D(2026, 9, 10), 8, 40, "card", 4, a)
    payment("b1-oct2", "b1", 900_000, D(2026, 10, 2), 4, 15, "bank_transfer", 5, o)
    payment("a1-oct5", "a1", 500_000, D(2026, 10, 5), 9, 5, "cash", 6, a)
    payment("a3-oct7", "a3", 1_250_000, D(2026, 10, 7), 10, 25, "click", 7, o)
    payment("a2-oct12", "a2", 200_000, D(2026, 10, 12), 3, 45, "cash", 8, a)
    payment("c1-oct15-old", "c1", 400_000, D(2026, 10, 15), 5, 0, "card", 9, o, voided_at=datetime(2026, 10, 15, 6, 30),
            voided_by=o.id, void_reason="Исправлено")
    payment("b2-oct20-void", "b2", 777_000, D(2026, 10, 20), 8, 8, "cash", 10, a, voided_at=datetime(2026, 10, 20, 9, 0),
            voided_by=a.id, void_reason="Ошибочный ввод")
    db.flush()
    payment("c1-oct15", "c1", 350_000, D(2026, 10, 15), 6, 30, "card", 11, o, replaces_payment_id=pay["c1-oct15-old"].id)
    payment("a2-oct21", "a2", 100_000, D(2026, 10, 21), 3, 10, "cash", 12, a)
    payment("b2-oct21", "b2", 150_000, D(2026, 10, 21), 6, 40, "cash", 13, o, note="=SUM(A1)")
    m3 = ms["a3"]
    db.add(StudentRefund(id=uid(w.org, "refund:a3"), org_id=w.org.id, membership_id=m3.id, student_id=m3.student_id, group_id=m3.group_id,
                         amount=100_000, method="cash", reason="Переплата", effective_on=D(2026, 10, 21), created_by=o.id,
                         created_at=datetime(2026, 10, 21, 7, 0)))
    m2 = ms["a2"]
    db.add(StudentWriteoff(id=uid(w.org, "writeoff:a2"), org_id=w.org.id, membership_id=m2.id, student_id=m2.student_id, group_id=m2.group_id,
                           amount=100_000, reason="Болезнь", effective_on=D(2026, 10, 20), created_by=o.id,
                           created_at=datetime(2026, 10, 20, 7, 0)))
    db.commit()
    return {"groups": {"A": ga, "B": gb, "C": gc}, "students": {"a1": a1, "a2": a2, "a3": a3, "b1": b1, "b2": b2, "c1": c1},
            "memberships": ms, "payments": pay}


def names_of(w, seeded: dict) -> dict:
    """``{uuid string: symbolic name}`` of everything a response can mention (fixtures are written with these names)."""
    out = {str(w.org.id): "org", str(w.owner.id): "user:owner", str(w.admin.id): "user:admin", str(w.teacher.id): "user:teacher-petr",
           str(w.teacher2.id): "user:teacher-maria"}
    for key, g in seeded["groups"].items():
        out[str(g.id)] = f"group:{key}"
    for key, s in seeded["students"].items():
        out[str(s.id)] = f"student:{key}"
    for key, m in seeded["memberships"].items():
        out[str(m.id)] = f"membership:{key}"
    for key, p in seeded["payments"].items():
        out[str(p.id)] = f"payment:{key}"
    return out
