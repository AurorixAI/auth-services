"""Money-1, stage walk D-6: a transfer of a student who paid in advance - the window says what the NEW account looks like after it, and the money is conserved (PostgreSQL).

The world of the stage walk (05.11.2026, a monthly mode with the common billing day 1): U5 pays «Геометрия 8» (360 000 a month, Mon / Thu) September, October and November + December (720 000) and is
transferred to «Алгебра 7» (400 000 a month, Tue / Fri) on Thursday 05.11 and back the same day.  What looked like an asymmetry (a window of 640 000 both times, an advance of 640 000 after the first
transfer and of 360 000 after the second) is two different ACCOUNTS: the new membership starts being charged the day AFTER the transfer (``first_billable_floor``: that day belongs to the old group), the
group the student comes back to still has its own history (its payments, its full month).  Neither is a loss: paid = spent + advance, on every day."""
from datetime import date, datetime

from src.auth.payments_service import PaymentService
from src.models.auth import StudentGroup

from tests.money1_helpers import changed_tables, db_fingerprint
from tests.ops_helpers import clock, grant_money_right, needs_pg, pg_session  # noqa: F401  (fixtures)
from tests.payf_helpers import API, D, build_org, client, new_group, new_slot, new_student, pay, set_payments
from tests.pg_world import give_group_price

pytestmark = needs_pg
THU_5_NOV = "2026-11-05 11:00"


def _world(pg_session, clock):
    """G2 (Mon / Thu 18:00, 360 000) with the student paid for September, October and November + December; G1 (Tue / Fri 16:00, 400 000)."""
    w = build_org(pg_session)
    clock.set("2026-11-05 09:00")
    set_payments(w, period_mode="calendar", billing_anchor="fixed_day", billing_day=1)
    w.g2 = new_group(w.db, w.org, w.ids, "G2", "Геометрия 8 (пн/чт)", w.A, [new_slot("lesson-mon", 1, "18:00", "19:00", "2026-09-01"), new_slot("lesson-thu", 4, "18:00", "19:00", "2026-09-01")])
    w.g1 = new_group(w.db, w.org, w.ids, "G1", "Алгебра 7 (вт/пт)", w.A, [new_slot("lesson-tue", 2, "16:00", "17:00", "2026-09-01"), new_slot("lesson-fri", 5, "16:00", "17:00", "2026-09-01")])
    give_group_price(w.db, w.g2, 360_000, valid_from=D(2026, 9, 1))
    give_group_price(w.db, w.g1, 400_000, valid_from=D(2026, 9, 1))
    w.u5 = new_student(w.db, w.org, w.ids, "u5", "Каримов", "Фарход", w.g2, joined=datetime(2026, 8, 31, 5, 0))
    w.db.commit()
    w.m2 = w.db.query(StudentGroup).filter_by(group_id=w.g2.id, student_id=w.u5.id).one()
    for when, amount in ((D(2026, 9, 1), 360_000), (D(2026, 10, 1), 360_000), (D(2026, 11, 1), 720_000)):
        pay(w, w.m2, amount, when)
    return w


def _transfer(w, group, *, dry_run=False, expect=200, who="owner"):
    r = client(w, who).post(f"{API}/org/groups/{group.id}/students/{w.u5.id}", params={"mode": "transfer", **({"dry_run": "true"} if dry_run else {})})
    assert r.status_code == expect, r.text
    return r.json()


def _account(w, group, on, *, tomorrow=False):
    """The screens' ledger of the membership of the student in ``group`` as of the business day ``on`` (noon), read after the commit - the same function the window used."""
    from src.auth import business_clock as bc

    w.db.expire_all()
    m = w.db.query(StudentGroup).filter_by(group_id=group.id, student_id=w.u5.id).first()
    if m is None:
        return None
    return PaymentService(w.db).account_after(w.org.id, m.id, bc.business_wall_to_utc(datetime(on.year, on.month, on.day, 12)), tomorrow=tomorrow)


def test_d6_the_window_of_the_first_transfer_says_the_new_account_and_is_the_state_after(pg_session, clock):
    w = _world(pg_session, clock)
    clock.set(THU_5_NOV)
    assert _account(w, w.g2, D(2026, 11, 5))["advance"] == 360_000                                          # before any transfer: 1 440 000 paid - 1 080 000 charged (Sep, Oct, the whole Nov)
    before = db_fingerprint(w.db)
    preview = _transfer(w, w.g1, dry_run=True)
    assert changed_tables(before, db_fingerprint(w.db)) == []                                              # the preview writes nothing
    real = _transfer(w, w.g1)
    # 1 440 000 paid - Sep 360 000 - Oct 360 000 - Nov 2 lessons of 9 (02.11, 05.11) 80 000 = 640 000 leave the old group
    assert [x["message"] for x in real["carried"] if x["code"] == "payment_carried_over"] == ["Остаток денег 640 000 сум перенесён в «Алгебра 7 (вт/пт)»"]
    window = preview["carry_after"]
    assert window == real["carry_after"]                                                                    # I-5: the window is what the transfer then says
    assert (window["carried"], window["charged_now"], window["advance"], window["debt"]) == (640_000, 0, 640_000, 0)
    assert (window["charged_tomorrow"], window["advance_tomorrow"]) == (350_000, 290_000)                  # 7 of the 8 lessons of November in the new group (06.11 ... 27.11) x 50 000, from tomorrow
    assert window["text"] == ("Перенесено 640 000 сум; аванс останется 640 000 сум. Со следующего дня начислят ещё 350 000 сум (аванс останется 290 000 сум)")
    assert any(x["code"] == "transfer_balance_after" and x["message"] == window["text"] for x in real["warnings"])
    now = _account(w, w.g1, D(2026, 11, 5), tomorrow=True)                                                  # what the screens say, read after the commit
    assert (now["advance"], now["charged"], now["advance_tomorrow"], now["charged_tomorrow"]) == (640_000, 0, 290_000, 350_000)


