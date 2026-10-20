"""N13 P1.5: until when a pending excuse must be confirmed (before the student's next payment)."""
import json
from datetime import date, timedelta

from src.auth import payment_status as ps
from src.auth.excuse_deadline import effective_threshold, excuse_deadline, month_end

from tests.pg_world import (  # noqa: F401  (fixtures)
    business_today, excused_lesson, make_client, make_group, make_student, needs_pg, pg_session, reconcile, save, world,
)
from tests.test_payments_service_pg import day, mid, owner, pay, set_settings

API = "/api/v1/auth"


# ---- the pure rule (no database) --------------------------------------------------


def rec(valid_from, valid_to=None, lessons=None, paid_on=None, n=0):
    return ps.PaymentRecord(
        paid_on=paid_on or valid_from, valid_from=valid_from, valid_to=valid_to, lessons_count=lessons,
        created_at=__import__("datetime").datetime(2026, 1, 1, 0, n),
    )


TODAY = date(2026, 10, 15)


def cal(lesson, valid_to, days=5, valid_from=date(2026, 9, 1)):
    return excuse_deadline(
        lesson_date=lesson, today=TODAY, payments=[rec(valid_from, valid_to)], deadline_days=days, deadline_lessons=2,
    )


def test_calendar_deadline_is_valid_to_minus_the_configured_days():
    result = cal(date(2026, 10, 2), date(2026, 10, 31))
    assert result == {"kind": "date", "date": "2026-10-26", "overdue": False, "days_left": 11}
    assert cal(date(2026, 10, 2), date(2026, 10, 31), days=0)["date"] == "2026-10-31"
    assert cal(date(2026, 9, 25), date(2026, 10, 31), days=30)["date"] == "2026-10-01"
    # a lesson on or after valid_to - days is confirmed by valid_to itself
    assert cal(date(2026, 10, 1), date(2026, 10, 31), days=30)["date"] == "2026-10-31"
    assert cal(date(2026, 10, 2), date(2026, 10, 31), days=30)["date"] == "2026-10-31"


def test_cutoff_day_the_day_before_it_and_valid_to():
    # valid_to 31.10 - 5 days = cutoff 26.10
    assert cal(date(2026, 10, 26), date(2026, 10, 31))["date"] == "2026-10-31"   # on the cutoff day: valid_to
    assert cal(date(2026, 10, 27), date(2026, 10, 31))["date"] == "2026-10-31"   # after it
    assert cal(date(2026, 10, 25), date(2026, 10, 31))["date"] == "2026-10-27"   # the day before: cutoff 26.10 < 25.10 + 2 days
    assert cal(date(2026, 10, 24), date(2026, 10, 31))["date"] == "2026-10-26"   # two days before: the cutoff itself
    assert cal(date(2026, 10, 31), date(2026, 10, 31))["date"] == "2026-10-31"   # on valid_to: the grace is capped at valid_to
    assert cal(date(2026, 10, 30), date(2026, 10, 31))["date"] == "2026-10-31"


def test_minimum_grace_is_two_days_but_never_past_valid_to():
    # cutoff = 15.10 (valid_to 20.10 - 5): a lesson on 14.10 would otherwise have to be confirmed the next day
    assert cal(date(2026, 10, 14), date(2026, 10, 20))["date"] == "2026-10-16"
    assert cal(date(2026, 10, 13), date(2026, 10, 20))["date"] == "2026-10-15"
    # days = 0: valid_to is the deadline, the grace cannot move it past valid_to
    assert cal(date(2026, 10, 19), date(2026, 10, 20), days=0)["date"] == "2026-10-20"
    assert cal(date(2026, 10, 20), date(2026, 10, 20), days=0)["date"] == "2026-10-20"
    assert cal(date(2026, 10, 1), date(2026, 10, 20), days=0)["date"] == "2026-10-20"


def test_overdue_is_strictly_after_the_deadline_day():
    assert cal(date(2026, 10, 1), date(2026, 10, 20)) == {
        "kind": "date", "date": "2026-10-15", "overdue": False, "days_left": 0}
    assert cal(date(2026, 10, 1), date(2026, 10, 19)) == {
        "kind": "date", "date": "2026-10-14", "overdue": True, "days_left": -1}


