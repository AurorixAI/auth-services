"""PAY-TAILS Б7.2: the generator of the ALLOWED worlds, the properties P1 / P2, the table «credit x period» and the shrinker of a counterexample (pure ledger, no database).

One command:   python tests/pay_tails_b7_tools.py count  SEED N          -> number of counterexamples per property
               python tests/pay_tails_b7_tools.py smallest PROPERTY SEED N -> the smallest world of a property with the table before / after
               B7_TREE=/path/to/another/worktree  python <this file> ...   -> the same on another checkout of ``src`` (a comparison with an older head)
               B7_CANCELS=none|with   -> the generator WITHOUT a lesson cancelled by the centre (no deduction on the line) / ONLY with one (common month, a cancel in October); unset = the old mix (30 % of the common months)
               python <this file> dump SEED N  -> one JSON line per world with the TOTALS of the account (a comparison of two trees: B7_TREE=old ... > a.txt; the new ... > b.txt; diff)
  R4 (Б7.8 шаг 5): the cancel of a lesson by the centre is an EVENT of the chain with its own moment (``entered``, the setting of the deductions may be switched on later: ``RecalcVersion.entered``);
  the moment of the event is the later of the two.

What the product allows (facts, file:line on head 1d323dd / f4642e2):
  * a payment: ``paid_on`` in [today - 3 years, today] (payments_service.py:1490-1494 ``_check_date``; PaymentCreate at :1714); an administrator not earlier than 7 days (payment_status.py:100,
    payments_service.py:1497-1501), the owner without a limit; nothing compares the date with the start of the stay or of the books; the day of entry = the business day of ``created_at`` (ledger_service.py:858).
  * a forgiveness: ``effective_on`` in the same range (payments_service.py:2090-2091), default today; the amount 1 .. the debt on the day of entry (:2107-2111, ``before.debt``); the day of entry = the
    business day of ``created_at`` (ledger_service.py:861-862); a correction of a payment = void + a new payment entered now (:1848).
  * a transfer from a group: dated and entered the day of the transfer (ledger_service.py:870-878, ``created_at`` = the moment of the transfer); the opening balance is not a credit (``Opening``), it is exempt from «had the period begun».
  * the entry day is never after today; a pause is entered any day (its first form is the moment ``created_at``), also between the credits.
The generator below builds the history in the order of entry; a forgiveness is capped by the debt of the account AS IT WAS the day it was entered (``allowed``)."""
import os
import random
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

_TREE = os.environ.get("B7_TREE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TREE)

from src.auth import billing_ledger as bl  # noqa: E402
from src.auth import payment_status as ps  # noqa: E402
from tests.pay2_ledger_helpers import D, at, edit_pause, flat, make, pause, weekly  # noqa: E402

TTF = weekly((1, 3, 4))
FIXED1 = bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)
BOOKS = D(2026, 9, 1)
EARLIEST = D(2026, 9, 15)                # the earliest date the generator gives a payment / a forgiveness (the product allows 3 years)
PROPERTIES = ("P1 debt", "P1 used", "P1 advance", "P1 advance<=money", "P1 identity", "P2 debt", "P2 advance<=money", "P2 advance today", "P2 advance back",
              "P3 used", "P3 debt", "P3 advance", "P3 advance<=money", "P3k used", "P3k debt", "P3k advance", "P3k advance<=money", "P3e used", "P3e debt", "P3e advance", "P3e advance<=money", "P3c advance", "P3ec advance", "P4 early", "P4 today", "P5 probe")


@dataclass(frozen=True)
class Cfg:
    """``mode``: "" = the old mix (60 % common month, 40 % personal period), "common", "personal", "packages", "all" (a third each); ``cancels``: "" = the old mix, "none", "with" (common month only)."""

    mode: str = ""
    cancels: str = ""


def cfg_from_env():
    return Cfg(os.environ.get("B7_MODE", ""), os.environ.get("B7_CANCELS", ""))


