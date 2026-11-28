"""LESSON-RULES FIX4 А4: the words of the answers.  (1) The history says WHO formalised a lesson afterwards («оформление: …») when an administrator / the owner did it.  (2) The line «Вид отмены
изменён» names the lesson (group, date, time), what became of what, why and who.  (3) A lesson that was given back is ONE row in the block of the teacher whoever cancelled it (the server
holds the rule).  (4) The dry run of the confirmation «праздник» says «Праздник — перерасчёта нет», not «никто не платит».

World: ``lesson_rules_helpers.world`` (group «Г» of the teacher A «Иванов И.»: Mon / Wed 10:00-11:30, nine students, today Thu 26.11.2026 12:00, Mon 23.11 is three days ago)."""
from tests.lesson_rules_helpers import MON, PAST_MON, WED, marks_of, reconcile, save, set_kind, teacher_cancels, world, clock, pg_session, needs_pg, client, API  # noqa: F401

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"


def listing(w, who="owner", **params):
    r = client(w, who).get(SUB, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def short(user):
    return f"{user.last_name} {user.first_name[0]}."


# ============================================================================================ (1) «оформление: <кто>»
def test_the_history_says_who_formalised_the_lesson_when_it_was_an_administrator_or_the_owner(pg_session, clock):
    """Hand: Wed 25.11 is formalised and completed on 26.11 by the teacher A himself: «…на 1 день позже даты урока» and nothing more.  Mon 23.11 is formalised and completed on 26.11 by the
    administrator (the right of money): «…на 3 дня позже даты урока; оформление: <the administrator, Фамилия И.>»."""
    w, g, people, members = world(pg_session, clock, money_right=True)
    reconcile(w, g, "2026-11-25")
    save(w, g, people, "2026-11-25", marks=marks_of(people, first_present=2))
    reconcile(w, g, PAST_MON, MON, who="admin")
    save(w, g, people, PAST_MON, MON, who="admin", marks=marks_of(people, first_present=1))
    texts = {i["lesson"]["date"]: i["text"] for i in listing(w, status="decided")["items"] if i.get("source") == "audit" and i["kind"] == "backdated"}
    assert texts == {"2026-11-25": "Урок оформлен задним числом: на 1 день позже даты урока",
                     PAST_MON: f"Урок оформлен задним числом: на 3 дня позже даты урока; оформление: {short(w.admin)}"}


# ============================================================================================ (2) «Вид отмены изменён»
def _kind_lines(w):
    return [i["decision"]["closedReasonText"] for i in listing(w, status="decided")["items"] if i.get("source") == "request" and i["decision"] and i["decision"]["closedReason"] == "KIND_CHANGED"]


def test_the_line_of_a_changed_kind_names_the_lesson_what_became_of_what_why_and_who(pg_session, clock):
    """Hand: the teacher A reports Mon 23.11 (waits).  The owner gives the day off 23.11: «ждёт подтверждения → праздник», why «выходной день 23.11», by the owner.  He voids the day off: back to
    waiting, why «выходной день 23.11 отменён».  He confirms «по вине центра», then changes it by hand to «праздник» with the reason «Ошибка в выходном»: «по вине центра → праздник», why - his words."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    owner = short(w.owner)
    r = client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": PAST_MON, "dateTo": PAST_MON, "reason": "Хайит"})
    assert r.status_code == 201, r.text
    assert _kind_lines(w) == [f"Вид отмены изменён: Г, 23.11 10:00–11:30: ждёт подтверждения → праздник. Причина: выходной день «Хайит» 23.11. Изменил: {owner}."]
    assert client(w, "owner").post(f"{API}/org/days-off/{r.json()['dayOff']['id']}/void").status_code == 200
    assert sorted(_kind_lines(w)) == sorted([f"Вид отмены изменён: Г, 23.11 10:00–11:30: ждёт подтверждения → праздник. Причина: выходной день «Хайит» 23.11. Изменил: {owner}.",
                                              f"Вид отмены изменён: Г, 23.11 10:00–11:30: праздник → ждёт подтверждения. Причина: выходной день «Хайит» 23.11 отменён. Изменил: {owner}."])
    set_kind(w, g, "centre")
    set_kind(w, g, "holiday", expected="centre", reason="Ошибка в выходном")
    assert f"Вид отмены изменён: Г, 23.11 10:00–11:30: по вине центра → праздник. Причина: Ошибка в выходном. Изменил: {owner}." in _kind_lines(w)


# ============================================================================================ (3) one row per returned lesson
def _rows_of_the_lesson(w, who="A"):
    items = client(w, who).get(f"{SUB}/mine", params={"status": "all"}).json()["items"]
    return [(i["status"], i["outcome"]) for i in items if i["lesson"]["date"] == PAST_MON and i["lesson"]["lessonId"] == MON]


def test_a_returned_lesson_is_one_row_of_the_teacher_whoever_cancelled_it(pg_session, clock):
    """Hand, three worlds: the lesson Mon 23.11 is cancelled (a) by the teacher «Урока не было» and returned by the owner («Урок был»); (b) by the OWNER himself («по вине центра») and given back to
    the mark; (c) by the decision of the teacher's request «Отменить урок» (taken on 20.11 while the lesson was ahead: «по вине центра» at once) and given back to the mark on 26.11.  The block of A lists the lesson ONCE each time: the cancellation with its outcome, no second row of the return."""
    wa, ga, pa, _ = world(pg_session, clock)
    teacher_cancels(wa, ga)
    assert client(wa, "owner").post(f"{API}/org/groups/{ga.id}/lessons/restore", json={"lessonId": MON, "lessonDate": PAST_MON, "reason": "Урок был"}).status_code == 200
    assert _rows_of_the_lesson(wa) == [("cancelled", "lesson_restored")]
    wb, gb, pb, _ = world(pg_session, clock)
    teacher_cancels(wb, gb, who="owner", cancellationKind="centre")
    r = client(wb, "owner").post(f"{API}/org/groups/{gb.id}/lessons/restore", json={"lessonId": MON, "lessonDate": PAST_MON, "reason": "Отменили зря", "expectedKind": "centre"})
    assert r.status_code == 200, r.text
    assert _rows_of_the_lesson(wb) == [("cancelled", "lesson_restored")]
    wc, gc, pc, _ = world(pg_session, clock)
    clock.set("2026-11-20 12:00")
    rid = client(wc, "A").post(SUB, json={"lessons": [{"groupId": str(gc.id), "lessonId": MON, "lessonDate": PAST_MON}], "reasonCode": "sick"}).json()["created"][0]["id"]
    assert client(wc, "owner").post(f"{SUB}/{rid}/decide", json={"decision": "cancel", "reason": "Заболел"}).status_code == 200
    clock.set("2026-11-26 12:00")
    r = client(wc, "owner").post(f"{API}/org/groups/{gc.id}/lessons/restore", json={"lessonId": MON, "lessonDate": PAST_MON, "reason": "Выздоровел", "expectedKind": "centre"})
    assert r.status_code == 200 and r.json()["returnedToReview"] is True, r.text          # decided on 20.11 while the lesson was ahead: «по вине центра» at once; now it has begun - back to the mark
    assert _rows_of_the_lesson(wc) == [("cancelled", "lesson_restored")]


# ============================================================================================ (4) the holiday has its own zero
def test_the_confirmation_holiday_says_holiday_not_nobody_pays(pg_session, clock):
    """Hand: a waiting cancellation, the dry run of «праздник»: nobody is recalculated BECAUSE it is a holiday: ``zero_reason`` holiday, the words of the server «Праздник — перерасчёта нет.»,
    total 0.  The dry run of «по вине центра» of the same lesson counts the deduction (9 x 50 000) - its zero reason stays what it was (none here)."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    d = set_kind(w, g, "holiday", dry_run=True).json()
    assert (d["total"], d["zero_reason"], d["zero_text"]) == (0, "holiday", "Праздник — перерасчёта нет.")
    c = set_kind(w, g, "centre", dry_run=True).json()
    assert (c["total"], c["zero_reason"]) == (450_000, None)
