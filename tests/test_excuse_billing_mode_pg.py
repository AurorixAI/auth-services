"""ROADMAP #103 / PAY-2: ``billing_mode`` of an excuse - how the student is billed in that group: the billing mode of the
centre while he has money on the account (calendar or package), ``None`` while he has none.  Excuse list / review responses
and the journal (administrators only)."""
from datetime import date, datetime, timedelta

from sqlalchemy import event

from src.auth import payment_status as ps

from tests.pg_world import business_today, make_client, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_attendance_excuses import journal, key_of
from tests.test_excuse_deadline import API, lesson_marks, listing
from tests.test_payments_service_pg import day, owner, pay, set_settings


# ---- the pure rule ----------------------------------------------------------------


def rec(paid_on, *, lessons=None, valid_to=None, created=0, carried_out_at=None, valid_from=None):
    return ps.PaymentRecord(
        paid_on=paid_on, valid_from=valid_from or paid_on, valid_to=valid_to, lessons_count=lessons,
        created_at=datetime(2026, 1, 1, 10, created), carried_out_at=carried_out_at,
    )


def test_the_mode_is_the_one_of_the_latest_live_payment():
    d = date(2026, 9, 1)
    assert ps.billing_mode([]) is None                                                       # no payment: no package line
    assert ps.billing_mode([rec(d, valid_to=d + timedelta(days=29))]) == "calendar"
    assert ps.billing_mode([rec(d, lessons=12)]) == "package"
    # the newest payment decides, whatever the older ones were
    assert ps.billing_mode([rec(d, lessons=12), rec(d + timedelta(days=40), valid_to=d + timedelta(days=70))]) == "calendar"
    assert ps.billing_mode([rec(d, valid_to=d + timedelta(days=29)), rec(d + timedelta(days=40), lessons=8)]) == "package"
    # the same day: the later entered one
    assert ps.billing_mode([rec(d, lessons=12, created=5), rec(d, valid_to=d + timedelta(days=29), created=1)]) == "package"
    assert ps.billing_mode([rec(d, lessons=12, created=1), rec(d, valid_to=d + timedelta(days=29), created=5)]) == "calendar"
    # the one definition: the very payment compute_status reads, covers-nothing payments left out
    moved_before_it_began = rec(d + timedelta(days=40), valid_from=d + timedelta(days=40), valid_to=d + timedelta(days=70),
                                carried_out_at=datetime(2026, 9, 5, 10, 0))
    assert moved_before_it_began.covers_nothing
    assert ps.billing_mode([rec(d, lessons=12), moved_before_it_began]) == "package"
    assert ps.billing_mode([moved_before_it_began]) is None
    status = ps.compute_status(today=d + timedelta(days=50), monthly_fee=None, payments=[rec(d, lessons=12), moved_before_it_began])
    assert status.period_mode == ps.LESSONS


# ---- through the API --------------------------------------------------------------


def excuse_all(w, offset=1):
    """One lesson where every student of the group is excused (pending, with a reason)."""
    return lesson_marks(w, offset, {s: "excused" for s in w.students}, reasons={s: "Болел" for s in w.students})


def modes(w):
    return {i["student"]["id"]: i["billing_mode"] for i in listing(w)}


def by_student(w, a=None, b=None, c=None):
    s0, s1, s2 = w.students
    return {str(s0.id): a, str(s1.id): b, str(s2.id): c}



@needs_pg
def test_decided_excuses_the_review_and_reset_responses_carry_it_too(world):
    s0, s1, _ = world.students
    set_settings(world, period_mode="lessons", lessons_per_payment=12)
    pay(world, s0, paid_on=day(-30), lessons_count=12)
    pay(world, s1, paid_on=day(-30), lessons_count=12)
    excuse_all(world)
    items = {i["student"]["id"]: i for i in listing(world)}
    client = make_client(world)
    r = client.post(f"{API}/org/attendance/excuses/{items[str(s1.id)]['id']}/review", json={"decision": "confirmed"})
    assert r.status_code == 200 and r.json()["status"] == "confirmed" and r.json()["billing_mode"] == "package"
    r = client.post(f"{API}/org/attendance/excuses/{items[str(s0.id)]['id']}/review", json={"decision": "rejected"})
    assert r.status_code == 200 and r.json()["billing_mode"] == "package"
    assert modes(world) == by_student(world, "package", "package", None)                      # decided ones are in the list as well
    r = client.post(f"{API}/org/attendance/excuses/{items[str(s1.id)]['id']}/reset")
    assert r.status_code == 200 and r.json()["status"] == "pending" and r.json()["billing_mode"] == "package"
    # the bulk answer only counts
    ids = [items[str(s)]["id"] for s in (str(s0.id), str(s1.id))]
    r = client.post(f"{API}/org/attendance/excuses/review-bulk", json={"ids": ids, "decision": "confirmed"})
    assert r.status_code == 200 and set(r.json()) == {"updated", "skipped"}


