"""Money-1 / Г: holes of the acceptance review MONEY1_G1_REVIEW (N1…N9) — each test carries the numbers of its scenario (PostgreSQL)."""
from datetime import date

import pytest

from src.models.auth import AttendanceExcuse

from tests.money1_helpers import hold_lesson, now_4_nov  # noqa: F401  (fixtures)
from tests.ops_helpers import grant_money_right
from tests.pg_world import make_client, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_money1_attendance_pg import post_save

pytestmark = needs_pg


# ---- Г-1 (N1): the text of a decided excuse does not move the decision --------------------------------------------------------------------------------

@pytest.mark.parametrize("decision", ["confirmed", "rejected"])
def test_g1_the_text_of_a_decided_excuse_keeps_the_decision_for_everybody(world, now_4_nov, decision):
    day, lid = hold_lesson(world, date(2026, 10, 30))
    s1 = world.students[0]
    grant_money_right(world)
    assert post_save(world, world.owner, day, lid, ["excused", "present", "present"], correctionReason="Болел", excuseReasons={str(s1.id): "Болел, написал"}).status_code == 200
    excuse = world.db.query(AttendanceExcuse).filter_by(student_id=s1.id).one()
    excuse.status, excuse.reviewed_by, excuse.review_note = decision, world.owner.id, "Решено"
    world.db.commit()
    grant_money_right(world, False)
    for who in (world.teacher, world.admin):                       # no right to the money: the text is saved, the decision (pack −1 lesson, teacher +20 000) stays
        r = post_save(world, who, day, lid, ["excused", "present", "present"], excuseReasons={str(s1.id): f"Другой текст {who.id}"})
        assert r.status_code == 200, r.text
        world.db.expire_all()
        row = world.db.query(AttendanceExcuse).filter_by(student_id=s1.id).one()
        assert (row.status, row.reviewed_by, row.review_note) == (decision, world.owner.id, "Решено") and row.reason == f"Другой текст {who.id}"


# ---- Г-2 (N2): the first price of a group begins inside the first period of a student who was added before it --------------------------------------------

from tests.money1b_helpers import account, charge_of, set_schedule, slot  # noqa: E402
from tests.pg_world import give_group_price  # noqa: E402
from tests.test_pay2_api_pg import fixed_today, membership, settings_block, stay  # noqa: E402,F401  (fixtures)
from tests.test_money1b_price_pg import everybody_from  # noqa: E402

D = date


def test_g2_the_first_price_from_the_10th_prices_the_period_of_the_student_added_on_the_5th(world, fixed_today):
    w = world
    fixed_today(D(2026, 11, 12))
    settings_block(w, accounting_start_date="2026-09-01")
    give_group_price(w.db, w.group, 500_000, valid_from=D(2026, 11, 10))      # set on 01.11 «с 10.11»; the student was added on 05.11
    everybody_from(w, D(2026, 11, 5))
    led = account(w, membership(w).id)
    assert (led.charged, led.debt) == (500_000, 500_000) and charge_of(led, D(2026, 11, 5)) == 500_000      # e7c44aa: 0 and 0 - the first month for free


def test_g2_a_period_that_ended_before_the_first_price_is_still_free_and_the_next_one_is_charged(world, fixed_today):
    w = world
    fixed_today(D(2026, 12, 12))
    settings_block(w, accounting_start_date="2026-09-01")
    give_group_price(w.db, w.group, 500_000, valid_from=D(2026, 11, 10))
    everybody_from(w, D(2026, 10, 1))
    led = account(w, membership(w).id)
    assert charge_of(led, D(2026, 10, 1)) == 0 and charge_of(led, D(2026, 11, 1)) == 500_000       # a month with no price on any day stays unpriced


def test_g2_the_shared_month_cell_of_the_student_added_before_the_first_price_is_priced(world, fixed_today):
    w = world
    fixed_today(D(2026, 11, 12))
    settings_block(w, accounting_start_date="2026-09-01", billing_anchor="fixed_day", billing_day=1)
    set_schedule(w, [slot("mon", 1, "2026-09-01"), slot("wed", 3, "2026-09-01"), slot("fri", 5, "2026-09-01")])
    give_group_price(w.db, w.group, 300_000, valid_from=D(2026, 11, 10))
    everybody_from(w, D(2026, 11, 5))
    assert charge_of(account(w, membership(w).id), D(2026, 11, 1)) == 254_000       # the same 11 of 13 lessons x 300 000 as when the price is typed «с сегодня»


