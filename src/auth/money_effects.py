"""Money-1 (A3, I-5): what an action does to money, in the answer of the server - one shape for every action that moves it.

``{"students": [...], "payroll": [...], "text": "...", "dryRun": bool}``:

* ``students`` - the lessons of the packs of the students that the action gives back (+1) or spends (-1), by the rule 4.3 (``payment_status.lesson_counts``);
* ``payroll`` - the SAVED sheets whose pay the action really moves: ``fresh total BEFORE`` != ``fresh total AFTER`` of THIS action (Money-1 R3 #21: not the drift that was there before it,
  and nothing at all when the difference is 0).  Everybody gets ``{"month", "state"}`` (``open`` | ``closed`` | ``reopened``, see ``closed_period.py``); only the OWNER also gets the sheet, the
  teacher, the saved total, ``freshBefore`` / ``freshAfter`` (= ``freshTotal``, the same in a dry run: the sum BEFORE the owner confirms), ``changedBy`` (= after - before) and ``driftAfter`` (= after - saved) - LESSONS 45;
* ``text`` - the sentence for the confirmation / the toast, in the words of the screen (no technical words);
* ``dryRun`` - the confirmation before the action (``True``: nothing was written) or the toast after it.

How the sheets are found for an action on the money of a student (Money-1 R3 #17): the lessons of the student are covered / priced by his account (``PaymentService.lesson_coverage``, what payroll
reads); the months where that coverage differs BEFORE and AFTER the action are the months touched, whatever their age (a debt of September paid in November names September).  For such a month
the fresh total of the sheet is worked out before and after.  ``run_with_effects`` runs the action twice: first inside a transaction that is rolled back (the numbers and the preview), then for real.

An error in the numbers never breaks the action (R3 #7): ``safe`` logs it and answers ``{"unavailable": true}``.  Nothing here commits.
"""
from __future__ import annotations

import functools
import logging
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from uuid import UUID

from src.models.auth import Group, PayrollPayout, PayrollSheet, Student, StudentGroup, User

from . import payment_status as ps
from .closed_period import month_first, month_title, period_state
from .ops_rules import short_name

logger = logging.getLogger(__name__)


def counted(mark: Optional[str], excuse_status: Optional[str]) -> bool:
    return ps.lesson_counts(mark=mark, excuse_status=excuse_status)


def student_deltas(before: Dict[str, Optional[str]], after: Dict[str, Optional[str]], status_before: Dict[str, Optional[str]], status_after: Dict[str, Optional[str]],
                   *, held_before: bool = True, held_after: bool = True) -> List[Dict[str, Any]]:
    """``[{"studentId", "delta"}]`` of the students whose pack gets a lesson back (+1) or spends one (-1) by the change; ``before`` / ``after`` are the marks, ``status_*`` the excuse statuses by
    student, ``held_*`` whether the lesson is completed before / after (only a completed lesson spends a pack, rule 4.3)."""
    out = []
    for sid in sorted(set(before) | set(after)):
        was = held_before and counted(before.get(sid), status_before.get(sid))
        will = held_after and counted(after.get(sid), status_after.get(sid))
        if was != will:
            out.append({"studentId": sid, "delta": (1 if was else 0) - (1 if will else 0)})       # +1: a lesson goes back into the pack, -1: one is spent
    return out