@needs_pg
def test_the_journal_carries_it_for_administrators_and_never_for_a_teacher(world):
    s0, s1, s2 = world.students
    set_settings(world, period_mode="lessons", lessons_per_payment=12)
    pay(world, s0, paid_on=day(-30), lessons_count=12)
    pay(world, s1, paid_on=day(-30), lessons_count=12)
    lesson_day = excuse_all(world)
    from tests.pg_world import past_lesson

    _, lesson_id = past_lesson(1)
    key = key_of(lesson_day, lesson_id)
    admin_view = journal(world, world.admin).excuses[key]
    assert {sid: e["billing_mode"] for sid, e in admin_view.items()} == by_student(world, "package", "package", None)
    assert set(admin_view[str(s0.id)]) == {"id", "status", "reason", "reason_by_name", "reason_at", "reviewed_by_name",
                                           "reviewed_at", "review_note", "has_files", "billing_mode",
                                           "decided_after_deadline", "deadline_at_decision"}      # the decision mark (#39)
    teacher_view = journal(world, world.teacher, is_admin=False).excuses[key]
    assert all("billing_mode" not in e for e in teacher_view.values()) and len(teacher_view) == 3
    # over HTTP, an administrator of the centre without the right to money sees it (it is not money), a teacher does not
    path = f"{API}/org/groups/{world.group.id}/attendance"
    window = {"date_from": lesson_day.isoformat(), "date_to": lesson_day.isoformat()}
    got = make_client(world, user=world.admin).get(path, params=window).json()["excuses"][key]
    assert got[str(s1.id)]["billing_mode"] == "package" and got[str(s2.id)]["billing_mode"] is None
    got = make_client(world, user=world.teacher).get(path, params=window).json()["excuses"][key]
    assert all("billing_mode" not in e for e in got.values())
    # a teacher cannot reach the excuse list at all
    assert make_client(world, user=world.teacher).get(f"{API}/org/attendance/excuses").status_code == 403


@needs_pg
def test_it_is_loaded_in_a_batch_the_same_queries_for_30_excuses_as_for_6(world):
    s0, s1, s2 = world.students
    set_settings(world, period_mode="lessons", lessons_per_payment=40)
    pay(world, s0, paid_on=day(-30), lessons_count=40)
    pay(world, s1, paid_on=day(-30), lessons_count=40)

    def statements(fn):
        world.db.expire_all()
        seen = []
        engine = world.db.get_bind()

        def record(conn, cursor, statement, *a):
            seen.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            result = fn()
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return result, seen

    for offset in (1, 2):
        excuse_all(world, offset)
    small_items, small = statements(lambda: listing(world))
    for offset in range(3, 11):
        excuse_all(world, offset)
    big_items, big = statements(lambda: listing(world))
    assert len(small_items) == 6 and len(big_items) == 30
    assert len(big) == len(small), (len(small), len(big))
    assert [i["billing_mode"] for i in big_items if i["student"]["id"] == str(s0.id)] == ["package"] * 10
    assert [i["billing_mode"] for i in big_items if i["student"]["id"] == str(s1.id)] == ["package"] * 10
    assert all(i["billing_mode"] is None for i in big_items if i["student"]["id"] == str(s2.id))
    # the payments are read ONCE for the whole page (shared with the deadlines), never per excuse
    assert len([q for q in big if "FROM student_payments" in q]) == 1
    # the journal: one read of the payments for all 30 excuses, none at all for a teacher
    today = business_today()
    params = (str(world.org.id), str(world.group.id))

    def read_journal(actor, is_admin):
        return lambda: world.svc.get_group_attendance(
            *params, date_from=(today - timedelta(days=30)).isoformat(), date_to=today.isoformat(),
            actor_id=str(actor.id), is_org_admin=is_admin, is_org_manager=is_admin)

    admin_journal, admin_sql = statements(read_journal(world.admin, True))
    assert sum(len(v) for v in admin_journal.excuses.values()) == 30
    assert len([q for q in admin_sql if "FROM student_payments" in q]) == 1
    _, teacher_sql = statements(read_journal(world.teacher, False))
    assert not [q for q in teacher_sql if "student_payments" in q]


@needs_pg
def test_the_mode_is_the_centre_regime_while_there_is_money_and_none_without(world):
    s0, s1, s2 = world.students
    pay(world, s0, paid_on=day(-30), valid_to=day(20))
    excuse_all(world)
    assert modes(world) == by_student(world, "calendar", None, None)             # nobody else has money: no package line
    set_settings(world, period_mode="lessons")
    assert modes(world) == by_student(world, "package", None, None)              # the centre bills by packages now
    set_settings(world, period_mode="calendar")
    assert modes(world) == by_student(world, "calendar", None, None)


@needs_pg
def test_a_voided_payment_does_not_count_and_another_group_says_nothing(world):
    s0, s1, s2 = world.students
    newest = pay(world, s0, paid_on=day(-2), valid_to=day(27))["payment"]
    excuse_all(world)
    assert modes(world)[str(s0.id)] == "calendar"
    r = owner(world).post(f"{API}/org/payments/{newest['id']}/void", json={"reason": "Внесено по ошибке"})
    assert r.status_code == 200, r.text
    assert modes(world)[str(s0.id)] is None                                       # the voided one is not live
    from tests.pg_world import make_group
    from src.models.auth import StudentGroup

    other = make_group(world.db, world.org, world.teacher, "Вторая")
    world.db.commit()
    world.db.add(StudentGroup(student_id=s2.id, group_id=other.id, is_active=True, joined_at=datetime.now() - timedelta(days=60)))
    world.db.commit()
    pay(world, s2, group=other, paid_on=day(-3), valid_to=day(20))
    assert modes(world)[str(s2.id)] is None                                       # money in ANOTHER group says nothing about this one