def test_only_the_payment_covering_the_lesson_counts():
    september = rec(date(2026, 9, 1), date(2026, 9, 30), n=1)
    october = rec(date(2026, 10, 1), date(2026, 10, 31), n=2)
    kwargs = dict(today=TODAY, payments=[september, october], deadline_days=5, deadline_lessons=2)
    assert excuse_deadline(lesson_date=date(2026, 9, 20), **kwargs)["date"] == "2026-09-25"
    assert excuse_deadline(lesson_date=date(2026, 10, 3), **kwargs)["date"] == "2026-10-26"


def test_no_covering_payment_falls_back_to_the_month_end():
    result = excuse_deadline(lesson_date=date(2026, 10, 8), today=TODAY, payments=[], deadline_days=5, deadline_lessons=2)
    assert result == {"kind": "date", "date": "2026-10-31", "overdue": False, "days_left": 16}
    earlier = excuse_deadline(lesson_date=date(2026, 9, 8), today=TODAY, payments=[], deadline_days=5, deadline_lessons=2)
    assert earlier["date"] == "2026-09-30" and earlier["overdue"] is True
    # a calendar payment that ends before the lesson does not cover it either
    gap = excuse_deadline(lesson_date=date(2026, 10, 8), today=TODAY, payments=[rec(date(2026, 9, 1), date(2026, 9, 30))],
                          deadline_days=5, deadline_lessons=2)
    assert gap["date"] == "2026-10-31"
    assert month_end(date(2026, 2, 3)) == date(2026, 2, 28) and month_end(date(2028, 2, 3)) == date(2028, 2, 29)


def less(lesson, packages, counted, threshold=2, lesson_counted=True):
    return excuse_deadline(
        lesson_date=lesson, today=TODAY, payments=packages, counted_dates=counted, lesson_counted=lesson_counted,
        deadline_days=5, deadline_lessons=threshold,
    )


def state(left, urgent, due_now, overdue):
    return {"kind": "lessons", "lessons_left": left, "urgent": urgent, "due_now": due_now, "overdue": overdue}


def test_lessons_rule_is_symmetric_with_the_calendar_one():
    # N = 2: more than N left is normal, exactly N is the last lesson to confirm, fewer is overdue
    package = rec(date(2026, 10, 1), lessons=5)
    dates = [date(2026, 10, d) for d in (2, 5, 7, 9, 12)]
    assert less(date(2026, 10, 2), [package], dates[:2]) == state(3, False, False, False)   # 3 left
    assert less(date(2026, 10, 2), [package], dates[:3]) == state(2, True, True, False)     # 2 left = N: due now
    assert less(date(2026, 10, 2), [package], dates[:4]) == state(1, True, False, True)     # 1 left < N: overdue
    assert less(date(2026, 10, 2), [package], dates) == state(0, True, False, True)         # used up
    # the threshold is the owner's setting
    assert less(date(2026, 10, 2), [package], dates[:3], threshold=1) == state(2, False, False, False)
    assert less(date(2026, 10, 2), [package], dates[:4], threshold=1) == state(1, True, True, False)
    assert less(date(2026, 10, 2), [package], dates, threshold=1) == state(0, True, False, True)
    # N = 0: the last lesson (1 left) is the one due now, a used-up package is overdue
    assert less(date(2026, 10, 2), [package], dates[:3], threshold=0) == state(2, False, False, False)
    assert less(date(2026, 10, 2), [package], dates[:4], threshold=0) == state(1, True, True, False)
    assert less(date(2026, 10, 2), [package], dates, threshold=0) == state(0, True, False, True)