# ------------------------------------------------------------------------------------------------------------------------------ the moments (Б7.7 п. 4)
# A fact is entered at a MOMENT (a time stamp), not on a day: the credit ``created_at``, the pause its first form.  The generator gives every fact the moment ``at(day, minute)``; the minutes inside one
# day decide who was first (class C: a pause entered the same day AFTER the forgiveness).
def mk_credit(cid, kind, on, entered, minute, amount):
    return bl.Credit(cid, kind, on, at(entered, minute), amount)


def mk_pause(first, last, entered, minute=0):
    return pause(first, last, entered, minute)


def pause_moment(p):
    return bl.entered_at(p)


def entered_day(c):
    return bl.entered_day(c)


def known_pauses(inp, moment):
    """The pauses as they were known at ``moment`` (a pause entered later is not there; an edited one has the form of that moment).  Written here on its own - the oracle does not read the money code."""
    out = []
    for p in inp.pauses:
        if not p.forms:
            out.append(p)
            continue
        form = None
        for m, a, b in p.forms:
            if m <= moment:
                form = (a, b)
        if form is not None:
            out.append(replace(p, first=form[0], last=form[1]))
    return tuple(out)


def mk_cancel(day, lesson_id, moment):
    """A lesson cancelled by the centre, entered at ``moment`` (a tree older than R4 has no such field: the cancel is then without a moment)."""
    try:
        return bl.CentreCancel(day, lesson_id, entered=moment)
    except TypeError:
        return bl.CentreCancel(day, lesson_id)


def mk_version(first, on, moment):
    try:
        return bl.RecalcVersion(first, on, entered=moment)
    except TypeError:
        return bl.RecalcVersion(first, on)


def cancel_moment(inp, c):
    """The moment a cancel is KNOWN to the account: the later of the moment it was entered and the moment the setting (the version in force on the day of the lesson) was switched on.  Written here on its own."""
    version = None
    for v in sorted(inp.recalc_versions, key=lambda x: x.effective_from):
        if v.effective_from <= c.day:
            version = v
    moment = getattr(c, "entered", datetime.min)
    if version is not None and version.on:
        moment = max(moment, getattr(version, "entered", datetime.min))
    return moment


def known_cancels(inp, moment):
    return tuple(c for c in inp.centre_cancels if cancel_moment(inp, c) <= moment)


def now_of(inp):
    return at(inp.today, 900)


def _debt_on(inp, day, entered_before, moment):
    """The debt of the account as it was on ``day``, at ``moment``: the credits entered before, the pauses known by then."""
    seen = tuple(c for c in inp.credits if entered_before(c))
    held = tuple(c for c in inp.counted_lessons if c.day <= day)                          # the lessons held BY THAT DAY (the product reads the account of the day it is entered on: no later lesson is there yet)
    return bl.run_ledger(replace(inp, today=day, credits=seen, pauses=known_pauses(inp, moment), counted_lessons=held, centre_cancels=known_cancels(inp, moment))).debt


def allowed(inp):
    """Every forgiveness is not larger than the debt of the day it was entered (the product refuses more); every credit is entered not after today."""
    for c in inp.credits:
        e = entered_day(c)
        if e > inp.today:
            return False
        if c.kind == bl.WRITEOFF:
            key = (e, c.created_at)
            if c.amount > _debt_on(inp, e, lambda o: (entered_day(o), o.created_at) < key, c.created_at):
                return False
    return True


