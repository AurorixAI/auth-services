"""LESSON-RULES С10.1: the marks of the window on EVERY lesson of the journal of a group and of the schedule - what the screen needs to offer (or not to offer) a teacher the buttons of the past lesson.

``canActSelf``  - may the viewer act on the lesson himself: a manager always; a teacher when the lesson is HIS and not older than the window of the centre.  It is the SAME function as the refusal of the
                  road (``attendance_rules.assert_lesson_rights``: ``NOT_LESSON_TEACHER`` / ``LESSON_TOO_OLD``), not a copy of it: ``ops_rules.is_too_old_for_teacher`` with the rule of the centre.
``monthClosed`` - the month of the lesson date of the lesson's teacher is closed by a saved sheet (``closed_period.ClosedPeriods``, the rule of ``PERIOD_CLOSED``): nobody acts, the owner reopens it.
                  It is independent of ``canActSelf`` (a closed month of a lesson of the window says «Месяц закрыт», not «Урок старше N дн.»).
``windowDays``  - the window of the centre in days (the screen says «Урок старше N дн.» with THIS number).

Nothing is written; the centre rule and the closed months are read ONCE per answer."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

from . import business_clock as bc
from .centre_policy import CentrePolicy
from .closed_period import ClosedPeriods
from .ops_rules import is_too_old_for_teacher


class WindowMarks:
    def __init__(self, db: Any, org_id: Any, *, manager: bool, actor: Any) -> None:
        self.manager = manager
        self.actor = str(actor) if actor else None
        self.today = bc.business_today()
        self.window = CentrePolicy.of(db, org_id).lesson_window_days
        self.periods = ClosedPeriods.of(db, org_id)

    def of(self, teacher_id: Any, lesson_date: str) -> Dict[str, Any]:
        """``{"canActSelf", "monthClosed", "windowDays"}`` of one lesson (``teacher_id``: its teacher, the substitute included; ``lesson_date``: ``YYYY-MM-DD``)."""
        try:
            day: Optional[date] = date.fromisoformat(lesson_date)
        except (TypeError, ValueError):
            day = None
        if day is None:
            return {"canActSelf": self.manager, "monthClosed": False, "windowDays": self.window}
        closed = bool(teacher_id) and self.periods.any_closed(day) and self.periods.closed_for([teacher_id], day) is not None
        mine = self.manager or (bool(teacher_id) and self.actor is not None and str(teacher_id) == self.actor)
        return {"canActSelf": bool(mine and (self.manager or not is_too_old_for_teacher(day, self.today, self.window))), "monthClosed": closed, "windowDays": self.window}


def apply_window(item: Any, marks: Dict[str, Any]) -> None:
    """Set the marks on an answer object (a model or a dict) - the lesson of the journal is a dict, the occurrence of the schedule a model."""
    for name, value in marks.items():
        if isinstance(item, dict):
            item[name] = value
        else:
            setattr(item, name, value)
