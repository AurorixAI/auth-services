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
from types import SimpleNamespace
from datetime import date, datetime, timedelta

_TREE = os.environ.get("B7_TREE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TREE)

from src.auth import billing_ledger as bl  # noqa: E402
from src.auth import payment_status as ps  # noqa: E402
from tests.pay2_ledger_helpers import D, at, edit_pause, flat, make, pause, weekly  # noqa: E402

# Б11.8 п. 8: the generator builds the facts as the LOADER of the head under test builds them.  These switches tell the heads apart (the same file runs on dc3c8d0 and on the fixed head, B7_TREE):
NEW_LEAVE = "left_at" in bl.Stay.__dataclass_fields__                    # a stay has the moment of the leaving; the pauses a leaving took off are kept as ``past_pauses``
NEW_CHAIN = hasattr(bl, "chain_moment")                                  # a corrected / restored payment keeps the moment of the FIRST record of its chain
TTF = weekly((1, 3, 4))
FIXED1 = bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)
BOOKS = D(2026, 9, 1)
EARLIEST = D(2026, 9, 15)                # the earliest date the generator gives a payment / a forgiveness (the product allows 3 years)
PROPERTIES = ("P1c debt", "P1c advance", "P1c used", "P3u advance>=", "P3u advance<=freed", "P3u advance<=money", "P3u debt", "P3u used", "P3u transfer", "P6 same", "P6 amount", "P6b restore", "P7 undo", "P7 relapse",
              "P1 debt", "P1 used", "P1 advance", "P1 advance<=money", "P1 identity", "P2 debt", "P2 advance<=money", "P2 advance today", "P2 advance back",
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


def mk_stay(first, last=None, joined=None, left_at=None):
    """A stay of the membership; ``left_at`` = the moment the leaving was entered (a head older than Б11.8 has no such field)."""
    if NEW_LEAVE:
        return bl.Stay(first, last, joined, left_at)
    return bl.Stay(first, last, joined)


def leave_pauses(pauses, past, day, moment):
    """``close_at_leave`` (logica_service.py): the pauses that begin after the day of the leaving are taken off (void_reason «ученик ушёл из группы»), the one that holds the day is cut to it - the cut is a form of
    the pause at the moment of the leaving.  A head older than Б11.8 drops the voided ones (the loader reads only the live pauses), the fixed head keeps them as ``past_pauses`` known until the leaving."""
    keep, gone = [], []
    for p in pauses:
        if p.first > day:
            gone.append(replace(p, voided_at=moment) if NEW_LEAVE else p)
        elif p.last > day:
            keep.append(replace(p, last=day, forms=(*p.forms, (moment, p.first, day))) if p.forms else replace(p, last=day))
        else:
            keep.append(p)
    return tuple(keep), (tuple(past) + tuple(gone) if NEW_LEAVE else ())


def with_pauses(inp, pauses, past=()):
    out = replace(inp, pauses=tuple(pauses))
    return replace(out, past_pauses=tuple(past)) if NEW_LEAVE else out


def pause_moment(p):
    return bl.entered_at(p)


def entered_day(c):
    return bl.entered_day(c)


def known_pauses(inp, moment):
    """The pauses as they were known at ``moment`` (a pause entered later is not there; an edited one has the form of that moment; one that a leaving took off is there until the leaving).  Written here on its own - the oracle does not read the money code."""
    out = []
    for p in (*inp.pauses, *getattr(inp, "past_pauses", ())):
        voided = getattr(p, "voided_at", None)
        if voided is not None and voided <= moment:
            continue
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


def known_stays(inp, moment):
    """Б11.8 п. 1: the stays as they were known at ``moment``: a leaving entered later is not there - the piece goes on to the next piece or is open."""
    stays = sorted(inp.stays, key=lambda s: s.first)
    out = []
    for k, s in enumerate(stays):
        left = getattr(s, "left_at", None)
        if left is not None and left > moment:
            nxt = stays[k + 1].first - timedelta(days=1) if k + 1 < len(stays) else None
            out.append(replace(s, last=nxt, left_at=None))
        else:
            out.append(s)
    return tuple(out)


def known_inp(inp, moment):
    """The input of the world KNOWN at ``moment`` (Б11.8 п. 1): the current facts without the events of truncation entered later (a pause, an edit of a pause, a leaving)."""
    return replace(inp, pauses=known_pauses(inp, moment), stays=known_stays(inp, moment))


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
    return bl.run_ledger(replace(known_inp(inp, moment), today=day, credits=seen, counted_lessons=held, centre_cancels=known_cancels(inp, moment))).debt


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
    left_at = at(stay_last, rng.choice([5, 25, 45, 90, 700])) if stay_last is not None else None      # Б11.8: the leaving is an event with a moment (the product always writes it; a leaving WITHOUT a moment - «after all the records» - is a virtual cut and old rows, not a world of the generator: a payment entered now would then come BEFORE it)
    stays = [mk_stay(D(2026, 10, 1), stay_last, None, left_at)]
    back = None
    if stay_last is not None and mode != "packages" and rng.random() < 0.3:                                   # Б11.8 п. 8: a return to the group (a second piece of the stay) after a gap
        back = stay_last + timedelta(days=rng.randint(2, 20))
        if back < today:
            stays.append(mk_stay(back, None, at(back, 10)))
        else:
            back = None
    inp = make(today=today, stays=stays, price_at=flat(price), regimes=[regime], start=BOOKS, lesson_days=TTF)
    if mode == "packages":
        held = [d for d in (D(2026, 10, 1) + timedelta(days=k) for k in range((today - D(2026, 10, 1)).days + 1)) if d.weekday() in (1, 3, 4) and (stay_last is None or d <= stay_last)]
        inp = replace(inp, counted_lessons=tuple(bl.CountedLesson(d, f"l{d}") for d in held))
    roll = rng.random()
    if roll < 0.08:                                                                                           # Б11.3: the opening balance - a debt on the start of the books, or «paid until»
        inp = replace(inp, opening=bl.Opening(None, rng.choice([100_000, 200_000, 300_000]), D(2026, 9, 30)))
    elif roll < 0.12 and mode != "packages":
        inp = replace(inp, opening=bl.Opening(D(2026, 10, 14)))
    if rng.random() < 0.7:
        moment_leave = left_at is not None and back is None                                                   # a leaving with a moment cuts / takes off the pauses itself (as ``close_at_leave``)
        last_stay = None if moment_leave else ((stay_last if back is None else None) or today + timedelta(days=40))
        if last_stay is None:
            last_stay = date.max
        if rng.random() < 0.2:                                                                                # a whole month in a pause (October, or November once it has begun)
            first = D(2026, 10, 1) if rng.random() < 0.5 or today < D(2026, 11, 1) else D(2026, 11, 1)
            last = D(2026, 10, 31) if first.month == 10 else D(2026, 11, 30)
        else:
            first = D(2026, 10, 5) + timedelta(days=rng.randint(0, 40))
            last = first + timedelta(days=rng.randint(2, 35))                                                 # may run past today
        first, last = min(first, last_stay), min(last, last_stay)
        if back is not None and first < back:                                                                 # a pause inside a piece of the stay: the second piece, or none
            first, last = max(first, back), max(last, back)
        entered_hi = stay_last if moment_leave else today                                                     # nobody enters a pause for a student who has left
        entered = D(2026, 9, 25) + timedelta(days=rng.randint(0, max(0, (entered_hi - D(2026, 9, 25)).days)))        # any day, also BETWEEN the credits
        pauses, past = (mk_pause(first, last, entered, 1 if entered == stay_last and moment_leave else rng.choice([5, 25, 45, 90, 700])),), ()
        if moment_leave:
            pauses, past = leave_pauses(pauses, (), stay_last, left_at)
        inp = with_pauses(inp, pauses, past)
    return inp


def _any_day(rng, lo, hi):
    return lo + timedelta(days=rng.randint(0, max(0, (hi - lo).days)))


def _with_cancel(rng, inp, cfg):
    """R4: a lesson of October (or the first of November) cancelled by the centre, entered at a moment of the chain (before or after the lesson, before or after the credits); sometimes the setting of the
    deductions is switched on LATER than the cancel (a version made on a day, its moment is the start of that day)."""
    if inp.regimes[0].mode == bl.LESSONS or cfg.cancels == "none":                  # a package has no deduction (the lessons mode); a common month and a personal period have
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
    stay_last = inp.stays[-1].last
    if stay_last is not None:
        first, last = min(first, stay_last), min(last, stay_last)
    first = max(first, inp.stays[-1].first)                              # a pause inside the last piece of the stay
    last = max(first, last)
    return mk_pause(first, last, inp.today, 900)


def new_cancel(rng, inp):
    """R4 P3k: a lesson cancelled by the centre NOW (today): a lesson of the October days or the first of November, one that is not cancelled yet; the setting must be on."""
    if not inp.recalc_versions or inp.regimes[0].mode == bl.LESSONS:
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
    stay_last = inp.stays[-1].last
    if rng.random() < 0.6:
        first, last = p.first - timedelta(days=rng.randint(0, 10)), p.last + timedelta(days=rng.randint(0, 15))
        if len(inp.stays) > 1:                                              # a pause lies inside a piece of the stay: it does not grow into the gap before a return
            first = max(first, inp.stays[-1].first)
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


# ------------------------------------------------------------------------------------------------------------------------------ Б11.8 п. 8: a leaving, its cancel, a return, a correction of a payment
def reopen(stay):
    return replace(stay, last=None, left_at=None) if NEW_LEAVE else replace(stay, last=None)


@dataclass(frozen=True)
class Leave:
    """A leaving entered NOW (``kind`` "leave"), or a transfer to another group (``kind`` "transfer": the same cut of the stay, and the money the account can give leaves it as ``transfer_out``)."""

    kind: str = "leave"


@dataclass(frozen=True)
class UndoLeave:
    """A leaving entered NOW and cancelled the same day (the return on the day of the leaving, ``leave_undo``)."""


@dataclass(frozen=True)
class Relapse:
    """Б11.12 п. 5: a leaving entered NOW, cancelled, a record (a payment or a forgiveness, ``record``) entered, and the student leaves AGAIN the same day.  The input is what the loader builds from the periods the product
    writes: ``stays_of_membership`` of the two periods (the second opened by the cancel and closed by the second leaving - a stay of no day when there is no mark)."""

    record: object


@dataclass(frozen=True)
class Return:
    """A return to the group entered NOW: a second piece of the stay (the student left earlier)."""


@dataclass(frozen=True)
class Correct:
    """A payment of the world corrected NOW (``kind`` "same": nothing changes but the row; "amount": the sum becomes ``amount``) or cancelled and restored (``kind`` "restore")."""

    credit_id: str
    kind: str
    amount: int = 0


def chain_moment_of(first_moment, now_moment):
    """The moment of the credit made by a correction / a restoration: the head under test says (``bl.chain_moment``: the moment of the FIRST record of the chain); an older head - NOW."""
    return bl.chain_moment([first_moment, now_moment]) if NEW_CHAIN else now_moment


def leave_moment(inp):
    return at(inp.today, 900)


def can_leave(inp):
    last = inp.stays[-1]
    return last.last is None and last.first < inp.today


def apply_leave(inp, moment=None):
    """The input after a leaving NOW as the loader of the head builds it: the open stay closed today (with the moment), the pauses cut / taken off by ``close_at_leave``."""
    moment = moment or leave_moment(inp)
    last = inp.stays[-1]
    stays = (*inp.stays[:-1], mk_stay(last.first, inp.today, last.joined_at, moment))
    pauses, past = leave_pauses(inp.pauses, getattr(inp, "past_pauses", ()), inp.today, moment)
    return with_pauses(replace(inp, stays=stays), pauses, past)


UNDO_MINUTE, RECORD_MINUTE, RELEAVE_MINUTE = 910, 920, 930      # the order inside the day: the leaving (900), its cancel, the record, the second leaving


def periods_after_undo(inp, releave=None):
    """The periods the product writes for the LAST stay of ``inp`` when it was left at ``leave_moment`` and the leaving was cancelled (``open_period``: a NEW period opened at the cancel, the first one stays closed at
    the leaving); ``releave`` = the moment of a second leaving (closes the second one)."""
    last = inp.stays[-1]
    joined = last.joined_at or at(last.first, 0)
    first = SimpleNamespace(joined_at=joined, left_at=leave_moment(inp), billing_from=last.first)
    second = SimpleNamespace(joined_at=at(inp.today, UNDO_MINUTE), left_at=releave, billing_from=None)
    return SimpleNamespace(joined_at=joined, left_at=None, is_active=True), [first, second]


def stays_after_undo(inp, releave=None):
    """Б11.12 п. 5 (lesson 166): the stays of the account after the cancel - built by the LOADER of the head under test (``ledger_service.stays_of_membership`` on the two periods), not by hand: a property that is
    red on a defect of the loader sees it."""
    from src.auth import ledger_service as ls
    membership, periods = periods_after_undo(inp, releave)
    return (*inp.stays[:-1], *ls.stays_of_membership(membership, periods, lambda day: False))


def apply_undo(before, moment=None):
    """The input after the leaving was cancelled the same day, in the form the PRODUCT writes: two touching pieces (the one closed by the leaving with its moment, the one the cancel opened - open); the cut of the
    pauses is erased (Б11.8 п. 2: the line ``leave_undone``), so the pauses are the ones BEFORE the leaving.  An older head reads the cancel as a new form of every pause it restored (the moment of the cancel, the
    days it had before): a pause that was cut is «extended» then."""
    if NEW_LEAVE:
        return replace(before, stays=stays_after_undo(before))
    undo, left = at(before.today, 950), leave_moment(before)
    pauses = []
    for p in before.pauses:
        if not p.forms:
            pauses.append(p)
        elif p.first > before.today:                                   # taken off by the leaving, brought back by the cancel: a new form (the days it had)
            pauses.append(replace(p, forms=(*p.forms, (undo, p.first, p.last))))
        elif p.last > before.today:                                    # cut by the leaving (a form), given its days back by the cancel (another form)
            pauses.append(replace(p, forms=(*p.forms, (left, p.first, before.today), (undo, p.first, p.last))))
        else:
            pauses.append(p)
    return replace(before, pauses=tuple(pauses))


def with_record(inp, record):
    """``record`` entered between the cancel and the second leaving (its moment is fixed here)."""
    return replace(inp, credits=(*inp.credits, replace(record, created_at=at(inp.today, RECORD_MINUTE))))


def apply_relapse(inp, record):
    """The input after: a leaving, its cancel, ``record``, a second leaving - all the same day.  Stays by the loader; the pauses are cut by the SECOND leaving (the first cut was erased by the cancel)."""
    new = with_record(inp, record)
    pauses, past = leave_pauses(new.pauses, getattr(new, "past_pauses", ()), new.today, at(new.today, RELEAVE_MINUTE))
    if not NEW_LEAVE:
        return apply_leave(new, at(new.today, RELEAVE_MINUTE))
    return with_pauses(replace(new, stays=stays_after_undo(new, at(new.today, RELEAVE_MINUTE))), pauses, past)


def oracle_relapse(inp, record):
    """What the model says: ONE leaving at the moment of the second one, the record entered before it."""
    return apply_leave(with_record(inp, record), at(inp.today, RELEAVE_MINUTE))


def apply_return(inp):
    return replace(inp, stays=(*inp.stays, mk_stay(inp.today, None, at(inp.today, 900))))


def can_return(inp):
    last = inp.stays[-1]
    return last.last is not None and last.last < inp.today and inp.regimes[0].mode != bl.LESSONS


def new_leave(rng, inp, kind="leave"):
    return Leave(kind) if can_leave(inp) else None


def new_undo(rng, inp):
    return UndoLeave() if can_leave(inp) else None


def new_relapse(rng, inp, kind):
    """Б11.12 п. 5: a leaving, its cancel, a payment (``kind`` "pay") or a forgiveness (``kind`` "forgive"), a second leaving - the same day."""
    if not can_leave(inp):
        return None
    record = new_payment(rng, inp, "rel") if kind == "pay" else new_forgiveness(rng, inp, "rel")
    return Relapse(record) if record is not None else None


def new_return(rng, inp):
    return Return() if can_return(inp) else None


def new_correct(rng, inp, kind):
    payments = [c for c in inp.credits if c.kind == bl.PAYMENT]
    if not payments:
        return None
    c = rng.choice(payments)
    return Correct(c.id, kind, rng.choice([100_000, 200_000, 300_000, 400_000, 500_000]) if kind == "amount" else c.amount)


def apply_correct(inp, x, *, in_place=False):
    """The credits after the correction: the corrected payment has the moment of the chain (``chain_moment_of``); ``in_place`` = the oracle of P6 amount - the same payment of the new sum at its OWN moment."""
    out = []
    for c in inp.credits:
        if c.id != x.credit_id:
            out.append(c)
        elif in_place:
            out.append(replace(c, amount=x.amount))
        else:
            out.append(bl.Credit(c.id + "r", c.kind, c.on, chain_moment_of(c.created_at, at(inp.today, 900)), x.amount if x.kind == "amount" else c.amount))
    return replace(inp, credits=tuple(out))


def state_of(inp):
    L = bl.run_ledger(inp)
    return (L.charged, L.debt, L.advance, L.written_off, L.recalculated, L.paid, L.refundable, tuple(sorted((s.period_start, s.amount) for s in L.segments)))


def event_violations(inp, x):
    """The properties of the events of Б11.8 п. 8 (they are checked by ``violations``)."""
    before = bl.run_ledger(inp)
    bad = []
    if isinstance(x, Leave):
        new = apply_leave(inp)
        outflow = None
        if x.kind == "transfer":
            carried = bl.refundable_at(new, inp.today)
            outflow = bl.Outflow("out", bl.TRANSFER_OUT, inp.today, leave_moment(inp), carried) if carried > 0 else None
            if outflow is not None:
                new = replace(new, outflows=(*new.outflows, outflow))
        after = bl.run_ledger(new)
        carried = outflow.amount if outflow is not None else 0
        if after.flags or not after.reconciles:
            bad.append("flags")
        if after.advance > money_of(new):
            bad.append("P3u advance<=money")
        if x.kind == "leave" and after.advance < before.advance:
            bad.append("P3u advance>=")
        if x.kind == "transfer" and after.advance + carried < before.advance:
            bad.append("P3u transfer")
        if x.kind == "leave":                                   # a transfer takes money OUT of an account (the outflow lies on the credits and moves what covers what): only the advance is promised there (above)
            cut = {}
            for line in after.recalcs:
                cut[line.segment_id] = cut.get(line.segment_id, 0) + line.amount
            after_cost = {period_key(s): s.amount - cut.get(s.id, 0) for s in after.segments if s.begun}
            freed = sum(max(0, s.money - after_cost.get(period_key(s), 0)) for s in before.segments)
            if after.advance - before.advance > freed:
                bad.append("P3u advance<=freed")
            if after.debt > before.debt:
                bad.append("P3u debt")
            if after.written_off > before.written_off:
                bad.append("P3u used")
    elif isinstance(x, UndoLeave):                                  # P7: the leaving cancelled the same day = the account before the leaving
        if state_of(apply_undo(inp)) != state_of(inp):
            bad.append("P7 undo")
    elif isinstance(x, Relapse):                                    # P7 relapse (Б11.12 п. 5): leave, cancel, a record, leave again = the account of ONE leaving the record is entered before
        new = apply_relapse(inp, x.record)
        after = bl.run_ledger(new)
        if after.flags or not after.reconciles:
            bad.append("flags")
        if state_of(new) != state_of(oracle_relapse(inp, x.record)):
            bad.append("P7 relapse")
    elif isinstance(x, Return):
        after = bl.run_ledger(apply_return(inp))
        if after.flags or not after.reconciles:
            bad.append("flags")
    elif isinstance(x, Correct):
        if not any(c.id == x.credit_id for c in inp.credits):
            return bad
        new = apply_correct(inp, x)
        after = bl.run_ledger(new)
        if after.flags or not after.reconciles:
            bad.append("flags")
        if x.kind == "same" and state_of(new) != state_of(inp):
            bad.append("P6 same")
        if x.kind == "restore" and state_of(new) != state_of(inp):
            bad.append("P6b restore")
        if x.kind == "amount" and state_of(new) != state_of(apply_correct(inp, x, in_place=True)):
            bad.append("P6 amount")
    return bad


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
            worlds[c.id] = _slot_first_days(known_inp(inp, c.created_at))
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
    return bl.place_key(s)                                                          # Б11.11: one function in ``src/``, the properties and the price check say the same key


def deductions_by_place(L):
    """``{key of a period: the sum of its deductions for the lessons the centre cancelled}`` of a ledger."""
    key = {s.id: period_key(s) for s in L.segments}
    out = {}
    for line in L.recalcs:
        out[key[line.segment_id]] = out.get(key[line.segment_id], 0) + line.amount
    return out


NO_CANCEL_WORLDS = {}         # kind -> [(world, growth)] of the events with a nonzero allowance in a world WITHOUT a cancelled lesson
TOLERANCE_KINDS = {}          # Б11.13 п. 4: kind of the event -> [events checked, events with a nonzero allowance, of them in a world with a cancelled lesson]
EVENT = [None, None]          # [kind, world] of the event being checked (set by ``run_counts``)
TOLERANCE = [0, 0]            # Б11.12 п. 4: [events checked with ``place_growth``, events where it was NOT zero] - the tool prints it (in a common month a pause entered now must give zero)


def place_growth(before, after):
    """Б11.9 п. 2 (replaces the allowance of step 7.3): the growth of the debt and of the used forgiveness is bounded by the sum over PLACES of max(0, N after - N before), N of a place = charged minus the deductions of
    it.  Б11.12 п. 4: only the places that HAVE BEGUN count (a place that has not begun costs nothing, its length depends on the money: it was a blind spot of up to 1 000 000 in a common month).  For a common month a
    pause entered now gives zero here (every place only gets cheaper): the property stays strict."""
    def n_by_place(L):
        cut = {}
        for line in L.recalcs:
            cut[line.segment_id] = cut.get(line.segment_id, 0) + line.amount
        out = {}
        for s in L.segments:
            if s.begun:
                out[period_key(s)] = out.get(period_key(s), 0) + s.amount - cut.get(s.id, 0)
        return out

    n0, n1 = n_by_place(before), n_by_place(after)
    grow = sum(max(0, n1.get(k, 0) - n0.get(k, 0)) for k in set(n0) | set(n1))
    TOLERANCE[0] += 1
    TOLERANCE[1] += grow > 0
    row = TOLERANCE_KINDS.setdefault(EVENT[0], [0, 0, 0])
    row[0] += 1
    row[1] += grow > 0
    row[2] += grow > 0 and bool(EVENT[1] is not None and EVENT[1].centre_cancels)
    if grow > 0 and not (EVENT[1] is not None and EVENT[1].centre_cancels):
        NO_CANCEL_WORLDS.setdefault(EVENT[0], []).append((EVENT[1], grow))
    return grow


def violations(inp, x):
    """The names of the properties that the addition of ``x`` (a payment, a forgiveness, a pause or an edit of a pause - all entered now) breaks."""
    if isinstance(x, (Leave, UndoLeave, Relapse, Return, Correct)):
        return event_violations(inp, x)
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
        n_grow = place_growth(before, after)                                                  # Б11.9 п. 2: by PLACES (a personal period shifts the borders of its periods: one place gets dearer while another gets cheaper)
        if after.written_off - before.written_off > n_grow:
            bad.append(f"{tag} used")
        if after.debt - before.debt > n_grow:
            bad.append(f"{tag} debt")
        cut = {}
        for line in after.recalcs:                                                         # R4: a deduction of a period takes its place INSIDE the period: the money room is the charge minus the deductions
            cut[line.segment_id] = cut.get(line.segment_id, 0) + line.amount
        after_cost = {period_key(s): s.amount - cut.get(s.id, 0) for s in after.segments if s.begun}       # what the money of a period can be NOW (by the KEY of the model): a period that has not begun costs nothing
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
        n_grow = place_growth(before, after)                                                  # Б11.9 п. 2
        if after.written_off - before.written_off > n_grow:
            bad.append("P3k used")
        if after.debt - before.debt > n_grow:
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
        if x.on == inp.today:                                                                # Б11.8 п. 8, П1с: a payment dated today pays the debt first, what is over is the advance; the used forgiveness stays
            if after.debt != before.debt - min(x.amount, before.debt):
                bad.append("P1c debt")
            if after.advance != before.advance + max(0, x.amount - before.debt):
                bad.append("P1c advance")
            if after.written_off != before.written_off:
                bad.append("P1c used")
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
    main, _ = bl._tie(segments, credits, amounts, bl._line_room(recalcs), may)
    tied = {c.id: {s.id: sum(a for cc, a in main[s.id] if cc.id == c.id) for s in segments} for c in credits}
    lines = [f"today {inp.today}  charged {L.charged}  debt {L.debt}  advance {L.advance}  used {L.written_off} of {L.written_off_granted}  covered_through {L.covered_through}"]
    lines.append("  pauses: " + (", ".join(f"{p.first}..{p.last} entered {pause_moment(p):%d.%m %H:%M}" for p in inp.pauses) or "-") + "   centre cancels: " + (", ".join(f"{c.day} entered {getattr(c, 'entered', datetime.min):%d.%m %H:%M}" for c in inp.centre_cancels) or "-"))
    head = "  credit (kind, date, entered, amount)".ljust(52) + "".join(f"| {s.period_start} (charge {s.amount}, first day {s.days[0] if s.days else '-'})".ljust(46) for s in segments) + "| burned / leftover"
    lines.append(head)
    for c in credits:
        cells = []
        for s in segments:
            got = placed.parts.get((s.id, c.id), 0)
            cells.append(f"| place {bl._line_room(recalcs)(s)} tied {tied[c.id][s.id]} placed {got}".ljust(46))
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
        if len(inp.stays) > 1 and not isinstance(x, PauseEdit):
            steps.append((replace(inp, stays=inp.stays[:1]), x))
        if len(inp.stays) == 1 and inp.stays[0].last is not None and not isinstance(x, PauseEdit):
            steps.append((replace(inp, stays=(reopen(inp.stays[0]),)), x))
        if inp.opening is not None:
            steps.append((replace(inp, opening=None), x))
        if getattr(inp, "past_pauses", ()):
            steps.append((replace(inp, past_pauses=()), x))
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
    if isinstance(x, (Leave, UndoLeave, Relapse, Return, Correct)):
        lines.append(f"  ADDED  {x}  (today {inp.today}; stays " + ", ".join(f"{t.first}..{t.last} left {getattr(t, 'left_at', None)}" for t in inp.stays) + f"; opening {inp.opening})")
    elif isinstance(x, bl.Pause):
        lines.append(f"  ADDED  pause {x.first}..{x.last} entered {pause_moment(x):%d.%m %H:%M}")
    elif isinstance(x, bl.CentreCancel):
        lines.append(f"  ADDED  lesson {x.day} cancelled by the centre, entered {x.entered:%d.%m %H:%M}")
    elif isinstance(x, PauseEdit):
        lines.append(f"  ADDED  edit of pause {x.index}: {x.kind} to {x.first}..{x.last} entered {inp.today} 12:00")
    else:
        lines.append(f"  ADDED  {x.id}: {x.kind} {x.amount} dated {x.on} entered {x.created_at:%d.%m %H:%M}")
    return "\n".join(lines)


def apply_x(inp, x):
    if isinstance(x, Leave):
        return apply_leave(inp)
    if isinstance(x, UndoLeave):
        return apply_undo(inp)
    if isinstance(x, Relapse):
        return apply_relapse(inp, x.record)
    if isinstance(x, Return):
        return apply_return(inp)
    if isinstance(x, Correct):
        return apply_correct(inp, x)
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
    TOLERANCE[:] = [0, 0]
    TOLERANCE_KINDS.clear()
    NO_CANCEL_WORLDS.clear()
    for _ in range(n):
        inp = allowed_world(rng, cfg)
        worlds += 1
        for kind in (bl.PAYMENT, bl.WRITEOFF, "pause", "edit", "cancel", "leave", "transfer", "undo", "relapse_pay", "relapse_forgive", "return", "same", "amount", "restore"):
            EVENT[0], EVENT[1] = kind, inp
            x = (new_payment(rng, inp) if kind == bl.PAYMENT else new_forgiveness(rng, inp) if kind == bl.WRITEOFF
                 else new_pause(rng, inp) if kind == "pause" else new_pause_edit(rng, inp) if kind == "edit" else new_cancel(rng, inp) if kind == "cancel" and inp.centre_cancels
                 else new_leave(rng, inp, kind) if kind in ("leave", "transfer") else new_undo(rng, inp) if kind == "undo" else new_relapse(rng, inp, kind[8:]) if kind.startswith("relapse_") else new_return(rng, inp) if kind == "return"
                 else new_correct(rng, inp, kind) if kind in ("same", "amount", "restore") else None)
            if x is None:
                continue
            pays += kind == bl.PAYMENT
            forgs += kind == bl.WRITEOFF
            names = violations(inp, x)
            for name in names:
                counts[name] = counts.get(name, 0) + 1
                found.setdefault(name, []).append((inp, x))
            counts["P4 nomatch"] += oracle_p4(apply_x(inp, x))[1]
    counts["_tolerance events/nonzero"] = tuple(TOLERANCE)
    return counts, found, (worlds, pays, forgs)


def _totals(inp):
    L = bl.run_ledger(inp)
    return [L.charged, L.debt, L.advance, L.written_off, L.recalculated, L.paid, L.refundable, sorted(s.amount for s in L.segments)]


def dump(seed, n, cfg):
    """Б7.8 5.3: the TOTALS of the account of the worlds WITHOUT a forgiveness (as they are, and with a payment / a pause / an edit of a pause / a cancel entered now): one JSON line per account.  The same seed on
    two trees (``B7_TREE``) must give the same lines: R4 moves money between the periods, never the totals."""
    import json
    for k in range(n):
        rng = random.Random(seed * 1_000_003 + k)                 # a stream of its own for every world: a world that differs in one tree (a debt that caps a forgiveness) does not shift the others
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
            tolerance = counts.pop("_tolerance events/nonzero")
            for k, v in counts.items():
                total[k] = total.get(k, 0) + v
            total["_tolerance events/nonzero"] = tuple(a + b for a, b in zip(total.get("_tolerance events/nonzero", (0, 0)), tolerance))
            total["_worlds/payments/forgivenesses"] = tuple(a + b for a, b in zip(total.get("_worlds/payments/forgivenesses", (0, 0, 0)), size))
        print(" | ".join(f"{k}: {v}" for k, v in total.items()))
        print("tolerance by kind (kind: checked / nonzero / nonzero with a cancelled lesson): " + " | ".join(f"{k}: {v[0]} / {v[1]} / {v[2]}" for k, v in TOLERANCE_KINDS.items() if v[1]))
        for k, worlds_ in NO_CANCEL_WORLDS.items():
            print(f"nonzero WITHOUT a cancelled lesson, kind {k}: {len(worlds_)} events; smallest world: " + repr(min(worlds_, key=lambda w: (len(w[0].credits), len(w[0].pauses)))[0])[:900])
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
