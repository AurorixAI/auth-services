"""«Деньги-1 / А»: the server speaks the one dictionary of money (LESSONS 109, ``artifacts/analysis/MONEY1_A_UX_PACK.md`` §1).

A text that reaches a screen, an Excel file, a receipt or a letter never says «закрыт ведомостью», «пересохраните», «с правом оплат», «вычет» (of a student), «Требует разбора»,
«Закрыла долг», «Переплачено», «Баланс» ...  The test reads every string literal of the server (not the docstrings: those are for developers and may name the old words) and fails on a banned
phrase, so a new text cannot bring the old words back; the texts that are built from data (a month, a teacher, a sum) are checked by the tests of their own modules.
"""
import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "auth"

# the old word -> the word of the dictionary
BANNED = {
    "закрыт ведомостью": "Месяц закрыт",
    "ересохран": "сохраните новую сумму",
    "правом оплат": "администратор с доступом к деньгам",
    "правом на оплат": "администратор с доступом к деньгам",
    "разрешил оплаты": "администратор с доступом к деньгам",
    "разрешил деньги": "администратор с доступом к деньгам",
    "Требует разбора": "Уроки без отметки",
    "на разбор": "вернуть на отметку",
    "незакрыт": "уроки без отметки",
    "неразобран": "уроки без отметки",
    "вычет": "перерасчёт",
    "Закрыла долг": "Погашен долг за ...",
    "Закроет долг": "Погасит долг",
    "оплачен преподавателю": "включён в зарплату",
    "оплатить преподавателю": "включить в зарплату",
    "ереплачено": "Выплачено вперёд / Выплачено больше на",
    "Перенесено из": "Деньги ученика перешли ...",
    "Перенесено в": "Деньги ученика перешли ...",
    "Цена периода": None,                     # only in the warning of the payment window (allowed there, see ALLOWED)
}
# (file, phrase): the places where the phrase is the right word
ALLOWED = {("payments_texts.py", "Цена периода")}


def _docstring_nodes(tree: ast.AST) -> set:
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                skip.add(id(first.value))
    return skip


def _literals(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            yield node.lineno, node.value


@pytest.mark.parametrize("path", sorted(SRC.glob("*.py")), ids=lambda p: p.name)
def test_no_text_of_the_server_uses_a_word_the_dictionary_replaced(path):
    found = []
    for line, text in _literals(path):
        for word, instead in BANNED.items():
            if word in text and (path.name, word) not in ALLOWED:
                found.append(f"{path.name}:{line}: «{word}» -> {instead}: {text[:90]!r}")
    assert not found, "\n".join(found)


# ---------------------------------------------------------------------------------------------------------------- the sentences built from data
def test_the_sentences_of_a_payment_and_of_a_payout_use_the_words_of_the_dictionary():
    from datetime import date

    from src.auth import payments_texts as tx

    start, end = date(2026, 9, 15), date(2026, 10, 14)
    assert tx.allocation_label(bucket="past_debt", closes=True, start=start, end=end) == "Погашен долг за 15.09–14.10"
    assert tx.allocation_label(bucket="past_debt", closes=False, start=start, end=end) == "Частично погашен долг за 15.09–14.10"
    assert tx.allocation_label(bucket="past_debt", closes=True, start=start, end=end, package_no=2) == "Погашен долг за пакет №2 (15.09–14.10)"
    assert tx.opening_label(date(2026, 9, 30), True) == "Погашен долг на начало учёта на 30.09.2026"
    assert tx.summary_text(past=269_000, current=31_000, advance=0, past_debt_before=269_000, debt_after=469_000, paid_until_after=date(2026, 10, 15)) == (
        "Погасит долг 269 000 сум · на текущий период 31 000 сум → оплачено до 15.10 · останется 469 000 сум")
    # a closed month has been paid MORE, an open one AHEAD (never «переплачено»); the next step is said in words
    assert tx.payout_text(state="overpaid", saved_total=83_334, fresh_total=None, target=83_334, paid=100_000, balance=-16_666) == "Выплачено больше на 16 666 сум — вычтем из следующей выплаты"
    open_text = tx.open_month_payout_text(state="overpaid", accrued=75_834, paid=150_000, balance=-74_166)
    assert "Выплачено вперёд 74 166 сум" in open_text and "переплачено" not in open_text and "уменьшит следующую выплату" in open_text
    assert tx.payout_text(state="settled", saved_total=1, fresh_total=None, target=0, paid=0, balance=0, carry_used=30_000) == "Закрыто выплатой прошлого месяца: 30 000 сум"
    assert tx.payout_change_text(-45_000) == "После сохранения зарплата изменилась на −45 000 сум. Сохраните новую сумму, чтобы учесть изменение"
    assert tx.transfer_balance_text(carried=640_000, charged_now=280_000, advance=360_000, debt=0, charged_tomorrow=0, advance_tomorrow=0, group_name="Геометрия 8") == (
        "Деньги ученика — 640 000 сум — переходят в группу «Геометрия 8»: сразу 280 000 сум пойдут на остаток месяца; аванс останется 360 000 сум")


def test_the_excel_of_a_sheet_says_ahead_for_an_open_month_and_more_for_a_closed_one():
    from src.auth import payroll_export as px

    def payout(balance, basis):
        return {"balance": balance, "target": 100_000, "carry_used": 0, "paid": 100_000 - balance, "basis": basis}

    assert px._balance(payout(-20_000, "fresh"), 100_000)[3] == "Выплачено вперёд"
    assert px._balance(payout(-20_000, "saved"), 100_000)[3] == "Выплачено больше на"
    assert px._balance(payout(20_000, "saved"), 100_000)[3] == "К выплате"
    assert px.advance_lines({"balance": -20_000, "carry_used": 0, "advance_out": 20_000, "basis": "saved"}) == ["Вычтем из следующей выплаты: 20 000 сум"]
