"""N13 P3: the payroll engine - pure maths, no database."""
from datetime import date
from decimal import Decimal
from fractions import Fraction

import pytest

from src.auth import payroll_engine as pe

IDENT = {
    "membership_id": "m1", "student_id": "s1", "student_name": "Каримова Алия", "display_id": "N13-001",
    "group_id": "g1", "group_name": "Группа А", "lesson_type": "group", "lesson_time": "10:00–11:00",
}


def marks(*states, divisor=12, start=1):
    """One column per state, days 1, 2, 3 ... of October 2026."""
    return [pe.Mark(date(2026, 10, start + i), f"s{i}", state, None if state in (pe.NOT_IN_GROUP, pe.EXCUSED_CONFIRMED) else divisor)
            for i, state in enumerate(states)]


def line(states, *, fee=450_000, percent="50", divisor=12, **kwargs):
    return pe.compute_line(
        IDENT, marks(*states, divisor=divisor), source_fee=fee,
        percent=None if percent is None else Decimal(percent), **kwargs)


# ---- the partner's table (mandatory) ----------------------------------------------------------


@pytest.mark.parametrize("lessons,expected", [(11, 206_250), (13, 243_750), (5, 93_750)])
def test_partner_table(lessons, expected):
    got = line([pe.COUNTED] * lessons)
    assert got["amount"] == expected and got["counted"] == lessons and got["divisor"] == 12
    assert pe.line_amount(450_000, Decimal("50"), [12] * lessons) == expected


def test_more_than_d_lessons_are_all_paid():
    assert line([pe.COUNTED] * 20)["amount"] == 450_000 * 50 * 20 // (100 * 12)      # no ceiling at 12


# ---- rounding ------------------------------------------------------------------------------------


def test_half_goes_up_not_to_even():
    for half, expected in ((Fraction(1, 2), 1), (Fraction(3, 2), 2), (Fraction(5, 2), 3), (Fraction(7, 2), 4)):
        assert pe.round_half_up(half) == expected
    assert pe.round_half_up(Fraction(4999, 10000)) == 0 and pe.round_half_up(Fraction(5001, 10000)) == 1
    assert pe.round_half_up(Fraction(0)) == 0


def test_a_twelfth_is_never_truncated_before_the_multiplication():
    # fee 1, 100 %, six lessons of 1/12 = exactly half a sum: Decimal(1)/12*6 would give 0.4999... and round DOWN
    assert pe.line_amount(1, Decimal("100"), [12] * 6) == 1
    # 25 000 * 33.33 % / 12 * 7 = 4860.625 -> 4861 (one rounding of the whole line, not seven roundings)
    assert pe.line_amount(25_000, Decimal("33.33"), [12] * 7) == 4861
    # a line is rounded ONCE: 7 lessons of 694.375 each is not 7 * 694
    assert pe.line_amount(100_001, Decimal("100"), [12] * 7) == round_half_up_ref(100_001 * 7, 12)


def round_half_up_ref(numerator, denominator):
    return (2 * numerator + denominator) // (2 * denominator)


def test_zero_fee_zero_percent_zero_lessons():
    assert line([pe.COUNTED] * 5, fee=0)["amount"] == 0
    assert line([pe.COUNTED] * 5, percent="0")["amount"] == 0
    assert line([])["amount"] == 0 and line([])["divisor"] == 12
    assert line([pe.COUNTED] * 3, percent="100")["amount"] == 450_000 * 3 // 12


def test_large_sums_stay_exact():
    fee = 9_999_999_999
    assert pe.line_amount(fee, Decimal("99.99"), [7] * 31) == round_half_up_ref(fee * 9999 * 31, 100 * 100 * 7)


# ---- divisor of a lesson -------------------------------------------------------------------------------


def test_package_lessons_use_the_package_size_as_divisor():
    # 8 lessons of an 8-lesson package: the teacher gets exactly p % of the fee, not 8/12 of it
    got = line([pe.COUNTED] * 8, fee=480_000, divisor=8)
    assert got["amount"] == 240_000 and got["divisor"] == 8
    assert line([pe.COUNTED] * 8, fee=480_000, divisor=12)["amount"] == 160_000


def test_one_line_can_mix_calendar_and_package_lessons():
    ms = [pe.Mark(date(2026, 10, d), f"s{d}", pe.COUNTED, 12) for d in range(1, 7)] + [
        pe.Mark(date(2026, 10, d), f"s{d}", pe.COUNTED, 8) for d in range(7, 11)]
    got = pe.compute_line(IDENT, ms, source_fee=480_000, percent=Decimal("50"))
    assert got["amount"] == 6 * 20_000 + 4 * 30_000 and got["divisor"] is None and got["counted"] == 10
    assert [m["divisor"] for m in got["marks"]] == [12] * 6 + [8] * 4        # every lesson keeps its own D


