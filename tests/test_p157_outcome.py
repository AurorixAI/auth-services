"""#157 A11: the one ``outcome`` code of a record (P157_API.md §2) - every code, by the rules, without a database (the function is pure), and the guard that nothing is left out."""
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest

from src.auth.lesson_substitutions import CLOSED_OUTCOME, Ctx, SubstitutionsBase
from src.auth.substitution_schemas import ClosedReason
from src.models.auth import LessonSubstitutionEvent as Ev, LessonSubstitutionRequest as Req

import typing

CLOSED_REASONS = set(typing.get_args(ClosedReason))


def rec(status, **kw):
    return Req(id=uuid4(), status=status, origin=kw.pop("origin", "teacher"), created_at=datetime(2026, 10, 21, 14, 0), **kw)


def event(request, action, **snapshot):
    return Ev(id=uuid4(), request_id=request.id, action=action, snapshot=snapshot, created_at=datetime(2026, 10, 21, 15, 0))


def outcome(r, *, cleared=None, triggers=None, closed=None, restored=False, voided=False):
    ctx = Ctx()
    if cleared is not None:
        ctx.cleared[r.id] = cleared
    if triggers:
        ctx.triggers[r.id] = triggers
    if closed:
        ctx.closed_trigger[r.id] = closed
    if restored:
        ctx.restored.add(r.id)
    if voided:
        ctx.voided.add(r.id)
    return SubstitutionsBase.outcome_of(r, ctx)


def clear(r, trigger, to=None, **extra):
    return event(r, "assignment_cleared", teacherId=[str(uuid4()), str(to) if to else None], trigger=trigger, **extra)


def test_every_status_and_reason_has_its_code():
    r = rec("pending")
    assert outcome(r) == "pending"
    assert outcome(rec("rescheduled")) == "rescheduled" and outcome(rec("rejected")) == "rejected" and outcome(rec("withdrawn")) == "withdrawn"
    assert outcome(rec("substituted")) == "substituted"
    assert outcome(rec("cancelled")) == "cancelled"
    assert outcome(rec("cancelled", decided_via="teacher_cancel")) == "lesson_not_held"
    assert outcome(rec("cancelled", origin="system"), triggers="teacher_cancel") == "lesson_not_held"
    assert outcome(rec("cancelled", origin="system"), triggers="manager_cancel") == "cancelled"
    assert outcome(rec("cancelled"), restored=True) == "lesson_restored"                          # A10: the cancellation that was undone
    assert outcome(rec("closed", closed_reason="LESSON_CANCELLED")) == "lesson_already_cancelled"
    assert outcome(rec("closed", closed_reason="LESSON_CANCELLED"), closed="day_off") == "cancelled_by_day_off"
    assert outcome(rec("closed", closed_reason="LESSON_CANCELLED"), closed="journal_row") == "lesson_already_cancelled"
    assert outcome(rec("closed", closed_reason="SOMETHING_NEW")) == "closed" and outcome(rec("closed", closed_reason=None)) == "closed"


@pytest.mark.parametrize("reason,code", sorted(CLOSED_OUTCOME.items()))
def test_each_closing_reason_maps_to_its_code(reason, code):
    assert outcome(rec("closed", closed_reason=reason)) == code


def test_every_closed_reason_of_the_schema_is_mapped_or_told_apart():
    """A new ``closedReason`` must get a code (LESSONS 46): the test fails until it does."""
    assert CLOSED_REASONS - {"LESSON_CANCELLED"} <= set(CLOSED_OUTCOME)
    assert set(CLOSED_OUTCOME) <= CLOSED_REASONS


def test_the_fate_of_a_substitution_picks_the_code():
    for trigger, to, extra, code in (
        ("put", None, {}, "substitution_cleared"), ("put", uuid4(), {}, "substitution_replaced"), ("release", None, {}, "substitution_cleared"),
        ("member_removed", None, {}, "substitution_cleared"), ("role_changed", None, {}, "substitution_cleared"), ("group_teacher_changed", None, {}, "substitution_cleared"),
        ("schedule_changed", None, {}, "substitution_cleared"), ("schedule_ended", None, {}, "substitution_cleared"),
        ("cancel", None, {}, "lesson_later_cancelled"), ("cancel", None, {"dayOffId": str(uuid4())}, "cancelled_by_day_off"), ("move", None, {}, "lesson_later_moved"),
    ):
        r = rec("substituted")
        assert outcome(r, cleared=clear(r, trigger, to, **extra)) == code, (trigger, to, extra)


def test_a_day_off_that_was_cancelled_wins_over_the_rest_and_a_pending_one_is_pending():
    r = rec("substituted")
    assert outcome(r, cleared=clear(r, "cancel", dayOffId=str(uuid4())), voided=True) == "day_off_voided"
    assert outcome(rec("closed", closed_reason="LESSON_CANCELLED"), closed="day_off", voided=True) == "day_off_voided"
    assert outcome(rec("pending"), voided=True) == "pending"


def test_the_codes_of_the_doc_are_the_codes_of_the_code():
    """P157_API.md §2 lists every code; the code returns no code the doc does not name and the doc names none the code cannot return."""
    doc = (Path(__file__).resolve().parents[2] / "artifacts" / "analysis" / "P157_API.md")
    if not doc.exists():
        pytest.skip("the doc lives next to the repository of the lead")
    named = set(re.findall(r"^\| `([a-z_]+)` \|", doc.read_text(encoding="utf-8").split("## 2. Коды")[1].split("## 3.")[0], re.M))
    returned = {"pending", "substituted", "rescheduled", "cancelled", "lesson_not_held", "rejected", "substitution_cleared", "substitution_replaced", "lesson_later_cancelled", "lesson_later_moved",
                "cancelled_by_day_off", "day_off_voided", "withdrawn", "lesson_already_cancelled", "closed"} | set(CLOSED_OUTCOME.values()) | {"lesson_restored"}
    assert named == returned and len(named) == 25