def base_world(rng, cfg=Cfg()):
    mode = cfg.mode
    if mode == "all":
        mode = rng.choice(["common", "personal", "packages"])
    elif not mode:
        mode = "common" if rng.random() < 0.6 or cfg.cancels == "with" else "personal"
    common = mode == "common"
    price = {"common": 600_000, "personal": 420_000, "packages": 400_000}[mode]
    regime = {"common": FIXED1, "personal": bl.Regime(), "packages": bl.Regime(mode=bl.LESSONS, package_size=8)}[mode]
    today = D(2026, 11, 3) + timedelta(days=rng.randint(0, 45))
    stay_last = None
    if rng.random() < 0.2:                                                                                    # a leaving, the stay is cut on a day inside the account
        stay_last = D(2026, 10, 12) + timedelta(days=rng.randint(0, max(0, (today - D(2026, 10, 13)).days)))
    inp = make(today=today, stays=[bl.Stay(D(2026, 10, 1), stay_last)], price_at=flat(price), regimes=[regime], start=BOOKS, lesson_days=TTF)
    if mode == "packages":
        held = [d for d in (D(2026, 10, 1) + timedelta(days=k) for k in range((today - D(2026, 10, 1)).days + 1)) if d.weekday() in (1, 3, 4) and (stay_last is None or d <= stay_last)]
        inp = replace(inp, counted_lessons=tuple(bl.CountedLesson(d, f"l{d}") for d in held))
    if rng.random() < 0.7:
        last_stay = stay_last or today + timedelta(days=40)
        if rng.random() < 0.2:                                                                                # a whole month in a pause (October, or November once it has begun)
            first = D(2026, 10, 1) if rng.random() < 0.5 or today < D(2026, 11, 1) else D(2026, 11, 1)
            last = D(2026, 10, 31) if first.month == 10 else D(2026, 11, 30)
        else:
            first = D(2026, 10, 5) + timedelta(days=rng.randint(0, 40))
            last = first + timedelta(days=rng.randint(2, 35))                                                 # may run past today
        first, last = min(first, last_stay), min(last, last_stay)
        entered = D(2026, 9, 25) + timedelta(days=rng.randint(0, (today - D(2026, 9, 25)).days))        # any day, also BETWEEN the credits
        inp = replace(inp, pauses=(mk_pause(first, last, entered, rng.choice([5, 25, 45, 90, 700])),))
    return inp


def _any_day(rng, lo, hi):
    return lo + timedelta(days=rng.randint(0, max(0, (hi - lo).days)))


def _with_cancel(rng, inp, cfg):
    """R4: a lesson of October (or the first of November) cancelled by the centre, entered at a moment of the chain (before or after the lesson, before or after the credits); sometimes the setting of the
    deductions is switched on LATER than the cancel (a version made on a day, its moment is the start of that day)."""
    if inp.regimes[0].anchor != ps.FIXED_DAY or inp.regimes[0].mode == bl.LESSONS or cfg.cancels == "none":
        return inp
    if cfg.cancels != "with" and rng.random() >= 0.3:
        return inp
    lesson = D(2026, 10, 13) + timedelta(days=7 * rng.randint(0, 3))
    entered = _any_day(rng, D(2026, 9, 25), inp.today)
    cancel = mk_cancel(lesson, "l", at(entered, rng.choice([5, 25, 45, 90, 700])))
    version = mk_version(date.min, True, datetime.min)
    if rng.random() < 0.25:
        first = _any_day(rng, D(2026, 10, 1), lesson)
        version = mk_version(first, True, at(first, 0))
    return replace(inp, centre_cancels=(cancel,), recalc_versions=(version,))


def allowed_world(rng, cfg=Cfg()):
    """The history in the order of entry: 1-4 credits, each entered on a day <= today; a forgiveness <= the debt of its entry day; a date from ``EARLIEST`` to the entry day."""
    inp = _with_cancel(rng, base_world(rng, cfg), cfg)
    days = sorted(_any_day(rng, D(2026, 10, 6), inp.today) for _ in range(rng.randint(1, 4)))
    credits = []
    for i, e in enumerate(days):
        on = _any_day(rng, EARLIEST, e) if rng.random() < 0.5 else e
        if rng.random() < 0.5:
            credits.append(mk_credit(f"c{i}", bl.PAYMENT, on, e, 20 * i + 10, rng.choice([100_000, 150_000, 200_000, 300_000, 400_000, 420_000, 600_000])))
        else:
            probe_moment = at(e, 20 * i + 10)
            debt = _debt_on(replace(inp, credits=tuple(credits)), e, lambda o: True, probe_moment)
            if debt > 0:
                credits.append(mk_credit(f"c{i}", bl.WRITEOFF, on, e, 20 * i + 10, min(debt, rng.choice([100_000, 150_000, 200_000, 300_000, 420_000, 600_000]))))
    if not credits:
        credits.append(mk_credit("c0", bl.PAYMENT, inp.today, inp.today, 10, 200_000))
    return replace(inp, credits=tuple(credits))


