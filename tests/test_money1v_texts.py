"""Money-1 pack V: the words of the new answers (pure, no database).  Every sum has a non-breaking space between the number and «сум» (LESSONS 112)."""
from src.auth import payments_texts as tx

NB = " "


def test_v1_the_stale_sum_says_was_and_now_in_the_words_of_the_dictionary():
    assert tx.stale_text("debt", 100_000, 70_000) == f"Долг изменился: было 100{NB}000{NB}сум, сейчас 70{NB}000{NB}сум"
    assert tx.stale_text("refundable", 344_000, 244_000) == f"Сумма к возврату изменилась: было 344{NB}000{NB}сум, сейчас 244{NB}000{NB}сум"
    assert tx.stale_text("advance", 0, 5_000).startswith("Аванс изменился: было 0")