def test_the_threshold_never_exceeds_the_package_size_minus_one():
    # a package of 2 with N = 2: the first lesson (1 left) is due now, not overdue; the second is overdue
    two = rec(date(2026, 10, 1), lessons=2)
    assert less(date(2026, 10, 2), [two], [date(2026, 10, 2)]) == state(1, True, True, False)
    assert less(date(2026, 10, 2), [two], [date(2026, 10, 2), date(2026, 10, 5)]) == state(0, True, False, True)
    # a package of 3 with N = 5: effective N = 2
    three = rec(date(2026, 10, 1), lessons=3)
    assert less(date(2026, 10, 2), [three], [date(2026, 10, 2)], threshold=5) == state(2, True, True, False)
    assert less(date(2026, 10, 2), [three], [date(2026, 10, 2), date(2026, 10, 5)], threshold=5) == state(1, True, False, True)
    # a package of 5 with N = 2 keeps N = 2
    five = rec(date(2026, 10, 1), lessons=5)
    assert less(date(2026, 10, 2), [five], [date(2026, 10, 2)]) == state(4, False, False, False)
    assert effective_threshold(2, 2) == 1 and effective_threshold(2, 1) == 0 and effective_threshold(0, 9) == 0
    assert effective_threshold(30, 12) == 11 and effective_threshold(2, 5) == 2 and effective_threshold(2, None) == 2


def test_effective_threshold_zero_means_one_left_is_due_and_none_left_is_overdue():
    # a package of one lesson (effective N = 0): the lesson used it up
    one = rec(date(2026, 10, 1), lessons=1)
    assert less(date(2026, 10, 2), [one], [date(2026, 10, 2)]) == state(0, True, False, True)
    # a package of two with N = 1 (effective 1): symmetric rule
    two = rec(date(2026, 10, 1), lessons=2)
    assert less(date(2026, 10, 2), [two], [date(2026, 10, 2)], threshold=1) == state(1, True, True, False)
    # N = 0 set by the owner on a big package: 2 left calm, 1 left due now, 0 overdue
    big = rec(date(2026, 10, 1), lessons=10)
    counted = [date(2026, 10, d) for d in range(2, 12)]
    assert less(date(2026, 10, 2), [big], counted[:8], threshold=0) == state(2, False, False, False)
    assert less(date(2026, 10, 2), [big], counted[:9], threshold=0) == state(1, True, True, False)
    assert less(date(2026, 10, 2), [big], counted, threshold=0) == state(0, True, False, True)


def test_lesson_goes_to_the_package_that_took_it_not_to_a_later_one():
    first = rec(date(2026, 9, 1), lessons=2, n=1)
    second = rec(date(2026, 9, 20), lessons=5, n=2)
    counted = [date(2026, 9, 3), date(2026, 9, 10), date(2026, 9, 25), date(2026, 9, 27)]
    # 03.09 and 10.09 fill the first package; 25.09 is the first lesson of the second (left 5-2=3)
    assert less(date(2026, 9, 10), [first, second], counted)["lessons_left"] == 0
    assert less(date(2026, 9, 25), [first, second], counted)["lessons_left"] == 3


def test_an_unfinished_lesson_would_be_charged_to_the_package():
    package = rec(date(2026, 10, 1), lessons=3)
    # 10.08 is not among the counted lessons: it is added, 3 - 2 = 1 left
    result = less(date(2026, 10, 8), [package], [date(2026, 10, 2)], lesson_counted=False)
    assert result == state(1, True, False, True)
    assert less(date(2026, 10, 8), [package], [date(2026, 10, 2)], lesson_counted=False, threshold=1) == state(1, True, True, False)


def test_two_lessons_on_one_day_the_unfinished_one_is_added_on_top():
    package = rec(date(2026, 10, 1), lessons=3)
    # A completed lesson on 05.10 is already counted; the excused lesson B of the same day is unfinished.
    counted = [date(2026, 10, 2), date(2026, 10, 5)]
    assert less(date(2026, 10, 5), [package], counted, lesson_counted=False)["lessons_left"] == 0   # 2 counted + B
    # when the excused lesson itself is the counted one, nothing is added
    assert less(date(2026, 10, 5), [package], counted, lesson_counted=True)["lessons_left"] == 1
    # the flag is the caller's word about THIS lesson's own entry (decided by its attendance row, never by its date):
    # a lesson whose excuse is confirmed has no entry, so it is added even though ANOTHER lesson of the day has one
    assert less(date(2026, 10, 5), [package], counted, lesson_counted=False)["lessons_left"] == 0
    assert less(date(2026, 10, 5), [package], [date(2026, 10, 2)], lesson_counted=False)["lessons_left"] == 1


