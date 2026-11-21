"""PAY-TAILS Б7.2: the generator of the ALLOWED worlds, the properties P1 / P2, the table «credit x period» and the shrinker of a counterexample (pure ledger, no database).

One command:   python tests/pay_tails_b7_tools.py count  SEED N          -> number of counterexamples per property
               python tests/pay_tails_b7_tools.py smallest PROPERTY SEED N -> the smallest world of a property with the table before / after
               B7_TREE=/path/to/another/worktree  python <this file> ...   -> the same on another checkout of ``src`` (a comparison with an older head)

What the product allows (facts, file:line on head 1d323dd / f4642e2):
  * a payment: ``paid_on`` in [today - 3 years, today] (payments_service.py:1490-1494 ``_check_date``; PaymentCreate at :1714); an administrator not earlier than 7 days (payment_status.py:100,
    payments_service.py:1497-1501), the owner without a limit; nothing compares the date with the start of the stay or of the books; ``entered_on`` = the business day of ``created_at`` (ledger_service.py:858).
  * a forgiveness: ``effective_on`` in the same range (payments_service.py:2090-2091), default today; the amount 1 .. the debt on the day of entry (:2107-2111, ``before.debt``); ``entered_on`` = the
    business day of ``created_at`` (ledger_service.py:861-862); a correction of a payment = void + a new payment entered now (:1848).
  * a transfer from a group: dated and entered the day of the transfer (ledger_service.py:870-878, no ``entered_on``); the opening balance is not a credit (``Opening``), it is exempt from «had the period begun».
  * the entry day is never after today; a pause is entered any day (``created_on``), also between the credits.
The generator below builds the history in the order of entry; a forgiveness is capped by the debt of the account AS IT WAS the day it was entered (``allowed``)."""
import os
import random
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta

_TREE = os.environ.get("B7_TREE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TREE)

from src.auth import billing_ledger as bl  # noqa: E402
from src.auth import payment_status as ps  # noqa: E402
from tests.pay2_ledger_helpers import D, T0, flat, make, weekly  # noqa: E402

TTF = weekly((1, 3, 4))
FIXED1 = bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)
BOOKS = D(2026, 9, 1)
EARLIEST = D(2026, 9, 15)                # the earliest date the generator gives a payment / a forgiveness (the product allows 3 years)
PROPERTIES = ("P1 debt", "P1 used", "P1 advance", "P1 advance<=money", "P1 identity", "P2 debt", "P2 advance<=money", "P2 advance today", "P2 advance back")


def _debt_on(inp, day, entered_before):
    """The debt of the account as it was on ``day``: the credits entered before, the pauses entered by then."""
    seen = tuple(c for c in inp.credits if entered_before(c))
    pauses = tuple(p for p in inp.pauses if p.created_on is None or p.created_on <= day)
    return bl.run_ledger(replace(inp, today=day, credits=seen, pauses=pauses)).debt


def allowed(inp):
    """Every forgiveness is not larger than the debt of the day it was entered (the product refuses more); every credit is entered not before its... (any date is allowed) and not after today."""
    for c in inp.credits:
        e = c.entered_on or c.on
        if e > inp.today:
            return False
        if c.kind == bl.WRITEOFF:
            key = (e, c.created_at)
            if c.amount > _debt_on(inp, e, lambda o: (o.entered_on or o.on, o.created_at) < key):
                return False
    return True


