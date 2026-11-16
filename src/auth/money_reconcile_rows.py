"""Money-2: invariant I-5 of the nightly reconciliation - the money rows the product NEVER edits are compared row by row with the baseline of the last night.

The product does not change a row of ``student_payments`` / ``student_refunds`` / ``student_writeoffs`` / ``membership_opening_balances`` / ``membership_transfers`` / ``payroll_payouts`` /
``payroll_rates`` in place: a correction is a cancellation plus a new row; the only lawful move of a row is «live -> cancelled» with the fields of the cancellation filled (transfers and rates are
insert-only).  So what the check needs is not «was there some event that night» but «is this row, by its id, still what it was»: it does not depend on the clock or on anything else that
happened to the student.

The baseline (``money_row_baseline``) keeps, per row, the fingerprint (``md5`` over the immutable fields, computed by the database - the rows never travel to Python), the sum, the sign «cancelled»
and the fingerprint of the cancellation.  ``KINDS`` is the ONE table of what is fingerprinted; ``compare`` is the ONE comparison for all kinds.

Findings (key = ``kind:row id:code`` - the same row says the same key every night, so a repeat is never «new»):
  changed      - the row was edited in place (fingerprint differs);
  deleted      - the row is in the baseline and not in the table;
  reopened     - a cancelled row is live again / the fields of its cancellation changed / a transfer or a rate got cancelled (they are never cancelled);
  no_journal   - a row cancelled after the baseline has no line in the journal that names its id;
  foreign      - the row does not belong to the membership / centre it points at (reported every night until accepted, but the row is in the baseline all the same - the first night included);
  untraced     - a NEW payment with no receipt snapshot (every path that makes a payment writes one).
(e) «the live payments of a membership add up to ``ledger.paid``» is I-1 (c) already (``check_rows``): not repeated here.  The gross sum of the payments (I-2) moves only by the rows of THIS comparison:
the new live payments minus the payments cancelled since the baseline, counted by id - and only rows without a finding count (a row with a finding keeps its old baseline, so it would be «new» / «cancelled»
again every night while the snapshot of the sum has taken it in already; the finding itself is the signal).
A new row is not a finding by itself.  A row with a finding keeps its OLD baseline: the finding repeats every night until the team accepts it (``accept``); a mass re-baseline after a data
migration or a restore is ``rebaseline`` - both write the journal of the centre.  Rows are read BEFORE the journal, in the one ``REPEATABLE READ`` transaction of the centre, so a row and its line of the
journal (committed together) are always seen together.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from uuid import UUID

from sqlalchemy import BigInteger, Text, and_, case, cast, false, func, literal, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.models.auth import (
    GroupAudit, MembershipOpeningBalance, MembershipTransfer, MoneyReconcileCentre, MoneyReconcileFinding, MoneyReconcileRun, MoneyRowBaseline, OrgMoneyAudit, PayrollPayout, PayrollRate, PaymentReceiptSnapshot, StudentPayment,
    StudentRefund, StudentWriteoff,
)

from . import business_clock as bc

# ---- the lines of the journal this check relies on (written by the services, in the transaction of the action)
PAYOUT_CREATED = "payroll_payout_created"
PAYOUT_VOIDED = "payroll_payout_voided"
ACCEPTED = "money_reconcile_accept"
REBASELINED = "money_reconcile_rebaseline"
TRACE_ACTIONS = (PAYOUT_CREATED, PAYOUT_VOIDED, ACCEPTED, REBASELINED)        # lines of ``org_money_audit`` that are not about the settings of the centre (the settings page does not show them)

_SEP, _NULL = "\x1f", "\x1e~"
CHUNK = 1000


@dataclass(frozen=True)
class Journal:
    """Where the cancellation of a row must be named: ``model`` is ``GroupAudit`` (``snapshot``) or ``OrgMoneyAudit`` (``details``); ``entries`` pairs each action with the fields of the JSON that carry the id of the
    row the action CANCELLED (``opening_balance_set`` names the NEW row in ``opening_id``: that is the trace of a creation, only ``voided_opening_id`` is the trace of a cancellation)."""

    model: Any
    entries: Tuple[Tuple[str, Tuple[str, ...]], ...]       # (action, the fields of the JSON that name the row it CANCELLED): a line of the CREATION of a row never counts as the trace of its cancellation


@dataclass(frozen=True)
class RowKind:
    name: str
    model: Any
    fingerprint: Tuple[str, ...]                          # the fields the product never edits
    amount: Optional[Any] = None                          # the sum the baseline remembers (a SQL expression); None: the row has no sum
    can_void: bool = True                                 # False: the row is insert-only (a cancellation is a finding)
    journal: Optional[Journal] = None                     # where its cancellation is written
    owner_joins: str = ""                                 # SQL: the rows the row points at (alias ``t`` = the row)
    owner_wrong: str = ""                                 # SQL: a condition that is true when the row does not belong to what it points at
    membership_col: Optional[str] = None                  # the membership the sum of a payment belongs to (for the paid-delta of I-2)
    traced_by_receipt: bool = False                       # a NEW row must have a receipt snapshot


_STUDENT_MEMBERSHIP = ("JOIN student_groups sg ON sg.id = t.membership_id JOIN groups g ON g.id = sg.group_id",
                       "t.student_id <> sg.student_id OR t.group_id <> sg.group_id OR g.org_id <> t.org_id")
_COMMON = ("org_id", "created_by", "created_at")

KINDS: Tuple[RowKind, ...] = (
    RowKind("payment", StudentPayment,
            ("org_id", "membership_id", "student_id", "group_id", "amount", "currency", "paid_on", "valid_from", "valid_to", "lessons_count", "note", "method", "kind", "idempotency_key",
             "replaces_payment_id", "carried_from_payment_id", "carried_out_at", "receipt_no", "partial_period", "created_by", "created_at"),
            amount=case((and_(StudentPayment.kind == "payment", StudentPayment.amount > 0), StudentPayment.amount), else_=0),
            journal=Journal(GroupAudit, (("payment_void", ("payment_id",)), ("payment_replace", ("payment_id",)))),
            owner_joins=_STUDENT_MEMBERSHIP[0] + " LEFT JOIN student_payments rp ON rp.id = t.replaces_payment_id LEFT JOIN student_payments cp ON cp.id = t.carried_from_payment_id",
            owner_wrong=_STUDENT_MEMBERSHIP[1] + " OR (rp.id IS NOT NULL AND (rp.membership_id <> t.membership_id OR rp.org_id <> t.org_id))"
                                                 " OR (cp.id IS NOT NULL AND (cp.student_id <> t.student_id OR cp.org_id <> t.org_id))",
            membership_col="membership_id", traced_by_receipt=True),
    RowKind("refund", StudentRefund, ("org_id", "membership_id", "student_id", "group_id", "amount", "method", "reason", "note", "effective_on", "idempotency_key", "receipt_no", "created_by", "created_at"),
            amount=StudentRefund.amount, journal=Journal(GroupAudit, (("payment_refund_void", ("refund_id",)),)), owner_joins=_STUDENT_MEMBERSHIP[0], owner_wrong=_STUDENT_MEMBERSHIP[1]),
    RowKind("writeoff", StudentWriteoff, ("org_id", "membership_id", "student_id", "group_id", "amount", "reason", "effective_on", "idempotency_key", "created_by", "created_at"),
            amount=StudentWriteoff.amount, journal=Journal(GroupAudit, (("payment_writeoff_void", ("writeoff_id",)),)), owner_joins=_STUDENT_MEMBERSHIP[0], owner_wrong=_STUDENT_MEMBERSHIP[1]),
    RowKind("opening", MembershipOpeningBalance, ("org_id", "membership_id", "student_id", "group_id", "paid_until", "debt_amount", "debt_on", "note", "source", "created_by", "created_at"),
            amount=MembershipOpeningBalance.debt_amount,
            journal=Journal(GroupAudit, (("opening_balance_void", ("opening_id", "voided_opening_id")), ("opening_balance_set", ("voided_opening_id",)))), owner_joins=_STUDENT_MEMBERSHIP[0], owner_wrong=_STUDENT_MEMBERSHIP[1]),
    RowKind("transfer", MembershipTransfer, ("org_id", "student_id", "from_membership_id", "to_membership_id", "at", "created_by", "created_at"), can_void=False,
            owner_joins="JOIN student_groups sf ON sf.id = t.from_membership_id JOIN groups gf ON gf.id = sf.group_id JOIN student_groups st ON st.id = t.to_membership_id JOIN groups gt ON gt.id = st.group_id",
            owner_wrong="t.student_id <> sf.student_id OR t.student_id <> st.student_id OR gf.org_id <> t.org_id OR gt.org_id <> t.org_id"),
    RowKind("payout", PayrollPayout, ("org_id", "sheet_id", "teacher_id", "period_month", "amount", "paid_on", "method", "note", "sheet_revision", "sheet_total", "idempotency_key", "created_by", "created_at"),
            amount=PayrollPayout.amount, journal=Journal(OrgMoneyAudit, ((PAYOUT_VOIDED, ("payout_id",)),)),
            owner_joins="JOIN payroll_sheets ps ON ps.id = t.sheet_id", owner_wrong="ps.org_id <> t.org_id OR ps.teacher_id <> t.teacher_id OR ps.period_month <> t.period_month"),
    RowKind("rate", PayrollRate, ("org_id", "teacher_id", "lesson_type", "percent", "valid_from", "note", "created_by", "created_at"), can_void=False,
            owner_wrong="NOT EXISTS (SELECT 1 FROM organization_members om WHERE om.org_id = t.org_id AND om.user_id = t.teacher_id)"),
)
KIND = {k.name: k for k in KINDS}
STATE_CODES = ("foreign",)                      # findings of the STATE of a row (not of a move): accepting one is remembered on the row
ARRIVAL_CODES = ("untraced", "no_journal", "reopened")      # findings of a row that has no baseline yet (it ARRIVED): taking the row into the baseline closes them, so the ones not accepted are carried as «open:<code>»
OPEN = "open:"
FIELDS_OF = {"changed": ("fingerprint", "amount"), "reopened": ("voided", "void_fingerprint"), "no_journal": ("voided", "void_fingerprint")}      # what accepting ONE finding of a row that has a baseline takes from the row

Key = Tuple[str, UUID]


@dataclass(frozen=True)
class RowState:
    fingerprint: str
    amount: Optional[int]
    voided: bool
    void_fingerprint: Optional[str]
    membership_id: Optional[UUID] = None


@dataclass(frozen=True)
class BaseState:
    fingerprint: str
    amount: Optional[int]
    voided: bool
    void_fingerprint: Optional[str]
    accepted: frozenset = frozenset()


@dataclass
class RowFinding:
    kind: str
    row_id: UUID
    code: str
    was: Optional[int] = None
    now: Optional[int] = None
    state: Optional[Dict[str, Any]] = None                # the row as the night saw it (None: deleted): stored with the finding, so that an acceptance can tell the row has moved since

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.row_id}:{self.code}"


@dataclass
class RowVerdict:
    findings: List[RowFinding] = field(default_factory=list)
    confirm: Dict[Key, RowState] = field(default_factory=dict)        # rows whose baseline is written / updated (new rows, lawful moves)
    now: Dict[Key, RowState] = field(default_factory=dict)            # the rows as they are
    flagged: Set[Key] = field(default_factory=set)                    # rows with a finding of a MOVE (not ``foreign``): their baseline stays as it was
    paid_delta: Dict[UUID, int] = field(default_factory=dict)         # per membership: the payments added minus the payments cancelled since the baseline (rows WITHOUT a finding only)
    rows: int = 0
    baselined: int = 0                                                # rows taken into the baseline tonight
    first: bool = False                                               # the first night of the centre: the baseline is taken, nothing is compared


def state_of(state: Optional["RowState"]) -> Optional[Dict[str, Any]]:
    """What a finding remembers of the row (JSON): the fingerprint, whether it is cancelled, the fingerprint of the cancellation.  ``None``: the row is not in the table."""
    return None if state is None else {"fingerprint": state.fingerprint, "voided": state.voided, "void_fingerprint": state.void_fingerprint}


# ====================================================================================================== reading (SQL)
def _fingerprint_sql(model: Any, columns: Sequence[str]):
    parts = [func.coalesce(cast(getattr(model, c), Text), literal(_NULL)) for c in columns]
    return func.md5(func.concat_ws(literal(_SEP), *parts))


def scan(db, org_id: UUID) -> Dict[Key, RowState]:
    """Every money row of the centre as ``{(kind, id): state}``: the fingerprint is computed by the database."""
    out: Dict[Key, RowState] = {}
    for kind in KINDS:
        model = kind.model
        has_void = hasattr(model, "voided_at")          # a rate has no cancellation at all; a transfer has the columns, but nothing cancels it (a cancelled one is a finding)
        voided = model.voided_at.isnot(None) if has_void else false()
        void_fp = _fingerprint_sql(model, ("voided_at", "voided_by", "void_reason")) if has_void else literal(None, Text)
        amount = kind.amount if kind.amount is not None else literal(None, BigInteger)
        membership = getattr(model, kind.membership_col) if kind.membership_col else literal(None, Text)
        query = select(model.id, _fingerprint_sql(model, kind.fingerprint), amount, voided, void_fp, membership).where(model.org_id == org_id)
        for row_id, fp, amt, is_voided, vfp, mid in db.execute(query):
            out[(kind.name, row_id)] = RowState(fp, int(amt) if amt is not None else None, bool(is_voided), vfp if is_voided else None, mid)
    return out


def load_baseline(db, org_id: UUID) -> Dict[Key, BaseState]:
    rows = db.query(MoneyRowBaseline.kind, MoneyRowBaseline.row_id, MoneyRowBaseline.fingerprint, MoneyRowBaseline.amount, MoneyRowBaseline.voided, MoneyRowBaseline.void_fingerprint,
                    MoneyRowBaseline.accepted).filter(MoneyRowBaseline.org_id == org_id).all()
    return {(k, rid): BaseState(fp, amt, bool(v), vfp, frozenset(acc or ())) for k, rid, fp, amt, v, vfp, acc in rows}


def was_checked(db, org_id: UUID) -> bool:
    """An earlier night compared the rows of this centre (so an empty baseline now is not a first night)."""
    return db.query(MoneyReconcileCentre.run_id).filter(MoneyReconcileCentre.org_id == org_id, MoneyReconcileCentre.rows_checked.is_(True)).first() is not None


def _chunks(items: Sequence[Any]) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), CHUNK):
        yield items[start:start + CHUNK]


def journal_ids(db, org_id: UUID, wanted: Dict[str, Set[UUID]]) -> Set[Key]:
    """``{(kind, id)}`` of the rows (among ``wanted``) that a line of their journal names.  Called AFTER the rows were read."""
    out: Set[Key] = set()
    for name, ids in wanted.items():
        journal = KIND[name].journal
        if journal is None or not ids:
            continue
        column = journal.model.snapshot if journal.model is GroupAudit else journal.model.details
        for chunk in _chunks(sorted(ids, key=str)):
            as_text = [str(i) for i in chunk]
            for action, keys in journal.entries:
                for key in keys:
                    found = db.query(column[key].astext).filter(journal.model.org_id == org_id, journal.model.action == action, column[key].astext.in_(as_text)).all()
                    out |= {(name, UUID(v)) for (v,) in found if v}
    return out


def receipts_of(db, ids: Set[UUID]) -> Set[UUID]:
    """The payments (among ``ids``) that have the snapshot of a receipt."""
    out: Set[UUID] = set()
    for chunk in _chunks(sorted(ids, key=str)):
        out |= {pid for (pid,) in db.query(PaymentReceiptSnapshot.payment_id).filter(PaymentReceiptSnapshot.payment_id.in_(chunk)).all()}
    return out


def foreign_rows(db, org_id: UUID) -> Set[Key]:
    """The rows that do not belong to what they point at (the student / group / centre of their membership, the membership a correction replaces, the sheet of a payout)."""
    out: Set[Key] = set()
    for kind in KINDS:
        if not kind.owner_wrong:
            continue
        table = kind.model.__tablename__
        sql = text(f"SELECT t.id FROM {table} t {kind.owner_joins} WHERE t.org_id = :org AND ({kind.owner_wrong})")
        out |= {(kind.name, rid) for (rid,) in db.execute(sql, {"org": org_id})}
    return out


# ====================================================================================================== the comparison (pure)
def compare(now: Dict[Key, RowState], base: Dict[Key, BaseState], *, journal: Set[Key], receipts: Set[UUID], foreign: Set[Key], first: bool) -> RowVerdict:
    """The ONE comparison for all kinds.  ``journal``: rows a line of the journal names; ``receipts``: payments with a receipt snapshot; ``foreign``: rows that do not belong to what they point at."""
    verdict = RowVerdict(now=now, rows=len(now), first=first)
    paid_moves: Dict[Key, Tuple[UUID, int]] = {}          # row -> (membership, +sum of a new live payment / -sum of a payment cancelled since the baseline)

    def flag(key: Key, code: str, was: Optional[int] = None, new: Optional[int] = None) -> None:
        verdict.findings.append(RowFinding(key[0], key[1], code, was, new, state_of(now.get(key))))
        if code != "foreign":                                         # «the row does not belong to what it points at» is the STATE of the row, not a move: the row is taken into the baseline all the same (the finding repeats every night until accepted)
            verdict.flagged.add(key)

    for key in sorted(set(now) | set(base), key=lambda k: (k[0], str(k[1]))):
        kind, current, before = KIND[key[0]], now.get(key), base.get(key)
        if current is None:
            flag(key, "deleted", before.amount if before else None, None)
            continue
        if before is None:                                            # a NEW row
            if not first:
                if kind.traced_by_receipt and key[1] not in receipts:
                    flag(key, "untraced", None, current.amount)
                if current.voided and kind.journal is not None and key not in journal:
                    flag(key, "no_journal", None, current.amount)
                if current.voided and not kind.can_void:
                    flag(key, "reopened", None, current.amount)
            if key in foreign:
                flag(key, "foreign", None, current.amount)
            if kind.name == "payment" and not current.voided and not first:
                paid_moves[key] = (current.membership_id, current.amount or 0)
            if key not in verdict.flagged:
                verdict.confirm[key] = current
            continue
        if current.fingerprint != before.fingerprint:
            flag(key, "changed", before.amount, current.amount)
        if before.voided and not current.voided:
            flag(key, "reopened", before.amount, current.amount)
        elif before.voided and current.voided and current.void_fingerprint != before.void_fingerprint:
            flag(key, "reopened", before.amount, current.amount)
        elif not before.voided and current.voided:
            if not kind.can_void:
                flag(key, "reopened", before.amount, current.amount)
            elif kind.journal is not None and key not in journal:
                flag(key, "no_journal", before.amount, current.amount)
            if kind.name == "payment":
                paid_moves[key] = (current.membership_id, -(before.amount or 0))
        if key in foreign and "foreign" not in before.accepted:
            flag(key, "foreign", before.amount, current.amount)
        for carried in sorted(a for a in before.accepted if a.startswith(OPEN)):          # a finding of the arrival of the row that the team has NOT accepted yet (another one of the row was)
            flag(key, carried[len(OPEN):], before.amount, current.amount)
        if key not in verdict.flagged and (current.voided, current.void_fingerprint, current.amount) != (before.voided, before.void_fingerprint, before.amount):
            verdict.confirm[key] = current
    # a row with a finding keeps its old baseline, so it would be «new» / «cancelled» again every night while the snapshot of the paid sum has taken it in already: only a row WITHOUT a finding
    # explains a move of the sum (the finding itself is the signal for the rest)
    for key, (membership_id, amount) in paid_moves.items():
        if key not in verdict.flagged:
            verdict.paid_delta[membership_id] = verdict.paid_delta.get(membership_id, 0) + amount
    verdict.baselined = sum(1 for k in verdict.confirm if k not in base)
    return verdict


def wanted_for_journal(now: Dict[Key, RowState], base: Dict[Key, BaseState], first: bool) -> Dict[str, Set[UUID]]:
    """The rows whose cancellation the journal must name: cancelled now and (new, or live in the baseline)."""
    out: Dict[str, Set[UUID]] = {}
    if first:
        return out
    for key, current in now.items():
        before = base.get(key)
        if current.voided and KIND[key[0]].journal is not None and (before is None or not before.voided):
            out.setdefault(key[0], set()).add(key[1])
    return out


def evaluate(db, org_id: UUID) -> RowVerdict:
    """Read the rows of the centre FIRST, then the baseline and the journal (one ``REPEATABLE READ`` transaction: a row and its line of the journal are seen together) and compare."""
    now = scan(db, org_id)
    base = load_baseline(db, org_id)
    first = not base and not was_checked(db, org_id)
    new_payments = {k[1] for k in now if k[0] == "payment" and k not in base}
    receipts = receipts_of(db, new_payments) if new_payments and not first else set()
    journal = journal_ids(db, org_id, wanted_for_journal(now, base, first))
    return compare(now, base, journal=journal, receipts=receipts, foreign=foreign_rows(db, org_id), first=first)


# ====================================================================================================== writing the baseline (only ``run --apply``)
def store(db, org_id: UUID, run_id: UUID, verdict: RowVerdict, at: datetime) -> None:
    """Write what the night confirmed: new rows, lawful moves; a row with a finding is untouched.  Every other confirmed row gets the run that saw it last."""
    items = [(k, s) for k, s in verdict.confirm.items()]
    for chunk in _chunks(items):
        values = [{"kind": k[0], "row_id": k[1], "org_id": org_id, "fingerprint": s.fingerprint, "amount": s.amount, "voided": s.voided, "void_fingerprint": s.void_fingerprint,
                   "accepted": [], "first_run_id": run_id, "last_run_id": run_id, "first_seen_at": at, "last_confirmed_at": at} for k, s in chunk]
        insert = pg_insert(MoneyRowBaseline).values(values)
        db.execute(insert.on_conflict_do_update(index_elements=[MoneyRowBaseline.kind, MoneyRowBaseline.row_id], set_={
            "fingerprint": insert.excluded.fingerprint, "amount": insert.excluded.amount, "voided": insert.excluded.voided, "void_fingerprint": insert.excluded.void_fingerprint,
            "last_run_id": insert.excluded.last_run_id, "last_confirmed_at": insert.excluded.last_confirmed_at}))
    untouched = [k for k in verdict.now if k not in verdict.flagged and k not in verdict.confirm]
    for chunk in _chunks(untouched):
        db.query(MoneyRowBaseline).filter(MoneyRowBaseline.org_id == org_id, tuple_(MoneyRowBaseline.kind, MoneyRowBaseline.row_id).in_(list(chunk))).update(
            {"last_run_id": run_id, "last_confirmed_at": at}, synchronize_session=False)


_MISSING = object()


def _recorded_finding(db, org_id: UUID, key: str) -> Any:
    """The state of the row that the LAST successful night of the centre recorded with this finding (``None``: it was a deleted row); ``_MISSING`` when that night did not record the finding (it appeared since,
    or the night is an old one without the state)."""
    last = (db.query(MoneyReconcileCentre.run_id).join(MoneyReconcileRun, MoneyReconcileRun.id == MoneyReconcileCentre.run_id)
            .filter(MoneyReconcileCentre.org_id == org_id, MoneyReconcileCentre.status == "ok").order_by(MoneyReconcileRun.started_at.desc()).first())
    if last is None:
        return _MISSING
    details = db.query(MoneyReconcileFinding.details).filter(MoneyReconcileFinding.run_id == last[0], MoneyReconcileFinding.org_id == org_id, MoneyReconcileFinding.invariant == "I-5",
                                                              MoneyReconcileFinding.subject_key == key).scalar()
    return _MISSING if details is None or "state" not in details else details["state"]


def accept(db, key: str, reason: str, *, actor_id: Optional[UUID] = None, at: Optional[datetime] = None) -> Dict[str, Any]:
    """The team accepts ONE finding (``kind:row id:code``) with a reason: the baseline of the row takes the row as it is (a deleted row leaves the baseline; a state finding is remembered on
    the row) and the journal of the centre gets the key, the reason and the old / new fingerprint.  Refuses a key that is not a finding right now.  The caller commits."""
    reason = (reason or "").strip()
    if not 3 <= len(reason) <= 500:
        raise ValueError("Причина: от 3 до 500 символов")
    db.rollback()
    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))          # the same view of the centre as the night has
    try:
        kind_name, raw_id, code = key.split(":")
        row_id = UUID(raw_id)
        kind = KIND[kind_name]
    except (ValueError, KeyError):
        raise ValueError("Ключ находки: вид:id строки:код") from None
    org_id = db.query(MoneyRowBaseline.org_id).filter(MoneyRowBaseline.kind == kind_name, MoneyRowBaseline.row_id == row_id).scalar() or db.query(kind.model.org_id).filter(kind.model.id == row_id).scalar()
    if org_id is None:
        raise ValueError("Такой записи нет ни в таблице, ни в эталоне")
    verdict = evaluate(db, org_id)
    found = {f.key: f for f in verdict.findings}
    if key not in found:
        raise ValueError("Сейчас такой находки нет")
    now, at = verdict.now.get((kind_name, row_id)), at or bc.utc_now()
    seen = _recorded_finding(db, org_id, key)
    if seen is _MISSING:
        raise ValueError("Эту находку ещё не фиксировала ночная проверка — дождитесь ночного запуска")
    if seen != state_of(now):
        raise ValueError("Запись изменилась после находки — дождитесь ночного запуска и проверьте заново")
    before = db.get(MoneyRowBaseline, (kind_name, row_id))
    old_fp, new_fp = (before.fingerprint if before else None), (now.fingerprint if now else None)
    if code == "deleted":
        db.query(MoneyRowBaseline).filter(MoneyRowBaseline.kind == kind_name, MoneyRowBaseline.row_id == row_id).delete(synchronize_session=False)
    elif before is None:
        # the row ARRIVED with findings: the baseline takes the row, so the findings of the arrival that are not THIS one stay open on it (``open:<code>``); a state finding is remembered by its code
        others = sorted({f.code for f in verdict.findings if (f.kind, f.row_id) == (kind_name, row_id) and f.code != code and f.code in ARRIVAL_CODES})
        db.add(MoneyRowBaseline(kind=kind_name, row_id=row_id, org_id=org_id, fingerprint=now.fingerprint, amount=now.amount, voided=now.voided, void_fingerprint=now.void_fingerprint,
                                accepted=([code] if code in STATE_CODES else []) + [OPEN + o for o in others], first_seen_at=at, last_confirmed_at=at))
    elif code in STATE_CODES:
        before.accepted = sorted({*(before.accepted or []), code})
    elif OPEN + code in (before.accepted or []):
        before.accepted = [a for a in before.accepted if a != OPEN + code]
    else:
        for name in FIELDS_OF[code]:                          # ONE finding: only the fields of ITS code move; the other findings of the row stand
            setattr(before, name, getattr(now, name))
        before.last_confirmed_at = at
    db.add(OrgMoneyAudit(org_id=org_id, actor_id=actor_id, action=ACCEPTED, occurred_at=at, details={
        "key": key, "reason": reason, "old_fingerprint": old_fp, "new_fingerprint": new_fp, "text": f"Сверка: принята находка {key}. Причина: {reason}"}))
    db.flush()
    return {"org": str(org_id), "key": key, "oldFingerprint": old_fp, "newFingerprint": new_fp}


MAX_NAMED = 200


@dataclass
class CentrePlan:
    now: Dict[Key, RowState]
    divergent: List[str]          # ``kind:id`` of the rows whose fingerprint differs from the old baseline or that left the table
    findings: List[str]           # ``kind:id:code`` of the open findings of I-5


def rebaseline_plan(db, org_ids: Iterable[UUID]) -> Dict[UUID, CentrePlan]:
    """What a re-baseline would throw away, per centre."""
    plan: Dict[UUID, CentrePlan] = {}
    for org_id in org_ids:
        verdict = evaluate(db, org_id)
        base = load_baseline(db, org_id)
        divergent = [f"{k[0]}:{k[1]}" for k, b in sorted(base.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))) if k not in verdict.now or verdict.now[k].fingerprint != b.fingerprint]
        plan[org_id] = CentrePlan(verdict.now, divergent, [f.key for f in verdict.findings])
    return plan


def rebaseline(db, org_ids: Iterable[UUID], run_id: UUID, reason: str, at: datetime, *, actor_id: UUID, with_open_findings: bool) -> Dict[UUID, CentrePlan]:
    """Take the baseline of the given centres again from the rows as they are (after a data migration / a restore): the old baseline of the centre is replaced, one line of the journal of each centre
    says who, how many rows and why.  Refuses (``ValueError``, nothing is written) while a row differs from the old baseline or a finding is open, unless ``with_open_findings``: then the keys of the
    findings and the rows that differ are named in the journal.  The caller commits."""
    plan = rebaseline_plan(db, list(org_ids))
    open_findings = [k for c in plan.values() for k in c.findings]
    divergent = sum(len(c.divergent) for c in plan.values())
    if (open_findings or divergent) and not with_open_findings:
        raise ValueError(f"В эталоне есть расхождения: строк {divergent}, открытых находок {len(open_findings)} ({', '.join(open_findings[:5])}). "
                         "Разберите их (--accept) или повторите с --with-open-findings: тогда они будут названы в журнале, а пересъёмка их сотрёт")
    for org_id, centre in plan.items():
        db.query(MoneyRowBaseline).filter(MoneyRowBaseline.org_id == org_id).delete(synchronize_session=False)
        store(db, org_id, run_id, RowVerdict(now=centre.now, confirm=dict(centre.now)), at)
        db.add(OrgMoneyAudit(org_id=org_id, actor_id=actor_id, action=REBASELINED, occurred_at=at, details={
            "rows": len(centre.now), "reason": reason, "run_id": str(run_id), "divergentRows": len(centre.divergent), "divergent": centre.divergent[:MAX_NAMED], "findings": centre.findings[:MAX_NAMED],
            "text": f"Сверка: эталон записей денег снят заново ({len(centre.now)}); расходящихся строк {len(centre.divergent)}, открытых находок {len(centre.findings)}. Причина: {reason}"}))
    return plan
