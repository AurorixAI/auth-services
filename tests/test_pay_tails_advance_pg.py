"""PAY-TAILS Б1.1 / Б1.2 / Б5.5 (step 2): the tab «Аванс» and the closed memberships of a group.  ЛП 420 000 for 30 days, today 21.11.
Students: Альфа paid 420 000 and left on the 10th day (charged 140 000, advance 280 000); Гамма paid 420 000 and left on the 15th (charged 210 000, advance 210 000); Бета paid two periods (840 000, advance 420 000, open,
paid until 31.12); Дельта left on the 12th with a debt (charged 168 000, nothing paid); Эпсилон paid 420 000 and left on the 30th day (charged 420 000: settled, no advance)."""
from datetime import date

from tests.money1b_helpers import add, fresh_student
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixtures)
from tests.test_pay_tails_golden_pg import get
from tests.test_pay_tails_money_pg import leave, lp, pay, refund
from src.auth import payments_texts as tx

pytestmark = needs_pg
D = date
M = tx.money


def build(w, fixed_today):
    g, first = lp(w, fixed_today)                                         # «Ученик ЛП» = Альфа
    names = {n: fresh_student(w, n) for n in ("Бета", "Гамма", "Дельта", "Эпсилон")}
    for s in names.values():
        add(w, s, group=g, mode="add")
    names["Альфа"] = first
    for n in ("Альфа", "Гамма", "Эпсилон"):
        pay(w, names[n], g, amount=420_000, paid_on=D(2026, 11, 1))
    pay(w, names["Бета"], g, amount=840_000, paid_on=D(2026, 11, 1))
    fixed_today(D(2026, 11, 10))
    leave(w, g, names["Альфа"])
    fixed_today(D(2026, 11, 12))
    leave(w, g, names["Дельта"])
    fixed_today(D(2026, 11, 15))
    leave(w, g, names["Гамма"])
    fixed_today(D(2026, 11, 30))
    leave(w, g, names["Эпсилон"])
    fixed_today(D(2026, 11, 21))
    return g, names


def tab(w, **params):
    return get(w, "/org/payments/status", bucket="advance", limit=100, **params)


def test_the_tab_lists_every_membership_with_an_advance_closed_first_and_the_sums_agree(world, fixed_today):
    g, n = build(world, fixed_today)
    page = tab(world)
    # closed first, the freshest leaving on top (Гамма 15.11, Альфа 10.11), then the open one (Бета): 210 000 + 280 000 + 420 000 = 910 000
    assert [r["student_id"] for r in page["items"]] == [str(n[x].id) for x in ("Гамма", "Альфа", "Бета")]
    assert [(r["tab_text"], r["tab_sub"]) for r in page["items"]] == [("Аванс " + M(210_000), "ушёл 15.11"), ("Аванс " + M(280_000), "ушёл 10.11"), ("Аванс " + M(420_000), "оплачено до 31.12")]
    assert (page["total"], page["students"], page["amount_total"], page["left_students"]) == (3, 3, 910_000, 2)
    left = tab(world, left_only="true")
    assert (left["total"], left["amount_total"], left["left_students"]) == (2, 490_000, 2) and all(not r["is_active"] for r in left["items"])
    sm = get(world, "/org/payments/summary")
    assert (sm["advance_total"], sm["students_with_advance"], sm["buckets"]["advance"], sm["bucket_rows"]["advance"], sm["advance_left_students"]) == (910_000, 3, 3, 3, 2)
    assert sm["advance_text"] == "3 ученика · 2 ушли из группы"
    inc = get(world, "/org/payments/income", **{"from": "2026-10-01", "to": "2026-12-31"})
    assert inc["position"]["advance_total"] == 910_000                                        # the list = the card = «Поступления»


def test_the_words_of_a_row_are_the_same_on_every_tab_and_the_tab_has_its_own(world, fixed_today):
    g, n = build(world, fixed_today)
    in_tab = {r["student_id"]: r for r in tab(world)["items"]}
    other = {r["student_id"]: r for r in get(world, "/org/payments/status", bucket="active", limit=100)["items"]}
    beta = str(n["Бета"].id)
    assert in_tab[beta]["state_text"] == other[beta]["state_text"] == "Оплачено до 31.12" and in_tab[beta]["tab_text"] == "Аванс " + M(420_000) and other[beta]["tab_text"] is None
    alpha = in_tab[str(n["Альфа"].id)]
    assert (alpha["state_text"], alpha["state_sub"]) == ("Аванс " + M(280_000), "ушёл 10.11")


def test_a_refund_moves_the_card_and_the_list_together(world, fixed_today):
    g, n = build(world, fixed_today)
    assert get(world, "/org/payments/summary")["advance_total"] == 910_000                    # the summary is read (and may be cached) before the refund
    refund(world, n["Альфа"], g, 280_000, expect=201)
    page, sm = tab(world), get(world, "/org/payments/summary")
    assert page["amount_total"] == sm["advance_total"] == 630_000 and sm["students_with_advance"] == page["students"] == 2 and sm["advance_text"] == "2 ученика · 1 ушёл из группы"
    assert tab(world, search="zzzz")["items"] == []


def test_the_closed_memberships_of_a_group_with_any_settlement(world, fixed_today):
    g, n = build(world, fixed_today)
    page = get(world, "/org/payments/status", bucket="left_open", group_id=str(g.id), limit=100)
    # Дельта: 168 000 debt (12 days x 14 000); the advances: Альфа 280 000, Гамма 210 000 -> debt first, then the advances by size; Эпсилон (settled) and the open Бета are not in the band
    assert [r["student_id"] for r in page["items"]] == [str(n[x].id) for x in ("Дельта", "Альфа", "Гамма")]
    assert (page["total"], page["left_count"]) == (3, 3) and all(not r["is_active"] for r in page["items"])
    head = get(world, "/org/payments/status", bucket="debtors", group_id=str(g.id), limit=1)
    assert (head["amount_total"], head["students"]) == (168_000, 1)                              # the label «Долг» of the group counts the closed membership too: the same set as the band
