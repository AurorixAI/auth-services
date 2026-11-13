"""Money-1 Г-17b: the sentence under the till of a day names a cancelled payment as «отменено», never as «возвращено»; the zero parts are not shown."""
import pytest

from src.auth import payments_texts as tx

N = " "


@pytest.mark.parametrize("received, refunded, cancelled, text", [
    (250_000, 0, 0, ""),                                                                                     # money only came in: nothing to explain
    (250_000, 100_000, 0, f" (принято 250{N}000{N}сум, возвращено 100{N}000{N}сум)"),
    (250_000, 0, 300_000, f" (принято 250{N}000{N}сум, отменено 300{N}000{N}сум)"),
    (250_000, 100_000, 300_000, f" (принято 250{N}000{N}сум, возвращено 100{N}000{N}сум, отменено 300{N}000{N}сум)"),
    (0, 0, 300_000, f" (отменено 300{N}000{N}сум)"),
])
def test_the_parts_of_the_till(received, refunded, cancelled, text):
    assert tx.cash_parts_text(received, refunded, cancelled) == text
