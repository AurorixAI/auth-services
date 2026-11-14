"""Money-1 (A3, I-5): what an action does to money, in the answer of the server - one shape for every action that moves it.

``{"students": [...], "payroll": [...], "text": "...", "dryRun": bool}``:

* ``students`` - the lessons of the packs of the students that the action gives back (+1) or spends (-1), by the rule 4.3 (``payment_status.lesson_counts``);
* ``payroll`` - the SAVED sheets whose pay the action really moves: ``fresh total BEFORE`` != ``fresh total AFTER`` of THIS action (Money-1 R3 #21: not the drift that was there before it,
  and nothing at all when the difference is 0).  Everybody gets ``{"month", "state"}`` (``open`` | ``closed`` | ``reopened``, see ``closed_period.py``); only the OWNER also gets the sheet, the
  teacher, the saved total, ``freshBefore`` / ``freshAfter`` (= ``freshTotal``, the same in a dry run: the sum BEFORE the owner confirms), ``changedBy`` (= after - before) ``driftAfter`` (= after - saved), ``paid`` and ``balanceAfter`` (what is left to pay in the month after the action, by ``PayrollPayoutService.state_in_month``: the advance of the earlier months taken off) - LESSONS 45;
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
from .ops_rules import name_parts, short_name
from .payments_texts import NBSP, plural
from .payments_texts import num as tx_num

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
    """``125 000`` - THE formatter of ``payments_texts`` (non-breaking spaces); the sign is the caller's."""
    return tx_num(abs(int(n)))


def signed(n: int) -> str:
    return ("+" if n > 0 else "−" if n < 0 else "") + num(n)          # 0 has no sign (stage re-walk 05.11: «всего к сохранённой −0 сум»)


def signed_money(n: int) -> str:
    """``+125 000 сум`` / ``−45 000 сум`` (the unit joined to the number by a non-breaking space)."""
    return signed(n) + NBSP + "сум"


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
    had = "commit" in db.__dict__
    previous = db.__dict__.get("commit")                  # something (a test, a wrapper) may have put its own ``commit`` on the session: it comes back, not the class method
    db.commit = db.flush
    db.info["money_preview"] = True
    try:
        yield
    finally:
        db.__dict__.pop("commit", None)
        if had:
            db.__dict__["commit"] = previous
        db.info.pop("money_preview", None)
        db.rollback()


def is_preview(db) -> bool:
    """True inside ``writes_rolled_back``: the code that runs is a PREVIEW of an action - whatever it does outside the database (a call of exam-service, a letter) must not happen."""
    return bool(db.info.get("money_preview"))


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
        owner_lines: List[Tuple[Dict[str, Any], PayrollSheet, int]] = []
        for sheet_id, sheet in sorted(self.sheets.items(), key=lambda kv: (kv[1].period_month, str(kv[1].teacher_id))):
            before, after = self.before.get(sheet_id), self.after.get(sheet_id)
            if before is None or after is None or before == after:
                continue                                                  # no rate / nothing moved: no sentence about a sheet that does not change
            line: Dict[str, Any] = {"month": sheet.period_month.strftime("%Y-%m"), "state": period_state(self.db, self.org_id, sheet.teacher_id, sheet.period_month)["state"]}
            if self.owner:
                user = self.db.query(User).filter(User.id == sheet.teacher_id).first()
                paid = int(sum(p.amount for p in self.db.query(PayrollPayout).filter(PayrollPayout.sheet_id == sheet.id, PayrollPayout.voided_at.is_(None)).all()))
                line.update({"sheetId": str(sheet.id), "teacherId": str(sheet.teacher_id), "teacherName": short_name(user), **{"teacherFirstName": name_parts(user)["first_name"], "teacherLastName": name_parts(user)["last_name"]}, "savedTotal": int(sheet.total_amount), "paid": paid,
                             "freshBefore": before, "freshAfter": after, "freshTotal": after, "changedBy": after - before, "driftAfter": after - int(sheet.total_amount)})
                owner_lines.append((line, sheet, after))
            out.append(line)
        self._balances_after(owner_lines)
        return out

    def _balances_after(self, owner_lines: List[Tuple[Dict[str, Any], PayrollSheet, int]]) -> None:
        """``balanceAfter`` of the owner's lines: what is left to pay in the month AFTER the action - the number of THE payout function of the list and of the sheet (``state_in_month``: the fresh total
        of the open month is the target, the saved one of a closed month; the advance of the earlier months is taken off).  ``Уже выплачено 150 000 — до новой суммы останется 15 834``.  Negative: paid
        ahead.  One query of the payouts for all the sheets, one pair of queries for the carry of each month."""
        if not owner_lines:
            return
        from .payroll_payouts import PayrollPayoutService

        service = PayrollPayoutService(self.db)
        payouts = service.payouts_of(self.org_id, teacher_ids={sheet.teacher_id for _, sheet, _ in owner_lines})
        carries: Dict[date, Dict[UUID, int]] = {}
        for month in {sheet.period_month for _, sheet, _ in owner_lines}:
            carries[month] = service.carry_ins(self.org_id, {sheet.teacher_id for _, sheet, _ in owner_lines if sheet.period_month == month}, month)
        for line, sheet, after in owner_lines:
            state = service.state_in_month(self.org_id, sheet.teacher_id, sheet.period_month, saved_total=int(sheet.total_amount), fresh_total=after,
                                           items=payouts.get((sheet.teacher_id, sheet.period_month), []), carry_in=carries[sheet.period_month].get(sheet.teacher_id, 0))
            line["balanceAfter"] = int(state.balance)


