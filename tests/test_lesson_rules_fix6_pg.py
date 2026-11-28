"""LESSON-RULES FIX6 (pack 8): the author of a record is who leads the lesson by the COMMON rule (``effective_teacher``: the row first); the name of a day off is a field of the event; one form of
the name in the notes of the journal (LESSONS 48).  World: ``lesson_rules_helpers.world`` (group «Г» of A «Иванов И.»: Mon / Wed 10:00-11:30, today Thu 26.11.2026 12:00); B «Петров П.»."""
from types import SimpleNamespace

from src.models.auth import LessonSubstitutionEvent
from tests.lesson_rules_helpers import MON, PAST_MON, marks_of, save, teacher_cancels, world, clock, pg_session, needs_pg, client, API  # noqa: F401
from tests.test_lesson_rules_fix4_a2_pg import mine
from tests.test_lesson_rules_fix5_pg import listing, rows_of

pytestmark = needs_pg


def test_a_lesson_formalised_with_b_as_who_taught_and_then_cancelled_is_the_record_of_b_not_of_a(pg_session, clock):
    """Hand: the administrator (the money right) formalises Mon 23.11 «провёл Б» (no assignment: the row has B), then on the started row saves «Урока не было».  The row names B, so the record of the
    cancellation is B's: his block has ONE cancelled row of the lesson, A's block has none; the owner's list names «Петров П.» as the teacher of the row."""
    w, g, people, members = world(pg_session, clock, money_right=True)
    r = client(w, "admin").post(f"{API}/org/groups/{g.id}/attendance/reconcile", json={"lessonDate": PAST_MON, "lessonId": MON, "taughtBy": str(w.B.id)})
    assert r.status_code == 200, r.text
    save(w, g, people, PAST_MON, MON, who="admin", status="cancelled", marks={}, cancellationReason="Урока не было")
    assert [s for s, _o in rows_of(w, "B") if s == "cancelled"] == ["cancelled"], rows_of(w, "B")
    assert [x for x in rows_of(w, "A") if x[0] == "cancelled"] == [], rows_of(w, "A")
    items = [i for i in listing(w, status="pending")["items"] + listing(w, status="decided")["items"] if i["lesson"]["date"] == PAST_MON and i.get("source") == "request"]
    assert items and all(i["teacher"]["name"] == "Петров П." for i in items), items


def _kind_lines(w):
    return [i["decision"]["closedReasonText"] for i in listing(w, status="decided")["items"]
            if i.get("source") == "request" and i["decision"] and i["decision"]["closedReason"] == "KIND_CHANGED"]


def test_the_name_of_a_day_off_with_quotes_inside_is_shown_whole_and_an_old_record_without_the_field_reads_as_before(pg_session, clock):
    """Hand: a day off «Навруз «весна»» on 23.11 turns a waiting cancellation into «праздник»: the line says «выходной день «Навруз «весна»» 23.11» (the name is a FIELD of the event, not cut from the text).
    An old record (the field removed by hand from the event) of a day off «Хайит» reads «выходной день «Хайит» 23.11» as before."""
    w, g, people, members = world(pg_session, clock)
    teacher_cancels(w, g)
    r = client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": PAST_MON, "dateTo": PAST_MON, "reason": "Навруз «весна»"})
    assert r.status_code == 201, r.text
    lines = _kind_lines(w)
    assert lines and all("выходной день «Навруз «весна»» 23.11" in t for t in lines), lines
    events = [e for e in w.db.query(LessonSubstitutionEvent).filter_by(org_id=w.org.id, action="closed") if (e.snapshot or {}).get("dayOffName")]
    assert events and all(e.snapshot["dayOffName"] == "Навруз «весна»" for e in events)

    w2, g2, p2, m2 = world(pg_session, clock)
    teacher_cancels(w2, g2)
    assert client(w2, "owner").post(f"{API}/org/days-off", json={"dateFrom": PAST_MON, "dateTo": PAST_MON, "reason": "Хайит"}).status_code == 201
    for e in w2.db.query(LessonSubstitutionEvent).filter_by(org_id=w2.org.id):
        if "dayOffName" in (e.snapshot or {}):
            e.snapshot = {k: v for k, v in e.snapshot.items() if k != "dayOffName"}
    w2.db.commit()
    lines = _kind_lines(w2)
    assert lines and all("выходной день «Хайит» 23.11" in t for t in lines), lines


def test_the_notes_of_the_journal_name_a_person_in_the_one_short_form():
    """Hand: ``_mark_name`` gives «Иванов И.» (last name + initial, the form of «задним числом» and of the history); only a last name - the last name; nobody - ``None`` (as ``_user_name``)."""
    from src.auth.attendance_excuses import _mark_name

    assert _mark_name(SimpleNamespace(first_name="Иван", last_name="Иванов")) == "Иванов И."
    assert _mark_name(SimpleNamespace(first_name="", last_name="Иванов")) == "Иванов"
    assert _mark_name(SimpleNamespace(first_name="Иван", last_name="")) == "Иван"
    assert _mark_name(SimpleNamespace(first_name="", last_name="")) is None and _mark_name(None) is None
