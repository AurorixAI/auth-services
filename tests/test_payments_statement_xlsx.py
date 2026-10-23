"""The statement Excel file reads like the page: a golden file built from a hand-made statement (no database), cell by cell."""
import io
from datetime import date

import pytest
from openpyxl import load_workbook

from src.auth.payments_schemas import (
    Allocation, PriceOut, Segment, StatementDetails, StatementMembership, StatementOut, StatementRow, StatementSummary,
)
from src.auth import xlsx_style as xs
from src.auth.payments_statement_xlsx import HEADER, build_statement_xlsx, statement_file_name

D = date


def seg(start, end, charge, *, days_text=None, basis="full", written_off=0, paid=0):
    return Segment(id=f"seg:{start}", kind="period", period_from=start, period_to=end, billable_days=1, days_text=days_text,
                   price=PriceOut(amount=500_000, source="override"), charge=charge, basis=basis, written_off=written_off, paid=paid)


def alloc(start, end, amount, *, bucket="current", closes=False, after=None):
    return Allocation(segment_id=f"seg:{start}", period_from=start, period_to=end, amount=amount, days=0, bucket=bucket,
                      closes=closes, label="x", segment_remaining_after=after)


AUG, SEP, OCT, NOV = ((D(2026, m, 1), D(2026, m, 28 if m == 2 else 30 if m in (4, 6, 9, 11) else 31)) for m in (8, 9, 10, 11))


def golden(*, note=None, voided=True) -> StatementOut:
    """Aug (a joiner on the 18th, individual price) - Sep - Oct - the advance for Nov; a payment with three lines, a write-off,
    a refund; a voided payment and a voided refund.  The balance before October is an advance of 74 194."""
    aug = seg(D(2026, 8, 18), AUG[1], 225_806, days_text="14 дней из 31", basis="days")
    sep, oct_, nov = seg(*SEP, 500_000), seg(*OCT, 500_000, written_off=100_000), seg(*NOV, 500_000, paid=100_000)
    rows = [
        StatementRow(seq=1, date=D(2026, 8, 18), type="charge", title="s", debit=225_806, balance_after=-225_806, details=StatementDetails(segment=aug)),
        StatementRow(seq=2, date=D(2026, 8, 20), type="payment", title="s", credit=200_000, balance_after=-25_806, details=StatementDetails(
            receipt_no=1, method="cash", created_by_name="Айбек О.", note=note,
            allocations=[alloc(D(2026, 8, 18), AUG[1], 200_000, after=25_806)])),
        StatementRow(seq=3, date=D(2026, 9, 1), type="charge", title="s", debit=500_000, balance_after=-525_806, details=StatementDetails(segment=sep)),
        StatementRow(seq=4, date=D(2026, 9, 5), type="payment", title="s", credit=600_000, balance_after=74_194, details=StatementDetails(
            receipt_no=2, method="card", created_by_name="Айбек О.", allocations=[
                alloc(D(2026, 8, 18), AUG[1], 25_806, bucket="past_debt", closes=True, after=0),
                alloc(*SEP, 500_000, closes=True, after=0),
                alloc(*OCT, 74_194, bucket="advance")])),
        StatementRow(seq=5, date=D(2026, 10, 1), type="charge", title="s", debit=500_000, balance_after=-425_806, details=StatementDetails(segment=oct_)),
        StatementRow(seq=6, date=D(2026, 10, 5), type="writeoff", title="s", credit=100_000, balance_after=-325_806,
                     details=StatementDetails(reason="болел", created_by_name="Директор")),
        StatementRow(seq=7, date=D(2026, 10, 10), type="payment", title="s", credit=325_806, balance_after=0, details=StatementDetails(
            receipt_no=3, method="click", allocations=[alloc(*OCT, 325_806, closes=True, after=0)])),
        StatementRow(seq=8, date=D(2026, 10, 15), type="payment", title="s", credit=100_000, balance_after=100_000, details=StatementDetails(
            receipt_no=4, method="bank_transfer", created_by_name="Айбек О.", allocations=[alloc(*NOV, 100_000, bucket="advance")])),
        StatementRow(seq=9, date=D(2026, 10, 18), type="refund", title="s", outflow=40_000, balance_after=60_000,
                     details=StatementDetails(method="card", reason="переехал", created_by_name="Айбек О.")),
    ]
    cancelled = [
        StatementRow(seq=1, date=D(2026, 9, 3), type="payment", title="Оплата № 99", credit=50_000, voided_at="2026-09-04T10:00:00",
                     void_reason="ошибка ввода", voided_by_name="Директор",
                     details=StatementDetails(receipt_no=99, method="cash", replaced_by_receipt_no=100)),
        StatementRow(seq=2, date=D(2026, 10, 20), type="refund", title="Возврат аванса", outflow=5_000, voided_at="2026-10-21T10:00:00",
                     void_reason="передумали", details=StatementDetails(method="card")),
    ] if voided else []
    return StatementOut(
        membership=StatementMembership(membership_id="m", student_id="s", student_name="Иванов Иван", display_id="A-0042",
                                       group_id="g", group_name="Математика 9", teacher_name="Петрова Анна"),
        as_of=D(2026, 10, 21), accounting_start=D(2026, 8, 1),
        summary=StatementSummary(charged=1_225_806, paid=1_225_806, written_off=100_000, refunded=40_000, advance=60_000),
        rows=rows, voided=cancelled, upcoming=[nov])