def test_d6_the_way_back_the_same_day_charges_the_rest_of_the_month_at_once_and_the_account_is_what_it_was(pg_session, clock):
    """The walk: the window said 640 000 and the advance after was 360 000 - 280 000 = 7/9 x 360 000 of the rest of November went at once to the group the student came back to."""
    w = _world(pg_session, clock)
    clock.set(THU_5_NOV)
    _transfer(w, w.g1)
    clock.set("2026-11-05 12:00")
    before = db_fingerprint(w.db)
    preview = _transfer(w, w.g2, dry_run=True)
    assert changed_tables(before, db_fingerprint(w.db)) == []
    real = _transfer(w, w.g2)
    window = preview["carry_after"]
    assert window == real["carry_after"]
    assert (window["carried"], window["charged_now"], window["advance"], window["debt"], window["charged_tomorrow"]) == (640_000, 280_000, 360_000, 0, 0)       # 7 lessons of 9 x 40 000 at once
    assert window["text"] == "Перенесено 640 000 сум, из них сразу 280 000 сум на оплату остатка месяца; аванс останется 360 000 сум"
    back = _account(w, w.g2, D(2026, 11, 5))
    assert (back["advance"], back["charged"]) == (360_000, 1_080_000)                                      # the account is exactly what it was before the first transfer: nothing is lost
    gone = _account(w, w.g1, D(2026, 11, 5), tomorrow=True)
    assert (gone["advance"], gone["advance_tomorrow"], gone["charged_tomorrow"]) == (0, 0, 0)               # and the group he left has no money and no charge


def test_d6_paid_is_spent_plus_advance_and_the_transfers_cancel_on_every_day(pg_session, clock):
    """Conservation (I-1): over the memberships of the student paid 1 440 000 = charged + advance - debt, every ledger reconciles, what left an account arrived at the other one - after the
    first transfer, the next day, after the way back and at the end of the month."""
    from src.auth import business_clock as bc

    w = _world(pg_session, clock)

    def books(on):
        w.db.expire_all()
        ms = w.db.query(StudentGroup).filter_by(student_id=w.u5.id).all()
        entries = [e.ledger for e in PaymentService(w.db)._loader(str(w.org.id), on, recalc=True).run([m.id for m in ms])]
        assert entries and all(l.reconciles for l in entries)
        assert sum(l.transferred_in for l in entries) == sum(l.transferred_out for l in entries)           # what left an account arrived at the other one
        assert sum(l.paid for l in entries) == 1_440_000
        assert sum(l.charged for l in entries) + sum(l.advance for l in entries) - sum(l.debt for l in entries) == 1_440_000
        return entries

    clock.set(THU_5_NOV)
    _transfer(w, w.g1)
    for day in (D(2026, 11, 5), D(2026, 11, 6), D(2026, 11, 30)):
        books(day)
    clock.set("2026-11-05 12:00")
    _transfer(w, w.g2)
    for day in (D(2026, 11, 5), D(2026, 11, 6), D(2026, 11, 30)):
        books(day)


def test_d6_a_transfer_that_carries_no_money_has_no_balance_block(pg_session, clock):
    """A student who paid nothing: nothing arrives, so there is nothing to say (no empty sentence)."""
    w = _world(pg_session, clock)
    from src.models.auth import StudentPayment

    w.db.query(StudentPayment).filter_by(student_id=w.u5.id).delete()
    w.db.commit()
    clock.set(THU_5_NOV)
    real = _transfer(w, w.g1)
    assert real["carry_after"] is None and not [x for x in real["warnings"] if x["code"] in ("payment_carried_over", "transfer_balance_after")]


def test_d6_the_bulk_preview_names_the_new_account_of_every_student(pg_session, clock):
    w = _world(pg_session, clock)
    clock.set(THU_5_NOV)
    before = db_fingerprint(w.db)
    r = client(w, "owner").post(f"{API}/org/groups/{w.g1.id}/students/bulk", params={"mode": "transfer", "dry_run": "true"}, json={"student_ids": [str(w.u5.id)]})
    assert r.status_code == 200, r.text
    assert changed_tables(before, db_fingerprint(w.db)) == []
    (line,) = [x for x in r.json()["warnings"] if x["code"] == "transfer_balance_after"]
    assert line["student_id"] == str(w.u5.id) and line["message"].startswith("Перенесено 640 000 сум; аванс останется 640 000 сум")