def base_world(rng):
    common = rng.random() < 0.6
    price = 600_000 if common else 420_000
    today = D(2026, 11, 3) + timedelta(days=rng.randint(0, 45))
    inp = make(today=today, stays=[bl.Stay(D(2026, 10, 1))], price_at=flat(price), regimes=[FIXED1] if common else [bl.Regime()], start=BOOKS, lesson_days=TTF)
    if common and rng.random() < 0.3:
        cancel = D(2026, 10, 13) + timedelta(days=7 * rng.randint(0, 3))
        inp = replace(inp, centre_cancels=(bl.CentreCancel(cancel, "l"),), recalc_versions=(bl.RecalcVersion(date.min, True),))
    if rng.random() < 0.7:
        first = D(2026, 10, 5) + timedelta(days=rng.randint(0, 40))
        last = first + timedelta(days=rng.randint(2, 18))
        entered = D(2026, 9, 25) + timedelta(days=rng.randint(0, (today - D(2026, 9, 25)).days))        # any day, also BETWEEN the credits
        inp = replace(inp, pauses=(bl.Pause(first, last, created_on=entered),))
    return inp


def _any_day(rng, lo, hi):
    return lo + timedelta(days=rng.randint(0, max(0, (hi - lo).days)))


def allowed_world(rng):
    """The history in the order of entry: 1-4 credits, each entered on a day <= today; a forgiveness <= the debt of its entry day; a date from ``EARLIEST`` to the entry day."""
    inp = base_world(rng)
    days = sorted(_any_day(rng, D(2026, 10, 6), inp.today) for _ in range(rng.randint(1, 4)))
    credits = []
    for i, e in enumerate(days):
        on = _any_day(rng, EARLIEST, e) if rng.random() < 0.5 else e
        created = T0 + timedelta(minutes=i)
        if rng.random() < 0.5:
            credits.append(bl.Credit(f"c{i}", bl.PAYMENT, on, created, rng.choice([100_000, 150_000, 200_000, 300_000, 420_000, 600_000]), entered_on=e))
        else:
            debt = _debt_on(replace(inp, credits=tuple(credits)), e, lambda o: True)
            if debt > 0:
                credits.append(bl.Credit(f"c{i}", bl.WRITEOFF, on, created, min(debt, rng.choice([100_000, 150_000, 200_000, 300_000, 420_000, 600_000])), entered_on=e))
    if not credits:
        credits.append(bl.Credit("c0", bl.PAYMENT, inp.today, T0, 200_000, entered_on=inp.today))
    return replace(inp, credits=tuple(credits))


def new_payment(rng, inp, name="late"):
    """A payment entered NOW (today); the date any allowed one."""
    on = inp.today if rng.random() < 0.3 else _any_day(rng, EARLIEST, inp.today)
    return bl.Credit(name, bl.PAYMENT, on, T0 + timedelta(minutes=5000), rng.choice([100_000, 200_000, 300_000, 420_000, 600_000]), entered_on=inp.today)


def new_forgiveness(rng, inp, name="extra"):
    """A forgiveness entered NOW: 1 .. the debt now; dated today or back; None when there is no debt."""
    debt = bl.run_ledger(inp).debt
    if debt <= 0:
        return None
    on = inp.today if rng.random() < 0.4 else _any_day(rng, EARLIEST, inp.today)
    return bl.Credit(name, bl.WRITEOFF, on, T0 + timedelta(minutes=5000), min(debt, rng.choice([100_000, 150_000, 200_000, 300_000, 420_000, 600_000])), entered_on=inp.today)


def money_of(inp):
    return sum(c.amount for c in inp.credits if c.kind != bl.WRITEOFF)


def violations(inp, x):
    """The names of the properties that the addition of ``x`` (a payment or a forgiveness entered today) breaks."""
    before = bl.run_ledger(inp)
    new = replace(inp, credits=(*inp.credits, x))
    after = bl.run_ledger(new)
    bad = []
    if after.flags or not after.reconciles:
        bad.append("flags")
    if x.kind == bl.PAYMENT:
        if after.debt > before.debt:
            bad.append("P1 debt")
        if after.written_off > before.written_off:
            bad.append("P1 used")
        if after.advance - before.advance > x.amount:
            bad.append("P1 advance")
        if after.advance > money_of(new):
            bad.append("P1 advance<=money")
        if (before.debt - after.debt) + (after.advance - before.advance) + (before.written_off - after.written_off) + (after.charged - before.charged) != x.amount:
            bad.append("P1 identity")
    else:
        if after.debt > before.debt:
            bad.append("P2 debt")
        if after.advance > money_of(new):
            bad.append("P2 advance<=money")
        if x.on == inp.today and after.advance != before.advance:
            bad.append("P2 advance today")
        if x.on < inp.today and after.advance - before.advance > x.amount:
            bad.append("P2 advance back")
    return bad