# ---- Г-5 (N6, ЗП-15): a reopened month keeps its advance in the chain; a payout locks the advance chain of the teacher ---------------------------------------

import threading  # noqa: E402
import time  # noqa: E402

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.auth.payroll_payouts import OverpayConfirmRequired, PayrollPayoutService  # noqa: E402
from src.auth.payroll_schemas import PayoutCreate  # noqa: E402
from src.models.auth import PayrollPayout  # noqa: E402

from tests.money1_helpers import reopen_now, sheet, tashkent  # noqa: E402


def payout_row(w, sheet_row, amount):
    row = PayrollPayout(org_id=w.org.id, sheet_id=sheet_row.id, teacher_id=sheet_row.teacher_id, period_month=sheet_row.period_month, amount=amount, paid_on=date(2026, 10, 3), method="cash",
                        sheet_revision=sheet_row.revision, sheet_total=sheet_row.total_amount, created_by=w.owner.id)
    return row


def september_and_october(w):
    """September closed at 1 000 000, paid 1 200 000 (a confirmed advance of 200 000); October closed at 800 000."""
    sept = sheet(w, w.teacher, date(2026, 9, 1), tashkent(2026, 10, 3), total=1_000_000)
    octo = sheet(w, w.teacher, date(2026, 10, 1), tashkent(2026, 11, 3), total=800_000)
    w.db.add(payout_row(w, sept, 1_200_000))
    w.db.commit()
    return sept, octo


def test_g5_reopening_september_does_not_move_the_advance_out_of_october(world, now_4_nov):
    w = world
    sept, octo = september_and_october(w)
    payouts = PayrollPayoutService(w.db)
    assert payouts.carry_ins(str(w.org.id), [w.teacher.id], date(2026, 10, 1)) == {w.teacher.id: 200_000}
    reopen_now(w.db, w.org.id, w.teacher.id, "2026-09", w.owner.id, "Поправить урок")
    # September is open again, its LAST saved total (1 000 000) still stands: October is to pay 800 000 - 200 000, not 800 000 (e7c44aa: carry_in 0, «к выплате» +200 000 and a double payout)
    assert payouts.carry_ins(str(w.org.id), [w.teacher.id], date(2026, 10, 1)) == {w.teacher.id: 200_000}
    assert payouts.carry_chain(str(w.org.id), w.teacher.id, [date(2026, 10, 1), date(2026, 11, 1)]) == {date(2026, 10, 1): 200_000, date(2026, 11, 1): 0}
    assert payouts.state_for(str(w.org.id), octo, None).balance == 600_000


def test_g5_a_payout_waits_for_the_advance_that_is_being_written_in_another_tab(world, now_4_nov):
    """The advance of September (+200 000 in October) is written in tab A under the lock of the chain; tab B pays October in full meanwhile: B must wait and then see 600 000 left."""
    w = world
    sept = sheet(w, w.teacher, date(2026, 9, 1), tashkent(2026, 10, 3), total=1_000_000)
    octo = sheet(w, w.teacher, date(2026, 10, 1), tashkent(2026, 11, 3), total=800_000)
    org, teacher, octo_id, owner_id = str(w.org.id), w.teacher.id, octo.id, str(w.owner.id)
    engine = w.db.get_bind()
    make = sessionmaker(engine, autoflush=False)
    tab_a = make()
    tab_a.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"payroll-advance:{org}:{teacher}"})       # tab A is in the middle of its write
    outcome = {}

    def tab_b():
        session = make()
        try:
            outcome["created"] = PayrollPayoutService(session).create(org, octo_id, owner_id, PayoutCreate(amount=800_000, method="cash"), None, lambda s: int(s.total_amount))
        except BaseException as exc:  # noqa: BLE001 - the test inspects it
            outcome["error"] = exc
        finally:
            session.close()

    thread = threading.Thread(target=tab_b)
    thread.start()
    thread.join(timeout=2)
    assert thread.is_alive(), "tab B did not wait for the advance chain of the teacher"
    tab_a.add(payout_row(w, sept, 1_200_000))
    tab_a.commit()                                                                                    # the advance is there, the lock is released
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), OverpayConfirmRequired), outcome       # 800 000 against 600 000 left: the owner is asked, not silently overpaid by 200 000
    assert outcome["error"].extra["balance"] == 600_000 and outcome["error"].extra["overpay"] == 200_000
    tab_a.close()


