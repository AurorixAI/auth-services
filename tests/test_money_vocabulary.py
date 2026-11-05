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