# ---- the states of a lesson ----------------------------------------------------------------------------------


def test_excused_confirmed_and_not_in_group_are_not_counted():
    got = line([pe.COUNTED, pe.EXCUSED_CONFIRMED, pe.NOT_IN_GROUP, pe.COUNTED])
    assert got["counted"] == 2 and got["amount"] == 450_000 * 50 * 2 // 1200
    assert [m["mark"] for m in got["marks"]] == [1, 0, None, 1]


def test_pending_excuse_is_counted_and_reported():
    got = line([pe.COUNTED, pe.EXCUSED_PENDING], pending_excused=1)
    assert got["counted"] == 2 and got["pending_excused"] == 1 and got["amount"] == 450_000 * 50 * 2 // 1200
    assert [w["code"] for w in got["warnings"]] == [pe.EXCUSE_PENDING]
    assert "1" in got["warnings"][0]["message"]


def test_unpaid_lessons_are_shown_but_not_paid_unless_included():
    states = [pe.COUNTED] * 5 + [pe.UNPAID] * 3
    out = line(states)
    assert (out["counted"], out["unpaid"], out["amount"]) == (5, 3, 450_000 * 50 * 5 // 1200)
    assert [w["code"] for w in out["warnings"]] == [pe.UNPAID_LESSONS]
    inc = line(states, include_unpaid=True)
    assert (inc["counted"], inc["unpaid"], inc["amount"]) == (5, 3, 450_000 * 50 * 8 // 1200)
    assert inc["include_unpaid"] is True and "включены" in inc["warnings"][0]["message"]
    assert pe.UNPAID == inc["marks"][-1]["state"] and inc["marks"][-1]["mark"] == 1


def test_missing_fee_and_missing_percent_give_zero_and_a_reason():
    nofee = line([pe.COUNTED] * 4, fee=None)
    assert nofee["amount"] == 0 and nofee["monthly_fee"] is None
    assert [w["code"] for w in nofee["warnings"]] == [pe.NO_FEE]
    nopercent = line([pe.COUNTED] * 4, percent=None)
    assert nopercent["amount"] == 0 and nopercent["percent"] is None
    assert [w["code"] for w in nopercent["warnings"]] == [pe.PERCENT_REQUIRED]


def test_fee_override_replaces_the_fee_of_the_membership():
    got = line([pe.COUNTED] * 12, fee=450_000, fee_override=300_000)
    assert got["monthly_fee"] == 300_000 and got["source_fee"] == 450_000 and got["fee_overridden"] is True
    assert got["amount"] == 150_000
    assert line([pe.COUNTED] * 12, fee=None, fee_override=0)["amount"] == 0          # an explicit 0 is a fee (free)
    assert line([pe.COUNTED] * 12, fee=None, fee_override=600_000)["warnings"] == []


# ---- recomputing a SAVED line ---------------------------------------------------------------------------------


def test_recompute_changes_percent_fee_and_tick_from_the_saved_marks_only():
    saved = line([pe.COUNTED] * 6 + [pe.UNPAID] * 2, pending_excused=0)
    assert saved["amount"] == 450_000 * 50 * 6 // 1200
    more = pe.recompute_line(saved, percent=Decimal("60"), fee_override=None)
    assert more["amount"] == 450_000 * 60 * 6 // 1200 and more["marks"] == saved["marks"]
    ticked = pe.recompute_line(saved, percent=Decimal("50"), fee_override=None, include_unpaid=True)
    assert ticked["amount"] == 450_000 * 50 * 8 // 1200 and ticked["include_unpaid"] is True
    # the tick survives a later change of the fee
    fee = pe.recompute_line(ticked, percent=Decimal("50"), fee_override=300_000)
    assert fee["include_unpaid"] is True and fee["amount"] == 300_000 * 50 * 8 // 1200 and fee["fee_overridden"]
    # ... and the overwritten fee survives a later change of the percent
    keep = pe.recompute_line(fee, percent=Decimal("40"), fee_override=None)
    assert keep["monthly_fee"] == 300_000 and keep["fee_overridden"] is True
    # reset: back to the fee of the membership as it was when the line was calculated
    back = pe.recompute_line(keep, percent=Decimal("40"), fee_override=None, reset_fee=True)
    assert back["monthly_fee"] == 450_000 and back["fee_overridden"] is False
    # untick
    off = pe.recompute_line(ticked, percent=Decimal("50"), fee_override=None, include_unpaid=False)
    assert off["amount"] == saved["amount"] and off["include_unpaid"] is False


# ---- percent ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ("50", "50"), (50, "50"), (50.5, "50.5"), ("62.50", "62.5"), ("62,5", "62.5"), ("0", "0"), ("0.00", "0"),
    ("100", "100"), ("99.99", "99.99"), (Decimal("7.10"), "7.1"), (" 33.33 ", "33.33"),
])
def test_percent_accepts(value, expected):
    assert pe.format_percent(pe.parse_percent(value)) == expected


