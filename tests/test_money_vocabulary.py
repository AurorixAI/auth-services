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
    "спишется занятие": "из пакета спишется 1 урок",
    "возвращено занятие": "в пакет возвращён 1 урок",
    "вернётся занятие": "в пакет вернётся 1 урок",
    "в следующей оплате": "долг уменьшится сразу",
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
class Plain:
    """``payments_texts`` as the words of a sentence are read: the sums with plain spaces (the non-breaking ones are pinned by ``test_the_sentences_built_from_big_sums...`` below)."""

    def __init__(self, module):
        self._module = module

    def __getattr__(self, name):
        target = getattr(self._module, name)
        if not callable(target):
            return target
        return lambda *a, **k: (lambda out: out.replace("\u00a0", " ") if isinstance(out, str) else out)(target(*a, **k))


def test_the_sentences_of_a_payment_and_of_a_payout_use_the_words_of_the_dictionary():
    from datetime import date

    from src.auth import payments_texts

    tx = Plain(payments_texts)

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


def test_the_refusals_that_the_front_shows_verbatim_speak_the_dictionary():
    """The ones the front found outside the dictionary (05.11): the pause in a closed month, the labels of an additional lesson, the texts of the open month and of the change after the save."""
    from src.auth import logica_errors, one_off_purpose, payments_texts as tx
    from src.auth.ops_rules import ERROR_CATALOGUE

    pause = next(message for _status, code, message in logica_errors.PAUSE if code == "PAUSE_LOCKED")
    assert pause.startswith("Месяц закрыт:") and "доступом к деньгам" in pause and "ведомост" not in pause
    assert ERROR_CATALOGUE["PERIOD_CLOSED"][1].startswith("Месяц закрыт (зарплата сохранена)") and "доступом к деньгам" in ERROR_CATALOGUE["PERIOD_CLOSED"][1]
    manager = [one_off_purpose.money_label(m, manager=True, held=True) for m in ("none", "paid", "wait")]
    assert manager == ["не входит в зарплату", "входит в зарплату", "ждёт решения"]
    assert [one_off_purpose.money_label("wait", manager=True, held=False, money_right=r) for r in (True, False)] == ["решите после урока", "решит владелец"]
    assert [one_off_purpose.money_label(m, manager=False, held=True) for m in ("none", "paid", "wait")] == ["не входит в зарплату", "включён в зарплату", "ждёт решения"]
    assert "переплачено" not in tx.open_month_payout_text(state="overpaid", accrued=1, paid=2, balance=-1).lower()
    assert "пересохран" not in tx.payout_change_text(5).lower() and "сохраните новую сумму" in tx.payout_change_text(5).lower()


# ================================================================================================ a sum never wraps: non-breaking spaces (U+00A0) between the groups and before «сум»
import re  # noqa: E402

BROKEN_SUM = re.compile(r"(?<=\d) (?=\d{3}(?!\d))|(?<=\d) (?=сум)")        # a digit, a plain space, three digits | a digit, a plain space, «сум»
# the texts of these modules go into Excel files only (a number in a file is a number: ``export_safety.plain``)
FILE_ONLY = {"payroll_export.py", "openings_excel.py", "import_service.py", "openings_service.py"}


@pytest.mark.parametrize("path", [p for p in sorted(SRC.glob("*.py")) if p.name not in FILE_ONLY], ids=lambda p: p.name)
def test_no_text_of_the_server_writes_a_sum_with_a_plain_space(path):
    """The sums are written by ONE formatter (``payments_texts.num`` / ``money``); a literal «360 000» or a piece «{x} сум» of an f-string with a plain space is a sum that can wrap on a phone («360 / 000 сум»)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstring_nodes(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip and BROKEN_SUM.search(node.value):
            found.append(f"{path.name}:{node.lineno}: {node.value[:90]!r}")
        if isinstance(node, ast.JoinedStr):
            for before, piece in zip(node.values, node.values[1:]):
                if isinstance(before, ast.FormattedValue) and isinstance(piece, ast.Constant) and isinstance(piece.value, str) and piece.value.startswith(" сум"):
                    found.append(f"{path.name}:{node.lineno}: a formatted value and a plain space before «сум»")
    assert not found, "\n".join(found)


def _texts_of(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in ("description", "summary"):          # the documentation of a route of a contract file (OpenAPI), not a text of an answer
                continue
            yield from _texts_of(item)
    elif isinstance(value, list):
        for item in value:
            yield from _texts_of(item)


def test_no_golden_answer_of_the_api_has_a_sum_with_a_plain_space():
    """Every text of the golden files (what the front is built against) carries its sums with non-breaking spaces."""
    import json

    fixtures = Path(__file__).resolve().parent / "fixtures"
    files = sorted(fixtures.rglob("*.json"))
    assert len(files) >= 100, len(files)
    found = []
    for path in files:
        for text in _texts_of(json.loads(path.read_text(encoding="utf-8"))):
            if BROKEN_SUM.search(text):
                found.append(f"{path.relative_to(fixtures)}: {text[:80]!r}")
    assert not found, "\n".join(found[:20]) + (f"\n... {len(found)} in all" if len(found) > 20 else "")


def test_the_sentences_built_from_big_sums_never_break_inside_a_sum_and_a_file_has_a_plain_space():
    from datetime import date

    from src.auth import export_safety, money_effects as effects, payments_texts as tx, xlsx_style

    big = 1_234_567_890
    texts = [
        tx.money(big), tx.num(big), tx.payout_text(state="overpaid", saved_total=1, fresh_total=None, target=1, paid=big, balance=-big),
        tx.payout_text(state="due", saved_total=1, fresh_total=None, target=big, paid=1_000, balance=big - 1_000),
        tx.open_month_payout_text(state="overpaid", accrued=big, paid=big * 2, balance=-big), tx.payout_change_text(-big),
        tx.summary_text(past=big, current=big, advance=big, past_debt_before=big, debt_after=big, paid_until_after=date(2026, 10, 15)),
        tx.transfer_balance_text(carried=big, charged_now=big, advance=big, debt=big, charged_tomorrow=big, advance_tomorrow=big, group_name="Г"),
        tx.price_change_warning(date(2026, 9, 1), date(2026, 9, 30), big, big * 2), tx.collection_text(big, big * 2, 50), tx.balance_text(-big), tx.recalc_text(big, date(2026, 10, 21)),
        effects.text_of([], [{"month": "2026-10", "state": "closed", "teacherName": "Э", "changedBy": big, "driftAfter": big * 2}], done=True),
        effects.deduction_text([{"amount": big}], done=False), effects.recalc_text([{"amount": big}], done=False),
    ]
    assert all(BROKEN_SUM.search(text) is None for text in texts), [t for t in texts if BROKEN_SUM.search(t)]
    assert all(tx.NBSP in text for text in texts if "сум" in text)
    # a file: a number is a number (the plain space), a screen: the non-breaking one
    assert xlsx_style.clean(tx.money(big)) == "1 234 567 890 сум" and export_safety.csv_safe_row([tx.money(big), 5]) == ["1 234 567 890 сум", 5]
