"""ALGO - an amount in Russian words (for the payment receipt, N13 P2.2).  Pure, no database.

``amount_words(500000)`` -> "Пятьсот тысяч сум".  The currency word "сум" does not decline (as in
the whole interface and in local business practice); the sum itself is spelled out with the usual
Russian agreement: "одна тысяча", "две тысячи", "пять тысяч", "двадцать один миллион".
"""
from __future__ import annotations

MAX_AMOUNT = 999_999_999_999

_HUNDREDS = (
    "", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот",
)
_TENS = (
    "", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто",
)
_TEENS = (
    "десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
    "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать",
)
_ONES_MASCULINE = ("", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять")
_ONES_FEMININE = ("", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять")

# (words of the scale for 1 / 2-4 / 5+, feminine?)
_SCALES = (
    (None, False),  # units: masculine ("один сум")
    (("тысяча", "тысячи", "тысяч"), True),
    (("миллион", "миллиона", "миллионов"), False),
    (("миллиард", "миллиарда", "миллиардов"), False),
)


def _plural(number: int, forms: tuple[str, str, str]) -> str:
    last, last2 = number % 10, number % 100
    if last == 1 and last2 != 11:
        return forms[0]
    if 2 <= last <= 4 and not 12 <= last2 <= 14:
        return forms[1]
    return forms[2]


def _triplet(number: int, feminine: bool) -> list[str]:
    """Words of a number 1..999."""
    words = [_HUNDREDS[number // 100]]
    rest = number % 100
    if 10 <= rest <= 19:
        words.append(_TEENS[rest - 10])
    else:
        words.append(_TENS[rest // 10])
        words.append((_ONES_FEMININE if feminine else _ONES_MASCULINE)[rest % 10])
    return [word for word in words if word]


def number_words(amount: int) -> str:
    """The integer ``0 .. 999 999 999 999`` in lower-case Russian words."""
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise TypeError("amount must be an int")
    if amount < 0 or amount > MAX_AMOUNT:
        raise ValueError(f"amount must be 0..{MAX_AMOUNT}")
    if amount == 0:
        return "ноль"
    words: list[str] = []
    groups = []
    while amount:
        groups.append(amount % 1000)
        amount //= 1000
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        if group == 0:
            continue
        forms, feminine = _SCALES[index]
        words.extend(_triplet(group, feminine))
        if forms is not None:
            words.append(_plural(group, forms))
    return " ".join(words)


def amount_words(amount: int, currency_word: str = "сум") -> str:
    """"Пятьсот тысяч сум": the sum in words, first letter capital, the currency word unchanged."""
    text = f"{number_words(amount)} {currency_word}"
    return text[0].upper() + text[1:]