def new_payment(rng, inp, name="late"):
    """A payment entered NOW (today); the date any allowed one."""
    on = inp.today if rng.random() < 0.3 else _any_day(rng, EARLIEST, inp.today)
    return mk_credit(name, bl.PAYMENT, on, inp.today, 900, rng.choice([100_000, 200_000, 300_000, 420_000, 600_000]))


def new_forgiveness(rng, inp, name="extra"):
    """A forgiveness entered NOW: 1 .. the debt now; dated today or back; None when there is no debt."""
    debt = bl.run_ledger(inp).debt
    if debt <= 0:
        return None
    on = inp.today if rng.random() < 0.4 else _any_day(rng, EARLIEST, inp.today)
    return mk_credit(name, bl.WRITEOFF, on, inp.today, 900, min(debt, rng.choice([100_000, 150_000, 200_000, 300_000, 420_000, 600_000])))


def new_pause(rng, inp):
    """Б7.5 P3: a pause entered NOW (today).  Б7.7 3.0: it may run past today, and it may cover a whole month."""
    if rng.random() < 0.25:
        first = D(2026, 10, 1) if rng.random() < 0.5 or inp.today < D(2026, 11, 1) else D(2026, 11, 1)
        last = D(2026, 10, 31) if first.month == 10 else D(2026, 11, 30)
    else:
        first = D(2026, 10, 1) + timedelta(days=rng.randint(0, max(0, (inp.today - D(2026, 10, 1)).days + 10)))
        last = first + timedelta(days=rng.randint(1, 35))
    stay_last = inp.stays[0].last
    if stay_last is not None:
        first, last = min(first, stay_last), min(last, stay_last)
    return mk_pause(first, last, inp.today, 900)


def new_cancel(rng, inp):
    """R4 P3k: a lesson cancelled by the centre NOW (today): a lesson of the October days or the first of November, one that is not cancelled yet; the setting must be on."""
    if not inp.recalc_versions or inp.regimes[0].anchor != ps.FIXED_DAY or inp.regimes[0].mode == bl.LESSONS:
        return None
    taken = {c.day for c in inp.centre_cancels}
    free = [d for d in (D(2026, 10, 13) + timedelta(days=7 * k) for k in range(4)) if d not in taken and d >= max(v.effective_from for v in inp.recalc_versions)]
    if not free:
        return None
    return mk_cancel(rng.choice(free), "l", at(inp.today, 900))


@dataclass(frozen=True)
class PauseEdit:
    """A pause of the world edited NOW (the ``PATCH /org/pauses/{id}`` of the product): ``extend`` = the days only grow, ``shorten`` = they only shrink."""

    index: int
    first: date
    last: date
    kind: str


def new_pause_edit(rng, inp):
    if not inp.pauses:
        return None
    index = rng.randrange(len(inp.pauses))
    p = inp.pauses[index]
    stay_last = inp.stays[0].last
    if rng.random() < 0.6:
        first, last = p.first - timedelta(days=rng.randint(0, 10)), p.last + timedelta(days=rng.randint(0, 15))
        if stay_last is not None:
            last = min(last, stay_last)
        if (first, last) == (p.first, p.last) or last < first:
            return None
        return PauseEdit(index, first, last, "extend")
    if p.last <= p.first:
        return None
    first = p.first + timedelta(days=rng.randint(0, (p.last - p.first).days - 1))
    last = p.last - timedelta(days=rng.randint(0, (p.last - first).days))
    return PauseEdit(index, first, last, "shorten") if (first, last) != (p.first, p.last) and first <= last else None


