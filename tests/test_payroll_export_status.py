"""The title line of a teacher's Excel sheet (``payroll_export.sheet_status``): the table is the SAVED sheet, so when the data moved after the save the line
names the current total too (stage re-walk R2-2а: «Начислено сейчас 75 834» on the screen, «Итого 120 834» in the Excel, no word between them)."""
from src.auth.payroll_export import sheet_status


def sheet(**payout) -> dict:
    return {"revision": 6, "saved": True, "updated_at": None, "month_open": False, "note": None, "payout": payout or None}


def test_an_open_month_that_moved_names_the_current_total():
    line = sheet_status(sheet(saved_total=120_834, fresh_total=75_834, basis="fresh"))
    assert "строки сохранённой ведомости; текущий расчёт: 75 834 сум (месяц открыт)" in line


def test_a_closed_month_that_moved_asks_to_save_again():
    line = sheet_status(sheet(saved_total=860_000, fresh_total=876_666, basis="saved"))
    assert "текущий расчёт: 876 666 сум — пересохраните ведомость" in line


def test_a_sheet_as_saved_or_without_a_payout_says_nothing_more():
    assert "текущий расчёт" not in sheet_status(sheet(saved_total=120_834, fresh_total=120_834, basis="fresh"))
    assert "текущий расчёт" not in sheet_status(sheet())
    assert sheet_status({"saved": False, "payout": None}) == "Предварительный расчёт — не сохранён"
