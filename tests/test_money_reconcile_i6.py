"""PAY-TAILS Б10: the invariant I-6 of the nightly reconciliation (the pure ledger; the run over a centre is in ``test_money_reconcile_i6_pg.py``).

An honest account with a forgiveness gives NO finding on any probe; a sabotaged binding (a forgiveness that disappears when a trial payment is put on the account - the shape of the holes of Б7)
gives a finding that reads without a key; the probes write nothing (the input of the account is not changed)."""
import random
from dataclasses import replace
from datetime import date

from src.auth import billing_ledger as bl
from src.auth import money_reconcile as mr
from src.auth import money_reconcile_probes as mp
from src.auth import payments_texts as tx
from tests.pay2_ledger_helpers import D, at, credit, flat, make, weekly

MWF = weekly((0, 2, 4))
ORG, MEMBERSHIP = "00000000-0000-0000-0000-0000000000a1", "00000000-0000-0000-0000-0000000000b1"


def account(*, paid=0, forgiven=100_000, today=D(2026, 10, 25)):
    """«Свой месяц у каждого ученика», 600 000, joined 06.10; the forgiveness entered on 20.10 (the debt of the day: 600 000)."""
    credits = [credit("p1", D(2026, 10, 6), paid)] if paid else []
    if forgiven:
        credits.append(bl.Credit("w1", bl.WRITEOFF, D(2026, 10, 20), at(D(2026, 10, 20), 10), forgiven))
    return make(today=today, stays=[bl.Stay(D(2026, 10, 6))], price_at=flat(600_000, D(2026, 1, 1)), start=D(2026, 9, 1), credits=credits, lesson_days=MWF)


def money(n):
    return tx.money(n)


def test_an_honest_account_with_a_forgiveness_gives_no_i6_on_any_probe():
    for paid in (0, 200_000, 600_000):
        inp = account(paid=paid)
        assert mp.check_probes(inp, bl.run_ledger(inp)) == [], paid


def test_an_account_without_a_forgiveness_is_not_probed():
    inp = account(forgiven=0)
    assert mp.check_probes(inp, bl.run_ledger(inp)) == []


def test_the_probes_write_nothing_the_input_is_the_same_after():
    inp = account(paid=200_000)
    snapshot = replace(inp)
    mp.check_probes(inp, bl.run_ledger(inp))
    assert inp == snapshot and len(inp.credits) == 2                                           # a payment and a forgiveness: no trial credit stayed in it


def test_a_broken_binding_gives_a_finding_that_reads_without_a_key(monkeypatch):
    """The binding forgets the forgiveness as soon as a trial record is on the account: the trial payment of 1 000 raises the debt from 500 000 to 599 000 (600 000 charged, 1 000 paid)."""
    real = bl._place_credits

    def forgetful(segments, credits, net, recalcs, worlds):
        if any(c.id == "preview" for c in credits):
            credits = [c for c in credits if c.kind != bl.WRITEOFF]
        return real(segments, credits, net, recalcs, worlds)

    inp = account()
    ledger = bl.run_ledger(inp)
    assert ledger.debt == 500_000                                                                  # the honest account: 600 000 - 100 000
    monkeypatch.setattr(bl, "_place_credits", forgetful)
    found = mr.check_probes(ORG_UUID, MEMBERSHIP, inp, ledger)
    texts = {f.subject_key.split(":", 2)[2]: f.details["text"] for f in found}
    assert texts["pay_today_small:debt_grew"] == f"Проба оплаты {money(1_000)} сегодня увеличила долг: было {money(500_000)}, стало {money(599_000)}"
    assert all(f.invariant == "I-6" and f.subject_id == MEMBERSHIP and f.org_id == ORG_UUID for f in found)
    key = next(f for f in found if f.subject_key.endswith("pay_today_small:debt_grew"))
    assert (key.expected, key.actual) == (500_000, 599_000) and key.details["before"]["debt"] == 500_000 and key.details["after"]["debt"] == 599_000
    assert "pay_back_small:debt_grew" in texts                                                    # the same hole on the back-dated probe
    assert "pay_today_debt:debt_grew" not in texts                                                # a payment of 500 000: the debt 600 000 - 500 000 = 100 000 is lower than 500 000, the rule keeps (the small probe sees it)


def test_a_forgiveness_that_lies_on_a_period_that_has_not_begun_is_a_finding_of_the_state():
    inp = account()
    ledger = bl.run_ledger(inp)
    future = [replace(s, forgiven=70_000) if not s.begun else s for s in ledger.segments]
    if not any(not s.begun for s in ledger.segments):
        future = [*ledger.segments, replace(ledger.segments[0], id="future", begun=False, forgiven=70_000)]
    bad = replace(ledger, segments=tuple(future))
    (found,) = [f for f in mp.check_probes(inp, bad) if f.probe == "state"]
    assert found.rule == "forgiveness_on_future_period" and found.text == f"Использованное прощение лежит на периоде, который ещё не начался: {money(70_000)}"


def test_the_trial_sums_the_backdate_and_the_forgiveness_of_the_probe_are_those_of_the_spec(monkeypatch):
    """The sums 1 000 and max(debt, 1 000); the back date = the first day of the stay (not before today - 3 years); a forgiveness probe only with a debt, of min(debt, 1 000)."""
    seen = []
    real = bl.run_ledger

    def spy(inp):
        probe = [c for c in inp.credits if c.id == "preview"]
        if probe:
            seen.append((probe[0].kind, probe[0].on, probe[0].amount))
        return real(inp)

    inp = account(paid=0, forgiven=100_000)
    ledger = real(inp)
    monkeypatch.setattr(mp.bl, "run_ledger", spy)
    mp.check_probes(inp, ledger)
    assert seen == [(bl.PAYMENT, date(2026, 10, 25), 1_000), (bl.PAYMENT, date(2026, 10, 25), 500_000), (bl.PAYMENT, date(2026, 10, 6), 1_000), (bl.PAYMENT, date(2026, 10, 6), 500_000),
                    (bl.WRITEOFF, date(2026, 10, 25), 1_000)]
    seen.clear()
    paid = account(paid=600_000, forgiven=100_000)                                                  # nothing owed: the sums 1 000 only and no forgiveness probe
    mp.check_probes(paid, real(paid))
    assert [(k, a) for k, _, a in seen] == [(bl.PAYMENT, 1_000), (bl.PAYMENT, 1_000)]


ORG_UUID = __import__("uuid").UUID(ORG)


def test_no_i6_on_2000_worlds_of_the_generator_with_a_forgiveness():
    """The worlds of the property tests (all three modes, with and without a cancelled lesson, pauses, a leaving, 1-4 credits in the order of entry): the probes of the night find nothing on any of them.
    The worlds without a forgiveness are not probed (the rule of the night); how many were probed is counted - the test must really look at something."""
    from tests import pay_tails_b7_tools as tools

    rng, probed, bad = random.Random(20261122), 0, []
    cfgs = (tools.Cfg("all", "none"), tools.Cfg("common", "with"), tools.Cfg("personal", "with"))
    for n in range(2000):
        inp = tools.allowed_world(rng, cfgs[n % 3])
        if not any(c.kind == bl.WRITEOFF for c in inp.credits):
            continue
        probed += 1
        found = mp.check_probes(inp, bl.run_ledger(inp))
        if found:
            bad.append((n, [f.text for f in found][:2]))
    assert probed > 300 and bad == [], (probed, bad[:3])
