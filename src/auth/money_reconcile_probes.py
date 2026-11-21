"""PAY-TAILS Б10: the invariant I-6 of the nightly reconciliation - «a trial record changes the account the way it must» (the rules of Б7 on the LIVE accounts).

Read only.  For every membership that has a forgiveness (every hole of Б7 had one) the account is checked as it stands, then three trial records are put on it - a payment «today», a payment dated at the
start of the stay, a forgiveness «today» - exactly the way the window of a payment does it (``ls.with_probe``: the stand-in credit entered at ``PROBE_MOMENT``, i.e. NOW, then ``run_ledger``).  Nothing is
written; no second copy of the calculation lives here.  The rules are those that the generator tests hold on random chains (``tests/pay_tails_b7_tools.py`` P1 / P2 / P5): a payment never raises the
debt, never raises the USED forgiveness, never gives more advance than its sum, and its sum is fully accounted for; a forgiveness takes exactly its sum off the debt and leaves the advance alone."""
from __future__ import annotations

from datetime import date, timedelta
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import billing_ledger as bl
from . import ledger_service as ls
from . import payments_texts as tx

SMALL = 1_000                                  # the small trial sum, sum
BACKDATE_LIMIT_DAYS = 3 * 365                  # the product does not take a date earlier than three years back


def _money(value: int) -> str:
    return tx.money(int(value))


@dataclass(frozen=True)
class ProbeFinding:
    """One broken rule: the probe (which record was tried), the rule (which promise broke), the sentence for the letter and the console, the numbers."""

    probe: str
    rule: str
    text: str
    expected: Optional[int]
    actual: Optional[int]
    details: Dict[str, Any] = field(default_factory=dict)


def _found(probe: str, rule: str, text: str, expected: Optional[int], actual: Optional[int], **details: Any) -> ProbeFinding:
    return ProbeFinding(probe, rule, text, expected, actual, details)


def _state(inp: bl.LedgerInput, ledger: bl.Ledger) -> List[ProbeFinding]:
    """1. The account as it stands: the USED forgiveness lies only on the periods that began, and no forgiveness is used above its sum.  (The advance is never above the money: the flag of the calculation,
    it is counted in I-1 - ``advance_exceeds_money`` - and not repeated here.)"""
    out: List[ProbeFinding] = []
    on_future = sum(s.forgiven for s in ledger.segments if not s.begun)
    if on_future > 0:
        out.append(_found("state", "forgiveness_on_future_period",
                          f"Использованное прощение лежит на периоде, который ещё не начался: {_money(on_future)}", 0, on_future))
    granted = {c.id: c.amount for c in inp.credits if c.kind == bl.WRITEOFF}
    over = sorted((used - granted.get(cid, 0), cid) for cid, used in ledger.forgiven_used_by.items() if used > granted.get(cid, 0))
    if over:
        extra, cid = over[-1]
        out.append(_found("state", "forgiveness_used_over_granted",
                          f"По прощению использовано больше, чем выдано: на {_money(extra)} (прощений с превышением: {len(over)})", granted.get(cid, 0), granted.get(cid, 0) + extra))
    return out


def _payment(label: str, day_text: str, amount: int, before: bl.Ledger, after: bl.Ledger) -> List[ProbeFinding]:
    """2 / 3. A trial payment: the debt does not grow, the used forgiveness does not grow, the advance grows by no more than the sum, and the sum is fully accounted for."""
    out: List[ProbeFinding] = []
    head = f"Проба оплаты {_money(amount)} {day_text}"
    snap = {"before": {"debt": before.debt, "advance": before.advance, "used": before.written_off}, "after": {"debt": after.debt, "advance": after.advance, "used": after.written_off}, "amount": amount}
    if after.debt > before.debt:
        out.append(_found(label, "debt_grew", f"{head} увеличила долг: было {_money(before.debt)}, стало {_money(after.debt)}", before.debt, after.debt, **snap))
    if after.written_off > before.written_off:
        out.append(_found(label, "used_grew",
                          f"{head} увеличила использованное прощение: было {_money(before.written_off)}, стало {_money(after.written_off)}", before.written_off, after.written_off, **snap))
    if after.advance - before.advance > amount:
        out.append(_found(label, "advance_over_sum",
                          f"{head} дала аванса больше суммы: аванс вырос с {_money(before.advance)} до {_money(after.advance)}", amount, after.advance - before.advance, **snap))
    spread = (before.debt - after.debt) + (after.advance - before.advance) + (before.written_off - after.written_off) + (after.charged - before.charged)
    if spread != amount:
        out.append(_found(label, "not_accounted",
                          f"{head} разошлась по счёту не полностью: долг, аванс, прощение и начисление изменились на {_money(spread)} вместо {_money(amount)}", amount, spread, **snap))
    return out


def _forgiveness(amount: int, before: bl.Ledger, after: bl.Ledger) -> List[ProbeFinding]:
    """4. A trial forgiveness «today» of min(debt, 1 000): the debt falls by exactly its sum, the advance does not move."""
    out: List[ProbeFinding] = []
    head = f"Проба прощения {_money(amount)} сегодня"
    snap = {"before": {"debt": before.debt, "advance": before.advance}, "after": {"debt": after.debt, "advance": after.advance}, "amount": amount}
    if before.debt - after.debt != amount:
        out.append(_found("forgive_today", "debt_not_exact",
                          f"{head} уменьшила долг не на сумму: было {_money(before.debt)}, стало {_money(after.debt)}", before.debt - amount, after.debt, **snap))
    if after.advance != before.advance:
        out.append(_found("forgive_today", "advance_moved",
                          f"{head} изменила аванс: было {_money(before.advance)}, стало {_money(after.advance)}", before.advance, after.advance, **snap))
    return out


def check_probes(inp: bl.LedgerInput, ledger: bl.Ledger) -> List[ProbeFinding]:
    """All I-6 findings of ONE membership (the caller adds the centre and the membership); nothing for a membership without a forgiveness."""
    if not any(c.kind == bl.WRITEOFF for c in inp.credits):
        return []
    out = _state(inp, ledger)
    today = inp.today
    sums = sorted({SMALL, max(ledger.debt, SMALL)})
    for amount in sums:
        label = "pay_today_small" if amount == SMALL else "pay_today_debt"
        out.extend(_payment(label, "сегодня", amount, ledger, bl.run_ledger(ls.with_probe(inp, bl.PAYMENT, today, amount))))
    first = min((s.first for s in inp.stays), default=None)
    if first is not None:
        back = min(today, max(first, today - timedelta(days=BACKDATE_LIMIT_DAYS)))
        for amount in sums:
            label = "pay_back_small" if amount == SMALL else "pay_back_debt"
            out.extend(_payment(label, f"задним числом ({tx.ddmm(back)})", amount, ledger, bl.run_ledger(ls.with_probe(inp, bl.PAYMENT, back, amount))))
    if ledger.debt > 0:
        amount = min(ledger.debt, SMALL)
        out.extend(_forgiveness(amount, ledger, bl.run_ledger(ls.with_probe(inp, bl.WRITEOFF, today, amount))))
    return out