def sheet(statement, **kw):
    return load_workbook(io.BytesIO(build_statement_xlsx(statement, **kw))).active


def title_block(ws):
    """``(the three title lines, the header row)``: the title block is rows 1-3, an empty row 4, the header on row 5."""
    head = next(r for r in range(1, 12) if ws.cell(r, 1).value == HEADER[0])
    return [ws.cell(r, 1).value for r in (1, 2, 3)], head


def columns(ws, head):
    """{header: column} - the optional columns («Прощено», «Возвращено», «Переведено») shift the others, so every check goes by the header text."""
    return {ws.cell(head, c).value: c for c in range(1, ws.max_column + 1) if ws.cell(head, c).value}


def grid(ws, first):
    return [[ws.cell(r, c).value for c in range(1, ws.max_column + 1)] for r in range(first, ws.max_row + 1)]


GOLDEN_HEADER = ["Дата", "Операция", "Стоимость", "Оплачено", "Прощено", "Возвращено", "Баланс", "Подробности"]


def test_title_block_header_freeze_and_widths():
    ws = sheet(golden())
    lines, head = title_block(ws)
    assert ws.title == "Выписка" and head == 5 and ws.freeze_panes == "A6" and ws.cell(4, 1).value is None
    assert lines == [
        "Выписка — Иванов Иван · ID A-0042 · Математика 9 · Петрова Анна",
        "По состоянию на 21.10.2026 · Учёт ведётся с 01.08.2026 · Период: Все месяцы",
        "Стоимость обучения 1 225 806 · Оплачено 1 225 806 · Прощено 100 000 · Возвращено 40 000 · Баланс: Аванс 60 000"]
    assert ws["A1"].font.bold and ws["A1"].font.size == 14 and {str(r) for r in ws.merged_cells.ranges} >= {"A1:H1", "A2:H2", "A3:H3"}
    assert all(ws.cell(r, 1).data_type == "s" for r in (1, 2, 3))
    assert [ws.cell(head, c).value for c in range(1, 9)] == GOLDEN_HEADER
    for c in range(1, 9):                                       # the header: D9E2F3, bold, wrapped, Calibri
        cell = ws.cell(head, c)
        assert cell.fill.fgColor.rgb.endswith("D9E2F3") and cell.font.bold and cell.font.name == "Calibri" and cell.alignment.wrap_text
    col = columns(ws, head)
    widths = {name: ws.column_dimensions[chr(64 + c)].width for name, c in col.items()}
    assert all(w >= 12 for w in widths.values()) and widths["Операция"] <= 60 and widths["Подробности"] <= 60
    # no #### : a date needs 10, the biggest sum 9 characters with the separators, the balance text 13
    assert widths["Дата"] >= 12 and widths["Стоимость"] >= 12 and widths["Оплачено"] >= 12 and widths["Баланс"] >= len("Долг 425 806") + 2
    assert widths["Операция"] >= len("Оплата № 4 · перевод · внёс Айбек О.")
    assert ws.cell(head, 1).alignment.horizontal == "center" and ws.cell(head, col["Оплачено"]).alignment.horizontal == "center"
    # print: landscape, fit to the width, the header repeated on every page
    assert ws.page_setup.orientation == "landscape" and ws.page_setup.fitToWidth == 1 and ws.page_setup.fitToHeight == 0
    assert ws.sheet_properties.pageSetUpPr.fitToPage and ws.print_title_rows == "$5:$5"


