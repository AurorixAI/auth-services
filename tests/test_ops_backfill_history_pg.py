"""The O1 backfill of ``validFrom`` (migration lb1a2b3c4d5) must not change history (lead, 22.10, after the stage-copy rehearsal).

A legacy slot gets ``validFrom`` = the business date the group was created, so the lessons the old code generated for it are generated unchanged:
the ledger (the proration of a partial first period counts the scheduled lessons) and «Незакрытые» are identical before and after the migration step.
The first version (first attendance row / the day of the migration) moved memberships from lessons to days and dropped the past lessons of a slot without rows.
"""
import importlib.util
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from src.models.auth import Organization, StudentPayment

from tests.ops_helpers import build_org, client, clock, history_row, needs_pg, new_group, new_student, pg_session  # noqa: F401
from tests.pg_world import give_group_price

pytestmark = [needs_pg, pytest.mark.legacy_valid_from]          # the real runtime fallback of a slot without validFrom (= the creation day of the group)
API = "/api/v1/auth"


def migration():
    path = next((Path(__file__).resolve().parents[1] / "alembic" / "versions").glob("lb1a2b3c4d5_*.py"))
    spec = importlib.util.spec_from_file_location("lb1_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def legacy_slot(lesson_id, dow, start, end):
    return {"id": lesson_id, "dayOfWeek": dow, "startTime": start, "endTime": end, "subject": "Математика", "room": None, "format": "offline", "color": "blue", "topic": None}


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")
    world = build_org(pg_session)
    db = pg_session
    org = db.get(Organization, world.org.id)
    org.settings = {"payments": {"accounting_start_date": "2026-08-01", "billing_anchor": "fixed_day", "billing_day": 1}}
    # a group of August: Monday 10:00 with attendance from September, Saturday 12:00 that never got a row; no validFrom (the old schema)
    world.G = new_group(db, world.org, world.ids, "GH", "Старая", world.A, [legacy_slot("slot-mon", 1, "10:00", "11:00"), legacy_slot("slot-sat", 6, "12:00", "13:00")],
                        created=datetime(2026, 8, 1, 5, 0))
    for slot in world.G.schedule:
        slot.pop("validFrom", None)
    world.early = new_student(db, world.org, world.ids, "early", "Ранний", "Ученик", world.G, joined=datetime(2026, 8, 20, 5, 0))     # a partial first period in August
    world.late = new_student(db, world.org, world.ids, "late", "Поздний", "Ученик", world.G, joined=datetime(2026, 9, 2, 5, 0))
    for day in ("2026-09-07", "2026-09-14", "2026-09-21", "2026-09-28", "2026-10-05", "2026-10-12", "2026-10-19"):                       # attendance starts in September
        history_row(db, world.org, world.G, "slot-mon", day, world.A, [world.early, world.late], start="10:00", end="10:55")
    give_group_price(db, world.G, 600_000, valid_from=date(2026, 1, 1))
    db.add(StudentPayment(org_id=world.org.id, membership_id=_membership(db, world.early, world.G).id, student_id=world.early.id, group_id=world.G.id, amount=700_000,
                          paid_on=date(2026, 8, 21), created_by=world.owner.id, method="cash", created_at=datetime(2026, 8, 21, 9)))
    db.commit()
    return world


def _membership(db, student, group):
    from src.models.auth import StudentGroup

    return db.query(StudentGroup).filter_by(student_id=student.id, group_id=group.id).one()


def ledger(w):
    out = {}
    for student in (w.early, w.late):
        rows = client(w, "owner").get(f"{API}/org/students/{student.id}/memberships").json()
        out[student.last_name] = [(r["debt"], r["advance"], r["paid_until"], r["status"], r["past_debt"], r["current_due"]) for r in rows]
    return out


def open_actions(w):
    got = client(w, "admin").get(f"{API}/org/attendance/open-actions")
    assert got.status_code == 200, got.text
    return sorted((i["lessonId"], i["lessonDate"], i["kind"] if "kind" in i else i.get("type")) for i in got.json() if i["groupId"] == str(w.G.id))


def run_backfill(w):
    w.db.commit()
    migration()._backfill_schedule(w.db.connection())
    w.db.commit()
    w.db.expire_all()


def test_the_backfill_changes_neither_the_ledger_nor_the_unclosed_lessons(w):
    before_ledger, before_open = ledger(w), open_actions(w)
    assert before_open and any(lid == "slot-sat" for lid, _d, _k in before_open)          # the Saturday slot has no rows: its past lessons are "missed"
    assert any(row[0] > 0 for rows in before_ledger.values() for row in rows)              # the money is not trivially zero
    run_backfill(w)
    schedule = w.db.execute(text("SELECT schedule FROM groups WHERE id=:g"), {"g": w.G.id}).scalar_one()
    assert {s["id"]: s["validFrom"] for s in schedule} == {"slot-mon": "2026-08-01", "slot-sat": "2026-08-01"}      # the creation day, not the first row (07.09) / the migration day
    assert ledger(w) == before_ledger
    assert open_actions(w) == before_open
    run_backfill(w)                                                                        # idempotent
    assert ledger(w) == before_ledger and open_actions(w) == before_open


def test_control_the_first_rule_would_have_changed_the_money_and_dropped_missed_lessons(w):
    """The rule the rehearsal rejected (validFrom = first row of the slot, else the migration day) really differs: the test above is not vacuous."""
    before_ledger, before_open = ledger(w), open_actions(w)
    schedule = [dict(s) for s in w.G.schedule]
    for slot in schedule:
        slot["validFrom"] = "2026-09-07" if slot["id"] == "slot-mon" else "2026-10-21"
    w.G.schedule = schedule
    w.db.commit()
    w.db.expire_all()
    assert ledger(w) != before_ledger
    assert len(open_actions(w)) < len(before_open)


def test_a_group_without_a_creation_date_falls_back_to_the_earliest_evidence(w):
    """``created_at`` missing: the earliest of the first attendance row and the first membership ``joined_at`` (never the migration day)."""
    w.db.execute(text("UPDATE groups SET created_at = NULL WHERE id=:g"), {"g": w.G.id})
    w.db.commit()
    run_backfill(w)
    schedule = w.db.execute(text("SELECT schedule FROM groups WHERE id=:g"), {"g": w.G.id}).scalar_one()
    assert {s["validFrom"] for s in schedule} == {"2026-08-20"}                           # first membership 20.08 (Tashkent) < first row 07.09


def test_a_slot_that_has_valid_from_is_kept(w):
    schedule = [dict(s) for s in w.G.schedule]
    schedule[0]["validFrom"] = "2026-10-01"
    w.G.schedule = schedule
    w.db.commit()
    run_backfill(w)
    got = {s["id"]: s["validFrom"] for s in w.db.execute(text("SELECT schedule FROM groups WHERE id=:g"), {"g": w.G.id}).scalar_one()}
    assert got == {"slot-mon": "2026-10-01", "slot-sat": "2026-08-01"}
