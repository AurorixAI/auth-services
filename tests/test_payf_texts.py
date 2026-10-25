"""PAY-F part 4 and the words of parts 1 and 3 (pure: no database): ONE dictionary of the names of the ways to count a payment, the line of a deduction, the forecast."""
from datetime import date

from src.auth import payments_statement_texts as st
from src.auth import payments_texts as tx
from src.auth.payments_schemas import PriceOut, RecalcOut, Segment, SegmentLessons, StatementDetails, StatementRow, StatementSummary

D = date


def test_4_the_names_of_the_ways_to_count_are_one_dictionary():
    assert tx.MODE_NAMES == {
        "payment_date": ("Свой месяц у каждого ученика", "Месяц считается с первого занятия ученика: 05.10 → 04.11"),
        "fixed_day": ("Общий месяц для всех", "У всех один период, например с 1-го по последнее число; первый неполный месяц — по урокам"),
        "lessons": ("Пакет уроков", "Оплата закрывает заданное число проведённых уроков; отменённые уроки не списываются"),
    }
    assert [tx.mode_title(k) for k in ("payment_date", "fixed_day", "lessons")] == ["Свой месяц у каждого ученика", "Общий месяц для всех", "Пакет уроков"]
    assert tx.mode_hint("lessons").endswith("отменённые уроки не списываются")


def test_4_the_statement_names_a_package_from_the_dictionary():
    package = Segment(id="s", kind="package", period_from=D(2026, 9, 25), period_to=D(2026, 10, 21), billable_days=12, price=PriceOut(amount=500_000, source="group"),
                      charge=500_000, basis="lessons", lessons=SegmentLessons(in_part=12, in_period=12))
    title, _ = st.charge_words(package)
    assert title.startswith(tx.mode_title("lessons") + " 25.09–21.10")


def test_3_the_line_of_a_deduction_and_of_the_receipt():
    assert tx.recalc_title(D(2026, 10, 21)) == "Перерасчёт: урок 21.10 отменён центром"
    assert tx.recalc_text(46_000, D(2026, 10, 21)) == "−46 000 сум: урок 21.10 отменён центром"
    assert tx.recalc_text(1_250_000, D(2026, 10, 3)) == "−1 250 000 сум: урок 03.10 отменён центром"


def test_1_the_forecast_words():
    assert tx.forecast_text(D(2026, 10, 30)) == "хватит до ~30.10 (по расписанию)"
    assert tx.forecast_text(None) == "пакет закончился"


def test_3_the_statement_file_knows_the_recalc_row_and_its_tile():
    line = RecalcOut(lesson_date=D(2026, 10, 21), lesson_id="lesson-wed", amount=46_000, text=tx.recalc_text(46_000, D(2026, 10, 21)))
    rows = [StatementRow(seq=1, date=D(2026, 10, 21), type="recalc", title=tx.recalc_title(D(2026, 10, 21)), credit=46_000, balance_after=-554_000,
                         details=StatementDetails(recalc=line))]
    (entry,) = st.build_entries(rows)
    assert (entry.title, entry.sub, entry.paid, entry.charged, entry.balance) == ("Перерасчёт: урок 21.10 отменён центром", line.text, 46_000, None, "Долг 554 000")
    summary = StatementSummary(charged=600_000, paid=0, recalculated=46_000, debt=554_000)
    assert ("Перерасчёт", 46_000) in st.total_lines(summary)
    assert "Перерасчёт" not in [label for label, _ in st.total_lines(StatementSummary(charged=600_000, debt=600_000))]
    assert st.STATEMENT_TYPE_LABEL["recalc"] == "Перерасчёт"
