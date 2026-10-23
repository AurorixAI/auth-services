"""The words of the statement FILE: the Python mirror of the page's rules (``payments-ledger.ts``: chargeWords, periodName,
closedWords, balanceCell, carryLine, openingBalance, buildStatementItems).  Pure: no database."""
from datetime import date

import pytest

from src.auth import payments_statement_texts as t
from src.auth.payments_schemas import (
    Allocation, PriceOut, Segment, SegmentLessons, StatementDetails, StatementMembership, StatementRow, StatementSummary,
)

D = date


def seg(start, end, *, kind="period", days_text=None, basis="full", amount=500_000, source="group", price=500_000,
        lessons=None, charge=None, written_off=0, sid=None, paid=0):
    return Segment(
        id=sid or f"seg:{start}", kind=kind, period_from=start, period_to=end, billable_days=1, days_text=days_text,
        price=PriceOut(amount=price, source=source), charge=amount if charge is None else charge, basis=basis, paid=paid,
        lessons=SegmentLessons(in_part=lessons[0], in_period=lessons[1]) if lessons else None, written_off=written_off)


def alloc(start, end, amount, *, bucket="current", closes=False, after=None, sid=None):
    return Allocation(segment_id=sid or f"seg:{start}", period_from=start, period_to=end, amount=amount, days=0,
                      bucket=bucket, closes=closes, label="x", segment_remaining_after=after)


def row(seq, day, type_, *, title="", debit=0, credit=0, outflow=0, balance=None, **details):
    return StatementRow(seq=seq, date=day, type=type_, title=title, debit=debit, credit=credit, outflow=outflow,
                        balance_after=balance, details=StatementDetails(**details))


# ---- methods ------------------------------------------------------------------------------------------------------

def test_methods_are_russian_and_an_unknown_one_says_so():
    assert [t.method_label(m) for m in ("cash", "card", "bank_transfer", "click", "payme", "other")] == [
        "Наличные", "Карта", "Перевод", "Click", "Payme", "Другое"]
    assert t.method_label("unknown") == t.method_label(None) == t.method_label("what") == "Способ не указан"
    assert t.lower_method("cash") == "наличные" and t.lower_method(None) == "способ не указан"
    # in the middle of a line the generic names are lowercase, the brands keep their capitals
    assert [t.lower_method(m) for m in ("cash", "card", "bank_transfer", "other", "unknown", "click", "payme")] == [
        "наличные", "карта", "перевод", "другое", "способ не указан", "Click", "Payme"]


# ---- months -------------------------------------------------------------------------------------------------------

def test_month_words():
    assert t.month_title("2026-09") == "Сентябрь 2026" and t.month_word("2026-03") == "март"
    assert t.shift_month_key("2026-01", -1) == "2025-12" and t.shift_month_key("2026-12", 1) == "2027-01"
    assert t.last_day_of("2026-02") == D(2026, 2, 28) and t.last_day_of("2028-02") == D(2028, 2, 29)
    assert t.last_day_of("2026-12") == D(2026, 12, 31)


def test_a_calendar_month_period_starts_on_the_first_or_ends_on_the_last_day():
    assert t.is_calendar_month_period(D(2026, 10, 1), D(2026, 10, 31))
    assert t.is_calendar_month_period(D(2026, 8, 18), D(2026, 8, 31))        # a joiner: ends on the last day
    assert t.is_calendar_month_period(D(2026, 10, 1), D(2026, 10, 14))       # starts on the 1st
    assert not t.is_calendar_month_period(D(2026, 9, 15), D(2026, 10, 14))   # crosses a month
    assert not t.is_calendar_month_period(D(2026, 9, 5), D(2026, 9, 20))


