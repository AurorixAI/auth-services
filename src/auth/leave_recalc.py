"""
ALGO - PAY-TAILS Б1.3 / Б5.1: «что было -> что станет» of a closing (a leaving, an archiving of a student, a transfer): ONE pure builder, no database.

``recalc_lines(before, after)`` takes two ledgers of the SAME membership on the SAME day: ``before`` - the account as the list of the centre shows it (the ordinary loader, no ``closing``), ``after`` - the account
with the stay closed today (the loader with ``closing``, the very account the window and the write use).  It matches the periods that have begun by ``period_start`` and says which charge falls, in which words
(the basis of the new sum is read from the segment of ``after``: no arithmetic of its own), which period that was paid ahead will never begin (the money on it - not its price - stays an advance), and what happens
to a forgiveness that covered the period.  The total of the window (``Останется долг ...``) is never derived from these lines: it is read from the account after the closing.

Nothing here writes, rounds or decides an amount.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence

from . import billing_ledger as bl
from . import payments_texts as tx

MAX_LINES = 2          # periods shown; the rest are told as a count («и ещё N периодов»)


@dataclass(frozen=True)
class RecalcItem:
    """One line before it is worded: ``kind`` - ``charge`` (the sum of a begun period falls), ``forgiven`` (the used forgiveness falls with it), ``ahead`` (a period paid ahead will not begin)."""

    kind: str
    label: str
    before: Optional[int] = None
    after: Optional[int] = None
    basis_text: Optional[str] = None


def _label(start: date, end: date) -> str:
    """``Ноябрь`` for a whole calendar month; ``01.11–30.11`` for any other period (a personal period)."""
    if start.day == 1 and (end + timedelta(days=1)).day == 1 and (start.year, start.month) == (end.year, end.month):
        return tx.MONTHS_NOMINATIVE[start.month - 1].capitalize()
    return tx.period_text(start, end)


def _basis(seg: bl.Segment) -> str:
    """The grey line under a charge: what the NEW sum is counted from (read from the segment, never recomputed)."""
    head = "пересчёт по день ухода"
    if seg.basis == "lessons" and seg.lessons_in_part is not None and seg.lessons_in_period is not None:
        return f"{head} · {seg.lessons_in_part} {tx.lessons_word(seg.lessons_in_part)} из {seg.lessons_in_period}"
    if seg.basis == "days" and seg.nominal_days:
        return f"{head} · {len(seg.days)} {tx.days_word(len(seg.days))} из {seg.nominal_days}"
    return head


def items(before: Optional[bl.Ledger], after: Optional[bl.Ledger]) -> List[RecalcItem]:
    """The lines of the change, oldest period first (see the module docstring); empty when the closing changes no charge and cancels no period that was paid ahead."""
    if before is None or after is None:
        return []
    begun_after = {s.period_start: s for s in after.segments if s.begun and s.kind != "opening"}
    out: List[RecalcItem] = []
    for seg in before.segments:
        if seg.kind == "opening" or not seg.begun:
            continue
        new = begun_after.get(seg.period_start)
        now = new.amount if new is not None else 0
        if now != seg.amount:
            out.append(RecalcItem("charge", _label(seg.period_start, seg.period_end), seg.amount, now, _basis(new) if new is not None else None))
    if out and before.written_off != after.written_off and before.written_off > 0:       # the forgiveness that covered the period falls with it: the money did not vanish
        out.append(RecalcItem("forgiven", "Прощено", before.written_off, after.written_off))
    for seg in before.segments:                                                           # paid ahead, never begins: what stays is the MONEY laid on it
        if seg.kind != "period" or seg.begun or seg.money <= 0:
            continue
        if seg.period_start in begun_after:
            continue
        out.append(RecalcItem("ahead", _label(seg.period_start, seg.period_end), None, seg.money))
    return out


def wording(item: RecalcItem, done: bool = False) -> Dict[str, Any]:
    """The line of the answer: ``{label, before, after, basis_text, text, done}``; ``done`` puts the sentence in the past tense (the message after the action)."""
    if item.kind == "ahead":
        text = f"{item.label} не {'начался' if done else 'начнётся'}: {tx.money(item.after or 0)} {'остались' if done else 'останутся'} авансом"
    elif item.kind == "forgiven":
        text = f"Прощено: {tx.num(item.before or 0)} → {tx.money(item.after or 0)}"
    else:
        text = f"{item.label}: {tx.num(item.before or 0)} → {tx.money(item.after or 0)}"
    return {"label": item.label, "before": item.before, "after": item.after, "basis_text": item.basis_text, "text": text, "done": done}


def lines_of(found: Sequence[RecalcItem], done: bool = False, group_name: Optional[str] = None) -> Optional[Any]:
    """The block of the answer, ALWAYS ``{"lines": [...], "more": N}`` (PAY-TAILS Б6.5): at most ``MAX_LINES`` periods (+ the line of the forgiveness that goes with them) and the number of the periods that were cut (``more``, 0 when all are shown; «и ещё N периодов»).
    ``group_name`` goes before the sentence when one answer speaks of several groups.  ``None`` when nothing changes (no block)."""
    periods = [i for i in found if i.kind != "forgiven"]
    kept: List[RecalcItem] = []
    shown = 0
    for i in found:
        if i.kind == "forgiven":
            if kept:
                kept.append(i)
            continue
        if shown < MAX_LINES:
            kept.append(i)
            shown += 1
    if not kept:
        return None
    words = [wording(i, done) for i in kept]
    if group_name:
        for w in words:
            w["text"] = f"{group_name}: {w['text']}"
    more = max(0, len(periods) - MAX_LINES)
    return {"lines": words, "more": more}
