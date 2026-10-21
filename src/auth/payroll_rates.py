"""
ALGO - Payroll rates (UX-2): the percent of a teacher per kind of lessons and month.  Owner only.

A rate is a row of ``payroll_rates`` (append-only): ``percent`` of a ``lesson_type`` of a teacher, valid from
``valid_from`` (always the 1st day of a month).  The rate for month M is the row with the greatest
``valid_from <= 1st of M``; rows with the same ``valid_from`` - the later ``created_at`` wins (a correction of the
same month).  Nothing is ever edited or deleted, the history is the rows.

``RateBook`` is the lookup, loaded once for a whole screen in ONE query; it has no clock and no HTTP.  The payroll
service asks it for the percents of a month and compares them with the percents a saved sheet was calculated
with (a different rate = "данные изменились"); the rates page reads and writes through ``payroll_rates_service``.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from src.models.auth import PayrollRate

from . import payroll_engine as engine


class RateBook:
    """Every rate of some teachers of a centre, grouped by (teacher, kind of lessons) and sorted by time."""

    def __init__(self, rows: Iterable[PayrollRate]) -> None:
        self._by_key: Dict[Tuple[UUID, str], List[PayrollRate]] = {}
        for row in rows:
            self._by_key.setdefault((row.teacher_id, row.lesson_type), []).append(row)
        for items in self._by_key.values():
            # equal valid_from: the later created_at wins, then a row a person wrote over a migrated one
            items.sort(key=lambda r: (r.valid_from, r.created_at, r.created_by is not None))

    def at(self, teacher_id: UUID, lesson_type: str, first: date) -> Optional[PayrollRate]:
        """The row in force for the month that starts on ``first``; None - no rate yet."""
        found = None
        for row in self._by_key.get((teacher_id, lesson_type), ()):
            if row.valid_from > first:
                break
            found = row  # sorted: the last one that qualifies has the greatest valid_from, then created_at
        return found

    def after(self, teacher_id: UUID, lesson_type: str, first: date) -> Optional[PayrollRate]:
        """The nearest FUTURE change: the row in force on the earliest ``valid_from`` after ``first`` (None - none)."""
        rows = [r for r in self._by_key.get((teacher_id, lesson_type), ()) if r.valid_from > first]
        if not rows:
            return None
        day = rows[0].valid_from
        return [r for r in rows if r.valid_from == day][-1]

    def percents_at(self, teacher_id: UUID, first: date) -> Dict[str, Decimal]:
        """``{kind: percent}`` of the kinds that have a rate in force for the month."""
        out: Dict[str, Decimal] = {}
        for lesson_type in engine.LESSON_TYPES:
            row = self.at(teacher_id, lesson_type, first)
            if row is not None:
                out[lesson_type] = Decimal(row.percent)
        return out

    def rows(self) -> List[PayrollRate]:
        return [r for items in self._by_key.values() for r in items]


def load_book(db: Session, org_id, teacher_ids: Optional[Iterable[UUID]] = None) -> RateBook:
    """One query: the rates of the centre (of ``teacher_ids`` when given)."""
    query = db.query(PayrollRate).filter(PayrollRate.org_id == org_id)
    if teacher_ids is not None:
        ids = list(set(teacher_ids))
        if not ids:
            return RateBook(())
        query = query.filter(PayrollRate.teacher_id.in_(ids))
    return RateBook(query.all())


def rate_changes(
    saved: Dict[str, str], fresh: Dict[str, Decimal]
) -> List[Tuple[str, Decimal, Optional[Decimal]]]:
    """Kinds whose percent on a saved sheet is not the rate in force now: ``[(kind, saved, fresh or None)]``.

    ``saved`` is ``percent_inputs`` of the sheet (only the kinds that have a line); a kind with no rate any more
    counts as a change, a rate of a kind the sheet does not have does not.
    """
    out = []
    for lesson_type in engine.LESSON_TYPES:
        if lesson_type not in saved:
            continue
        try:
            before = engine.parse_percent(saved[lesson_type])
        except engine.PercentError:
            continue
        now = fresh.get(lesson_type)
        if now is None or now != before:
            out.append((lesson_type, before, now))
    return out


def rate_reason(lesson_type: str, before: Decimal, now: Optional[Decimal]) -> str:
    """«Ставка «Группа»: 50% → 45%» - the line the owner reads in the diff."""
    after = f"{engine.format_percent(now)}%" if now is not None else "не задана"
    return f"Ставка «{engine.LESSON_TYPE_LABELS[lesson_type]}»: {engine.format_percent(before)}% → {after}"