def test_golden_all_months():
    ws = sheet(golden(note="за двоих"), include_voided=True)
    _, head = title_block(ws)
    got = grid(ws, head + 1)
    d = lambda y, m, dd: __import__("datetime").datetime(y, m, dd)
    n = None
    assert got == [
        ["Август 2026", n, n, n, n, n, n, n],
        [d(2026, 8, 18), "Август 2026 — неполный месяц (14 дней из 31)", 225_806, n, n, n, "Долг 225 806",
         "цена 500 000 сум в месяц · индивидуальная цена"],
        [d(2026, 8, 20), "Оплата № 1 · наличные · внёс Айбек О.", n, 200_000, n, n, "Долг 25 806",
         "комментарий: за двоих\nза август — 200 000 (осталось 25 806)"],
        ["Сентябрь 2026", n, n, n, n, n, n, n],
        [d(2026, 9, 1), "Сентябрь 2026", 500_000, n, n, n, "Долг 525 806", "цена 500 000 сум в месяц · индивидуальная цена"],
        [d(2026, 9, 5), "Оплата № 2 · карта · внёс Айбек О.", n, 600_000, n, n, "Аванс 74 194",
         "за август — 25 806 (август закрыт)\nза сентябрь — 500 000 (сентябрь закрыт)\nаванс на октябрь — 74 194"],
        ["Октябрь 2026", n, n, n, n, n, n, n],
        [d(2026, 10, 1), "Октябрь 2026", 500_000, n, n, n, "Долг 425 806", "цена 500 000 сум в месяц · индивидуальная цена"],
        [d(2026, 10, 5), "Долг прощён — причина: болел · Директор", n, n, 100_000, n, "Долг 325 806", n],
        [d(2026, 10, 10), "Оплата № 3 · Click", n, 325_806, n, n, "0", "за октябрь — 325 806 (октябрь закрыт)"],
        [d(2026, 10, 15), "Оплата № 4 · перевод · внёс Айбек О.", n, 100_000, n, n, "Аванс 100 000", "аванс на ноябрь — 100 000"],
        [d(2026, 10, 18), "Возврат аванса — карта · Айбек О.", n, n, n, 40_000, "Аванс 60 000", "причина: переехал"],
        ["Итого", n, 1_225_806, 1_225_806, 100_000, 40_000, "Аванс 60 000", n],
        [n] * 8,
        ["Следующие периоды (ещё не начались)", n, n, n, n, n, n, n],
        ["01.11–30.11: оплачено 100 000 сум из 500 000 сум", n, n, n, n, n, n, n],
        [n] * 8,
        ["Отменённые записи", n, n, n, n, n, n, n],
        [d(2026, 9, 3), "Оплата № 99", n, 50_000, n, n, n, "Отменено: ошибка ввода · Директор · заменена оплатой № 100"],
        [d(2026, 10, 20), "Возврат аванса", n, n, n, 5_000, n, "Отменено: передумали"],
    ]
    # no signed number anywhere: a refund and a forgiven debt are words of their own columns, never a «-40 000» in «Оплачено»
    assert all(c.value >= 0 for row in ws.iter_rows() for c in row if isinstance(c.value, int))


def test_the_blue_total_row_and_the_one_balance_rule():
    ws = sheet(golden())
    _, head = title_block(ws)
    col = columns(ws, head)
    total = next(r for r in range(head + 1, ws.max_row + 1) if ws.cell(r, 1).value == "Итого")
    for c in range(1, ws.max_column + 1):                       # blue over the whole width, bold, a medium line above
        cell = ws.cell(total, c)
        assert cell.fill.fgColor.rgb.endswith("D9E2F3") and cell.font.bold and cell.border.top.style == "medium" and cell.border.top.color.rgb.endswith("8EA9DB")
    assert [ws.cell(total, col[h]).value for h in ("Стоимость", "Оплачено", "Прощено", "Возвращено")] == [1_225_806, 1_225_806, 100_000, 40_000]
    assert ws.cell(total, col["Стоимость"]).number_format == "#,##0"
    # ONE rule: the balance is the words of the screen - in the rows, in the total and in the title line alike
    assert ws.cell(total, col["Баланс"]).value == "Аванс 60 000" and ws.cell(total, col["Баланс"]).data_type == "s"
    assert ws.cell(1, 1).font.size == 14 and ws.cell(3, 1).value.endswith("Баланс: Аванс 60 000")
    balances = [ws.cell(r, col["Баланс"]).value for r in range(head + 1, total) if ws.cell(r, col["Баланс"]).value]
    assert all(isinstance(b, str) and (b.startswith(("Долг ", "Аванс ")) or b == "0") for b in balances)
    # a month: the total is of the month, the balance is the balance after its last record
    ws = sheet(golden(), date_from=D(2026, 10, 1), date_to=D(2026, 10, 31))
    _, head = title_block(ws)
    col = columns(ws, head)
    total = next(r for r in range(head + 1, ws.max_row + 1) if ws.cell(r, 1).value == "Итого")
    assert [ws.cell(total, col[h]).value for h in ("Стоимость", "Оплачено", "Прощено", "Возвращено", "Баланс")] == [500_000, 425_806, 100_000, 40_000, "Аванс 60 000"]