def package_only(db, org_id: Any, group_id: Any, deltas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Only the students who pay by LESSONS (a package) have a pack to spend: a mark never changes the account of a student who pays by the calendar (area-attendance.md 4.4)."""
    if not deltas or group_id is None:
        return deltas
    from src.models.auth import StudentGroup

    from .payments_service import PaymentService

    members = {str(sid): mid for mid, sid in db.query(StudentGroup.id, StudentGroup.student_id).filter(
        StudentGroup.group_id == group_id, StudentGroup.student_id.in_([UUID(d["studentId"]) for d in deltas])).all()}
    if not members:
        return []
    modes = {e.membership.id: e.ledger.mode for e in PaymentService(db)._loader(str(org_id), recalc=False).run(list(members.values()))}
    return [d for d in deltas if modes.get(members.get(d["studentId"])) == "lessons"]


def with_names(db, deltas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not deltas:
        return deltas
    ids = [UUID(d["studentId"]) for d in deltas]
    names = {str(s.id): f"{s.last_name or ''} {s.first_name or ''}".strip() for s in db.query(Student).filter(Student.id.in_(ids)).all()}
    return [{**d, "name": names.get(d["studentId"])} for d in deltas]


def num(n: int) -> str:
    """``125 000`` (a thin grouping by spaces; only the digits are touched - a comma in the sentence stays)."""
    return f"{abs(int(n)):,}".replace(",", " ")


def signed(n: int) -> str:
    return ("+" if n > 0 else "−") + num(n)


def unavailable(done: bool) -> Dict[str, Any]:
    """The block when the numbers could not be worked out: the action itself is fine (R3 #7)."""
    return {"students": [], "payroll": [], "text": "", "dryRun": not done, "unavailable": True}


def safe(done_of: Callable[..., bool] = lambda *a, **k: True):
    """Decorator for the functions that work the numbers out (``db`` is the first argument): an exception is logged and answered with ``unavailable`` - in a SAVEPOINT, so a failed read does not poison
    the transaction of the action that has just been written."""

    def wrap(fn):
        @functools.wraps(fn)
        def inner(db, *args, **kwargs):
            try:
                with db.begin_nested():
                    return fn(db, *args, **kwargs)
            except Exception:
                logger.exception("money effects of %s could not be worked out", fn.__name__)
                return unavailable(bool(done_of(*args, **kwargs)))

        return inner

    return wrap


@contextmanager
def writes_rolled_back(db):
    """Run a money action to its last line WITHOUT writing (R3 #15): ``commit`` only flushes, and at the exit everything is rolled back.  The action sees its own writes (it builds its answer from them),
    nothing reaches the database."""
    db.commit = db.flush
    try:
        yield
    finally:
        db.__dict__.pop("commit", None)
        db.rollback()


def _fresh(db, org_id: Any, sheet: PayrollSheet) -> Optional[int]:
    from .payroll_service import PayrollService

    return PayrollService(db)._fresh_total(str(org_id), sheet)


class SheetProbe:
    """The fresh totals of some SAVED sheets before and after one action.  ``lines`` names the ones the action moved."""

    def __init__(self, db, org_id: Any, owner: bool) -> None:
        self.db, self.org_id, self.owner = db, org_id, owner
        self.sheets: Dict[UUID, PayrollSheet] = {}
        self.before: Dict[UUID, Optional[int]] = {}
        self.after: Dict[UUID, Optional[int]] = {}

    def track(self, sheets: Iterable[PayrollSheet]) -> None:
        for sheet in sheets:
            self.sheets.setdefault(sheet.id, sheet)

    def measure(self, into: str) -> None:
        """R3 n4: one calculation of the month for ALL its tracked teachers (and one load of their rates), not one per sheet - a rate backdated over a year names hundreds of sheets."""
        from . import payroll_rates
        from .payroll_service import PayrollService

        bucket = self.before if into == "before" else self.after
        service = PayrollService(self.db)
        by_month: Dict[date, List[PayrollSheet]] = {}
        for sheet_id, sheet in self.sheets.items():
            if sheet_id not in bucket:
                by_month.setdefault(sheet.period_month, []).append(sheet)
        for first, sheets in by_month.items():
            teachers = {s.teacher_id for s in sheets}
            last = date(first.year + (first.month == 12), first.month % 12 + 1, 1) - timedelta(days=1)
            try:
                seed = service._collect(str(self.org_id), first, last, teacher_ids=teachers)
                book = payroll_rates.load_book(self.db, str(self.org_id), list(teachers))
            except Exception:
                logger.exception("could not calculate the month %s for the effects", first)
                seed = book = None
            for sheet in sheets:
                bucket[sheet.id] = service._fresh_total(str(self.org_id), sheet, seed, book) if seed is not None else _fresh(self.db, self.org_id, sheet)

    def lines(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for sheet_id, sheet in sorted(self.sheets.items(), key=lambda kv: (kv[1].period_month, str(kv[1].teacher_id))):
            before, after = self.before.get(sheet_id), self.after.get(sheet_id)
            if before is None or after is None or before == after:
                continue                                                  # no rate / nothing moved: no sentence about a sheet that does not change
            line: Dict[str, Any] = {"month": sheet.period_month.strftime("%Y-%m"), "state": period_state(self.db, self.org_id, sheet.teacher_id, sheet.period_month)["state"]}
            if self.owner:
                user = self.db.query(User).filter(User.id == sheet.teacher_id).first()
                paid = int(sum(p.amount for p in self.db.query(PayrollPayout).filter(PayrollPayout.sheet_id == sheet.id, PayrollPayout.voided_at.is_(None)).all()))
                line.update({"sheetId": str(sheet.id), "teacherId": str(sheet.teacher_id), "teacherName": short_name(user), "savedTotal": int(sheet.total_amount), "paid": paid,
                             "freshBefore": before, "freshAfter": after, "freshTotal": after, "changedBy": after - before, "driftAfter": after - int(sheet.total_amount)})
            out.append(line)
        return out


def text_of(students: List[Dict[str, Any]], payroll: List[Dict[str, Any]], *, done: bool, closed_blocks: bool = False) -> str:
    """The sentence under the confirmation (``done=False``) or in the toast after (``done=True``); empty when the action moves no money.  ``closed_blocks``: a lesson action is refused
    in a closed month (the sentence says to reopen it first); a payment is not (ДП-07)."""
    parts: List[str] = []
    gave = sum(1 for s in students if s["delta"] > 0)
    took = sum(1 for s in students if s["delta"] < 0)
    if took:
        parts.append(f"в пакетах учеников спишется занятие: {took}" if not done else f"в пакетах учеников списано занятие: {took}")
    if gave:
        parts.append(f"в пакеты учеников вернётся занятие: {gave}" if not done else f"в пакеты учеников возвращено занятие: {gave}")
    for line in payroll:
        title = month_title(date.fromisoformat(line["month"] + "-01"))
        note = " (месяц закрыт: сначала переоткройте его)" if line["state"] == "closed" and closed_blocks and not done else (" (месяц закрыт ведомостью)" if line["state"] == "closed" else "")
        if "changedBy" in line:
            who = f" {line['teacherName']}," if line.get("teacherName") else ""
            tail = f"{'изменилась' if done else 'изменится'} на {signed(line['changedBy'])} сум"
            if line["driftAfter"] != line["changedBy"]:
                tail += f" (всего к сохранённой {signed(line['driftAfter'])} сум)"
            parts.append(f"ведомость{who} {title} {tail}" + (" — пересохраните её" if done else "") + note)
        else:
            parts.append(f"ведомость {title} {'изменилась' if done else 'изменится'}" + note)
    text = "; ".join(parts)
    return (text[0].upper() + text[1:]) if text else ""


def _block(db, org_id: Any, students: List[Dict[str, Any]], payroll: List[Dict[str, Any]], *, done: bool, group_id: Any = None, closed_blocks: bool = False) -> Dict[str, Any]:
    names = with_names(db, package_only(db, org_id, group_id, students))
    return {"students": names, "payroll": payroll, "text": text_of(names, payroll, done=done, closed_blocks=closed_blocks), "dryRun": not done}


# ============================================================================================ the probes of the actions
@safe(lambda *a, **k: k.get("done", True))
def lesson_block(db, org_id: Any, *, students: List[Dict[str, Any]], probe: Optional[SheetProbe], owner: bool, done: bool, group_id: Any = None) -> Dict[str, Any]:
    """The block of an action on ONE lesson (return, correction, restore, decision on an excuse): ``probe`` was started BEFORE the action (``lesson_probe``) and is finished here (the action is flushed)."""
    lines: List[Dict[str, Any]] = []
    if probe is not None:
        probe.measure("after")
        lines = probe.lines()
    return _block(db, org_id, students, lines, done=done, group_id=group_id, closed_blocks=True)


def lesson_probe(db, org_id: Any, teachers: Iterable[Any], day: Any, owner: bool) -> Optional[SheetProbe]:
    """Start the probe of an action on the lesson of ``day``: the saved sheets of its teachers for its month, measured now (BEFORE the action).  ``None`` when there is nothing saved."""
    try:
        ids = [t for t in (UUID(str(t)) for t in teachers if t is not None)]
        if not ids:
            return None
        first = month_first(day)
        sheets = db.query(PayrollSheet).filter(PayrollSheet.org_id == org_id, PayrollSheet.teacher_id.in_(ids), PayrollSheet.period_month == first).all()
        if not sheets:
            return None
        probe = SheetProbe(db, org_id, owner)
        probe.track(sheets)
        with db.begin_nested():
            probe.measure("before")
        return probe
    except Exception:
        logger.exception("money effects probe of a lesson could not be started")
        return None


def teacher_probe(db, org_id: Any, first: date, owner: bool, teacher_id: Any = None) -> Optional[SheetProbe]:
    """The probe of a change of a rate / of the divisor from the month ``first``: the saved sheets of the teacher (or of the centre) from that month, measured BEFORE the change."""
    try:
        query = db.query(PayrollSheet).filter(PayrollSheet.org_id == org_id, PayrollSheet.period_month >= month_first(first))
        if teacher_id is not None:
            query = query.filter(PayrollSheet.teacher_id == teacher_id)
        sheets = query.all()
        if not sheets:
            return None
        probe = SheetProbe(db, org_id, owner)
        probe.track(sheets)
        with db.begin_nested():
            probe.measure("before")
        return probe
    except Exception:
        logger.exception("money effects probe of a rate could not be started")
        return None


class MembershipProbe:
    """The effect of an action on the money of a student on the saved sheets of the teachers of his group(s) (R3 #17)."""

    def __init__(self, db, org_id: Any, membership_ids: Iterable[Any], owner: bool) -> None:
        self.db, self.org_id, self.owner = db, org_id, owner
        self.membership_ids = [UUID(str(m)) for m in membership_ids]
        self.today = ps.business_today()
        self.sheet_rows: List[Tuple[UUID, UUID, date]] = []       # (sheet id, teacher, month) of the saved sheets of the teachers involved
        self.first: Optional[date] = None
        self.cover_before: Dict[Any, Tuple] = {}
        self.probe = SheetProbe(db, org_id, owner)
        self._ready = False

    def _teachers(self, membership_ids: Iterable[Any]) -> set:
        from .closed_period import group_teacher_ids

        groups = [g for g in self.db.query(Group).join(StudentGroup, StudentGroup.group_id == Group.id).filter(StudentGroup.id.in_(list(membership_ids))).all()]
        out: set = set()
        for group in groups:
            out |= group_teacher_ids(self.db, group, date(2000, 1, 1), self.today)
        return out

    def _cover(self, ids: Iterable[Any]) -> Dict[Any, Tuple]:
        from .payments_service import PaymentService

        found = PaymentService(self.db).lesson_coverage(str(self.org_id), list(ids), self.first, self.today)
        return {key: (c.paid, getattr(c.price, "amount", None) if c.price is not None else None, c.cover_source) for key, c in found.items()}

    def start(self) -> "MembershipProbe":
        """BEFORE the action: which saved sheets can be touched at all, and how the lessons of the student are covered now (two cheap reads)."""
        rows = self.db.query(PayrollSheet.id, PayrollSheet.teacher_id, PayrollSheet.period_month).filter(PayrollSheet.org_id == self.org_id).all()
        if not rows or not self.membership_ids:
            return self
        teachers = self._teachers(self.membership_ids)
        self.sheet_rows = [r for r in rows if r[1] in teachers]
        if not self.sheet_rows:
            return self
        self.first = min(r[2] for r in self.sheet_rows)
        self.cover_before = self._cover(self.membership_ids)
        self._ready = True
        return self

    def after_in_transaction(self, extra_membership_ids: Iterable[Any] = ()) -> None:
        """INSIDE the transaction of the dry run (the action is flushed, not committed): the lessons whose coverage changed name the months; the fresh totals of those sheets AFTER are worked out."""
        if not self._ready:
            return
        ids = list(dict.fromkeys([*self.membership_ids, *[UUID(str(m)) for m in extra_membership_ids]]))
        after = self._cover(ids)
        months = {month_first(key[1]) for key in set(after) | set(self.cover_before) if after.get(key) != self.cover_before.get(key)}
        wanted = [r[0] for r in self.sheet_rows if r[2] in months]
        if wanted:
            self.probe.track(self.db.query(PayrollSheet).filter(PayrollSheet.id.in_(wanted)).all())
            self.probe.measure("after")

    def before_after_rollback(self) -> None:
        """AFTER the rollback (the state as it was): the fresh totals of the same sheets BEFORE the action."""
        if self._ready and self.probe.sheets:
            self.probe.measure("before")

    def block(self, done: bool) -> Dict[str, Any]:
        return _block(self.db, self.org_id, [], self.probe.lines(), done=done)


def run_with_effects(db, org_id: Any, membership_ids: Iterable[Any], owner: bool, action: Callable[[], Any], *, dry_run: bool,
                     after_ids: Callable[[Any], Iterable[Any]] = lambda result: ()) -> Tuple[Any, Dict[str, Any]]:
    """Run ``action()`` (a service call of the money of a student that COMMITS) and say what it does to the saved sheets of the teachers.

    First the action runs inside a transaction that is rolled back (``writes_rolled_back``): its refusals are the refusals of the real one, the coverage of the lessons after it names the months, the
    fresh totals after are worked out; after the rollback the totals before.  With ``dry_run`` that is all: ``(the answer the action would give, the block)`` - nothing is written.  Otherwise the action
    runs again for real and the block of the first pass is what the answer says (the same numbers the confirmation showed).  A failure of the numbers is ``unavailable``, never an error of the action."""
    probe = None
    try:
        with db.begin_nested():
            probe = MembershipProbe(db, org_id, membership_ids, owner).start()
    except Exception:
        logger.exception("money effects probe of a student could not be started")
    preview = None
    try:
        with writes_rolled_back(db):
            preview = action()
            if probe is not None:
                try:
                    with db.begin_nested():
                        probe.after_in_transaction(after_ids(preview))
                except Exception:
                    logger.exception("money effects (after) could not be worked out")
                    probe = None
        if probe is not None:
            try:
                with db.begin_nested():
                    probe.before_after_rollback()
            except Exception:
                logger.exception("money effects (before) could not be worked out")
                probe = None
        block = probe.block(done=not dry_run) if probe is not None else unavailable(not dry_run)
    except Exception:
        if dry_run:
            raise                                   # a refusal of the action (PaymentError, ...) is the answer of the dry run, too
        block = unavailable(True)                   # the real run decides: it gives its own refusal or its own answer
    if dry_run:
        return preview, block
    return action(), block


# ============================================================================================ the deduction for a lesson the centre cancelled (restore)
def centre_deductions(db, org_id: Any, group_id: Any) -> Dict[str, int]:
    """``{student_id: recalculated}`` - what the centre has deducted from each student of the group for the lessons it cancelled (the screens' ledger, ``recalc=True``).  Read before and after
    the return of a cancelled lesson, the difference is what disappears with it.  Never raises: an unreadable ledger is an empty answer (the caller then says the rule in words)."""
    from src.models.auth import StudentGroup

    from .payments_service import PaymentService

    try:
        ids = [mid for (mid,) in db.query(StudentGroup.id).filter(StudentGroup.group_id == group_id).all()]
        out: Dict[str, int] = {}
        loader = PaymentService(db)._loader(str(org_id), recalc=True)
        for start in range(0, len(ids), 150):
            for entry in loader.run(ids[start:start + 150]):
                out[str(entry.membership.student_id)] = int(entry.ledger.recalculated)
        return out
    except Exception:
        logger.exception("could not read the deductions of group %s", group_id)
        return {}


def deductions_gone(db, org_id: Any, group_id: Any, was: Dict[str, int]) -> List[Dict[str, Any]]:
    """``[{"studentId", "name", "amount"}]`` of the students whose deduction got smaller since ``was`` (the lesson went back)."""
    now = centre_deductions(db, org_id, group_id)
    return with_names(db, [{"studentId": sid, "amount": amount - now.get(sid, 0)} for sid, amount in sorted(was.items()) if amount - now.get(sid, 0) > 0])


def deduction_text(gone: List[Dict[str, Any]], *, done: bool) -> str:
    if not gone:
        return ("У учеников вычет за этот урок не изменится" if not done else "Вычет учеников не изменился") + "; урок снова ждёт разбора — преподавателю он оплатится, когда его проведут"
    total = sum(g["amount"] for g in gone)
    who = f"у {len(gone)} " + ("ученика" if len(gone) == 1 else "учеников")
    return f"{who.capitalize()} {'исчез' if done else 'исчезнет'} вычет за этот урок: {num(total)} сум; урок снова ждёт разбора — преподавателю он оплатится, когда его проведут"
