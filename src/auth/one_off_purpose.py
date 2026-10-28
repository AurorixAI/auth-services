"""#154: the PURPOSE of a one-off lesson - a make-up of a cancelled lesson (``makeup``), an additional lesson (``extra``) or the copy a move made (``moved``).

One module, the rules in one place (LESSONS 38 / 15):

* creation: ``check_request`` (what the author may choose; the teacher who sends nothing gets ``extra`` + ``wait``), ``makeup_origin`` (which cancelled lesson can be made up - the row is LOCKED, so
  two make-ups of one lesson never both live), the audit row ``one_off_created``;
* the life of the pointer ``group_attendance.made_up_by`` (the row of the cancelled lesson): ``link`` / ``release`` / ``repoint`` / ``relink`` - the hooks of the cancellation, the restore and the move;
* the decision «Оплатить / Не оплачивать» of the money of an additional lesson (and of an EMPTY make-up) and the list of what waits for it;
* what the answers say about the lesson (``purpose`` / ``money`` / labels), with the viewer's mask: the teacher reads only the status of HIS lesson, the student / parent never the money (LESSONS 45 / 51).

Money is never stored here: the deduction of the recalculation, the package, the forecast and the payroll are functions of these facts (``ledger_service``, ``payments_service``, ``payroll_service``)."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import UUID

from sqlalchemy import and_, false, or_

from . import business_clock as bc
from .attendance_rules import AttendanceRules
from .lesson_teachers import TeacherBook, _s, _uuid
from .models import Group, GroupAttendance, GroupAudit, GroupScheduleOneOffLesson, LessonSubstitutionRequest as Req, OrganizationMember, User
from .ops_rules import conflict, forbidden, not_found, short_name, unprocessable

MAKEUP, EXTRA, MOVED = "makeup", "extra", "moved"
NONE, WAIT, PAID = "none", "wait", "paid"
MAKEUP_BACK_DAYS = 60                         # the cancelled lesson is at most this old (the date of the ORIGINAL)
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
TEACHER_MONEY = (WAIT, NONE)                  # what a teacher may choose for an additional lesson ...
MANAGER_MONEY = (NONE, PAID)                  # ... and what an owner / admin may (radio, no flags)

CREATED_ACTION, DECIDED_ACTION = "one_off_created", "one_off_money_decided"

MAKEUP_REFUSALS = {
    "NOT_FOUND": "Этого урока уже нет в расписании группы: отработать его нельзя",
    "NOT_CANCELLED": "Урок не отменён: отрабатывать нечего",
    "ONE_OFF": "Разовый урок отработать нельзя: назначьте новый урок",
    "HOLIDAY": "Урок отменён как праздник или выходной: вычета нет, отрабатывать нечего. Добавьте дополнительный урок",
    "NO_KIND": "Это отмена до обновления: отработать её нельзя. Добавьте дополнительный урок",
    "MOVED": "Урок перенесён: отрабатывать нечего",
    "TOO_OLD": f"Отработать можно урок не старше {MAKEUP_BACK_DAYS} дней",
}
MAKEUP_EXISTS_TEXT = "По уроку назначена отработка: сначала отмените её"
NO_CANDIDATES_TEXT = ("Нет отменённых уроков, которые можно отработать: нужен урок по расписанию, отменённый по вине центра за последние "
                      f"{MAKEUP_BACK_DAYS} дней, без отработки")
TITLES = {EXTRA: "Дополнительный урок — оплатить преподавателю?", MAKEUP: "Отработка без учеников — оплатить преподавателю?"}


def day_label(day: str) -> str:
    """``2026-11-03`` -> ``пн 03.11`` (the day of the week and the date: one place for every text)."""
    d = date.fromisoformat(day)
    return f"{WEEKDAYS[d.weekday()]} {d.strftime('%d.%m')}"


def purpose_label(purpose: Optional[str], made_up_date: Optional[str]) -> Optional[str]:
    if purpose == MAKEUP and made_up_date:
        return f"Отработка за {day_label(made_up_date)}"
    return "Дополнительный" if purpose == EXTRA else None


def is_empty_roster_lesson(records: Any) -> bool:
    """A held lesson on which nobody was present or late (the make-up nobody came to)."""
    marks = records.values() if isinstance(records, dict) else ()
    return not any(m in ("present", "late") for m in marks)


def money_label(money: Optional[str], *, manager: bool, held: bool) -> Optional[str]:
    """The words of the status of the money; ``held``: the lesson has been held (a ``wait`` before it says when it is decided)."""
    if money is None:
        return None
    if manager:
        return {NONE: "без оплаты", PAID: "оплачивается", WAIT: "оплата: не решено" if held else "оплата: решите после урока"}[money]
    return {NONE: "без оплаты", PAID: "оплачено", WAIT: "не решено"}[money]


def one_off_of(db, group_id: Any, lesson_id: str) -> Optional[GroupScheduleOneOffLesson]:
    return db.query(GroupScheduleOneOffLesson).filter(GroupScheduleOneOffLesson.id == lesson_id, GroupScheduleOneOffLesson.group_id == group_id).first()


def copy_fields(source: GroupScheduleOneOffLesson) -> Dict[str, Any]:
    """What a copy of a move inherits from a one-off lesson it moves (one helper for the two places that write a copy)."""
    return {"purpose": source.purpose, "money": source.money, "made_up_lesson_id": source.made_up_lesson_id, "made_up_date": source.made_up_date, "created_by": source.created_by}


class OneOffPurpose:
    def __init__(self, svc) -> None:
        self.svc = svc
        self.db = svc.db

    # ================================================================================================ creation
    @staticmethod
    def check_request(req: Any, *, manager: bool) -> Tuple[str, Optional[str]]:
        """``(purpose, money)`` of a new one-off lesson, or a coded 422.  The server requires the purpose; a TEACHER who sends none (an old tab, the API) gets ``extra`` + ``wait`` (АД-10)."""
        purpose = getattr(req, "purpose", None)
        money = getattr(req, "money", None)
        target = (getattr(req, "madeUpLessonId", None), getattr(req, "madeUpDate", None))
        if purpose is None:
            if manager:
                raise unprocessable("PURPOSE_REQUIRED", "Выберите, что это за урок: отработка или дополнительный")
            purpose = EXTRA
        if purpose not in (MAKEUP, EXTRA):
            raise unprocessable("PURPOSE_INVALID", "Урок бывает «отработка» или «дополнительный»")
        if purpose == MAKEUP:
            if money is not None:
                raise unprocessable("MONEY_INVALID", "У отработки оплата не выбирается: деньги как у обычного урока")
            if not target[0] or not target[1]:
                raise unprocessable("MAKEUP_TARGET_REQUIRED", "Выберите отменённый урок, который отрабатывается")
            return MAKEUP, None
        if target[0] or target[1]:
            raise unprocessable("MAKEUP_TARGET_NOT_ALLOWED", "Отменённый урок указывается только у отработки")
        allowed = MANAGER_MONEY if manager else TEACHER_MONEY
        if money is None:
            if manager:
                raise unprocessable("MONEY_REQUIRED", "Выберите оплату преподавателю: без оплаты или оплачивается")
            money = WAIT
        if money not in allowed:
            raise unprocessable("MONEY_INVALID", "Такой вариант оплаты вам недоступен")
        return EXTRA, money

    def require_teacher(self, group: Group, day: date, end_minutes: int) -> None:
        """A lesson of a group that has no teacher cannot be held and paid: ``TEACHER_REQUIRED`` (ОД-17)."""
        if TeacherBook(self.db).default_teacher(group, day.isoformat(), end_minutes) is None:
            raise unprocessable("TEACHER_REQUIRED", "Выберите преподавателя")

    def lock_origin(self, group: Group, lesson_id: str, day: date) -> Optional[GroupAttendance]:
        return (self.db.query(GroupAttendance).filter(GroupAttendance.group_id == group.id, GroupAttendance.lesson_id == lesson_id, GroupAttendance.lesson_date == day.isoformat())
                .with_for_update().first())

    def _origin_state(self, group: Group, lesson_id: str, day: date, row: Optional[GroupAttendance], occ: Optional[Dict[str, Any]], today: date) -> Optional[str]:
        """Why the lesson cannot be made up (a key of ``MAKEUP_REFUSALS``); ``None`` = it can.  The one rule of the list and of the creation."""
        if occ is None:
            return "NOT_FOUND"
        if row is None or row.status != "cancelled":
            return "NOT_CANCELLED"
        if occ.get("isOneOff") or one_off_of(self.db, group.id, lesson_id) is not None:
            return "ONE_OFF"
        if self.db.query(Req.id).filter(Req.group_id == group.id, Req.lesson_id == lesson_id, Req.lesson_date == day.isoformat(), Req.status == "rescheduled").first() is not None:
            return "MOVED"
        if row.cancellation_kind == "holiday":
            return "HOLIDAY"
        if row.cancellation_kind is None:
            return "NO_KIND"
        if day < today - timedelta(days=MAKEUP_BACK_DAYS):
            return "TOO_OLD"
        return None

    def makeup_origin(self, group: Group, lesson_id: str, day: date) -> Tuple[GroupAttendance, Dict[str, Any]]:
        """The row of the cancelled lesson (LOCKED) and its occurrence, or a coded refusal: 422 ``MAKEUP_NOT_ALLOWED`` (``reasonCode`` + text) / 409 ``MAKEUP_EXISTS``.  The caller holds the
        lock of the group; a second author waits here and then sees ``made_up_by`` - the make-up of one lesson never lives twice."""
        occ = next((o for o in self.svc._resolved_occurrences_for_group(group, date_from=day, date_to=day) if o["id"] == lesson_id), None)
        row = self.lock_origin(group, lesson_id, day)
        state = self._origin_state(group, lesson_id, day, row, occ, bc.business_today())
        if state is not None:
            raise unprocessable("MAKEUP_NOT_ALLOWED", MAKEUP_REFUSALS[state], reasonCode=state)
        if row.made_up_by:
            raise conflict("MAKEUP_EXISTS", MAKEUP_EXISTS_TEXT)
        return row, occ

    def candidates(self, group: Group, *, manager: bool, actor: Optional[Any] = None) -> Dict[str, Any]:
        """The cancelled lessons of the group that can be made up (``GET makeup-candidates``): the date, the time and the reason of the cancellation - for an owner / admin always, for a teacher only where
        the journal of the lesson shows it to him (``AttendanceRules.teacher_sees_lesson``: the journal gives the reason to him by the very same function, decision of the lead 29.10 item 2)."""
        today = bc.business_today()
        first = today - timedelta(days=MAKEUP_BACK_DAYS)
        rows = (self.db.query(GroupAttendance).filter(GroupAttendance.group_id == group.id, GroupAttendance.status == "cancelled", GroupAttendance.cancellation_kind == "centre",
                                                      GroupAttendance.made_up_by.is_(None), GroupAttendance.lesson_date >= first.isoformat())
                .order_by(GroupAttendance.lesson_date, GroupAttendance.lesson_id).all())
        items: List[Dict[str, Any]] = []
        if group.status == "active" and rows:
            days = [date.fromisoformat(r.lesson_date) for r in rows]
            occurrences = {(o["occurrenceDate"], o["id"]): o for o in self.svc._resolved_occurrences_for_group(group, date_from=min(days), date_to=max(days))}
            teachers = self._journal_teachers(group, rows, occurrences) if not manager else {}
            for row in rows:
                occ = occurrences.get((row.lesson_date, row.lesson_id))
                if self._origin_state(group, row.lesson_id, date.fromisoformat(row.lesson_date), row, occ, today) is not None:
                    continue
                shown = manager or AttendanceRules.teacher_sees_lesson(group, actor, teachers.get(f"{row.lesson_date}_{row.lesson_id}"), row.lesson_date)
                items.append({"lessonId": row.lesson_id, "lessonDate": row.lesson_date, "startTime": occ["startTime"], "endTime": occ["endTime"],
                              "label": f"{day_label(row.lesson_date)}, {occ['startTime']}–{occ['endTime']}", "reason": row.cancellation_reason if shown else None})
        return {"items": items, "emptyReason": None if items else NO_CANDIDATES_TEXT}

    def _journal_teachers(self, group: Group, rows: List[GroupAttendance], occurrences: Dict[Tuple[str, str], Dict[str, Any]]) -> Dict[str, Optional[str]]:
        """``{<date>_<lessonId>: the teacher of the lesson}`` as the journal computes it (``AttendanceRules.journal_extras``: the same function, not a copy)."""
        lessons = [{"instanceKey": f"{r.lesson_date}_{r.lesson_id}", "id": r.lesson_id, "date": r.lesson_date, "endTime": occurrences[(r.lesson_date, r.lesson_id)]["endTime"]}
                   for r in rows if (r.lesson_date, r.lesson_id) in occurrences]
        if not lessons:
            return {}
        _, infos = AttendanceRules(self.svc).journal_extras(group, rows, lessons, date.fromisoformat(rows[0].lesson_date), date.fromisoformat(rows[-1].lesson_date))
        return {key: info["teacherId"] for key, info in infos.items()}

    def makeup_slot(self, group: Group, origin_occ: Dict[str, Any], origin_day: date, new_day: date, payload: Dict[str, Any], *, manager: bool) -> List[Dict[str, str]]:
        """The rules of the slot of a make-up: THE function of a move (АД-7) - from today to +60 days, not the very slot of the cancelled lesson, not after the end of its series, the group and the
        teacher are free (``GROUP_BUSY`` / ``TEACHER_BUSY`` 409); a student with another lesson then is the warning ``STUDENT_BUSY``.  The teacher of the new slot is locked first (the pattern of a
        move: two make-ups into one slot of one teacher serialise).  Returns the warnings."""
        from .lesson_substitutions import Substitutions

        subs = Substitutions(self.svc)
        start, end = payload["startTime"], payload["endTime"]
        start_m, end_m = self.svc._validate_lesson_time_range(start, end)
        ref = subs._ref_of(group, origin_occ["id"], origin_day, origin_occ)
        refusal = subs._slot_refusal(ref, new_day, start, end, start_m, subs.now()) or subs._series_refusal(group, ref, new_day)
        if refusal is not None:
            raise refusal
        return self.busy_checks(group, ref, new_day, start, end, start_m, end_m, manager=manager)

    def slot_checks(self, group: Group, lesson_id: str, day: date, payload: Dict[str, Any], *, manager: bool) -> List[Dict[str, str]]:
        """The overlaps of an ADDITIONAL lesson (decision of the lead 29.10, items 3 and 5; also the return of a cancelled make-up / additional lesson): the same three checks as a make-up - ``GROUP_BUSY`` / ``TEACHER_BUSY`` 409, ``STUDENT_BUSY`` a warning.  No limit
        of days ahead and the past stays refused by the caller (the existing rule).  Called after the lock of the group, so two clicks of one author never both pass.  Returns the warnings."""
        from .lesson_substitutions import Substitutions

        start, end = payload["startTime"], payload["endTime"]
        start_m, end_m = self.svc._validate_lesson_time_range(start, end)
        ref = Substitutions(self.svc)._ref_of(group, lesson_id, day, payload)
        return self.busy_checks(group, ref, day, start, end, start_m, end_m, manager=manager)

    def busy_checks(self, group: Group, ref: Any, new_day: date, start: str, end: str, start_m: int, end_m: int, *, manager: bool) -> List[Dict[str, str]]:
        """The one place of the overlaps of a new slot (a make-up and an additional lesson): the teacher of the slot is locked first (the pattern of a move: two lessons into one slot of one teacher
        serialise), then ``TEACHER_BUSY`` / ``GROUP_BUSY`` 409; a student with another lesson then is the warning ``STUDENT_BUSY``."""
        from . import substitution_rules as R
        from .lesson_substitutions import Substitutions

        subs = Substitutions(self.svc)
        teacher = TeacherBook(self.db).default_teacher(group, new_day.isoformat(), end_m)
        if teacher is not None:
            self.db.query(User.id).filter(User.id == teacher).with_for_update(key_share=True).first()        # FOR NO KEY UPDATE (LESSONS 23)
        teacher_id, t_conf, g_conf = subs.conflicts_for_move(group, ref, new_day, start_m, end_m, start, end)
        if t_conf:
            subs.teachers.raise_if_busy(t_conf, short_name(self.db.query(User).filter(User.id == teacher_id).first()))
        if g_conf:
            raise R.err(409, "GROUP_BUSY", conflicts=g_conf[:20], total=len(g_conf))
        return subs._student_busy(group, new_day, start_m, end_m, detail=manager)

    def actor_role(self, org_id: Any, actor_id: Any) -> str:
        """The role of the author IN THIS centre (the audit row says it).  Never guessed from the global role and never a default (LESSONS 17): no membership in the centre = 403 ``NOT_A_MEMBER``."""
        member = self.db.query(OrganizationMember.org_role).filter(OrganizationMember.org_id == org_id, OrganizationMember.user_id == _uuid(actor_id)).first()
        if not member or member[0] not in ("owner", "admin", "teacher"):
            raise forbidden("NOT_A_MEMBER", "Вы не состоите в этом центре")
        return member[0]

    def link(self, origin: GroupAttendance, lesson_id: str) -> None:
        origin.made_up_by = lesson_id

    def audit_created(self, group: Group, actor: UUID, actor_role: str, one_off: GroupScheduleOneOffLesson, payload: Dict[str, Any]) -> None:
        self.db.add(GroupAudit(
            org_id=group.org_id, group_id=group.id, actor_id=actor, actor_role=actor_role, action=CREATED_ACTION,
            assigned_teacher_id=group.teacher_id, created_at=bc.utc_now(),
            snapshot={"lessonId": one_off.id, "lessonDate": one_off.lesson_date, "startTime": payload.get("startTime"), "endTime": payload.get("endTime"), "purpose": one_off.purpose,
                      "money": one_off.money, "madeUp": {"lessonId": one_off.made_up_lesson_id, "date": one_off.made_up_date} if one_off.made_up_lesson_id else None},
        ))

    # ===================================================================================== the pointer of a make-up
    def _origin_of(self, group_id: Any, one_off: GroupScheduleOneOffLesson) -> Optional[GroupAttendance]:
        if one_off.purpose != MAKEUP or not one_off.made_up_lesson_id:
            return None
        return (self.db.query(GroupAttendance).filter(GroupAttendance.group_id == group_id, GroupAttendance.lesson_id == one_off.made_up_lesson_id,
                                                      GroupAttendance.lesson_date == one_off.made_up_date).with_for_update().first())

    def release(self, group: Group, lesson_id: str, actor_id: Any) -> None:
        """The make-up lesson is gone (cancelled / deleted): the cancelled lesson it made up for is free again (the deduction comes back by itself - it is a function of the facts).  If a live day off
        holds the date of that lesson, it becomes a holiday now (LESSONS 47: the day off converts every «по вине центра» cancellation of its dates, and this one was kept only for the make-up)."""
        from .days_off import DaysOffService  # lazy: it imports this module's neighbours

        one_off = one_off_of(self.db, group.id, lesson_id)
        origin = self._origin_of(group.id, one_off) if one_off is not None else None
        if origin is not None and origin.made_up_by == lesson_id:
            origin.made_up_by = None
            self.db.flush()
            DaysOffService(self.svc).convert_released(group, origin, actor_id)

    def repoint(self, group: Group, source: GroupScheduleOneOffLesson, new_id: str) -> None:
        """A move of a make-up: the cancelled lesson points at the COPY (the copy was written with the same ``made_up_*``)."""
        origin = self._origin_of(group.id, source)
        if origin is not None and origin.made_up_by == source.id:
            origin.made_up_by = new_id
            self.db.flush()

    def restore_slot(self, group: Group, lesson_id: str, day: date, payload: Dict[str, Any]) -> Optional[List[Dict[str, str]]]:
        """«Вернуть урок» for a cancelled make-up / additional lesson: its slot may have been taken meanwhile - the same ``GROUP_BUSY`` / ``TEACHER_BUSY`` 409 (+ the warning ``STUDENT_BUSY``) as at the
        creation.  ``None``: not such a lesson (the caller makes its usual check)."""
        one_off = one_off_of(self.db, group.id, lesson_id)
        if one_off is None or one_off.purpose not in (MAKEUP, EXTRA):
            return None
        return self.slot_checks(group, lesson_id, day, payload, manager=True)

    def relink(self, group: Group, lesson_id: str) -> None:
        """«Вернуть урок» for a cancelled make-up: it makes up for the same lesson again if that lesson is still cancelled by the centre and free; else 409 ``RESTORE_MAKEUP_TARGET``."""
        one_off = one_off_of(self.db, group.id, lesson_id)
        if one_off is None or one_off.purpose != MAKEUP:
            return
        origin = self._origin_of(group.id, one_off)
        if origin is None or origin.status != "cancelled" or origin.cancellation_kind != "centre" or origin.made_up_by:
            raise conflict("RESTORE_MAKEUP_TARGET", "Отменённый урок, который отрабатывался, уже изменён или у него другая отработка: вернуть отработку нельзя")
        origin.made_up_by = lesson_id
        self.db.flush()

    # ================================================================================================== decisions
    def _lesson_row(self, one_off: GroupScheduleOneOffLesson) -> Optional[GroupAttendance]:
        return self.db.query(GroupAttendance).filter(GroupAttendance.group_id == one_off.group_id, GroupAttendance.lesson_id == one_off.id, GroupAttendance.lesson_date == one_off.lesson_date).first()

    def pending_rows(self, org_id: Any) -> List[Tuple[GroupScheduleOneOffLesson, GroupAttendance, Group]]:
        """The held lessons whose money waits for a decision: an additional lesson with ``wait`` and an EMPTY make-up without a decision (nobody was present or late)."""
        rows = (self.db.query(GroupScheduleOneOffLesson, GroupAttendance, Group)
                .join(GroupAttendance, and_(GroupAttendance.group_id == GroupScheduleOneOffLesson.group_id, GroupAttendance.lesson_id == GroupScheduleOneOffLesson.id,
                                            GroupAttendance.lesson_date == GroupScheduleOneOffLesson.lesson_date))
                .join(Group, Group.id == GroupScheduleOneOffLesson.group_id)
                .filter(GroupScheduleOneOffLesson.org_id == org_id, GroupAttendance.status == "completed",
                        or_(and_(GroupScheduleOneOffLesson.purpose == EXTRA, GroupScheduleOneOffLesson.money == WAIT),
                            and_(GroupScheduleOneOffLesson.purpose == MAKEUP, GroupScheduleOneOffLesson.money.is_(None))))
                .order_by(GroupScheduleOneOffLesson.lesson_date, GroupScheduleOneOffLesson.id).all())
        return [(o, a, g) for o, a, g in rows if o.purpose == EXTRA or is_empty_roster_lesson(a.records)]

    def pending_count(self, org_id: Any) -> int:
        return len(self.pending_rows(org_id))

    def sheet_of(self, org_id: Any, lesson_date: str, teacher_id: Optional[Any], *, owner: bool) -> Optional[Dict[str, Any]]:
        """The saved payroll sheet the decision touches (the month of the lesson, the teacher who held it): the owner reads its payout state, an admin only the month (LESSONS 45)."""
        from .payroll_hooks import sheets_touched_by_lesson_teacher_change

        found = sheets_touched_by_lesson_teacher_change(self.db, org_id, lesson_date, teacher_id, None, owner=owner) if teacher_id else []
        if not found:
            return None
        return {"month": found[0]["month"], **({"status": found[0]["status"]} if owner else {})}

    def pending(self, org_id: Any, *, owner: bool) -> Dict[str, Any]:
        rows = self.pending_rows(org_id)
        users = {u.id: u for u in self.db.query(User).filter(User.id.in_({x for o, a, _ in rows for x in (a.teacher_id, o.created_by) if x})).all()} if rows else {}
        roles = {m.user_id: m.org_role for m in self.db.query(OrganizationMember).filter(OrganizationMember.org_id == org_id, OrganizationMember.user_id.in_([o.created_by for o, _, _ in rows])).all()} if rows else {}
        items = []
        for one_off, att, group in rows:
            payload = one_off.payload or {}
            roster = [r for r in (att.roster_snapshot or []) if isinstance(r, dict)]
            marks = att.records if isinstance(att.records, dict) else {}
            teacher = users.get(att.teacher_id)
            author = users.get(one_off.created_by)
            items.append({
                "lessonId": one_off.id, "groupId": str(group.id), "groupName": group.name, "lessonDate": one_off.lesson_date, "startTime": payload.get("startTime"),
                "endTime": payload.get("endTime"), "purpose": one_off.purpose, "title": TITLES[one_off.purpose],
                "teacher": {"id": str(att.teacher_id), "name": short_name(teacher)} if att.teacher_id else None,
                "createdBy": {"id": str(one_off.created_by), "name": short_name(author), "role": roles.get(one_off.created_by)},
                "studentsPresent": sum(1 for m in marks.values() if m in ("present", "late")), "studentsTotal": len(roster),
                "payrollSheet": self.sheet_of(org_id, one_off.lesson_date, att.teacher_id, owner=owner),
            })
        return {"total": len(items), "items": items}

    def decide(self, org_id: Any, actor_id: Any, actor_role: str, lesson_id: str, decision: str, *, owner: bool) -> Dict[str, Any]:
        """«Оплатить» / «Не оплачивать»: once, only a held lesson whose money waits (an additional ``wait`` or an empty make-up).  Under the lock of the group, so two administrators never both decide."""
        actor = _uuid(actor_id)
        if actor_role not in ("owner", "admin"):                    # the road is closed to the others already; no default role is written to the audit (LESSONS 17)
            raise forbidden("DECISION_FORBIDDEN", "Решать об оплате могут владелец и администратор")
        one_off = self.db.query(GroupScheduleOneOffLesson).filter(GroupScheduleOneOffLesson.id == lesson_id, GroupScheduleOneOffLesson.org_id == org_id).first()
        if one_off is None:
            raise not_found("ONE_OFF_NOT_FOUND", "Урок не найден")
        group = self.db.query(Group).filter(Group.id == one_off.group_id, Group.org_id == org_id).with_for_update().populate_existing().first()
        one_off = self.db.query(GroupScheduleOneOffLesson).filter(GroupScheduleOneOffLesson.id == lesson_id).with_for_update().populate_existing().first()
        att = (self.db.query(GroupAttendance).filter(GroupAttendance.group_id == one_off.group_id, GroupAttendance.lesson_id == one_off.id, GroupAttendance.lesson_date == one_off.lesson_date)
               .with_for_update().first())
        waiting = (one_off.purpose == EXTRA and one_off.money == WAIT) or (one_off.purpose == MAKEUP and one_off.money is None)
        if not waiting:
            decided = self.db.query(GroupAudit.id).filter(GroupAudit.group_id == one_off.group_id, GroupAudit.action == DECIDED_ACTION, GroupAudit.snapshot["lessonId"].astext == lesson_id).first()
            if decided is not None:
                raise conflict("ALREADY_DECIDED", "Решение по этому уроку уже принято")
            raise unprocessable("DECISION_NOT_ALLOWED", "По этому уроку решение не нужно")
        if att is None or att.status != "completed":
            raise conflict("LESSON_NOT_COMPLETED", "Урок ещё не проведён: решить можно после урока")
        if one_off.purpose == MAKEUP and not is_empty_roster_lesson(att.records):
            raise unprocessable("DECISION_NOT_ALLOWED", "По этому уроку решение не нужно")
        before = one_off.money or "empty_makeup"
        one_off.money = PAID if decision == "pay" else NONE
        self.db.add(GroupAudit(
            org_id=group.org_id, group_id=group.id, actor_id=actor, actor_role=actor_role, action=DECIDED_ACTION,
            assigned_teacher_id=att.teacher_id, created_at=bc.utc_now(),
            snapshot={"lessonId": one_off.id, "lessonDate": one_off.lesson_date, "purpose": one_off.purpose, "from": before, "to": one_off.money, "decision": decision,
                      "teacherId": _s(att.teacher_id)},
        ))
        sheet = self.sheet_of(org_id, one_off.lesson_date, att.teacher_id, owner=owner)
        self.db.commit()
        return {"lessonId": lesson_id, "money": one_off.money, "payrollSheet": sheet}

    # ============================================================================== what the answers say (the mask)
    def marks(self, keys: Sequence[Tuple[Any, str, str]], *, manager: bool, actor: Optional[Any] = None, money_visible: bool = True) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
        """The fields of the purpose of the lessons ``keys`` = ``[(group_id, lesson_id, date)]`` (``GET schedule/occurrences``, the journal, the student card): ``purpose`` / ``purposeLabel`` /
        ``madeUpFor`` of a one-off lesson, ``madeUpBy`` of a cancelled lesson that has a make-up, ``money`` / ``moneyLabel``.  The mask (LESSONS 45 / 51): an owner / admin read the money; a teacher
        only of a lesson of HIS group or one he made; ``money_visible=False`` (the student, the parent) never.  No sums, no decisions - only the state."""
        out: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        if not keys:
            return out
        gids = sorted({_uuid(g) for g, _, _ in keys if _uuid(g)}, key=str)
        ids = sorted({lid for _, lid, _ in keys if lid})
        days = sorted({d for _, _, d in keys})
        one_offs = {o.id: o for o in self.db.query(GroupScheduleOneOffLesson).filter(GroupScheduleOneOffLesson.group_id.in_(gids), GroupScheduleOneOffLesson.id.in_(ids)).all()} if gids and ids else {}
        rows = {(str(r.group_id), r.lesson_id, r.lesson_date): r for r in self.db.query(GroupAttendance).filter(
            GroupAttendance.group_id.in_(gids), GroupAttendance.lesson_date >= days[0], GroupAttendance.lesson_date <= days[-1],
            or_(GroupAttendance.lesson_id.in_(list(one_offs)) if one_offs else false(), GroupAttendance.made_up_by.isnot(None))).all()}
        by_id = {}
        pointed = {rk: r.made_up_by for rk, r in rows.items() if r.made_up_by}
        if pointed:
            by_id = {o.id: o for o in self.db.query(GroupScheduleOneOffLesson).filter(GroupScheduleOneOffLesson.id.in_(set(pointed.values()))).all()}
        teachers = {str(g.id): g.teacher_id for g in self.db.query(Group).filter(Group.id.in_(gids)).all()} if (not manager and gids) else {}
        actor_id = _uuid(actor)
        for gid, lid, day in keys:
            key = (str(gid), lid, day)
            item: Dict[str, Any] = {}
            one_off = one_offs.get(lid)
            if one_off is not None and one_off.lesson_date == day:
                held = (rows.get(key) is not None and rows[key].status == "completed")
                money = one_off.money
                if money is None and one_off.purpose == MAKEUP and held and is_empty_roster_lesson(rows[key].records):
                    money = WAIT            # an empty make-up: the teacher's money waits as for an additional lesson
                own = manager or (actor_id is not None and (teachers.get(str(gid)) == actor_id or one_off.created_by == actor_id))
                shown = money if (money_visible and own) else None
                made_for = None
                if one_off.purpose == MAKEUP and one_off.made_up_lesson_id:
                    made_for = {"lessonId": one_off.made_up_lesson_id, "date": one_off.made_up_date}
                item = {"purpose": one_off.purpose, "purposeLabel": purpose_label(one_off.purpose, one_off.made_up_date), "madeUpFor": made_for, "money": shown,
                        "moneyLabel": money_label(shown, manager=manager, held=held)}
            row = rows.get(key)
            if row is not None and row.made_up_by and row.made_up_by in by_id:
                target = by_id[row.made_up_by]
                payload = target.payload or {}
                item["madeUpBy"] = {"lessonId": target.id, "date": target.lesson_date, "startTime": payload.get("startTime"), "endTime": payload.get("endTime"),
                                    "label": f"Отработан → {day_label(target.lesson_date)}"}
            if item:
                out[key] = item
        return out


def apply_marks(item: Any, marks: Optional[Dict[str, Any]]) -> None:
    """Set the fields of ``marks`` on an answer object (a model or a dict); a lesson without them gets the empty ones (the front reads one shape)."""
    empty = {"purpose": None, "purposeLabel": None, "madeUpFor": None, "madeUpBy": None, "money": None, "moneyLabel": None}
    values = {**empty, **(marks or {})}
    for name, value in values.items():
        if isinstance(item, dict):
            item[name] = value
        else:
            setattr(item, name, value)