def _of_students(count: int) -> str:
    """``у 1 ученика`` / ``у 3 учеников``: every student's pack spends or gets back ONE lesson."""
    return f"у {count} {plural(count, 'ученика', 'учеников', 'учеников')}"


def with_sentence(effects: Dict[str, Any], sentence: str) -> Dict[str, Any]:
    """The block of effects with one more sentence in its ``text`` (the debt that appears in the new group, Г-4); the block as it was when there is nothing to add."""
    if not sentence:
        return effects
    return {**effects, "text": "; ".join(x for x in (effects.get("text"), sentence) if x)}


def decision_texts(effects: Dict[str, Any], owner: bool) -> Dict[str, str]:
    """G3M С9: the payroll sentence of a money decision for the journal of decisions - the SAME sentence as the answer of the action (``text_of`` of the lines of the block).  The journal is read
    by the owner AND by an administrator with the right to money, the sums only by the owner: ``payroll_text_public`` has no sums (the lines cut to month and state), ``payroll_text`` (the answer
    as the owner read it) is kept only when the owner made the decision.  Nothing moved: no key."""
    lines = effects.get("payroll") or []
    if not lines:
        return {}
    texts = {"payroll_text_public": text_of([], [{"month": l["month"], "state": l["state"]} for l in lines], done=True)}
    if owner:
        texts["payroll_text"] = text_of([], lines, done=True)
    return texts