# ------------------------------------------------------------------------------------------------------------------------------ the table «credit x period»

def explain(inp):
    """The table of one world: for every credit and period - the capacity in the eyes of the credit, what the tying put there («tied»), what the passes left («placed»); then the periods."""
    captured = []
    real = bl._place_credits

    def spy(*args):
        out = real(*args)
        captured.append((args, out))
        return out

    bl._place_credits = spy
    try:
        L = bl.run_ledger(inp)
    finally:
        bl._place_credits = real
    (segments, credits, net, recalcs), placed = captured[-1]
    tied = {c.id: {s.id: 0 for s in segments} for c in credits}
    filled = {s.id: 0 for s in segments}
    for c in credits:                      # the same greedy as the tying of ``_place_credits`` (read-only copy, for the table)
        entered = c.entered_on or c.on
        rest = net[c.id]
        for s in segments:
            if rest <= 0:
                break
            part = min(rest, max(0, s.capacity(entered) - filled[s.id]))
            tied[c.id][s.id] += part
            filled[s.id] += part
            rest -= part
    lines = [f"today {inp.today}  charged {L.charged}  debt {L.debt}  advance {L.advance}  used {L.written_off} of {L.written_off_granted}  covered_through {L.covered_through}"]
    lines.append("  pauses: " + (", ".join(f"{p.first}..{p.last} entered {p.created_on}" for p in inp.pauses) or "-") + "   centre cancels: " + (", ".join(str(c.day) for c in inp.centre_cancels) or "-"))
    head = "  credit (kind, date, entered, amount)".ljust(52) + "".join(f"| {s.period_start} (charge {s.amount}, {'begun' if s.begun else 'future'})".ljust(46) for s in segments) + "| burned / leftover"
    lines.append(head)
    for c in credits:
        cells = []
        for s in segments:
            got = placed.parts.get((s.id, c.id), 0)
            cells.append(f"| cap {s.capacity(c.entered_on or c.on)} tied {tied[c.id][s.id]} placed {got}".ljust(46))
        gone = net[c.id] - sum(placed.parts.get((s.id, c.id), 0) for s in segments) - sum(a for cid, a in placed.leftover if cid == c.id)
        left = sum(a for cid, a in placed.leftover if cid == c.id)
        lines.append(f"  {c.id:5s} {c.kind[:3]} {c.on} {c.entered_on or c.on} {net[c.id]}".ljust(52) + "".join(cells) + f"| burned {gone}, leftover {left}")
    for what, key in (("money", placed.money), ("forgiven", placed.forgiven), ("recalc", placed.recalculated)):
        lines.append(f"  {what:9s}".ljust(52) + "".join(f"| {key[s.id]}".ljust(46) for s in segments))
    lines.append("  unpaid".ljust(52) + "".join(f"| {s.amount - placed.money[s.id] - placed.forgiven[s.id] - placed.recalculated[s.id]}".ljust(46) for s in segments))
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------------------------------------ the shrinker