def test_period_label_one_month_a_range_or_all():
    assert t.period_label(D(2026, 10, 1), D(2026, 10, 31)) == "Октябрь 2026"
    assert t.period_label(D(2026, 2, 1), D(2026, 2, 28)) == "Февраль 2026"
    assert t.period_label(D(2026, 10, 1), D(2026, 10, 15)) == "01.10.2026–15.10.2026"
    assert t.period_label(D(2026, 9, 1), D(2026, 10, 31)) == "01.09.2026–31.10.2026"
    assert t.period_label(None, None) == "Все месяцы"
    assert t.period_label(D(2026, 9, 1), None) == "с 01.09.2026" and t.period_label(None, D(2026, 9, 30)) == "по 30.09.2026"
    assert t.single_month(D(2026, 10, 1), D(2026, 10, 31)) == "2026-10" and t.single_month(D(2026, 10, 2), D(2026, 10, 31)) is None


# ---- balance and carry --------------------------------------------------------------------------------------------

def test_balance_cell_words():
    assert t.balance_cell(-92_000) == ("debt", "Долг 92 000")
    assert t.balance_cell(508_000) == ("advance", "Аванс 508 000")
    assert t.balance_cell(0) == ("none", "0")
    assert t.balance_cell(None) == ("none", "")


def test_carry_line_genitive_month_and_the_year_only_when_it_differs():
    assert t.carry_line("2026-10", 508_000) == "С сентября перенесён аванс 508 000"
    assert t.carry_line("2026-10", -92_000) == "С сентября перенесён долг 92 000"
    assert t.carry_line("2026-01", 1_000) == "С декабря 2025 перенесён аванс 1 000"
    assert t.carry_line("2026-03", 0) is None


def test_opening_balance_is_taken_from_the_whole_ledger():
    rows = [row(1, D(2026, 8, 18), "charge", debit=100, balance=-100), row(2, D(2026, 9, 5), "payment", credit=600, balance=500),
            row(3, D(2026, 10, 1), "charge", debit=700, balance=-200)]
    assert t.opening_balance(rows, "2026-10") == 500
    assert t.opening_balance(rows, "2026-09") == -100
    assert t.opening_balance(rows, "2026-08") == 0                           # before the first row
    assert t.opening_balance(rows, "2026-11") == -200


# ---- charge words -------------------------------------------------------------------------------------------------

def test_charge_words_month_partial_month_period_and_package():
    assert t.charge_words(seg(D(2026, 9, 1), D(2026, 9, 30))) == (
        "Сентябрь 2026", "цена 500 000 сум в месяц · цена группы")
    title, sub = t.charge_words(seg(D(2026, 8, 18), D(2026, 8, 31), days_text="14 дней из 31", basis="days", source="override"))
    assert title == "Август 2026 — неполный месяц (14 дней из 31)"
    assert sub == "цена 500 000 сум в месяц · индивидуальная цена"
    title, _ = t.charge_words(seg(D(2026, 8, 28), D(2026, 8, 31), days_text="с 28.08 (2 урока из 13)", basis="lessons"))
    assert title == "Август 2026 — неполный месяц (с 28.08, 2 урока из 13)"
    title, _ = t.charge_words(seg(D(2026, 9, 15), D(2026, 10, 14)))
    assert title == "Период 15.09–14.10"
    title, sub = t.charge_words(seg(D(2026, 9, 25), D(2026, 10, 21), kind="package", lessons=(12, 12)))
    assert title == "Пакет уроков 25.09–21.10 (12 из 12 уроков)"
    assert sub == "цена 500 000 сум за пакет · цена группы"
    title, _ = t.charge_words(seg(D(2026, 9, 25), D(2026, 10, 21), kind="package", lessons=(1, 21)))
    assert title.endswith("(1 из 21 урок)")                                   # the page's own plural
    assert t.charge_words(seg(D(2026, 9, 1), D(2026, 9, 30), price=None))[1] == "цена не задана"
    assert t.charge_words(seg(D(2026, 9, 1), D(2026, 9, 30), price=0))[1] == "бесплатно"
    assert t.part_words("с 16.10 (7 уроков из 13)") == "с 16.10, 7 уроков из 13" and t.part_words("11 дней из 31") == "11 дней из 31"