def test_two_lessons_of_one_day_across_two_packages_go_by_the_rank_of_the_lesson():
    # package 1 (2 lessons) is closed by the FIRST lesson of 05.10, package 2 (3 lessons) is opened by the SECOND
    first, second = rec(date(2026, 10, 1), lessons=2, n=1), rec(date(2026, 10, 3), lessons=3, n=2)
    counted = [date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 5)]

    def at(rank, flag=True, pool=counted):
        return excuse_deadline(lesson_date=date(2026, 10, 5), today=TODAY, payments=[first, second], counted_dates=pool,
                               lesson_counted=flag, lesson_rank=rank, deadline_days=5, deadline_lessons=1)

    assert at(0) == state(0, True, False, True)                     # package 1: 2 - 2 = 0 left, overdue (N = 1)
    assert at(1) == state(2, False, False, False)                   # package 2: 3 - 1 = 2 left
    assert at(None) == at(0)                                        # rank unknown: the earliest package of the date
    # a lesson that is not counted yet is added on top: its rank says where in the day it falls
    assert at(0, False, counted[:2]) == state(0, True, False, True)  # the earlier of the day: it closes package 1
    assert at(1, False, counted[:2]) == state(2, False, False, False)
    assert at(2, False, counted)["lessons_left"] == 1               # a third lesson that day: package 2 again (3 - 2)
    # a rank beyond what the packages took is not covered at all (a month-end date, not a package)
    small = [rec(date(2026, 10, 1), lessons=1, n=1), rec(date(2026, 10, 3), lessons=1, n=2)]
    result = excuse_deadline(lesson_date=date(2026, 10, 5), today=TODAY, payments=small, counted_dates=[date(2026, 10, 5)] * 2,
                             lesson_counted=True, lesson_rank=1, deadline_days=5, deadline_lessons=1)
    assert result["kind"] == "lessons" and result["lessons_left"] == 0   # rank 1 is in package 2 (its only lesson)
    third = excuse_deadline(lesson_date=date(2026, 10, 5), today=TODAY, payments=small, counted_dates=[date(2026, 10, 5)] * 2,
                            lesson_counted=False, lesson_rank=2, deadline_days=5, deadline_lessons=1)
    assert third["kind"] == "date" and third["date"] == "2026-10-31"     # no package has a place for the 3rd lesson


def test_a_lesson_that_no_package_took_is_treated_like_no_payment():
    full = rec(date(2026, 9, 1), lessons=1)
    # the package took 03.09; 20.09 lies beyond the paid lessons: the month end of the lesson, a "date" deadline
    expected = {"kind": "date", "date": "2026-09-30", "overdue": True, "days_left": -15}
    assert less(date(2026, 9, 20), [full], [date(2026, 9, 3), date(2026, 9, 20)]) == expected
    assert less(date(2026, 9, 20), [full], [date(2026, 9, 3), date(2026, 9, 20)], threshold=0) == expected
    # an unfinished lesson after a full package is not covered either (this month: not yet overdue)
    after = less(date(2026, 10, 8), [rec(date(2026, 10, 1), lessons=1)], [date(2026, 10, 2)], lesson_counted=False)
    assert after == {"kind": "date", "date": "2026-10-31", "overdue": False, "days_left": 16}
    # two full packages, the lesson is beyond both
    two_full = [rec(date(2026, 9, 1), lessons=1, n=1), rec(date(2026, 9, 5), lessons=1, n=2)]
    assert less(date(2026, 9, 20), two_full, [date(2026, 9, 3), date(2026, 9, 6), date(2026, 9, 20)])["kind"] == "date"
    later = rec(date(2026, 10, 10), lessons=5)  # starts after the lesson: no package covers it
    assert less(date(2026, 9, 20), [later], [date(2026, 9, 20)])["kind"] == "date"


def test_a_calendar_payment_beats_a_lessons_package_for_the_same_day():
    result = excuse_deadline(
        lesson_date=date(2026, 10, 3), today=TODAY,
        payments=[rec(date(2026, 10, 1), lessons=3), rec(date(2026, 9, 25), date(2026, 10, 24), n=1)],
        counted_dates=[date(2026, 10, 3)], deadline_days=5, deadline_lessons=2,
    )
    assert result["kind"] == "date" and result["date"] == "2026-10-19"


# ---- through the API --------------------------------------------------------------