def shrink(inp, x, prop):
    """Make the world smaller while ``prop`` is still broken by the addition of ``x`` and the world stays allowed: drop a credit / a pause / the cancels, lower an amount, move the dates
    towards the entry day, lower ``today``.  Returns (inp, x)."""
    def ok(i, xx):
        return allowed(i) and prop in violations(i, xx)

    changed = True
    while changed:
        changed = False
        steps = []
        for k in range(len(inp.credits)):
            steps.append((replace(inp, credits=inp.credits[:k] + inp.credits[k + 1:]), x))
        if inp.pauses:
            steps.append((replace(inp, pauses=()), x))
        if inp.centre_cancels:
            steps.append((replace(inp, centre_cancels=(), recalc_versions=()), x))
        for k, c in enumerate(inp.credits):
            for amount in (100_000, 200_000, 300_000, 420_000, 600_000):
                if amount < c.amount:
                    steps.append((replace(inp, credits=inp.credits[:k] + (replace(c, amount=amount),) + inp.credits[k + 1:]), x))
            if (c.entered_on or c.on) != c.on:
                steps.append((replace(inp, credits=inp.credits[:k] + (replace(c, entered_on=c.on),) + inp.credits[k + 1:]), x))
        for amount in (100_000, 200_000, 300_000, 420_000):
            if amount < x.amount:
                steps.append((inp, replace(x, amount=amount)))
        steps.append((inp, replace(x, on=inp.today)))
        for step_inp, step_x in steps:
            if step_inp.credits and (step_inp, step_x) != (inp, x) and ok(step_inp, step_x):
                inp, x = step_inp, step_x
                changed = True
                break
    return inp, x


def describe(inp, x):
    lines = [f"regime {inp.regimes[0].anchor}, price {inp.price_at(inp.today).amount if callable(inp.price_at) else '?'}, stay from {inp.stays[0].first}, books from {BOOKS}, lessons Tue/Thu/Fri, today {inp.today}"]
    for c in inp.credits:
        lines.append(f"  credit {c.id}: {c.kind} {c.amount} dated {c.on} entered {c.entered_on} (order {int((c.created_at - T0).total_seconds() // 60)})")
    lines.append(f"  ADDED  {x.id}: {x.kind} {x.amount} dated {x.on} entered {x.entered_on}")
    return "\n".join(lines)


def run_counts(seed, n):
    rng = random.Random(seed)
    counts = {p: 0 for p in PROPERTIES}
    counts["flags"] = 0
    found = {}
    worlds = pays = forgs = 0
    for _ in range(n):
        inp = allowed_world(rng)
        worlds += 1
        for kind in (bl.PAYMENT, bl.WRITEOFF):
            x = new_payment(rng, inp) if kind == bl.PAYMENT else new_forgiveness(rng, inp)
            if x is None:
                continue
            pays += kind == bl.PAYMENT
            forgs += kind == bl.WRITEOFF
            for name in violations(inp, x):
                counts[name] += 1
                found.setdefault(name, []).append((inp, x))
    return counts, found, (worlds, pays, forgs)


def main(argv):
    cmd = argv[1]
    if cmd == "count":
        total = {}
        for seed in range(int(argv[2]), int(argv[2]) + int(argv[4]) if len(argv) > 4 else int(argv[2]) + 1):
            counts, _, size = run_counts(seed, int(argv[3]))
            for k, v in counts.items():
                total[k] = total.get(k, 0) + v
            total["_worlds/payments/forgivenesses"] = tuple(a + b for a, b in zip(total.get("_worlds/payments/forgivenesses", (0, 0, 0)), size))
        print(" | ".join(f"{k}: {v}" for k, v in total.items()))
    elif cmd == "smallest":
        prop, seed, n = argv[2], int(argv[3]), int(argv[4])
        _, found, _ = run_counts(seed, n)
        if prop not in found:
            print("no counterexample of", prop)
            return
        best = None
        for inp, x in found[prop][:60]:
            small, sx = shrink(inp, x, prop)
            size = (len(small.credits), len(small.pauses), len(small.centre_cancels))
            if best is None or size < best[0]:
                best = (size, small, sx)
        _, small, sx = best
        print(describe(small, sx))
        print("BEFORE\n" + explain(small))
        print("AFTER\n" + explain(replace(small, credits=(*small.credits, sx))))
        print("broken:", violations(small, sx))


if __name__ == "__main__":
    main(sys.argv)