def text_of(students: List[Dict[str, Any]], payroll: List[Dict[str, Any]], *, done: bool, closed_blocks: bool = False, action_month: Optional[str] = None) -> str:
    """The sentence under the confirmation (``done=False``) or in the toast after (``done=True``); empty when the action moves no money.  ``closed_blocks``: a lesson action is refused
    in a closed month (the sentence says to reopen it first); a payment is not (ДП-07).  ``action_month`` (``YYYY-MM``, G3M С4): the month of the lesson the action is about - a line of ANOTHER month says why
    it moves (the recalculation pays off the oldest debt of the students, so the lessons of that month become paid / unpaid); unknown (a payment) - no reason, nothing is guessed."""
    parts: List[str] = []
    gave = sum(1 for s in students if s["delta"] > 0)
    took = sum(1 for s in students if s["delta"] < 0)
    if took:
        parts.append(f"из пакета {'спишется' if not done else 'списан'} 1 урок ({_of_students(took)})")
    if gave:
        parts.append(f"в пакет {'вернётся' if not done else 'возвращён'} 1 урок ({_of_students(gave)})")
    for line in payroll:
        title = month_title(date.fromisoformat(line["month"] + "-01"))
        note = " (месяц закрыт: сначала переоткройте его)" if line["state"] == "closed" and closed_blocks and not done else (" (месяц закрыт)" if line["state"] == "closed" else "")
        if "changedBy" in line:
            who = f" ({line['teacherName']})" if line.get("teacherName") else ""
            tail = f"{'изменилась' if done else 'изменится'} на {signed_money(line['changedBy'])}"
            if line["driftAfter"] != line["changedBy"]:
                tail += " (снова как сохранённая)" if line["driftAfter"] == 0 else f" (к сохранённой сумме: {signed_money(line['driftAfter'])})"
            cause = ""
            if action_month and line["month"] != action_month and line["changedBy"]:
                cause = " — перерасчёт погасит долг учеников за этот месяц" if line["changedBy"] > 0 else " — долг учеников за этот месяц вернётся"
            if done and line["driftAfter"] != 0:                      # the next step is on the screen all the time (the line «сохраните новую сумму»); the sentence says it once, with the state of the month
                parts.append(f"зарплата за {title}{who} {tail}{cause}{',' if cause else ' —'} " + ("месяц закрыт, " if line["state"] == "closed" else "") + "сохраните новую сумму")
            else:
                parts.append(f"зарплата за {title}{who} {tail}" + note + cause)
        else:
            parts.append(f"зарплата за {title} {'изменилась' if done else 'изменится'}" + note)
    text = "; ".join(parts)
    return (text[0].upper() + text[1:]) if text else ""


def _block(db, org_id: Any, students: List[Dict[str, Any]], payroll: List[Dict[str, Any]], *, done: bool, group_id: Any = None, closed_blocks: bool = False, action_month: Optional[str] = None) -> Dict[str, Any]:
    names = with_names(db, package_only(db, org_id, group_id, students))
    return {"students": names, "payroll": payroll, "text": text_of(names, payroll, done=done, closed_blocks=closed_blocks, action_month=action_month), "dryRun": not done}


def calendar_mode_students(db, org_id: Any, group_id: Any, student_ids: List[str]) -> Dict[str, str]:
    """``{student id: name}`` of the students of the group who pay by the CALENDAR (a month): the ones whose excused lesson gives no lesson back to a pack."""
    if not student_ids or group_id is None:
        return {}
    from src.models.auth import StudentGroup

    from .payments_service import PaymentService

    members = {str(sid): mid for mid, sid in db.query(StudentGroup.id, StudentGroup.student_id).filter(
        StudentGroup.group_id == group_id, StudentGroup.student_id.in_([UUID(x) for x in student_ids])).all()}
    if not members:
        return {}
    modes = {e.membership.id: e.ledger.mode for e in PaymentService(db)._loader(str(org_id), recalc=False).run(list(members.values()))}
    wanted = [{"studentId": sid, "delta": 0} for sid, mid in members.items() if modes.get(mid) == "calendar"]
    return {d["studentId"]: d["name"] for d in with_names(db, wanted)}


MAKEUP_TEXT = "Ученик может отработать пропущенный урок на дополнительном уроке или в другой группе"


def makeup_right(db, org_id: Any, group_id: Any, student_id: str, *, gained: bool) -> Optional[Dict[str, Any]]:
    """Money-1 B8 (the owner, 04.11): an excuse of a student who pays by the MONTH gives no lesson back to a pack and no money back; the answer says what the student can do - make the missed lesson up on an
    additional lesson or in another group (R3 code №15 / lead: nothing is promised that the system does not keep: no «право», no term, no record - the administrator plans the lesson).  ``gained``: the
    excuse is confirmed now; the withdrawal of a confirmation says nothing.  ``None``: not a month student / nothing changes / the reading failed.

    The reading of the account runs in a SAVEPOINT: a failure of the sentence never costs the decision on the excuse (R3 code №5; ``lesson_block`` is protected the same way)."""
    if not gained:
        return None
    try:
        with db.begin_nested():
            names = calendar_mode_students(db, org_id, group_id, [student_id])
    except Exception:
        logger.exception("the make-up sentence of an excuse could not be worked out student=%s", student_id)
        return None
    if student_id not in names:
        return None
    return {"studentId": student_id, "name": names[student_id], "text": MAKEUP_TEXT}