def listing(w, **params):
    params = {"status": "all", "date_from": (business_today() - timedelta(days=60)).isoformat(),
              "date_to": business_today().isoformat(), **params}
    r = make_client(w).get(f"{API}/org/attendance/excuses", params=params)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def deadline_of(w, student=None):
    sid = str((student or w.students[0]).id)
    found = [i for i in listing(w) if i["student"]["id"] == sid]
    assert len(found) == 1
    return found[0]["deadline"]


@needs_pg
def test_calendar_payment_deadline_through_the_api(world):
    excused_lesson(world, offset_days=3)
    pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(20))
    assert deadline_of(world) == {"kind": "date", "date": day(15).isoformat(), "overdue": False, "days_left": 15}


@needs_pg
def test_lesson_in_the_last_days_of_the_paid_period_uses_valid_to(world):
    excused_lesson(world, offset_days=1)
    pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(3))   # valid_to - 5 = day(-2) < lesson day(-1)
    assert deadline_of(world) == {"kind": "date", "date": day(3).isoformat(), "overdue": False, "days_left": 3}


@needs_pg
def test_overdue_and_last_day_through_the_api(world):
    excused_lesson(world, offset_days=3)
    payment = pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(-1))["payment"]
    assert deadline_of(world) == {"kind": "date", "date": day(-1).isoformat(), "overdue": True, "days_left": -1}
    world.db.query(__import__("src.models.auth", fromlist=["StudentPayment"]).StudentPayment).filter_by(
        id=payment["id"]).update({"valid_to": day(0)})
    world.db.commit()
    assert deadline_of(world) == {"kind": "date", "date": day(0).isoformat(), "overdue": False, "days_left": 0}


@needs_pg
def test_cutoff_day_day_before_and_valid_to_through_the_api(world):
    from src.models.auth import StudentPayment

    excused_lesson(world, offset_days=1)   # the lesson is on day(-1)
    payment = pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(4))["payment"]   # cutoff = day(-1)
    assert deadline_of(world) == {"kind": "date", "date": day(4).isoformat(), "overdue": False, "days_left": 4}   # on the cutoff day
    world.db.query(StudentPayment).filter_by(id=payment["id"]).update({"valid_to": day(5)})                       # cutoff day(0): the lesson is the day before
    world.db.commit()
    assert deadline_of(world) == {"kind": "date", "date": day(1).isoformat(), "overdue": False, "days_left": 1}   # cutoff day(0) < lesson + 2 days
    world.db.query(StudentPayment).filter_by(id=payment["id"]).update({"valid_to": day(-1)})                      # the lesson is on valid_to
    world.db.commit()
    assert deadline_of(world) == {"kind": "date", "date": day(-1).isoformat(), "overdue": True, "days_left": -1}


@needs_pg
def test_two_lessons_on_one_day_count_both_through_the_api(world):
    from src.auth.schemas import StartAttendanceRequest
    from tests.pg_world import LIVE_SLOT

    s0, s1, s2 = world.students
    set_settings(world, period_mode="lessons", lessons_per_payment=3)
    pay(world, s0, paid_on=day(-10), valid_from=day(-10))
    d = lesson_marks(world, 7)                       # a completed lesson on day(-7): counted
    world.svc.reconcile_missed_group_attendance(      # the second lesson of the same day, unfinished
        str(world.org.id), str(world.group.id),
        StartAttendanceRequest(lessonDate=d, lessonId=LIVE_SLOT),
        reconciled_by=str(world.admin.id), is_org_admin=True,
    )
    save(world, d, LIVE_SLOT, [(s0, "excused"), (s1, "present"), (s2, "present")],
         status="in_progress", excuseReasons={s0.id: "Болел"})
    # 3 lessons in the package: one completed + the excused one = 2 charged, 1 left
    assert deadline_of(world) == state(1, True, False, True)


