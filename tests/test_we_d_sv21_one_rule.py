"""Wave E, E-D (SV-21): the three places of ``service.py`` that each laid the schedule revisions over a slot, the two that parsed «YYYY-MM» and the three
that cut a diagnostic result into a dict now use one function each.  The behaviour was the same in every copy (the audit found no divergence); what is pinned
is the rule itself and that no copy comes back."""
import pathlib
from datetime import date
from types import SimpleNamespace

import pytest

from src.auth import business_clock, schedule_rules
from src.auth.diagnostic_level import result_summary

SERVICE = pathlib.Path(__file__).resolve().parents[1] / "src" / "auth" / "service.py"


def revision(effective, payload):
    return SimpleNamespace(effective_date=effective, payload=payload)


def test_revisions_in_force_on_the_day_lie_over_the_slot_in_order():
    slot = {"dayOfWeek": 1, "startTime": "10:00", "endTime": "11:00"}
    revisions = [revision("2026-10-01", {"dayOfWeek": 1, "startTime": "11:00", "endTime": "12:00"}),
                 revision("2026-10-15", {"dayOfWeek": 3, "startTime": "11:00", "endTime": "12:00"}),
                 revision("2026-11-01", {"dayOfWeek": 5, "startTime": "09:00", "endTime": "10:00"})]
    assert schedule_rules.revised_payload(slot, revisions, "2026-09-30") == slot
    assert schedule_rules.revised_payload(slot, revisions, "2026-10-01")["startTime"] == "11:00"           # the day itself counts
    assert schedule_rules.revised_payload(slot, revisions, "2026-10-20")["dayOfWeek"] == 3
    assert schedule_rules.revised_payload(slot, revisions, "2026-12-01")["dayOfWeek"] == 5
    assert slot["startTime"] == "10:00"                                                                       # the slot is never changed in place


def test_a_revision_without_a_payload_changes_nothing():
    slot = {"dayOfWeek": 2, "startTime": "10:00", "endTime": "11:00"}
    out = schedule_rules.revised_payload(slot, [revision("2026-10-01", None), revision("2026-10-02", {})], "2026-10-05")
    assert out == slot


@pytest.mark.parametrize("month,first,last", [
    ("2026-02", date(2026, 2, 1), date(2026, 2, 28)), ("2028-02", date(2028, 2, 1), date(2028, 2, 29)),
    ("2026-12", date(2026, 12, 1), date(2026, 12, 31)), ("2026-01", date(2026, 1, 1), date(2026, 1, 31)),
])
def test_month_bounds(month, first, last):
    assert business_clock.month_bounds(month) == (first, last)


@pytest.mark.parametrize("bad", ["2026-1", "2026-13", "2026-00", "2026-12-01", "", "abc", "2026/12", " 2026-12"])
def test_month_bounds_refuses_with_the_api_text(bad):
    with pytest.raises(ValueError) as caught:
        business_clock.month_bounds(bad)
    assert str(caught.value) == "month должен иметь формат YYYY-MM"


def test_result_summary_is_the_one_shape_of_a_diagnostic_result():
    result = SimpleNamespace(final_theta="0.5", accuracy="0.8", total_tasks=20, correct_answers=16, overall_level="on_grade", level_status="reliable",
                             class_equivalent=9, level_provisional=False)
    out = result_summary(result)
    assert list(out) == ["theta", "accuracy", "recommended_level", "level_status", "class_equivalent", "level_provisional", "total_questions", "correct_answers"]
    assert out["theta"] == 0.5 and out["accuracy"] == 0.8 and out["total_questions"] == 20 and out["correct_answers"] == 16
    empty = result_summary(SimpleNamespace(final_theta=None, accuracy=None, total_tasks=None, correct_answers=None))
    assert empty["theta"] is None and empty["accuracy"] is None


def test_the_copies_do_not_come_back_to_service_py():
    text = SERVICE.read_bytes().decode("utf-8")
    assert "revision.effective_date <=" not in text           # the loops of three schedule readers
    assert 'strptime(selected_month + "-01"' not in text       # the two parsers of «YYYY-MM»
    assert '"accuracy": float(' not in text                    # the three result dicts