def test_period_name_and_closed_words():
    assert t.period_name(D(2026, 8, 1), D(2026, 8, 31)) == "август"
    assert t.period_name(D(2026, 9, 15), D(2026, 10, 14)) == "15.09–14.10"
    assert t.period_name(D(2026, 9, 25), D(2026, 10, 21), "package") == "пакет 25.09–21.10"
    month = alloc(D(2026, 8, 1), D(2026, 8, 31), 1)
    other = alloc(D(2026, 9, 15), D(2026, 10, 14), 1)
    assert t.closed_words("август", "period", month) == "август закрыт"
    assert t.closed_words("15.09–14.10", "period", other) == "период закрыт"
    assert t.closed_words("пакет", "package", other) == "пакет закрыт"


# ---- entries ------------------------------------------------------------------------------------------------------

def entries_of(rows, upcoming=()):
    return t.build_entries(rows, upcoming)


def test_a_payment_reads_like_the_page_with_allocation_lines_in_the_details():
    aug, sep, oct_ = seg(D(2026, 8, 1), D(2026, 8, 31)), seg(D(2026, 9, 1), D(2026, 9, 30)), seg(D(2026, 10, 1), D(2026, 10, 31))
    rows = [
        row(1, D(2026, 8, 1), "charge", debit=500_000, balance=-500_000, segment=aug),
        row(2, D(2026, 9, 1), "charge", debit=500_000, balance=-1_000_000, segment=sep),
        row(3, D(2026, 9, 5), "payment", credit=700_000, balance=-300_000, receipt_no=7, method="cash", created_by_name="Айбек О.",
            note="за двоих", allocations=[
                alloc(D(2026, 8, 1), D(2026, 8, 31), 474_000, bucket="past_debt", closes=True, after=0),
                alloc(D(2026, 9, 1), D(2026, 9, 30), 226_000, after=274_000),
                alloc(D(2026, 10, 1), D(2026, 10, 31), 0, bucket="advance")]),
        row(4, D(2026, 9, 6), "payment", credit=100_000, balance=-200_000, receipt_no=8, method="card",
            allocations=[alloc(D(2026, 10, 1), D(2026, 10, 31), 100_000, bucket="advance"),
                         alloc(D(2026, 9, 1), D(2026, 9, 30), 50_000, sid="seg:2026-09-01")], replaces_amount=90_000),
    ]
    first, second = entries_of(rows)[2:]
    assert first.title == "Оплата № 7 · наличные · внёс Айбек О."
    assert first.paid == 700_000 and first.charged is None and first.balance == "Долг 300 000"
    assert first.allocations == ["за август — 474 000 (август закрыт)", "за сентябрь — 226 000 (осталось 274 000)"]
    assert first.details == "комментарий: за двоих\nза август — 474 000 (август закрыт)\nза сентябрь — 226 000 (осталось 274 000)"
    assert second.title == "Оплата № 8 · карта"
    assert second.allocations == ["аванс на октябрь — 100 000", "за сентябрь — 50 000 (осталось 224 000)"]   # 500 000 - 226 000 - 50 000
    assert second.replaced == "исправлена: было 90 000" and second.details.splitlines()[0] == "исправлена: было 90 000"


def test_the_remainder_is_recomputed_only_without_the_servers_figure_and_a_writeoff_counts_after_it():
    sep = seg(D(2026, 9, 1), D(2026, 9, 30), written_off=100_000)
    rows = [row(1, D(2026, 9, 1), "charge", debit=500_000, balance=-500_000, segment=sep),
            row(2, D(2026, 9, 2), "payment", credit=100_000, balance=-400_000, receipt_no=1, method="cash",
                allocations=[alloc(D(2026, 9, 1), D(2026, 9, 30), 100_000)]),
            row(3, D(2026, 9, 3), "writeoff", credit=100_000, balance=-300_000, reason="болел"),
            row(4, D(2026, 9, 4), "payment", credit=300_000, balance=0, receipt_no=2, method="cash",
                allocations=[alloc(D(2026, 9, 1), D(2026, 9, 30), 300_000, closes=True)])]
    entries = entries_of(rows)
    assert entries[1].allocations == ["за сентябрь — 100 000 (осталось 400 000)"]        # the write-off is not here yet
    assert entries[3].allocations == ["за сентябрь — 300 000 (сентябрь закрыт)"]


