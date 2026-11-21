"""PAY-TAILS part 4 (А6.4-А6.5, Р5, Р6): what the pages say about a forgiveness that covered LESS than it was entered for.  The world is С2: ЛП 420 000, paid 200 000 on 01.11, forgiven 220 000 on 21.11,
left on the 21st day: charged 294 000, money 200 000, the forgiveness used 94 000 of 220 000, advance 0."""
import io
from datetime import date

from openpyxl import load_workbook

from src.auth import payments_texts as tx
from src.auth.payments_service import AUDIT_WRITEOFF
from tests.money1b_helpers import account
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay_tails_money_pg import D, API, fixed_today, forgive, leave, lp, membership, owner, pay, refund, statement  # noqa: F401

pytestmark = needs_pg


def c2(world, fixed_today):
    g, s = lp(world, fixed_today)
    pay(world, s, g, amount=200_000, paid_on=D(2026, 11, 1))
    fixed_today(D(2026, 11, 21))
    made = forgive(world, s, g, 220_000)
    leave(world, g, s)
    return g, s, made


def walk(node, key):
    """Every value of ``key`` anywhere in a JSON answer."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            yield from walk(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v, key)


def test_the_record_of_the_forgiveness_says_entered_used_and_the_words(world, fixed_today):
    g, s, made = c2(world, fixed_today)
    assert (made["writeoff"]["amount"], made["writeoff"]["used"], made["writeoff"]["text"]) == (220_000, 220_000, None)      # the day it was entered the whole period was open: all of it used
    st = statement(world, s, g)
    (row,) = [r for r in st["rows"] if r["type"] == "writeoff"]
    assert row["details"]["amount"] == 220_000 and row["details"]["used"] == 94_000 and row["credit"] == 94_000
    assert row["details"]["text"] == "использовано " + tx.money(94_000) and row["title"].startswith("Прощено " + tx.money(220_000))      # the title keeps what was entered
    sm = st["summary"]
    assert (sm["charged"], sm["paid"], sm["written_off"], sm["advance"], sm["reconciles"]) == (294_000, 200_000, 94_000, 0, True)          # the equality of the statement on the USED sum


def test_a_forgiveness_used_in_full_has_no_words(world, fixed_today):
    g, s = lp(world, fixed_today)
    forgive(world, s, g, 420_000)
    (row,) = [r for r in statement(world, s, g)["rows"] if r["type"] == "writeoff"]
    assert row["details"]["used"] == 420_000 and row["details"]["text"] is None and row["credit"] == 420_000


def test_a_forgiveness_that_was_not_needed_at_all_says_used_zero_and_adds_nothing(world, fixed_today):
    # С3 shape in ЛП: paid 420 000, forgiveness impossible (no debt) - so: ОМ-like cut instead: paid 300 000 (01.11), forgiven 120 000 (01.11), left on day 10: charged 140 000 (420 000 x 10 / 30),
    # money 300 000 covers it: used 0, the line stays with credit 0 and the words «использовано 0 сум»; advance 160 000 is MONEY
    g, s = lp(world, fixed_today)
    forgive(world, s, g, 120_000)
    pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 1))
    fixed_today(D(2026, 11, 10))
    leave(world, g, s)
    st = statement(world, s, g)
    (row,) = [r for r in st["rows"] if r["type"] == "writeoff"]
    assert (row["credit"], row["details"]["used"], row["details"]["amount"]) == (0, 0, 120_000) and row["details"]["text"] == "использовано " + tx.money(0)
    sm = st["summary"]
    assert (sm["charged"], sm["written_off"], sm["advance"], sm["reconciles"]) == (140_000, 0, 160_000, True)


def test_income_receipts_and_the_journal_count_the_used_forgiveness(world, fixed_today):
    g, s, made = c2(world, fixed_today)
    params = {"from": "2026-11-01", "to": "2026-11-30"}
    income = owner(world).get(f"{API}/org/payments/income", params=params)
    assert income.status_code == 200, income.text
    assert 94_000 in set(walk(income.json()["current"], "written_off")) and 220_000 not in set(walk(income.json(), "written_off"))
    rec = owner(world).get(f"{API}/org/payments/receipts", params={**params, "group_by": "month"})
    assert rec.status_code == 200, rec.text
    assert rec.json()["totals"]["written_off"] == 94_000
    drill = owner(world).get(f"{API}/org/payments/receipts/items", params={**params, "column": "written_off"})
    assert drill.status_code == 200, drill.text
    assert [(i["amount"], i["column_amount"]) for i in drill.json()["items"]] == [(220_000, 94_000)]      # the whole record and the part in the column
    dec = owner(world).get(f"{API}/org/payments/decisions", params=params)
    assert dec.status_code == 200, dec.text
    (line,) = [r for r in dec.json()["rows"] if r["kind"] == AUDIT_WRITEOFF]
    assert (line["amount"], line["used"], line["used_text"]) == (220_000, 94_000, "использовано " + tx.money(94_000))      # the journal keeps the sum that was entered
    # А7.4: an administrator WITH the right to the money (the owner switched it on) sees the same words and numbers as the owner; the route itself is closed to anyone without that right
    from tests.pg_world import make_client
    from tests.test_pay2_api_pg import settings_block

    settings_block(world, admin_can_manage=True)
    admin = make_client(world, user=world.admin).get(f"{API}/org/payments/decisions", params=params)
    assert admin.status_code == 200, admin.text
    (seen,) = [r for r in admin.json()["rows"] if r["kind"] == AUDIT_WRITEOFF]
    assert (seen["amount"], seen["used"], seen["used_text"]) == (220_000, 94_000, "использовано " + tx.money(94_000))
    teacher = make_client(world, user=world.teacher).get(f"{API}/org/payments/decisions", params=params)
    assert teacher.status_code == 403, teacher.text                          # no right to the money: no words and no numbers at all


def test_the_excel_of_the_statement_shows_the_used_sum_in_the_forgiven_column(world, fixed_today):
    g, s, made = c2(world, fixed_today)
    r = owner(world).get(f"{API}/org/memberships/{membership(world, s, g).id}/statement/export", params={"format": "xlsx"})
    assert r.status_code == 200, r.text
    ws = load_workbook(io.BytesIO(r.content)).active
    cells = [c.value for row in ws.iter_rows() for c in row if c.value is not None]
    assert 94_000 in cells and 126_000 not in cells and not any(v == 220_000 for v in cells)       # the entered sum is only in the words of the title, never in a sum column


def test_the_refusal_of_a_refund_says_there_is_no_paid_money_over_the_charge(world, fixed_today):
    g, s, made = c2(world, fixed_today)
    r = refund(world, s, g, 1, expect=422)
    assert r.json()["detail"]["message"] == "Возвращать нечего: оплаченных денег сверх начисленного нет" == tx.NOTHING_TO_REFUND
    assert r.json()["detail"]["code"] == "REFUND_EXCEEDS_ADVANCE"


def test_the_night_reconciliation_has_no_finding_for_an_unused_forgiveness(world, fixed_today):
    from src.auth import money_reconcile as mr

    g, s, made = c2(world, fixed_today)
    result = mr.run(world.db, apply=True, only=[world.org.id])
    assert result["failed"] == []                                      # LESSONS 133: the whole line of the result, not only the findings
    assert [i for i in result["items"] if i["org"] == str(world.org.id) and i["invariant"] == "I-1"] == []
    L = account(world, membership(world, s, g).id, D(2026, 11, 21))
    assert (L.written_off, L.written_off_granted) == (94_000, 220_000)