def same_day_lessons(w, offset, excused_in="first", reason="Болел"):
    """Two completed lessons of the group on ONE day; the first student is excused (pending) in one of them.

    ``excused_in``: ``"first"`` = the regular slot of the weekday (10:00), ``"second"`` = LIVE_SLOT (00:00, so it is the
    EARLIER lesson of the day), ``"both"`` = an excuse in each.
    Returns the day.  The attendance rows differ only by their lesson id - which is how the system tells them apart.
    """
    from src.auth.schemas import StartAttendanceRequest
    from tests.pg_world import LIVE_SLOT

    s0, s1, s2 = w.students
    d, slot = reconcile(w, offset)
    for lesson_id, excuse in ((slot, excused_in in ("first", "both")), (LIVE_SLOT, excused_in in ("second", "both"))):
        if lesson_id != slot:
            w.svc.reconcile_missed_group_attendance(
                str(w.org.id), str(w.group.id), StartAttendanceRequest(lessonDate=d, lessonId=lesson_id),
                reconciled_by=str(w.admin.id), is_org_admin=True,
            )
        extra = {"excuseReasons": {s0.id: reason}} if excuse else {}
        save(w, d, lesson_id, [(s0, "excused" if excuse else "present"), (s1, "present"), (s2, "present")],
             status="completed", **extra)
    return d


@needs_pg
def test_deadline_days_come_from_the_settings(world):
    excused_lesson(world, offset_days=3)
    pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(20))
    assert set_settings(world, excuse_deadline_days=0)["excuse_deadline_days"] == 0
    assert deadline_of(world)["date"] == day(20).isoformat()
    set_settings(world, excuse_deadline_days=10)
    assert deadline_of(world)["date"] == day(10).isoformat()
    set_settings(world, excuse_deadline_days=30)       # the lesson is inside the last 30 days: valid_to
    assert deadline_of(world)["date"] == day(20).isoformat()


@needs_pg
def test_no_payment_means_the_last_day_of_the_lesson_month(world):
    day_, _ = excused_lesson(world, offset_days=2)
    expected = month_end(day_)
    result = deadline_of(world)
    assert result["kind"] == "date" and result["date"] == expected.isoformat()
    assert result["overdue"] is False and result["days_left"] == (expected - business_today()).days


@needs_pg
def test_a_voided_payment_does_not_count(world):
    excused_lesson(world, offset_days=3)
    payment = pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(20))["payment"]
    assert deadline_of(world)["date"] == day(15).isoformat()
    r = owner(world).post(f"{API}/org/payments/{payment['id']}/void", json={"reason": "Внесено по ошибке"})
    assert r.status_code == 200, r.text
    assert deadline_of(world)["date"] == month_end(business_today() - timedelta(days=3)).isoformat()


@needs_pg
def test_payment_of_another_group_membership_is_ignored(world):
    excused_lesson(world, offset_days=3)
    other = make_group(world.db, world.org, world.teacher, "Вторая")
    world.db.commit()
    from src.models.auth import StudentGroup
    world.db.add(StudentGroup(student_id=world.students[0].id, group_id=other.id, is_active=True,
                              joined_at=__import__("datetime").datetime.now() - timedelta(days=60)))
    world.db.commit()
    pay(world, world.students[0], group=other, paid_on=day(-30), valid_from=day(-30), valid_to=day(20))
    assert deadline_of(world)["date"] == month_end(business_today() - timedelta(days=3)).isoformat()


def lesson_marks(w, offset, marks=None, reasons=None):
    day_, lesson_id = reconcile(w, offset)
    full = {s: "present" for s in w.students}
    full.update(marks or {})
    extra = {"excuseReasons": {s.id: r for s, r in (reasons or {}).items()}} if reasons else {}
    save(w, day_, lesson_id, list(full.items()), status="completed", **extra)
    return day_


@needs_pg
def test_lessons_package_countdown_through_the_api(world):
    s0 = world.students[0]
    set_settings(world, period_mode="lessons", lessons_per_payment=3, excuse_deadline_lessons=1)
    pay(world, s0, paid_on=day(-10), valid_from=day(-10))
    lesson_marks(world, 3, {s0: "excused"}, reasons={s0: "Болел"})
    assert deadline_of(world) == state(2, False, False, False)   # 2 left > N=1
    lesson_marks(world, 2)
    assert deadline_of(world) == state(1, True, True, False)      # 1 left = N: the last lesson to confirm
    lesson_marks(world, 1)
    assert deadline_of(world) == state(0, True, False, True)      # 0 left < N: overdue


@needs_pg
def test_unfinished_excused_lesson_counts_against_the_package(world):
    s0 = world.students[0]
    set_settings(world, period_mode="lessons", lessons_per_payment=3)
    pay(world, s0, paid_on=day(-10), valid_from=day(-10))
    excused_lesson(world, offset_days=2)   # in progress: not counted yet, but it would be
    assert deadline_of(world) == state(2, True, True, False)   # 2 left = the default N=2