def test_the_optional_columns_exist_only_when_the_file_has_such_operations():
    st = golden(voided=False)
    st.rows = [r for r in st.rows if r.type not in ("writeoff", "refund")]
    ws = sheet(st)
    _, head = title_block(ws)
    assert [ws.cell(head, c).value for c in range(1, ws.max_column + 1)] == HEADER == ["Дата", "Операция", "Стоимость", "Оплачено", "Баланс", "Подробности"]
    assert {str(r) for r in ws.merged_cells.ranges} >= {"A1:F1", "A3:F3"}
    # a transfer to another group has «Переведено» (positive); money that came in stays in «Оплачено» as on the page
    st = golden(voided=False)
    st.rows.append(StatementRow(seq=10, date=D(2026, 10, 19), type="transfer_out", title="Перенос в группу Физика", outflow=30_000, balance_after=30_000,
                                details=StatementDetails()))
    st.rows.append(StatementRow(seq=11, date=D(2026, 10, 20), type="transfer_in", title="Перенос из группы Химия", credit=10_000, balance_after=40_000,
                                details=StatementDetails()))
    ws = sheet(st)
    _, head = title_block(ws)
    col = columns(ws, head)
    assert [ws.cell(head, c).value for c in range(1, ws.max_column + 1)] == ["Дата", "Операция", "Стоимость", "Оплачено", "Прощено", "Возвращено", "Переведено", "Баланс", "Подробности"]
    out = next(r for r in range(head, ws.max_row + 1) if ws.cell(r, 2).value == "Перенос в группу Физика")
    inn = next(r for r in range(head, ws.max_row + 1) if ws.cell(r, 2).value == "Перенос из группы Химия")
    assert (ws.cell(out, col["Переведено"]).value, ws.cell(out, col["Оплачено"]).value) == (30_000, None)
    assert (ws.cell(inn, col["Оплачено"]).value, ws.cell(inn, col["Переведено"]).value) == (10_000, None)
    total = next(r for r in range(head + 1, ws.max_row + 1) if ws.cell(r, 1).value == "Итого")
    assert ws.cell(total, col["Переведено"]).value == 30_000 and ws.cell(total, col["Оплачено"]).value == 1_225_806 + 10_000 and ws.cell(total, col["Баланс"]).value == "Аванс 40 000"