def apply_edit(inp, e):
    pauses = list(inp.pauses)
    pauses[e.index] = edit_pause(pauses[e.index], e.first, e.last, inp.today, 900)
    return replace(inp, pauses=tuple(pauses))


def probe_credit(x):
    """The probe of the payment window as the product builds it (``payments_service.py`` ``preview``: a stand-in credit entered at ``PROBE_MOMENT`` - NOW)."""
    return bl.Credit("preview", x.kind, x.on, bl.PROBE_MOMENT, x.amount)


def money_of(inp):
    return sum(c.amount for c in inp.credits if c.kind != bl.WRITEOFF)


def _slot_first_days(inp):
    """``{key of a period: its first charged day}`` of the world; the key is the start of the month (common month), the place in the chain (personal period) or the number of the package (packages)."""
    if inp.regimes[0].mode == bl.LESSONS:
        return {s.package_no: s.days[0] for s in bl.run_ledger(inp).segments if s.kind == "package"}
    segs = [s for s in bl.run_ledger(inp).segments if s.kind == "period"]
    if inp.regimes[0].anchor == ps.FIXED_DAY:
        return {s.period_start: (s.days[0] if s.days else s.period_start) for s in segs}
    return {k: (s.days[0] if s.days else s.period_start) for k, s in enumerate(sorted(segs, key=lambda x: x.period_start))}


def oracle_p4(inp):
    """Б7.6 / Б7.7 P4, found by REBUILDING the world (no line of the money code is read except the periods of a world): a forgiveness lies only on a period that had begun by the day it was entered in the world
    KNOWN AT THAT MOMENT, and that has begun today.  Returns ``(names of broken properties, number of forgivenesses whose period was not found in the old world)``."""
    bad, lost = [], 0
    L = bl.run_ledger(inp)
    forgiveness = {c.id: c for c in inp.credits if c.kind == bl.WRITEOFF}
    if not forgiveness:
        return bad, lost
    seg = {s.id: s for s in L.segments}
    packages = inp.regimes[0].mode == bl.LESSONS
    worlds = {}
    order = {s.id: k for k, s in enumerate(sorted((s for s in L.segments if s.kind == "period"), key=lambda x: x.period_start))}
    for a in L.allocations:
        c = forgiveness.get(a.credit_id)
        if c is None or a.segment_id not in seg or a.amount <= 0:
            continue
        s = seg[a.segment_id]
        if s.kind == "opening":
            continue
        if not s.begun:
            bad.append("P4 today")
            continue
        day = entered_day(c)
        if c.id not in worlds:
            worlds[c.id] = _slot_first_days(replace(inp, pauses=known_pauses(inp, c.created_at)))
        key = s.package_no if packages else (s.period_start if inp.regimes[0].anchor == ps.FIXED_DAY else order[s.id])
        if key not in worlds[c.id]:
            lost += 1
            continue
        first = worlds[c.id][key]
        if first > day:
            bad.append("P4 early")
    return bad, lost


def period_key(s):
    """Б7.8 4.1: the key of a period is the one of the MODEL: a common month / a personal period - its ``slot`` (start of the cell; piece of the stay and the number in the chain); a package - its number."""
    return s.slot if s.slot is not None else (s.kind, s.package_no, s.id if s.package_no is None else "")


