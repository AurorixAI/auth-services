"""Money-1 R3 #2: «open for ever» - an unsaved month is open back to the accounting start of the centre, and the list «Требует разбора», the sheet warnings, I-4 and the return
of «Урока не было» all see the same lessons (no calendar window).  Numbers: the world has a lesson EVERY day at 10:00 (slots ``s1``..``s7``), the clock is 04.11.2026 12:00,
the students are enrolled since 01.01.2026."""
from collections import Counter
from datetime import date

from src.auth.payroll_service import PayrollService
from src.auth.schemas import ReconcileMissedLessonRequest

from tests.money1_helpers import now_4_nov, reopen_now, sheet, tashkent  # noqa: F401  (fixtures)
from tests.pg_world import make_client, needs_pg, payments_block, pg_session, world  # noqa: F401  (fixtures)

pytestmark = needs_pg
API = "/api/v1/auth"


def missed(w, **kw):
    items = w.svc.get_open_attendance_actions(str(w.org.id), actor_id=None, is_org_admin=True, **kw)
    return [i for i in items if i.actionType == "missed" and i.lessonId.startswith("s")]          # not the all-day «live» slot of the world


def by_month(items):
    return dict(sorted(Counter(i.lessonDate[:7] for i in items).items()))


def test_the_list_reaches_back_to_the_accounting_start_not_only_to_last_month(world, now_4_nov):
    payments_block(world.db, world.org, accounting_start_date="2026-08-01")
    items = missed(world)
    assert by_month(items) == {"2026-08": 31, "2026-09": 30, "2026-10": 31, "2026-11": 4}          # before: October (31) + November (4) = 35
    assert min(i.lessonDate for i in items) == "2026-08-01" and len(items) == 96


def test_a_young_centre_loses_nothing_the_window_is_never_narrower_than_last_month(world, now_4_nov):
    payments_block(world.db, world.org, accounting_start_date="2026-10-20")
    assert by_month(missed(world)) == {"2026-10": 31, "2026-11": 4}


def test_a_closed_month_leaves_the_list_and_comes_back_when_it_is_reopened_the_sheet_still_names_it(world, now_4_nov):
    payments_block(world.db, world.org, accounting_start_date="2026-08-01")
    row = sheet(world, world.teacher, date(2026, 9, 1), tashkent(2026, 10, 3))                                    # September is saved after its end: closed
    assert by_month(missed(world)) == {"2026-08": 31, "2026-10": 31, "2026-11": 4}
    seen = missed(world, include_closed=True)
    assert by_month(seen) == {"2026-08": 31, "2026-09": 30, "2026-10": 31, "2026-11": 4}
    assert all(i.periodClosed for i in seen if i.lessonDate.startswith("2026-09")) and not any(i.periodClosed for i in seen if not i.lessonDate.startswith("2026-09"))
    named = PayrollService(world.db)._open_lessons(str(world.org.id), date(2026, 9, 1), date(2026, 9, 30))
    assert len([x for x in named[str(world.teacher.id)] if x["lessonId"].startswith("s")]) == 30                  # the closed sheet still names what is left
    assert {x["groupName"] for x in named[str(world.teacher.id)]} == {world.group.name}                           # R3 #26: with the name of the group - no second request for it
    reopen_now(world.db, world.org.id, world.teacher.id, "2026-09", world.owner.id, "Разбираем сентябрь")
    assert by_month(missed(world)) == {"2026-08": 31, "2026-09": 30, "2026-10": 31, "2026-11": 4}
    assert row.id is not None


def test_the_list_by_month_has_a_counter_in_each_month_and_in_total_oldest_first(world, now_4_nov):
    payments_block(world.db, world.org, accounting_start_date="2026-09-01")
    body = make_client(world, user=world.owner).get(f"{API}/org/attendance/open-actions/by-month").json()
    assert [m["month"] for m in body["months"]] == ["2026-09", "2026-10", "2026-11"]
    assert body["months"][0]["title"] == "Сентябрь 2026" and body["since"] == "2026-09-01"
    assert body["total"] == sum(m["count"] for m in body["months"]) == sum(len(m["items"]) for m in body["months"])
    assert all(m["count"] == m["missed"] + m["inProgress"] for m in body["months"])
    ordinary = [len([i for i in m["items"] if i["lessonId"].startswith("s")]) for m in body["months"]]            # the all-day «live» slot of the world is left out
    assert ordinary == [30, 31, 4]
    teacher = make_client(world, user=world.teacher).get(f"{API}/org/attendance/open-actions/by-month")
    assert teacher.status_code == 200 and teacher.json()["total"] == body["total"]                                  # the teacher of the group leads every lesson here


def test_a_cancelled_lesson_before_the_start_of_the_list_cannot_be_returned_to_it(world, now_4_nov):
    """«Урока не было» -> «Вернуть»: the lesson goes back to «Требует разбора»; if the list does not reach that day, it would become a ghost - refused with the reason."""
    payments_block(world.db, world.org, accounting_start_date="2026-08-01")
    for day in (date(2026, 7, 10), date(2026, 8, 12)):
        world.svc.cancel_missed_group_attendance(str(world.org.id), str(world.group.id), ReconcileMissedLessonRequest(lessonDate=day, lessonId=f"s{day.isoweekday()}", cancellationReason="Нажали по ошибке"),
                                                 cancelled_by=str(world.admin.id), is_org_admin=True, is_org_manager=True)
    owner = make_client(world, user=world.owner)
    url = f"{API}/org/groups/{world.group.id}/lessons/restore"
    early = owner.post(url, json={"lessonDate": "2026-07-10", "lessonId": "s5", "reason": "Урок был"})
    assert early.status_code == 409 and early.json()["detail"]["code"] == "RESTORE_BEFORE_START" and "начала учёта" in early.json()["detail"]["message"]
    ok = owner.post(url, json={"lessonDate": "2026-08-12", "lessonId": "s3", "reason": "Урок был"})
    assert ok.status_code == 200 and ok.json()["returnedToReview"] is True
    assert "2026-08-12" in {i.lessonDate for i in missed(world)}                                                   # and it IS in the list (before the round: not, it is older than last month)