@needs_pg
def test_lessons_fifo_picks_the_package_that_took_the_lesson(world):
    s0 = world.students[0]
    set_settings(world, period_mode="lessons")
    pay(world, s0, paid_on=day(-20), valid_from=day(-20), lessons_count=1)
    pay(world, s0, paid_on=day(-6), valid_from=day(-6), lessons_count=4)
    lesson_marks(world, 10)                                   # fills the first package
    lesson_marks(world, 3, {s0: "excused"}, reasons={s0: "Болел"})
    assert deadline_of(world) == state(3, False, False, False)


@needs_pg
def test_lesson_beyond_the_paid_lessons_falls_back_to_the_month_end(world):
    s0 = world.students[0]
    set_settings(world, period_mode="lessons")
    pay(world, s0, paid_on=day(-20), valid_from=day(-20), lessons_count=1)
    lesson_marks(world, 10)                                   # the only paid lesson
    d = lesson_marks(world, 3, {s0: "excused"}, reasons={s0: "Болел"})   # no package took this one
    expected = month_end(d)
    result = deadline_of(world)
    assert result["kind"] == "date" and result["date"] == expected.isoformat()
    assert result["overdue"] is (business_today() > expected)
    assert set(result) == {"kind", "date", "overdue", "days_left"}


@needs_pg
def test_small_package_threshold_through_the_api(world):
    s0 = world.students[0]
    set_settings(world, period_mode="lessons", excuse_deadline_lessons=2)
    pay(world, s0, paid_on=day(-10), valid_from=day(-10), lessons_count=2)     # effective N = 1
    lesson_marks(world, 3, {s0: "excused"}, reasons={s0: "Болел"})
    assert deadline_of(world) == state(1, True, True, False)                    # the first lesson is due now, not overdue
    lesson_marks(world, 2)
    assert deadline_of(world) == state(0, True, False, True)                    # package used up


@needs_pg
def test_decided_excuses_have_no_deadline_and_a_reset_one_gets_it_back(world):
    excused_lesson(world, offset_days=3)
    item = listing(world)[0]
    assert item["deadline"] is not None
    client = make_client(world)
    r = client.post(f"{API}/org/attendance/excuses/{item['id']}/review", json={"decision": "confirmed"})
    assert r.status_code == 200 and r.json()["deadline"] is None
    assert listing(world)[0]["deadline"] is None
    r = client.post(f"{API}/org/attendance/excuses/{item['id']}/reset")
    assert r.status_code == 200 and r.json()["deadline"]["kind"] == "date"


@needs_pg
def test_deadline_exposes_no_money(world):
    excused_lesson(world, offset_days=3)
    pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(20), amount=987654, note="секретная сумма")
    body = json.dumps(make_client(world).get(
        f"{API}/org/attendance/excuses",
        params={"status": "all", "date_from": day(-60).isoformat(), "date_to": day(0).isoformat()},
    ).json(), ensure_ascii=False)
    assert "987654" not in body and "amount" not in body and "секретная" not in body
    assert set(deadline_of(world)) == {"kind", "date", "overdue", "days_left"}


@needs_pg
def test_deadlines_are_batch_loaded(world):
    from sqlalchemy import event

    s0, s1, _ = world.students
    pay(world, s0, paid_on=day(-30), valid_from=day(-30), valid_to=day(20))
    set_settings(world, period_mode="lessons")
    pay(world, s1, paid_on=day(-30), valid_from=day(-30), lessons_count=12)

    def statements_for_list():
        world.db.expire_all()
        statements = []
        engine = world.db.get_bind()

        def record(conn, cursor, statement, *a):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            items = listing(world)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return items, len(statements)

    for offset in (1, 2):   # both a calendar and a lessons-mode excuse already: all loaders run
        day_, lesson_id = reconcile(world, offset)
        save(world, day_, lesson_id, [(s0, "excused"), (s1, "excused"), (world.students[2], "present")],
             status="completed", excuseReasons={s0.id: "Болел", s1.id: "Болел"})
    small_items, small = statements_for_list()
    for offset in range(3, 9):
        day_, lesson_id = reconcile(world, offset)
        save(world, day_, lesson_id, [(s0, "excused"), (s1, "excused"), (world.students[2], "present")],
             status="completed", excuseReasons={s0.id: "Болел", s1.id: "Болел"})
    big_items, big = statements_for_list()
    assert len(big_items) == 16 > len(small_items) == 4
    assert big == small, (small, big)
    assert all(i["deadline"] and i["deadline"]["kind"] in ("date", "lessons") for i in big_items)