def violations(inp, x):
    """The names of the properties that the addition of ``x`` (a payment, a forgiveness, a pause or an edit of a pause - all entered now) breaks."""
    before = bl.run_ledger(inp)
    if isinstance(x, (bl.Pause, PauseEdit)):                       # Б7.5 P3: a pause entered now; Б7.7: an edit of a pause is the same entry
        new = replace(inp, pauses=(*inp.pauses, x)) if isinstance(x, bl.Pause) else apply_edit(inp, x)
        after = bl.run_ledger(new)
        tag = "P3" if isinstance(x, bl.Pause) else "P3e"
        bad = []
        if after.flags or not after.reconciles:
            bad.append("flags")
        if after.advance > money_of(new):
            bad.append(f"{tag} advance<=money")
        if isinstance(x, PauseEdit) and x.kind == "shorten":      # the days only shrink: the charge may grow, nothing else is promised
            return bad
        if after.written_off > before.written_off:
            bad.append(f"{tag} used")
        if after.debt > before.debt:
            bad.append(f"{tag} debt")
        after_cost = {period_key(s): s.amount for s in after.segments if s.begun}       # what a period costs NOW (by the KEY of the model): a period that has not begun costs nothing
        freed = sum(max(0, s.money - after_cost.get(period_key(s), 0)) for s in before.segments)
        if after.advance - before.advance > freed:
            bad.append(f"{tag} advance")
        if after.advance - before.advance > max(0, before.charged - after.charged):       # information: the same bound by the TOTAL charge (a period changes its place on a personal chain, the id of a period is not the same after)
            bad.append(f"{tag}c advance")
        return bad
    if isinstance(x, bl.CentreCancel):                                  # R4 P3k: a lesson cancelled by the centre entered NOW
        after = bl.run_ledger(apply_x(inp, x))
        bad = []
        if after.flags or not after.reconciles:
            bad.append("flags")
        if after.advance > money_of(inp):
            bad.append("P3k advance<=money")
        if after.written_off > before.written_off:
            bad.append("P3k used")
        if after.debt > before.debt:
            bad.append("P3k debt")
        if after.advance - before.advance > after.recalculated - before.recalculated:        # the advance grows only by the deduction that came on the line
            bad.append("P3k advance")
        return bad
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
        probe = bl.run_ledger(replace(inp, credits=(*inp.credits, probe_credit(x))))        # P5: the window shows what the record will give
        if (probe.debt, probe.advance, probe.written_off) != (after.debt, after.advance, after.written_off):
            bad.append("P5 probe")
    else:
        if after.debt > before.debt:
            bad.append("P2 debt")
        if after.advance > money_of(new):
            bad.append("P2 advance<=money")
        if x.on == inp.today and after.advance != before.advance:
            bad.append("P2 advance today")
        if x.on < inp.today and after.advance - before.advance > x.amount:
            bad.append("P2 advance back")
    bad.extend(oracle_p4(new)[0])
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
    (segments, credits, net, recalcs, worlds), placed = captured[-1]
    may = bl._may_lie(worlds)
    alive = bl._alive_forgiveness(worlds, credits, net)
    amounts = {c.id: (alive[c.id] if c.kind == bl.WRITEOFF else net[c.id]) for c in credits}
    main, _ = bl._tie(segments, credits, amounts, getattr(bl, "_line_room", lambda recalcs: (lambda s: s.capacity))(recalcs), may)
    tied = {c.id: {s.id: sum(a for cc, a in main[s.id] if cc.id == c.id) for s in segments} for c in credits}
    lines = [f"today {inp.today}  charged {L.charged}  debt {L.debt}  advance {L.advance}  used {L.written_off} of {L.written_off_granted}  covered_through {L.covered_through}"]
    lines.append("  pauses: " + (", ".join(f"{p.first}..{p.last} entered {pause_moment(p):%d.%m %H:%M}" for p in inp.pauses) or "-") + "   centre cancels: " + (", ".join(f"{c.day} entered {getattr(c, 'entered', datetime.min):%d.%m %H:%M}" for c in inp.centre_cancels) or "-"))
    head = "  credit (kind, date, entered, amount)".ljust(52) + "".join(f"| {s.period_start} (charge {s.amount}, first day {s.days[0] if s.days else '-'})".ljust(46) for s in segments) + "| burned / leftover"
    lines.append(head)
    for c in credits:
        cells = []
        for s in segments:
            got = placed.parts.get((s.id, c.id), 0)
            cells.append(f"| cap {s.capacity} tied {tied[c.id][s.id]} placed {got}".ljust(46))
        gone = net[c.id] - sum(placed.parts.get((s.id, c.id), 0) for s in segments) - sum(a for cid, a in placed.leftover if cid == c.id)
        left = sum(a for cid, a in placed.leftover if cid == c.id)
        lines.append(f"  {c.id:5s} {c.kind[:3]} {c.on} {c.created_at:%d.%m %H:%M} {net[c.id]}{' alive ' + str(alive[c.id]) if c.kind == bl.WRITEOFF and alive[c.id] != net[c.id] else ''}".ljust(52) + "".join(cells) + f"| burned {gone}, leftover {left}")
    for what, key in (("money", placed.money), ("forgiven", placed.forgiven), ("recalc", placed.recalculated)):
        lines.append(f"  {what:9s}".ljust(52) + "".join(f"| {key[s.id]}".ljust(46) for s in segments))
    lines.append("  unpaid".ljust(52) + "".join(f"| {s.amount - placed.money[s.id] - placed.forgiven[s.id] - placed.recalculated[s.id]}".ljust(46) for s in segments))
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------------------------------------ the shrinker

