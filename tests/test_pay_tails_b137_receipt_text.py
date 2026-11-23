"""PAY-TAILS Б11.24 п. 4 (step 8.13.7): the state of the receipt says the money ahead of a period that has not begun ONCE - by the meaning of the head, not by the two sums being equal.  The words of the receipt are made on the server
(lesson 143); the pure function of the text is checked with the numbers of its inputs worked out by hand."""
from src.auth import payments_texts as tx

COVER = "оплачено до 30.11"
FORWARD = tx.paid_forward_text(98_000)


def test_the_head_that_says_the_advance_does_not_say_the_money_ahead_again_even_when_the_sums_differ():
    # no debt, an advance 98 000 (the head says it) and 70 000 ahead of the next period: the second phrase would be the same money said twice - not said, whatever the numbers
    assert tx.receipt_state_text(0, 98_000, COVER, forward=tx.paid_forward_text(70_000)) == f"аванс {tx.money(98_000)}, {COVER}"
    assert tx.receipt_state_text(0, 98_000, COVER, forward=FORWARD) == f"аванс {tx.money(98_000)}, {COVER}"


def test_a_debt_and_money_ahead_the_forward_phrase_stays_in_its_place():
    # debt 100 000 and an advance 98 000: the head says the debt only, the money ahead is not in it - «вперёд оплачено» stays (it was dropped when the two sums were equal)
    assert tx.receipt_state_text(100_000, 98_000, COVER, forward=FORWARD) == f"не оплачено {tx.money(100_000)}, {COVER} · {FORWARD}"


def test_no_debt_no_advance_the_forward_phrase_stays_and_nothing_ahead_is_not_said():
    assert tx.receipt_state_text(0, 0, COVER, forward=FORWARD) == f"всё оплачено, {COVER} · {FORWARD}"
    assert tx.receipt_state_text(0, 98_000, COVER) == f"аванс {tx.money(98_000)}, {COVER}"
    assert tx.receipt_state_text(0, 0, COVER) == f"всё оплачено, {COVER}"