# ---- a payment that moved to another group (N13 P2.1) ----------------------------------


def moved(record, day, late=0):
    """Moved at 15:00 Tashkent (10:00 UTC) of ``day``."""
    return ps.PaymentRecord(
        paid_on=record.paid_on, valid_from=record.valid_from, valid_to=record.valid_to,
        lessons_count=record.lessons_count, created_at=record.created_at,
        carried_out_at=__import__("datetime").datetime(day.year, day.month, day.day, 10, 0), carried_out_late=late,
    )


def test_a_calendar_payment_that_moved_ends_on_the_transfer_day_for_the_deadline():
    payment = moved(rec(date(2026, 9, 1), date(2026, 10, 31)), date(2026, 10, 15))   # ends 15.10 here now
    result = excuse_deadline(lesson_date=date(2026, 10, 10), today=TODAY, payments=[payment],
                             deadline_days=5, deadline_lessons=2)
    assert result == {"kind": "date", "date": "2026-10-15", "overdue": False, "days_left": 0}
    # the lesson of the transfer day itself (it started before the instant) is covered too
    same_day = excuse_deadline(lesson_date=date(2026, 10, 15), today=TODAY, payments=[payment],
                               deadline_days=5, deadline_lessons=2)
    assert same_day["date"] == "2026-10-15"
    # a lesson after the move day is not covered by it at all: month end
    late = excuse_deadline(lesson_date=date(2026, 10, 16), today=TODAY, payments=[payment],
                           deadline_days=5, deadline_lessons=2)
    assert late["date"] == "2026-10-31" and late["kind"] == "date"
    assert month_end(date(2026, 10, 16)).isoformat() == "2026-10-31"


def test_a_lessons_package_that_moved_has_nothing_left_for_the_deadline():
    package = moved(rec(date(2026, 9, 1), lessons=12), date(2026, 10, 15))
    taken = [date(2026, 9, 5), date(2026, 9, 8), date(2026, 9, 10)]
    result = excuse_deadline(lesson_date=date(2026, 9, 10), today=TODAY, payments=[package],
                             counted_dates=taken, lesson_counted=True, deadline_days=5, deadline_lessons=2)
    assert result["kind"] == "lessons" and result["lessons_left"] == 0 and result["overdue"] is True
    # lessons after the move day are outside the package: nothing covers them
    outside = excuse_deadline(lesson_date=date(2026, 10, 20), today=TODAY, payments=[package],
                              counted_dates=taken, lesson_counted=False, deadline_days=5, deadline_lessons=2)
    assert outside["kind"] == "date" and outside["date"] == "2026-10-31"


def test_a_moved_package_is_measured_against_the_live_remainder_of_its_chain():
    base = rec(date(2026, 9, 1), lessons=12)
    package = ps.PaymentRecord(
        paid_on=base.paid_on, valid_from=base.valid_from, lessons_count=12, created_at=base.created_at,
        carried_out_at=__import__("datetime").datetime(2026, 10, 15, 10, 0), moved_left=2, moved_size=12,
    )
    taken = [date(2026, 9, 5), date(2026, 9, 8)]
    result = excuse_deadline(lesson_date=date(2026, 9, 8), today=TODAY, payments=[package], counted_dates=taken,
                             lesson_counted=True, deadline_days=5, deadline_lessons=2)
    assert result == {"kind": "lessons", "lessons_left": 2, "urgent": True, "due_now": True, "overdue": False}
    more = ps.PaymentRecord(**{**package.__dict__, "moved_left": 7})
    assert excuse_deadline(lesson_date=date(2026, 9, 8), today=TODAY, payments=[more], counted_dates=taken,
                           lesson_counted=True, deadline_days=5, deadline_lessons=2)["urgent"] is False