def with_makeup(block: Dict[str, Any], makeup: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Add the make-up right to the block of a decision on an excuse: ``makeup`` and one more sentence in ``text``."""
    if makeup is None:
        return block
    block["makeup"] = makeup
    sentence = makeup["text"]
    block["text"] = sentence if not block.get("text") else f"{block['text']}; {sentence[0].lower() + sentence[1:]}"
    return block


def merge_blocks(blocks: List[Dict[str, Any]], *, done: bool = False) -> Dict[str, Any]:
    """Money-1 G-14: the blocks of SEVERAL lessons (one ``lesson_block`` per decision, all worked out from the same saved state) as one block: the lessons of the packs that go back (``lessonsBack``) or
    are spent (``lessonsSpent``), the change of every sheet.  Sums are added, never recalculated: ``freshAfter`` / ``driftAfter`` / ``balanceAfter`` of an owner's line move by the total ``changedBy``.
    A line without ``changedBy`` (no money right) is only a month that changes."""
    students: Dict[str, Dict[str, Any]] = {}
    lines: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for block in blocks:
        for item in block.get("students", []):
            students.setdefault(item["studentId"], {**item, "delta": 0})["delta"] += item["delta"]
        for line in block.get("payroll", []):
            key = (line.get("teacherId", ""), line["month"])
            if key not in lines:
                lines[key] = {**line}
            elif "changedBy" in line:
                for field in ("changedBy", "freshAfter", "freshTotal", "driftAfter", "balanceAfter"):
                    if field in lines[key]:
                        lines[key][field] += line["changedBy"]
    package = sorted(students.values(), key=lambda s: str(s.get("name") or ""))
    back, spent = sum(max(s["delta"], 0) for s in package), sum(max(-s["delta"], 0) for s in package)
    payroll = [lines[k] for k in sorted(lines, key=lambda k: (k[1], k[0]))]
    parts: List[str] = []
    if spent:
        parts.append(f"из пакетов {'списано' if done else 'спишется'} {spent} {plural(spent, 'урок', 'урока', 'уроков')}")
    if back:
        parts.append(f"в пакеты {'возвращено' if done else 'вернётся'} {back} {plural(back, 'урок', 'урока', 'уроков')}")
    pay_text = text_of([], payroll, done=done, closed_blocks=True)
    text = "; ".join(parts + ([pay_text[0].lower() + pay_text[1:]] if pay_text else []))
    return {"students": package, "lessonsBack": back, "lessonsSpent": spent, "payroll": payroll,
            "text": (text[0].upper() + text[1:]) if text else "", "dryRun": not done}


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
        logger.exception("money effects probe of a lesson could not be started org=%s", org_id)
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
        logger.exception("money effects probe of a rate could not be started org=%s", org_id)
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

    def block(self, done: bool, action_month: Optional[str] = None) -> Dict[str, Any]:
        return _block(self.db, self.org_id, [], self.probe.lines(), done=done, action_month=action_month)


def run_with_effects(db, org_id: Any, membership_ids: Iterable[Any], owner: bool, action: Callable[[], Any], *, dry_run: bool,
                     after_ids: Callable[[Any], Iterable[Any]] = lambda result: (), action_month: Optional[str] = None, journal: bool = False) -> Tuple[Any, Dict[str, Any]]:
    """Run ``action()`` (a service call of the money of a student that COMMITS) and say what it does to the saved sheets of the teachers.

    First the action runs inside a transaction that is rolled back (``writes_rolled_back``): its refusals are the refusals of the real one, the coverage of the lessons after it names the months, the
    fresh totals after are worked out; after the rollback the totals before.  With ``dry_run`` that is all: ``(the answer the action would give, the block)`` - nothing is written.  Otherwise the action
    runs again for real and the block of the first pass is what the answer says (the same numbers the confirmation showed).  A failure of the numbers is ``unavailable``, never an error of the action.

    ``journal`` (G3M Р3-2, the money decisions that leave a line in the journal of decisions): ``action`` takes ONE argument - the payroll sentence for the line (``decision_texts`` of the block of the first
    pass; ``None`` in the pre-pass) - and writes it in the line in its own transaction: the journal says what the answer says, and a rolled-back action leaves neither."""
    probe = None
    try:
        with db.begin_nested():
            probe = MembershipProbe(db, org_id, membership_ids, owner).start()
    except Exception:
        logger.exception("money effects probe of a student could not be started org=%s", org_id)
    preview = None
    try:
        with writes_rolled_back(db):
            preview = action(None) if journal else action()
            if probe is not None:
                try:
                    with db.begin_nested():
                        probe.after_in_transaction(after_ids(preview))
                except Exception:
                    logger.exception("money effects (after) could not be worked out org=%s", org_id)
                    probe = None
        if probe is not None:
            try:
                with db.begin_nested():
                    probe.before_after_rollback()
            except Exception:
                logger.exception("money effects (before) could not be worked out org=%s", org_id)
                probe = None
        block = probe.block(done=not dry_run, action_month=action_month) if probe is not None else unavailable(not dry_run)
    except Exception as exc:
        from .payments_service import PaymentError

        if dry_run or isinstance(exc, PaymentError):
            raise                                   # a refusal of the action (PaymentError, ...) is the answer of the dry run - and of the real run: the pre-pass ran the same code, no second round of attempts
        block = unavailable(True)                   # any other failure of the pre-pass (not of the action itself): the real run decides, it gives its own refusal or its own answer
    if dry_run:
        return preview, block
    return (action(decision_texts(block, owner)) if journal else action()), block


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


def recalc_added(db, org_id: Any, group_id: Any, was: Dict[str, int]) -> List[Dict[str, Any]]:
    """``[{"studentId", "name", "amount"}]`` of the students whose deduction (the recalculation for the lessons the centre cancelled) got BIGGER since ``was`` - what a cancellation «по вине центра» just
    added; ``[]`` when the setting «вычитать уроки, отменённые центром» is off or the kind was «Праздник, выходной» (the ledger then has nothing to add)."""
    now = centre_deductions(db, org_id, group_id)
    return with_names(db, [{"studentId": sid, "amount": amount - was.get(sid, 0)} for sid, amount in sorted(now.items()) if amount - was.get(sid, 0) > 0])


def recalc_text(added: List[Dict[str, Any]], *, done: bool) -> str:
    """The sentence about the students for the dialog of «Урока не было»: ``Ученикам — перерасчёт: долг уменьшится сразу на 120 000 сум (3 ученика)`` / ``Ученикам перерасчёта не будет``."""
    if not added:
        return "Ученикам перерасчёта не будет" if not done else "Ученикам перерасчёта нет"
    count = len(added)
    return f"Ученикам — перерасчёт: долг {'уменьшился' if done else 'уменьшится'} сразу на {num(sum(a['amount'] for a in added))}{NBSP}сум ({count} {plural(count, 'ученик', 'ученика', 'учеников')})"


def cancel_consequence_text(added: List[Dict[str, Any]], *, manager: bool) -> str:
    """Money-1 Г-16: what a cancellation «по вине центра» means for the students, in words, for WHOEVER cancels (the sums of the whole group stay with the money right): the centre credits each
    student, or - setting off, a package, a holiday - the payment is not touched.  G3M С7: ``manager`` (owner / administrator of the centre, said by the road) - a teacher gets the sentence without any sum."""
    if not added:
        return "На оплату не влияет"
    if not manager:
        return "Ученикам будет перерасчёт за этот урок"
    amounts = sorted({a["amount"] for a in added})
    if len(amounts) == 1:
        return f"Ученикам зачтётся по {num(amounts[0])}{NBSP}сум"
    return f"Ученикам зачтётся от {num(amounts[0])} до {num(amounts[-1])}{NBSP}сум"


def lesson_cancel_consequence(db, org_id: Any, group_id: Any, cancel: Callable[[], Any], *, manager: bool) -> str:
    """Money-1 Р-4 (R3 В3): ``cancel_consequence_text`` of a cancellation «по вине центра» that has NOT happened - ``cancel()`` (THE cancellation of the lesson) runs to its last line inside a
    transaction that is rolled back: its refusals are the refusals of the real one, nothing is written, and what the students would be credited is read from the same ledger."""
    with writes_rolled_back(db):
        _, added = measure_cancellation(db, org_id, group_id, cancel)
    return cancel_consequence_text(added, manager=manager)


def measure_cancellation(db, org_id: Any, group_id: Any, cancel: Callable[[], Any]) -> Tuple[Any, List[Dict[str, Any]]]:
    """Money-1 Д-3: «было -> отмена -> стало» of a cancellation, the ONE place of the measurement: ``(the answer of cancel(), what it added to the deduction of each student)``.  The caller says
    whether it is rolled back (the sentence before the cancellation, the dry run) or real."""
    gid = UUID(str(group_id))
    was = centre_deductions(db, org_id, gid)
    out = cancel()
    return out, recalc_added(db, org_id, gid, was)


def deduction_text(gone: List[Dict[str, Any]], *, done: bool, past: bool = True) -> str:
    """What the return of a lesson that the centre cancelled does to the students (the word is «перерасчёт», never «вычет») and to the teacher; ``past``: the lesson had begun, so it goes back to the
    list «Уроки без отметки» (a future one just comes back into the schedule)."""
    tail = ("урок снова ждёт отметки — в зарплату преподавателя он войдёт, когда его проведут" if past else "урок снова в расписании")
    if not gone:
        return ("Перерасчёт учеников за этот урок не изменится" if not done else "Перерасчёт учеников не изменился") + f"; {tail}"
    total = sum(g["amount"] for g in gone)
    count = len(gone)
    who = f"{count} {plural(count, 'ученик', 'ученика', 'учеников')}"
    return f"Ученикам {'снят' if done else 'снимем'} перерасчёт за этот урок: всего {num(total)}{NBSP}сум ({who}); {tail}"


# ============================================================================================ Money-1 V9: the pay of the teacher in the windows of a price and of a forgiveness
def forgiveness_block(closes: Optional[Dict[str, Any]], effects: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The forgiven debt is PAID to the teacher (owner's decision 21.10, confirmed 04.11): the window names the period it closes and the pay (the sum only for the owner: the lines of the saved sheets carry
    ``changedBy`` only for him).  ``closes`` = ``{"from", "to", "label"}`` of the periods whose unpaid part the forgiveness closes; ``None`` - it closes nothing."""
    if not closes:
        return None
    pay = sum(int(line["changedBy"]) for line in (effects or {}).get("payroll", []) if int(line.get("changedBy") or 0) > 0)
    text = f"Прощение закроет период {closes['label']}. Эти уроки будут оплачены преподавателю"
    if pay:
        text += f": {signed_money(pay)}"
    return {"period": closes, "teacher_pay": pay or None, "text": text}


def teacher_share_text(new_amount: Optional[int]) -> str:
    """The window of an individual price: the pay of the teacher is a percent of the REAL price of the student (owner's answer 04.11), so a discount lowers it."""
    if new_amount is None:
        return "Зарплата преподавателя за этого ученика снова считается от цены группы"
    if new_amount == 0:
        return "Зарплата преподавателя за этого ученика станет 0: процент считается от фактической цены"
    return "Зарплата преподавателя за этого ученика тоже изменится: процент считается от фактической цены"