def test_lines_of_a_package_an_advance_for_lessons_and_a_closed_period_by_the_servers_zero():
    pack = seg(D(2026, 9, 25), D(2026, 10, 21), kind="package", lessons=(12, 12), sid="seg:p1")
    rows = [row(1, D(2026, 9, 25), "charge", debit=500_000, balance=-500_000, segment=pack),
            row(2, D(2026, 9, 26), "payment", credit=600_000, balance=100_000, receipt_no=3, method="payme", allocations=[
                alloc(D(2026, 9, 25), D(2026, 10, 21), 500_000, sid="seg:p1", after=0),
                Allocation(segment_id="advance", period_from=D(2026, 10, 22), period_to=D(2026, 10, 22), amount=100_000, days=0,
                           bucket="advance", closes=False, label="x")])]
    (_, payment) = entries_of(rows)
    assert payment.allocations == ["за пакет 25.09–21.10 — 500 000 (пакет закрыт)", "аванс на следующие уроки — 100 000"]


def test_writeoff_refund_transfers_opening_pause_and_a_charge_without_a_segment():
    rows = [
        row(1, D(2026, 9, 1), "opening_paid_until", title="На начало учёта долга нет (оплачено до 31.08)", balance=0),
        row(2, D(2026, 9, 2), "opening_debt", title="Долг на начало учёта на 01.09.2026", debit=40_000, balance=-40_000),
        row(3, D(2026, 9, 3), "pause", title="Пауза 03.09–10.09 — стоимость не считается", balance=-40_000),
        row(4, D(2026, 9, 4), "writeoff", credit=40_000, balance=0, reason="болел", created_by_name="Директор"),
        row(5, D(2026, 9, 5), "writeoff", credit=1, balance=1),
        row(6, D(2026, 9, 6), "refund", outflow=30_000, balance=-29_999, method="card", reason="переехал", created_by_name="Айбек О."),
        row(7, D(2026, 9, 7), "refund", outflow=1, balance=-30_000, method=None),
        row(8, D(2026, 9, 8), "transfer_in", title="Перенос из «Группа A»", credit=70_000, balance=40_000),
        row(9, D(2026, 9, 9), "transfer_out", title="Перенос в «Группа B»", outflow=10_000, balance=30_000),
        row(10, D(2026, 9, 10), "charge", title="Стоимость 10.09–30.09", debit=5_000, balance=25_000),
    ]
    got = {e.seq: e for e in entries_of(rows)}
    assert got[1].title == "На начало учёта долга нет (оплачено до 31.08)" and got[1].paid is None and got[1].charged is None
    assert (got[2].charged, got[2].sub) == (40_000, "до запуска в ALGO")
    assert got[3].title.startswith("Пауза") and got[3].charged is None
    assert got[4].title == "Долг прощён — причина: болел · Директор" and got[4].paid == 40_000
    assert got[5].title == "Долг прощён"
    assert got[6].title == "Возврат аванса — карта · Айбек О." and got[6].paid == -30_000 and got[6].sub == "причина: переехал"
    assert got[7].title == "Возврат аванса — способ не указан"
    assert (got[8].title, got[8].paid) == ("Перенос из «Группа A»", 70_000)
    assert (got[9].title, got[9].paid) == ("Перенос в «Группа B»", -10_000)
    assert (got[10].title, got[10].sub, got[10].charged) == ("Стоимость 10.09–30.09", None, 5_000)
    assert got[1].balance == "0" and got[2].balance == "Долг 40 000" and got[8].balance == "Аванс 40 000"


