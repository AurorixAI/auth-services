"""Money-1 / G-13 (N13): a refund dated BEFORE a refund that already exists is judged on the money of its own day and of the days after it - and the refusal says why (PostgreSQL).

Numbers (price 500 000 from 01.01, joined 15.09, today 21.10): paid 600 000 on 16.09, 500 000 on 25.09, 1 000 000 on 20.10; a refund of 500 000 is dated 20.09."""
from src.models.auth import StudentRefund
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, D, fixed_today, give_group_price, membership, owner, seed_payment, settings_block, stay  # noqa: F401

pytestmark = needs_pg
NB = "\u00a0"


def refund(w, m, amount, day, expect=None):
    r = owner(w).post(f"{API}/org/memberships/{m.id}/refunds", json={"amount": amount, "method": "cash", "reason": "ученик ушёл", "effective_on": day})
    assert expect is None or r.status_code == expect, r.text
    return r


def n13_world(w, fixed_today, later_refund=500_000):
    fixed_today(D(2026, 10, 21))
    settings_block(w, accounting_start_date="2026-09-01")
    give_group_price(w.db, w.group, 500_000, valid_from=D(2026, 1, 1))
    m = membership(w)
    stay(w, m, D(2026, 9, 15))
    for number, (amount, day) in enumerate(((600_000, D(2026, 9, 16)), (500_000, D(2026, 9, 25)), (1_000_000, D(2026, 10, 20))), start=1):
        seed_payment(w, m, amount, day, receipt_no=number)
    refund(w, m, later_refund, "2026-09-20", expect=201)
    return m


def test_g13_a_refund_before_a_later_refund_is_refused_with_the_reason_and_writes_nothing(world, fixed_today):
    """Было: «можно вернуть» 567 000 (на 16.09) и 600 000 (сегодня), запись проходила до проверки и давала 409 «Сначала отмените возврат: без этой оплаты…» (про оплату, которой никто не трогал)."""
    m = n13_world(world, fixed_today)
    r = refund(world, m, 200_000, "2026-09-16", expect=422)
    detail = r.json()["detail"]
    assert detail["code"] == "REFUND_EXCEEDS_ADVANCE" and detail["refundable"] == 0
    assert detail["message"] == "После этой даты уже есть возврат от 20.09 — выберите дату не раньше"
    assert world.db.query(StudentRefund).filter_by(membership_id=m.id).count() == 1          # nothing was written


def test_g13_a_date_after_the_later_refund_is_judged_as_before(world, fixed_today):
    m = n13_world(world, fixed_today)
    assert refund(world, m, 200_000, "2026-10-21", expect=201).json()["refund"]["amount"] == 200_000        # today: 600 000 of the advance are there


def test_g13_a_refund_before_a_later_one_that_still_fits_names_the_limit_and_the_reason(world, fixed_today):
    """The later refund is smaller: 100 000 on 20.09 leaves 400 000 of the advance of that day - 450 000 on 16.09 would break it."""
    m = n13_world(world, fixed_today, later_refund=100_000)
    detail = refund(world, m, 450_000, "2026-09-16", expect=422).json()["detail"]
    assert detail["refundable"] == 400_000
    assert detail["message"] == f"Вернуть на эту дату можно не больше 400{NB}000{NB}сум: после неё уже есть возврат от 20.09 — выберите дату не раньше"
    refund(world, m, 400_000, "2026-09-16", expect=201)