def test_cell_types_formats_alignment_and_merges():
    ws = sheet(golden())
    _, head = title_block(ws)
    col = columns(ws, head)
    first = head + 1
    merged = {str(r) for r in ws.merged_cells.ranges}
    assert {f"A{first}:H{first}", f"A{first + 3}:H{first + 3}", f"A{first + 6}:H{first + 6}"} <= merged     # a band per month
    band = ws.cell(first, 1)
    assert band.font.bold and band.fill.fgColor.rgb.endswith("EDF1F7") and band.alignment.horizontal == "left"
    # rows: the second row of the table is the August charge
    r = first + 1
    date_cell, op = ws.cell(r, col["Дата"]), ws.cell(r, col["Операция"])
    charged, paid, balance, details = (ws.cell(r, col[h]) for h in ("Стоимость", "Оплачено", "Баланс", "Подробности"))
    assert date_cell.is_date and date_cell.number_format == "DD.MM.YYYY" and date_cell.alignment.horizontal == "center"
    assert isinstance(charged.value, int) and charged.number_format == "#,##0" and charged.alignment.horizontal == "center"
    assert paid.value is None and balance.data_type == "s" and balance.value == "Долг 225 806" and balance.alignment.horizontal == "center"
    assert op.data_type == "s" and op.alignment.wrap_text and op.alignment.vertical == "top"
    assert details.alignment.wrap_text and details.alignment.vertical == "top"
    assert all(ws.cell(r, c).font.name == "Calibri" for c in range(1, ws.max_column + 1))
    pay = first + 2
    assert isinstance(ws.cell(pay, col["Оплачено"]).value, int) and ws.cell(pay, col["Оплачено"]).number_format == "#,##0" and ws.cell(pay, col["Стоимость"]).value is None
    refund = next(rr for rr in range(first, ws.max_row + 1) if str(ws.cell(rr, 2).value).startswith("Возврат аванса —"))
    assert ws.cell(refund, col["Возвращено"]).value == 40_000 and ws.cell(refund, col["Возвращено"]).number_format == "#,##0" and ws.cell(refund, col["Оплачено"]).value is None
    # the multi-line details are one cell with a taller row
    alloc_row = next(rr for rr in range(first, ws.max_row + 1) if str(ws.cell(rr, 2).value).startswith("Оплата № 2"))
    assert ws.cell(alloc_row, col["Подробности"]).value.count("\n") == 2 and ws.row_dimensions[alloc_row].height >= 45
    flat = [str(c.value) for row in ws.iter_rows() for c in row if c.value is not None]
    assert not any("Начисл" in x for x in flat)
    assert not any(x in flat for x in ("cash", "card", "bank_transfer", "click", "payme", "№", "Возвращено / перенесено", "Оплачено / прощено"))
    assert "ID A-0042" in ws.cell(1, 1).value


def test_the_cancelled_block_is_below_in_grey_and_struck():
    ws = sheet(golden(), include_voided=True)
    rows = list(range(1, ws.max_row + 1))
    head = next(r for r in rows if ws.cell(r, 1).value == "Отменённые записи")
    assert head > max(r for r in rows if ws.cell(r, 2).value == "Возврат аванса — карта · Айбек О.")
    assert any(str(rg) == f"A{head}:H{head}" for rg in ws.merged_cells.ranges) and ws.cell(head, 1).font.bold
    for r in (head + 1, head + 2):
        for c in range(1, 7):
            cell = ws.cell(r, c)
            if cell.value is not None:
                assert cell.font.strike and cell.font.color.rgb.endswith("808080")
        assert ws.cell(r, 8).font.color.rgb.endswith("808080") and not ws.cell(r, 8).font.strike and ws.cell(r, 8).value.startswith("Отменено:")
    # the cancelled rows are never mixed into the months or into the total: the month bands carry no «Отменено» text
    assert not any("Отменено" in str(ws.cell(r, 8).value) for r in range(1, head))
    total = next(r for r in rows if ws.cell(r, 1).value == "Итого")
    assert ws.cell(total, 4).value == 1_225_806 and total < head
    # without include_voided there is no block at all
    ws2 = sheet(golden(voided=False), include_voided=False)
    assert not any(ws2.cell(r, 1).value == "Отменённые записи" for r in range(1, ws2.max_row + 1))
    ws3 = sheet(golden(voided=False), include_voided=True)
    assert [ws3.cell(r, 1).value for r in range(ws3.max_row - 1, ws3.max_row + 1)] == ["Отменённые записи", "Отменённых записей нет"]


def test_a_single_month_has_its_carry_line_and_the_period_in_the_title():
    ws = sheet(golden(), date_from=D(2026, 10, 1), date_to=D(2026, 10, 31), include_voided=True)
    lines, head = title_block(ws)
    col = columns(ws, head)
    assert lines[1].endswith("Период: Октябрь 2026")
    # the title totals stay for the whole membership
    assert lines[2] == "Стоимость обучения 1 225 806 · Оплачено 1 225 806 · Прощено 100 000 · Возвращено 40 000 · Баланс: Аванс 60 000"
    got = grid(ws, head + 1)
    assert got[0][0] == "Октябрь 2026" and got[1][0] == "С сентября перенесён аванс 74 194"
    assert [x for x in ws.merged_cells.ranges if x.min_row == head + 2] and ws.cell(head + 2, 1).font.italic
    body = [g for g in got[2:] if g[1] and not str(g[0]).startswith("Отменённые")]
    assert [g[1] for g in body[:6]] == ["Октябрь 2026", "Долг прощён — причина: болел · Директор", "Оплата № 3 · Click",
                                         "Оплата № 4 · перевод · внёс Айбек О.", "Возврат аванса — карта · Айбек О.", "Возврат аванса"]
    # the periods to come belong to the whole statement only (as on the page)
    assert not any("Следующие периоды" in str(c.value) for row in ws.iter_rows() for c in row)
    # months outside the range are gone, also from the cancelled block (the September payment)
    assert not any(isinstance(g[0], str) and g[0] in ("Август 2026", "Сентябрь 2026") for g in got)
    assert not any(g[1] == "Оплата № 99" for g in got)
    # the balance is the ledger's, not recomputed over the cut rows: a charge row keeps its running debt
    assert got[2][col["Баланс"] - 1] == "Долг 425 806"