def test_group_by_month_bands_in_order_and_the_carry_only_for_one_month():
    rows = [row(1, D(2026, 8, 18), "charge", title="a", debit=100, balance=-100), row(2, D(2026, 9, 5), "payment", credit=600, balance=500),
            row(3, D(2026, 10, 1), "charge", title="c", debit=700, balance=-200)]
    entries = entries_of(rows)
    allm = t.group_by_month(entries, rows, None)
    assert [(type(i).__name__, getattr(i, "title", None)) for i in allm] == [
        ("Band", "Август 2026"), ("Entry", "a"), ("Band", "Сентябрь 2026"), ("Entry", "Оплата · способ не указан"), ("Band", "Октябрь 2026"), ("Entry", "c")]
    one = t.group_by_month([e for e in entries if e.date.month == 10], rows, "2026-10")
    assert [type(i).__name__ for i in one] == ["Band", "Carry", "Entry"] and one[1].text == "С сентября перенесён аванс 500"
    empty = t.group_by_month([], rows, "2026-12")
    assert [type(i).__name__ for i in empty] == ["Band", "Carry", "Quiet"] and empty[1].text == "С ноября перенесён долг 200"
    assert empty[2].text == "Записей нет"
    first = t.group_by_month([e for e in entries if e.date.month == 8], rows, "2026-08")
    assert [type(i).__name__ for i in first] == ["Band", "Entry"]               # nothing before the first month


def test_cancel_note_and_totals():
    r = StatementRow(seq=1, date=D(2026, 9, 1), type="payment", title="x", voided_at="x", void_reason="ошибка",
                     voided_by_name="Директор", details=StatementDetails(replaced_by_receipt_no=9))
    assert t.cancel_note(r) == "Отменено: ошибка · Директор · заменена оплатой № 9"
    bare = StatementRow(seq=1, date=D(2026, 9, 1), type="payment", title="x", voided_at="x")
    assert t.cancel_note(bare) == "Отменено: причина не указана"
    s = StatementSummary(charged=1_200_000, paid=1_100_000, written_off=100_000, refunded=0, debt=0, advance=0)
    assert t.total_lines(s) == [("Стоимость обучения", 1_200_000), ("Оплачено", 1_100_000), ("Прощено", 100_000), ("Баланс", "0")]
    s = StatementSummary(charged=10, paid=100, transferred_in=5, refunded=7, transferred_out=3, advance=92)
    assert [x[0] for x in t.total_lines(s)] == ["Стоимость обучения", "Оплачено", "Перенесено из групп", "Возвращено",
                                                 "Перенесено в группы", "Баланс"]
    assert t.total_lines(StatementSummary(debt=92_000))[-1] == ("Баланс", "Долг 92 000")
    assert t.total_lines(StatementSummary(advance=508_000))[-1] == ("Баланс", "Аванс 508 000")


def test_title_lines_and_the_periods_to_come():
    m = StatementMembership(membership_id="m", student_id="s", student_name="Иванов Иван", display_id="A-1", group_id="g",
                            group_name="Математика 9", teacher_name="Петрова Анна")
    assert t.title_line(m) == "Выписка — Иванов Иван · ID A-1 · Математика 9 · Петрова Анна"
    m.display_id, m.teacher_name = None, None
    assert t.title_line(m) == "Выписка — Иванов Иван · Математика 9"
    assert t.asof_line(D(2026, 10, 21), D(2026, 8, 1), None, None) == "По состоянию на 21.10.2026 · Учёт ведётся с 01.08.2026 · Период: Все месяцы"
    assert t.asof_line(D(2026, 10, 21), D(2026, 8, 1), D(2026, 10, 1), D(2026, 10, 31)).endswith("Период: Октябрь 2026")
    s = StatementSummary(charged=1_103_000, paid=1_153_000, written_off=50_000, refunded=40_000, advance=60_000)
    assert t.totals_line(s) == "Стоимость обучения 1 103 000 · Оплачено 1 153 000 · Прощено 50 000 · Возвращено 40 000 · Баланс: Аванс 60 000"
    assert t.totals_line(StatementSummary(charged=5, paid=5)) == "Стоимость обучения 5 · Оплачено 5 · Баланс: 0"
    nov = seg(D(2026, 11, 1), D(2026, 11, 30), charge=500_000)
    assert t.upcoming_line(nov) == "01.11–30.11: к оплате 500 000 сум"
    nov.paid = 274_000
    assert t.upcoming_line(nov) == "01.11–30.11: оплачено 274 000 сум из 500 000 сум"
    pack = seg(D(2026, 9, 25), D(2026, 10, 21), kind="package", charge=600_000, lessons=(12, 12))
    assert t.upcoming_line(pack) == "Пакет 25.09–21.10: к оплате 600 000 сум"
