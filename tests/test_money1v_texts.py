"""Money-1 pack V: the words of the new answers (pure, no database).  Every sum has a non-breaking space between the number and «сум» (LESSONS 112)."""
from src.auth import payments_texts as tx

NB = " "


def test_v1_the_stale_sum_says_was_and_now_in_the_words_of_the_dictionary():
    assert tx.stale_text("debt", 100_000, 70_000) == f"Долг изменился: было 100{NB}000{NB}сум, сейчас 70{NB}000{NB}сум"
    assert tx.stale_text("refundable", 344_000, 244_000) == f"Сумма к возврату изменилась: было 344{NB}000{NB}сум, сейчас 244{NB}000{NB}сум"
    assert tx.stale_text("advance", 0, 5_000).startswith("Аванс изменился: было 0")


def test_v3_the_borders_of_a_sensible_price_and_its_question():
    from src.auth import pricing

    for old, new, unusual in ((500_000, 1_000_000, False), (500_000, 1_000_001, True), (500_000, 250_000, False), (500_000, 249_999, True),
                              (500_000, 0, True), (500_000, 500_000, False), (None, 5_000_000, False), (0, 400_000, False)):
        assert pricing.unusual_change(old, new) is unusual, (old, new)
    assert pricing.unusual_warnings(300_000, 3_000_000) == [{"code": "price_unusual", "text": f"Вы уверены: 3{NB}000{NB}000{NB}сум? Сейчас 300{NB}000{NB}сум"}]
    assert pricing.unusual_warnings(300_000, 310_000) == []
