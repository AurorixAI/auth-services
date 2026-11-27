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

    def within_window(self, lesson_date: Any) -> bool:
        """THE question «is the lesson still inside the window of the teacher's own actions» (``ops_rules.is_too_old_for_teacher`` with the rule of the centre): ``canActSelf``, ``teacherCanMark`` and the window of
        «Отозвать» read it here.  A date that cannot be read is inside (the refusal of the road decides)."""
        try:
            day = lesson_date if isinstance(lesson_date, date) else date.fromisoformat(lesson_date)
        except (TypeError, ValueError):
            return True
        return not is_too_old_for_teacher(day, self.today, self.window)

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
        return {"canActSelf": bool(mine and (self.manager or self.within_window(day))), "monthClosed": closed, "windowDays": self.window}


def apply_window(item: Any, marks: Dict[str, Any]) -> None:
    """Set the marks on an answer object (a model or a dict) - the lesson of the journal is a dict, the occurrence of the schedule a model."""
    for name, value in marks.items():
        if isinstance(item, dict):
            item[name] = value
        else:
            setattr(item, name, value)


def reported_by_role(org_role: Optional[str]) -> str:
    """C13.6: the screen says «Преподаватель сообщил…» / «Администратор сообщил…» by THIS word of the server: a teacher or - whoever else cancels - an administrator."""
    return "teacher" if org_role == "teacher" else "admin"


class WaitingMarks:
    """C13.4 / C13.6: the cancellations that WAIT for the owner among the lessons of one answer (the journal of a group, the schedule): ``waiting``, WHO reported it (``reportedBy`` the short name,
    ``reportedByRole`` ``teacher`` | ``admin``) and ``canWithdraw`` for the viewer - the SAME rule as the road (``cancellation_kind.withdraw_refusal``, the open month, the active group), nobody keeps a copy.
    One query for the rows, one for the names, whatever the number of lessons."""

    def __init__(self, db: Any, org_id: Any, window: WindowMarks, *, actor: Any, org_role: Optional[str]) -> None:
        self.db, self.org_id, self.window = db, org_id, window
        self.actor = str(actor) if actor else None
        self.org_role = org_role
        self.rows: Dict[tuple, Any] = {}
        self.names: Dict[str, Any] = {}
        self.roles: Dict[str, str] = {}
        self.inactive: set = set()

    def load(self, group_ids: Any, first: str, last: str) -> "WaitingMarks":
        from .models import Group, GroupAttendance, OrganizationMember, User
        from .ops_rules import short_name

        gids = list(group_ids)
        if not gids:
            return self
        rows = self.db.query(GroupAttendance).filter(GroupAttendance.group_id.in_(gids), GroupAttendance.lesson_date >= first, GroupAttendance.lesson_date <= last,
                                                     GroupAttendance.status == "cancelled", GroupAttendance.cancellation_kind == "unconfirmed").all()
        self.rows = {(str(r.group_id), r.lesson_id, r.lesson_date): r for r in rows}
        if not rows:
            return self
        authors = {r.marked_by for r in rows if r.marked_by}
        self.names = {str(u.id): short_name(u) for u in self.db.query(User).filter(User.id.in_(authors)).all()} if authors else {}
        self.roles = {str(m.user_id): m.org_role for m in self.db.query(OrganizationMember).filter(OrganizationMember.org_id == self.org_id, OrganizationMember.user_id.in_(authors)).all()} if authors else {}
        self.inactive = {str(g.id) for g in self.db.query(Group).filter(Group.id.in_({r.group_id for r in rows})).all() if g.status != "active"}
        return self

    def of(self, group_id: Any, lesson_id: Any, lesson_date: Any) -> Dict[str, Any]:
        """``{waiting, reportedBy, reportedByRole, canWithdraw}`` of one lesson; a lesson that does not wait has the empty ones (the front reads one shape)."""
        from .cancellation_kind import withdraw_refusal

        row = self.rows.get((str(group_id), lesson_id, lesson_date))
        if row is None:
            return {"waiting": False, "reportedBy": None, "reportedByRole": None, "canWithdraw": None}
        author = str(row.marked_by) if row.marked_by else None
        can = bool(self.actor is not None and withdraw_refusal(author=author == self.actor, role=self.org_role, in_window=self.window.within_window(lesson_date)) is None
                   and not self.window.of(row.teacher_id, lesson_date)["monthClosed"] and str(group_id) not in self.inactive)
        return {"waiting": True, "reportedBy": self.names.get(author) if author else None, "reportedByRole": reported_by_role(self.roles.get(author)) if author else None, "canWithdraw": can}