def _minute(c):
    return int((c.created_at - at(entered_day(c))).total_seconds() // 60)


def shrink(inp, x, prop):
    """Make the world smaller while ``prop`` is still broken by the addition of ``x`` and the world stays allowed: drop a credit / a pause / the cancels / the leaving, lower an amount, move the dates
    towards the entry day, lower ``today``.  Returns (inp, x)."""
    def ok(i, xx):
        return allowed(i) and prop in violations(i, xx)

    changed = True
    while changed:
        changed = False
        steps = []
        for k in range(len(inp.credits)):
            steps.append((replace(inp, credits=inp.credits[:k] + inp.credits[k + 1:]), x))
        if inp.pauses and not isinstance(x, PauseEdit):
            steps.append((replace(inp, pauses=()), x))
        if inp.centre_cancels:
            steps.append((replace(inp, centre_cancels=(), recalc_versions=()), x))
        if inp.stays[0].last is not None and not isinstance(x, PauseEdit):
            steps.append((replace(inp, stays=(replace(inp.stays[0], last=None),)), x))
        for k, c in enumerate(inp.credits):
            for amount in (100_000, 200_000, 300_000, 400_000, 420_000, 600_000):
                if amount < c.amount:
                    steps.append((replace(inp, credits=inp.credits[:k] + (replace(c, amount=amount),) + inp.credits[k + 1:]), x))
            if entered_day(c) != c.on:
                steps.append((replace(inp, credits=inp.credits[:k] + (mk_credit(c.id, c.kind, c.on, c.on, _minute(c), c.amount),) + inp.credits[k + 1:]), x))
        if isinstance(x, bl.Credit):
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
    lines = [f"regime {inp.regimes[0].mode}/{inp.regimes[0].anchor}, price {inp.price_at(inp.today).amount if callable(inp.price_at) else '?'}, stay {inp.stays[0].first}..{inp.stays[0].last}, books from {BOOKS}, lessons Tue/Thu/Fri, today {inp.today}"]
    for c in inp.credits:
        lines.append(f"  credit {c.id}: {c.kind} {c.amount} dated {c.on} entered {c.created_at:%d.%m %H:%M}")
    for p in inp.pauses:
        lines.append(f"  pause {p.first}..{p.last} entered {pause_moment(p):%d.%m %H:%M}")
    for c in inp.centre_cancels:
        lines.append(f"  cancel {c.day} entered {getattr(c, 'entered', datetime.min):%d.%m %H:%M}; setting " + ", ".join(f"{v.effective_from} {v.on} made {getattr(v, 'entered', datetime.min):%d.%m %H:%M}" for v in inp.recalc_versions))
    if isinstance(x, bl.Pause):
        lines.append(f"  ADDED  pause {x.first}..{x.last} entered {pause_moment(x):%d.%m %H:%M}")
    elif isinstance(x, bl.CentreCancel):
        lines.append(f"  ADDED  lesson {x.day} cancelled by the centre, entered {x.entered:%d.%m %H:%M}")
    elif isinstance(x, PauseEdit):
        lines.append(f"  ADDED  edit of pause {x.index}: {x.kind} to {x.first}..{x.last} entered {inp.today} 12:00")
    else:
        lines.append(f"  ADDED  {x.id}: {x.kind} {x.amount} dated {x.on} entered {x.created_at:%d.%m %H:%M}")
    return "\n".join(lines)


def apply_x(inp, x):
    if isinstance(x, bl.Pause):
        return replace(inp, pauses=(*inp.pauses, x))
    if isinstance(x, PauseEdit):
        return apply_edit(inp, x)
    if isinstance(x, bl.CentreCancel):
        return replace(inp, centre_cancels=(*inp.centre_cancels, x))
    return replace(inp, credits=(*inp.credits, x))


def run_counts(seed, n, cfg=Cfg()):
    rng = random.Random(seed)
    counts = {p: 0 for p in PROPERTIES}
    counts["flags"] = 0
    counts["P4 nomatch"] = 0
    found = {}
    worlds = pays = forgs = 0
    for _ in range(n):
        inp = allowed_world(rng, cfg)
        worlds += 1
        for kind in (bl.PAYMENT, bl.WRITEOFF, "pause", "edit", "cancel"):
            x = (new_payment(rng, inp) if kind == bl.PAYMENT else new_forgiveness(rng, inp) if kind == bl.WRITEOFF
                 else new_pause(rng, inp) if kind == "pause" else new_pause_edit(rng, inp) if kind == "edit" else new_cancel(rng, inp) if inp.centre_cancels else None)
            if x is None:
                continue
            pays += kind == bl.PAYMENT
            forgs += kind == bl.WRITEOFF
            names = violations(inp, x)
            for name in names:
                counts[name] += 1
                found.setdefault(name, []).append((inp, x))
            counts["P4 nomatch"] += oracle_p4(apply_x(inp, x))[1]
    return counts, found, (worlds, pays, forgs)


def _totals(inp):
    L = bl.run_ledger(inp)
    return [L.charged, L.debt, L.advance, L.written_off, L.recalculated, L.paid, L.refundable, sorted(s.amount for s in L.segments)]


def dump(seed, n, cfg):
    """Б7.8 5.3: the TOTALS of the account of the worlds WITHOUT a forgiveness (as they are, and with a payment / a pause / an edit of a pause / a cancel entered now): one JSON line per account.  The same seed on
    two trees (``B7_TREE``) must give the same lines: R4 moves money between the periods, never the totals."""
    import json
    rng = random.Random(seed)
    for k in range(n):
        inp = allowed_world(rng, cfg)
        pay, pse, edit = new_payment(rng, inp), new_pause(rng, inp), new_pause_edit(rng, inp)
        cancel = new_cancel(rng, inp) if inp.centre_cancels else None
        if any(c.kind == bl.WRITEOFF for c in inp.credits):
            continue
        row = {"world": k, "now": _totals(inp)}
        for name, x in (("payment", pay), ("pause", pse), ("edit", edit), ("cancel", cancel)):
            if x is not None:
                row[name] = _totals(apply_x(inp, x))
        print(json.dumps(row))


def main(argv):
    cmd = argv[1]
    cfg = cfg_from_env()
    if cmd == "dump":
        dump(int(argv[2]), int(argv[3]), cfg)
        return
    if cmd == "count":
        total = {}
        for seed in range(int(argv[2]), int(argv[2]) + (int(argv[4]) if len(argv) > 4 else 1)):
            counts, _, size = run_counts(seed, int(argv[3]), cfg)
            for k, v in counts.items():
                total[k] = total.get(k, 0) + v
            total["_worlds/payments/forgivenesses"] = tuple(a + b for a, b in zip(total.get("_worlds/payments/forgivenesses", (0, 0, 0)), size))
        print(" | ".join(f"{k}: {v}" for k, v in total.items()))
    elif cmd == "smallest":
        prop, seed, n = argv[2], int(argv[3]), int(argv[4])
        _, found, _ = run_counts(seed, n, cfg)
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
        print("AFTER\n" + explain(apply_x(small, sx)))
        print("broken:", violations(small, sx))


if __name__ == "__main__":
    main(sys.argv)