def test_range_label_and_a_month_without_records_or_carry():
    ws = sheet(golden(), date_from=D(2026, 10, 1), date_to=D(2026, 10, 15))
    lines, head = title_block(ws)
    assert lines[1].endswith("Период: 01.10.2026–15.10.2026")
    assert ws.cell(head + 1, 1).value == "Октябрь 2026" and not str(ws.cell(head + 2, 1).value).startswith("С сентября")   # not a month: no carry
    # a month before the first record: no carry line; an empty month after: carry and a quiet «Записей нет»
    ws = sheet(golden(), date_from=D(2026, 8, 1), date_to=D(2026, 8, 31))
    _, head = title_block(ws)
    assert [ws.cell(head + 1, 1).value, str(ws.cell(head + 2, 1).value)[:4]] == ["Август 2026", "2026"]
    ws = sheet(golden(), date_from=D(2026, 12, 1), date_to=D(2026, 12, 31))
    _, head = title_block(ws)
    assert [ws.cell(r, 1).value for r in (head + 1, head + 2, head + 3)] == ["Декабрь 2026", "С ноября перенесён аванс 60 000", "Записей нет"]
    assert not any(ws.cell(r, 1).value == "Итого" for r in range(head, ws.max_row + 1))      # nothing to total
    ws = sheet(golden(), date_from=D(2026, 7, 1), date_to=D(2026, 7, 31))
    _, head = title_block(ws)
    assert [ws.cell(r, 1).value for r in (head + 1, head + 2)] == ["Июль 2026", "Записей нет"]


def test_a_hostile_text_stays_text_everywhere():
    st = golden(note='=HYPERLINK("http://evil")')
    st.membership.student_name = "=1+1"
    st.membership.teacher_name = "@SUM(1)"
    st.rows[5].details.reason = "=cmd|' /C calc'!A0"
    ws = sheet(st)
    hot = [c for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value[:1] in "=+-@" or
           (isinstance(c.value, str) and "=HYPERLINK" in c.value)]
    assert hot and all(c.data_type == "s" for c in hot)
    assert all(c.data_type != "f" for row in ws.iter_rows() for c in row)


def test_an_empty_statement():
    st = golden()
    st.rows, st.voided, st.upcoming = [], [], []
    st.summary = StatementSummary()
    ws = sheet(st)
    lines, head = title_block(ws)
    assert lines[2] == "Стоимость обучения 0 · Оплачено 0 · Баланс: 0" and ws.cell(head + 1, 1).value == "В выписке пока нет записей"


def test_the_russian_file_name_without_technical_ids():
    st = golden()
    assert statement_file_name(st) == "Выписка_Иванов_Иван_на-21.10.2026.xlsx"
    assert statement_file_name(st, D(2026, 10, 1), D(2026, 10, 31)) == "Выписка_Иванов_Иван_октябрь-2026.xlsx"
    assert statement_file_name(st, D(2026, 10, 1), D(2026, 10, 15)) == "Выписка_Иванов_Иван_с-01.10.2026_по-15.10.2026.xlsx"
    assert "A-0042" not in statement_file_name(st)
    st.membership.student_name = 'Иван/"<x>"\\Петров'
    assert not set('/"<>\\') & set(statement_file_name(st))
    header = xs.content_disposition(statement_file_name(golden(), D(2026, 10, 1), D(2026, 10, 31)))
    assert header.startswith('attachment; filename="Vypiska_Ivanov_Ivan_oktyabr-2026.xlsx"') and "filename*=UTF-8''%D0%92%D1%8B%D0%BF" in header
