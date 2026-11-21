"""PAY-TAILS Б10 (PostgreSQL): the nightly run over a centre with a forgiveness - honest accounts give no I-6; a sabotaged binding gives I-6 that is stored, written in the letter and shown by the console.

The probes write nothing: the rows of the money tables are the same before and after the night (counted here)."""
import pytest

from src.auth import billing_ledger as bl
from src.auth import money_reconcile as mr
from src.models.auth import MoneyReconcileFinding, StudentPayment, StudentWriteoff

from tests.pg_world import business_today, ensure_billing, needs_pg, world  # noqa: F401  (fixtures)
from tests.pg_world import pg_session  # noqa: F401
from tests.test_logicA_payroll_pg import API, owner
from tests.test_payments_service_pg import mid

pytestmark = needs_pg


def night(world, **kw):
    return mr.run(world.db, apply=True, only=[world.org.id], **kw)


def forgiven_world(world):
    """A centre with a price, the first student owes, the owner forgives the whole debt (an honest forgiveness through the product's route)."""
    ensure_billing(world)
    m = mid(world, world.students[0])
    r = owner(world).post(f"{API}/org/memberships/{m}/writeoffs", json={"all": True, "reason": "болезнь", "effective_on": business_today().isoformat()})
    assert r.status_code == 201, r.text
    return m


def test_i6_an_honest_night_over_a_centre_with_a_forgiveness_gives_none_and_writes_no_money_row(world):
    forgiven_world(world)
    rows = (world.db.query(StudentPayment).count(), world.db.query(StudentWriteoff).count())
    result = night(world)
    assert [i for i in result["items"] if i["org"] == str(world.org.id) and i["invariant"] == "I-6"] == []
    assert result["byInvariant"]["I-6"] == 0
    assert (world.db.query(StudentPayment).count(), world.db.query(StudentWriteoff).count()) == rows                  # the probes wrote nothing


def test_i6_a_broken_binding_is_a_stored_finding_with_a_sentence_in_the_letter_and_in_the_console_answer(world, monkeypatch):
    m = forgiven_world(world)
    real = bl._place_credits

    def forgetful(segments, credits, net, recalcs, worlds):
        if any(c.id == "preview" for c in credits):
            credits = [c for c in credits if c.kind != bl.WRITEOFF]
        return real(segments, credits, net, recalcs, worlds)

    sent = []
    from src.mail import sender

    monkeypatch.setattr(sender, "deliver", lambda msg, cfg=None: sent.append(msg) or True)
    monkeypatch.setenv("MONEY_RECONCILE_EMAIL", "money-team@example.test")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test"), monkeypatch.setenv("SMTP_USER", "u"), monkeypatch.setenv("SMTP_PASSWORD", "p")
    monkeypatch.setattr(bl, "_place_credits", forgetful)
    first = night(world, mail=True)
    hits = [i for i in first["items"] if i["org"] == str(world.org.id) and i["invariant"] == "I-6"]
    assert hits and all(i["key"].startswith(f"membership:{m}:") for i in hits)
    small = next(i for i in hits if i["key"].endswith("pay_today_small:debt_grew"))
    assert small["text"].startswith("Проба оплаты") and "увеличила долг: было 0" in small["text"].replace(" ", " ").replace(" сум", "")
    stored = world.db.query(MoneyReconcileFinding).filter_by(run_id=first["runId"], org_id=world.org.id, invariant="I-6").all()
    assert len(stored) == len(hits) and all(f.is_new and f.details["text"] for f in stored)            # the CHECK of the table takes I-6
    assert first["mail"] == "sent" and len(sent) == 1
    body = sent[0].get_body(preferencelist=("plain",)).get_content()
    assert "I-6" in body and "увеличила долг" in body and str(m) in body
    second = night(world, mail=True)                                                                  # the same hole stands: «прежнее», no second letter
    assert second["new"] == 0 and len(sent) == 1