@pytest.mark.parametrize("value", [-1, "-0.01", 100.01, "101", "1.234", "abc", "", "  ", None, True, "NaN", "Infinity", "1e2x"])
def test_percent_rejects(value):
    with pytest.raises(pe.PercentError):
        pe.parse_percent(value)


# ---- blocks, totals, diff -------------------------------------------------------------------------------------------


def two_group_lines():
    other = dict(IDENT, membership_id="m2", student_id="s2", student_name="Юсупов Бек", group_id="g2",
                 group_name="Группа Б", lesson_type="pair")
    a = line([pe.COUNTED, pe.COUNTED, pe.UNPAID])
    b = pe.compute_line(other, marks(pe.COUNTED, pe.EXCUSED_CONFIRMED, start=5), source_fee=300_000, percent=Decimal("40"))
    c = pe.compute_line(dict(IDENT, membership_id="m3", student_id="s3", student_name="Орлова Дана"),
                        marks(pe.NOT_IN_GROUP, pe.COUNTED, pe.COUNTED), source_fee=None, percent=Decimal("50"))
    return [a, c, b]


def test_totals_blocks_and_counters_come_from_the_lines():
    lines = two_group_lines()
    assert pe.sheet_total(lines) == sum(x["amount"] for x in lines)
    counters = pe.sheet_counters(lines)
    assert counters == {"total_amount": pe.sheet_total(lines), "counted": 2 + 2 + 1, "unpaid": 1,
                        "pending_excused": 0, "no_fee": 1}
    blocks = pe.group_blocks(lines)
    assert [b["group_id"] for b in blocks] == ["g1", "g2"]
    first = blocks[0]
    assert first["line_count"] == 2 and first["counted"] == 4 and first["unpaid"] == 1
    assert [d["date"] for d in first["dates"]] == ["2026-10-01", "2026-10-02", "2026-10-03"]      # one column per lesson
    assert first["total_amount"] == lines[0]["amount"] + lines[1]["amount"]
    assert blocks[1]["lesson_type"] == "pair" and blocks[1]["percent"] == "40"


def test_diff_lists_only_what_changed():
    saved = two_group_lines()
    fresh = [dict(x) for x in saved]
    assert pe.diff_lines(saved, fresh) == []
    fresh[0] = line([pe.COUNTED] * 3)                                   # the unpaid lesson got paid
    fresh.pop()                                                         # one line is gone
    fresh.append(dict(saved[0], membership_id="m9", student_name="Новый Ученик", counted=1, amount=100))  # one is new
    got = {row["membership_id"]: row for row in pe.diff_lines(saved, fresh)}
    assert set(got) == {"m1", "m2", "m9"}
    assert got["m1"]["status"] == "changed" and "не оплачено" in got["m1"]["changes"] and "засчитано" in got["m1"]["changes"]
    assert got["m1"]["delta_amount"] == fresh[0]["amount"] - saved[0]["amount"]
    assert got["m2"]["status"] == "removed" and got["m2"]["fresh"] is None and got["m2"]["delta_amount"] == -saved[2]["amount"]
    assert got["m9"]["status"] == "added" and got["m9"]["saved"] is None and got["m9"]["delta_amount"] == 100


def test_diff_notices_a_changed_mark_even_when_the_money_is_the_same():
    a = line([pe.COUNTED, pe.COUNTED])
    ms = marks(pe.COUNTED, pe.EXCUSED_PENDING)
    b = pe.compute_line(IDENT, ms, source_fee=450_000, percent=Decimal("50"), pending_excused=0)
    assert a["amount"] == b["amount"] and a["counted"] == b["counted"]
    (row,) = pe.diff_lines([a], [b])
    assert row["status"] == "changed" and row["changes"] == ["отметки"] and row["delta_amount"] == 0
