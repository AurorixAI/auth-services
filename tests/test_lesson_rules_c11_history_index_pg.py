"""LESSON-RULES С11 п.1: the list «История» reads the journal of attendance BY ITS INDEX (``ix_gaa_org_done_corrected``, partial: ``action IN ('updated', 'corrected')``).

Hand: the organisation of the test has 3 lessons completed late (3 hits) among 4 000 lines ``started`` (not in the index) and 6 000 lines ``updated`` of the OTHER centres.  The branch of the list says
``action IN ('updated', 'corrected')`` itself, so the planner may use the partial index (it needs the condition of the query to imply the condition of the index).  The plan of the page and of the count of
the list «История» must name the index and must not read the table of the journal in a sequence."""
from sqlalchemy import event, text

from tests.lesson_rules_helpers import marks_of, reconcile, save, world, clock, pg_session, needs_pg, client, API  # noqa: F401

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"


def _noise(db, w, group, attendance_id, other_org_id, other_group_id, other_actor_id, other_att):
    """4 000 lines ``started`` of the organisation (hit nothing) and 6 000 ``updated`` of another centre; spread over the last 20 days."""
    db.execute(text(
        "INSERT INTO group_attendance_audit (id, attendance_id, lesson_date, lesson_id, org_id, group_id, actor_id, action, before_status, after_status, after_records, created_at) "
        "SELECT gen_random_uuid(), :att, '2026-11-20', 'lesson-mon', :org, :grp, :actor, 'started', NULL, 'in_progress', '{}'::jsonb, now() - (g % 20) * interval '1 day' FROM generate_series(1, 4000) g"),
        {"att": attendance_id, "org": w.org.id, "grp": group.id, "actor": w.A.id})
    db.execute(text(
        "INSERT INTO group_attendance_audit (id, attendance_id, lesson_date, lesson_id, org_id, group_id, actor_id, action, before_status, after_status, after_records, created_at) "
        "SELECT gen_random_uuid(), :att, '2026-11-20', 'lesson-mon', :org, :grp, :actor, 'updated', 'in_progress', 'completed', '{}'::jsonb, now() - (g % 20) * interval '1 day' FROM generate_series(1, 6000) g"),
        {"att": other_att, "org": other_org_id, "grp": other_group_id, "actor": other_actor_id})
    db.commit()
    db.execute(text("ANALYZE group_attendance_audit"))
    db.commit()


def test_the_history_reads_the_journal_of_attendance_by_its_partial_index(pg_session, clock):
    w, g, people, members = world(pg_session, clock)
    for day in ("2026-11-23", "2026-11-25"):                         # 3 lessons completed on a later day (here 2; the third below)
        reconcile(w, g, day, lesson="lesson-mon" if day == "2026-11-23" else "lesson-wed")
        save(w, g, people, day, lesson="lesson-mon" if day == "2026-11-23" else "lesson-wed", marks=marks_of(people, first_present=2))
    w2, g2, p2, m2 = world(pg_session, clock, salt="other")
    from src.models.auth import GroupAttendance
    att = pg_session.query(GroupAttendance).filter_by(group_id=g.id).first()
    att2 = pg_session.query(GroupAttendance).filter_by(group_id=g2.id).first()
    other_att = att2.id if att2 else att.id
    _noise(pg_session, w, g, att.id, w2.org.id, g2.id, w2.A.id, other_att)

    plans = []
    engine = pg_session.get_bind()

    def hook(conn, cursor, statement, params, context, executemany):
        if "group_attendance_audit" in statement and statement.lstrip()[:6].upper() == "SELECT" and "UNION ALL" in statement:
            plans.append((statement, params))
    event.listen(engine, "before_cursor_execute", hook)
    try:
        got = client(w, "owner").get(SUB, params={"status": "decided"})
    finally:
        event.remove(engine, "before_cursor_execute", hook)
    assert got.status_code == 200 and got.json()["total"] == 2, got.text          # hand: two entries «оформлен задним числом» (23.11 on 26.11 = 3 days; 25.11 = 1 day)
    assert plans, "the union of the history was not seen"
    for statement, params in plans:
        with engine.connect() as conn:
            plan = "\n".join(r[0] for r in conn.exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS) " + statement, params))
        import os
        if os.environ.get("C11_PLAN_OUT"):           # for the report: the plan of each statement
            with open(os.environ["C11_PLAN_OUT"], "a") as f:
                f.write("--- PLAN ---\n" + plan + "\n")
        assert "ix_gaa_org_done_corrected" in plan, plan
        assert "Seq Scan on group_attendance_audit" not in plan, plan
