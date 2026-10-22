"""INCOME-2: periods, the comparison with the previous period and the Russian sentences (pure functions, no database)."""
from datetime import date, datetime

import pytest

from src.auth import payments_texts as tx
from src.auth.payments_income import (
    _lead, _previous_dative, _previous_label, _previous_ref, csv_safe_row, entry_times, month_end, previous_period, resolve_period, split_kinds, strip_parts)
from src.auth.payments_service import PaymentInvalid

D = date
TODAY = D(2026, 10, 21)


def prev(first, last, kind=None, today=TODAY):
    cur = resolve_period(first, last, today, kind)
    return cur, previous_period(cur, today)


def test_kind_is_inferred_from_the_dates():
    assert resolve_period(D(2026, 10, 5), D(2026, 10, 5), TODAY).kind == "day"
    assert resolve_period(D(2026, 10, 12), D(2026, 10, 18), TODAY).kind == "week"         # a Monday
    assert resolve_period(D(2026, 10, 13), D(2026, 10, 19), TODAY).kind == "custom"       # seven days from a Tuesday
    assert resolve_period(D(2026, 9, 1), D(2026, 9, 30), TODAY).kind == "month"
    assert resolve_period(D(2026, 9, 1), D(2026, 9, 29), TODAY).kind == "custom"
    assert resolve_period(D(2026, 2, 1), D(2026, 2, 28), TODAY).kind == "month"
    assert month_end(D(2028, 2, 10)) == D(2028, 2, 29)


def test_a_told_kind_must_fit_the_dates():
    for first, last, kind in ((D(2026, 10, 1), D(2026, 10, 2), "day"), (D(2026, 10, 1), D(2026, 10, 3), "week"),
                              (D(2026, 10, 2), D(2026, 10, 31), "month"), (D(2026, 9, 1), D(2026, 10, 5), "month")):
        with pytest.raises(PaymentInvalid):
            resolve_period(first, last, TODAY, kind)
    with pytest.raises(PaymentInvalid):
        resolve_period(D(2026, 10, 5), D(2026, 10, 4), TODAY)
    assert resolve_period(D(2026, 10, 1), D(2026, 10, 21), TODAY, "month").kind == "month"      # month to date is fine


def test_previous_of_a_day_a_week_and_a_custom_range():
    cur, p = prev(D(2026, 10, 21), D(2026, 10, 21))
    assert (p.first, p.last, p.label, cur.label) == (D(2026, 10, 20), D(2026, 10, 20), "вчера", "сегодня")
    cur, p = prev(D(2026, 10, 12), D(2026, 10, 18))
    assert (p.first, p.last, p.label) == (D(2026, 10, 5), D(2026, 10, 11), "неделя 5–11 октября")
    cur, p = prev(D(2026, 9, 3), D(2026, 9, 20))                                       # 18 days
    assert (p.first, p.last, p.days) == (D(2026, 8, 16), D(2026, 9, 2), 18)
    assert p.label == "период 16.08–02.09"


def test_previous_of_a_whole_month_is_the_previous_calendar_month():
    cur, p = prev(D(2026, 9, 1), D(2026, 9, 30))
    assert (p.first, p.last, p.label) == (D(2026, 8, 1), D(2026, 8, 31), "август")
    cur, p = prev(D(2026, 3, 1), D(2026, 3, 31))
    assert (p.first, p.last) == (D(2026, 2, 1), D(2026, 2, 28))
    cur, p = prev(D(2026, 1, 1), D(2026, 1, 31))
    assert (p.first, p.last) == (D(2025, 12, 1), D(2025, 12, 31))
    assert p.label == "декабрь 2025"


def test_a_running_period_is_compared_like_for_like():
    cur, p = prev(D(2026, 10, 1), D(2026, 10, 31))
    assert cur.in_progress and (p.first, p.last, p.label) == (D(2026, 9, 1), D(2026, 9, 21), "1–21 сентября")
    cur, p = prev(D(2026, 10, 20), D(2026, 10, 26))                                    # a custom range, 2 days elapsed
    assert cur.in_progress and (p.first, p.last) == (D(2026, 10, 13), D(2026, 10, 14)) and p.label == "период 13.10–14.10"
    cur, p = prev(D(2026, 3, 1), D(2026, 3, 31), today=D(2026, 3, 30))                 # the previous month is shorter than the elapsed days
    assert (p.first, p.last) == (D(2026, 2, 1), D(2026, 2, 28))
    cur, p = prev(D(2026, 10, 1), D(2026, 10, 21), "month")                            # month to date, ends today -> not "running"
    assert not cur.in_progress and (p.first, p.last) == (D(2026, 9, 1), D(2026, 9, 21))
    cur, p = prev(D(2026, 11, 1), D(2026, 11, 30))                                     # a future month: the whole previous month
    assert not cur.in_progress and (p.first, p.last) == (D(2026, 10, 1), D(2026, 10, 31))


def test_percent_change_rounds_half_away_from_zero():
    assert tx.percent_change(112, 100) == 12
    assert tx.percent_change(88, 100) == -12
    assert tx.percent_change(1, 3) == -67
    assert tx.percent_change(2, 3) == -33
    assert tx.percent_change(1005, 1000) == 1 and tx.percent_change(995, 1000) == -1
    assert tx.percent_change(100, 0) is None and tx.percent_change(0, 100) == -100


def text(**kw):
    base = dict(lead="За октябрь", received=7_885_000, previous_received=7_040_000, previous_ref="за сентябрь")
    return tx.income_summary_text(**{**base, **kw})


def test_summary_sentences():
    assert text(collected_pct=63) == "За октябрь пришло 7 885 000 сум — на 12% больше, чем за сентябрь. Собрано 63% стоимости месяца."
    assert text(received=6_000_000, previous_received=8_000_000) == "За октябрь пришло 6 000 000 сум — на 25% меньше, чем за сентябрь."
    assert text(received=100, previous_received=100) == "За октябрь пришло 100 сум — столько же, сколько за сентябрь."
    assert text(received=100_001, previous_received=100_000).startswith("За октябрь пришло 100 001 сум — почти столько же, сколько")
    assert text(previous_received=0) == "За октябрь пришло 7 885 000 сум."                      # S2: no comparison clause when the previous period had no payments
    assert text(previous_received=0, collected_pct=69) == "За октябрь пришло 7 885 000 сум. Собрано 69% стоимости месяца."
    assert text(received=0) == "За октябрь оплат не было; за сентябрь пришло 7 040 000 сум."
    assert text(received=0, previous_received=0, collected_pct=0) == "За октябрь оплат не было. Собрано 0% стоимости месяца."
    assert tx.income_summary_text(lead="Сегодня", received=1_200_000, previous_received=1_000_000, previous_ref="вчера") == (
        "Сегодня пришло 1 200 000 сум — на 20% больше, чем вчера.")
    assert tx.income_summary_text(lead="Сегодня", received=250_000, previous_received=0, previous_ref="вчера") == "Сегодня пришло 250 000 сум."


def test_allocation_summary_joins_pieces_in_the_owners_words():
    assert tx.allocation_summary([("за сентябрь", 200_000), ("аванс", 50_000)]) == "за сентябрь — 200 000 сум · аванс — 50 000 сум"
    assert tx.allocation_summary([("за сентябрь", 100_000), ("за сентябрь", 100_000), ("возвращено", 0)]) == "за сентябрь — 200 000 сум"
    assert tx.allocation_summary([("за 22.10–21.11", 450_000)]) == "за 22.10–21.11 — 450 000 сум"
    assert tx.allocation_summary([]) == ""


def test_dates_and_method_words():
    assert tx.span_text(D(2026, 10, 12), D(2026, 10, 18)) == "12–18 октября"
    assert tx.span_text(D(2026, 9, 30), D(2026, 10, 6)) == "30 сентября – 6 октября"
    assert tx.span_text(D(2026, 10, 5), D(2026, 10, 5)) == "5 октября"
    assert [tx.method_label(m) for m in ("cash", "card", "bank_transfer", "click", "payme", "other", "unknown", None)] == [
        "Наличные", "Карта", "Перевод", "Click", "Payme", "Другое", "Способ не указан", "Способ не указан"]


def test_the_short_comparison_phrase():
    cases = [((D(2026, 9, 1), D(2026, 9, 30)), "к августу"), ((D(2026, 10, 1), D(2026, 10, 31)), "к 1–21 сентября"),
             ((D(2026, 10, 21), D(2026, 10, 21)), "ко вчерашнему дню"), ((D(2026, 10, 15), D(2026, 10, 15)), "к 14 октября"),
             ((D(2026, 10, 5), D(2026, 10, 11)), "к прошлой неделе"), ((D(2026, 10, 19), D(2026, 10, 25)), "к 12–14 октября"),
             ((D(2026, 9, 3), D(2026, 9, 20)), "к предыдущему периоду"), ((D(2026, 9, 1), D(2026, 9, 21)), "к предыдущему периоду")]
    for (first, last), want in cases:
        cur = resolve_period(first, last, TODAY, "custom" if (first, last) == (D(2026, 9, 1), D(2026, 9, 21)) else None)
        assert _previous_dative(cur, previous_period(cur, TODAY), TODAY) == want, (first, last)
    assert tx.delta_text(-12, "к сентябрю") == "12% к сентябрю"
    assert tx.collection_text(1_900_000, 2_500_000, 76) == "Оплачено 1 900 000 из 2 500 000 сум (76%)"


def test_short_span_labels():
    assert tx.short_span_text(D(2026, 10, 12), D(2026, 10, 18)) == "12–18 окт."
    assert tx.short_span_text(D(2026, 9, 30), D(2026, 10, 6)) == "30 сент. – 6 окт."
    assert tx.short_span_text(D(2026, 5, 3), D(2026, 5, 3)) == "3 мая"


# ---- R3: leap years -------------------------------------------------------------------------------------------


def test_leap_year_previous_period():
    cur, p = prev(D(2028, 3, 1), D(2028, 3, 31), today=D(2028, 4, 15))                    # a whole March against the whole leap February
    assert not cur.in_progress and (p.first, p.last, p.days, p.label) == (D(2028, 2, 1), D(2028, 2, 29), 29, "февраль")
    cur, p = prev(D(2028, 3, 1), D(2028, 3, 31), today=D(2028, 3, 30))                    # a running March, 30 days elapsed: February has 29 -> cut to 29
    assert cur.in_progress and (p.first, p.last, p.label) == (D(2028, 2, 1), D(2028, 2, 29), "1–29 февраля")
    cur, p = prev(D(2028, 3, 1), D(2028, 3, 31), today=D(2028, 3, 29))                    # 29 elapsed: exactly the leap February
    assert (p.first, p.last) == (D(2028, 2, 1), D(2028, 2, 29))
    cur, p = prev(D(2028, 3, 1), D(2028, 3, 31), today=D(2028, 3, 10))                    # 10 elapsed: 1-10 February
    assert (p.first, p.last, p.label) == (D(2028, 2, 1), D(2028, 2, 10), "1–10 февраля")
    cur, p = prev(D(2028, 2, 1), D(2028, 2, 29), today=D(2028, 4, 15))                    # the leap February itself: the whole January
    assert cur.kind == "month" and cur.days == 29 and (p.first, p.last) == (D(2028, 1, 1), D(2028, 1, 31))
    cur, p = prev(D(2027, 3, 1), D(2027, 3, 31), today=D(2027, 4, 15))                    # the same March in a common year: 28 days
    assert (p.first, p.last) == (D(2027, 2, 1), D(2027, 2, 28))
    cur, p = prev(D(2028, 2, 24), D(2028, 3, 1), today=D(2028, 4, 15))                    # a custom week across the leap day: the seven days right before
    assert (p.first, p.last, p.days) == (D(2028, 2, 17), D(2028, 2, 23), 7)


# ---- R3: the time of a payment ------------------------------------------------------------------------------------


def test_paid_time_only_for_a_payment_entered_on_its_own_day():
    same_day = entry_times(datetime(2026, 10, 21, 6, 40), D(2026, 10, 21))                # 06:40 UTC = 11:40 Tashkent, same date
    assert same_day == ("11:40", None)
    back_dated = entry_times(datetime(2026, 10, 21, 6, 40), D(2026, 10, 15))              # entered on the 21st for the 15th
    assert back_dated == (None, "внесена 21.10 в 11:40")
    ahead = entry_times(datetime(2026, 10, 21, 6, 40), D(2026, 10, 25))                   # a payment for a later day
    assert ahead == (None, "внесена 21.10 в 11:40")
    assert entry_times(None, D(2026, 10, 21)) == (None, None)


def test_tashkent_midnight_boundary():
    """19:30 UTC is 00:30 of the NEXT day in Tashkent (UTC+5): the Tashkent date decides, not the UTC one."""
    late = datetime(2026, 10, 20, 19, 30)
    assert entry_times(late, D(2026, 10, 21)) == ("00:30", None)                            # paid_on = the Tashkent date: it has a time
    assert entry_times(late, D(2026, 10, 20)) == (None, "внесена 21.10 в 00:30")           # paid_on = the UTC date: entered after midnight, back-dated
    just_before = datetime(2026, 10, 20, 18, 59)                                           # 23:59 Tashkent, still the 20th
    assert entry_times(just_before, D(2026, 10, 20)) == ("23:59", None)
    assert entry_times(datetime(2026, 12, 31, 19, 0), D(2027, 1, 1)) == ("00:00", None)    # a year border
    assert entry_times(datetime(2026, 12, 31, 19, 0), D(2026, 12, 31)) == (None, "внесена 01.01 в 00:00")


def test_csv_formula_guard_covers_tab_and_cr():
    assert csv_safe_row(["=1+1", "+7", "-2", "@x", "\t=cmd", "\r=cmd", "ok", "a=b", "", 5, None, "-"]) == [
        "'=1+1", "'+7", "'-2", "'@x", "'\t=cmd", "'\r=cmd", "ok", "a=b", "", 5, None, "'-"]


# ---- S2: a running period says so and is compared day for day ----------------------------------------------------


def test_running_week_and_month_are_worded_as_running():
    cur, p = prev(D(2026, 10, 19), D(2026, 10, 25))                                       # Mon 19 - Sun 25, today Wed 21
    assert cur.in_progress and _lead(cur, TODAY) == "За эту неделю (пн–ср)"
    assert _previous_ref(cur, p, TODAY) == "за те же дни прошлой недели" and _previous_label(cur, p, TODAY) == "те же дни прошлой недели"
    assert (p.first, p.last) == (D(2026, 10, 12), D(2026, 10, 14))
    assert tx.income_summary_text(lead=_lead(cur, TODAY), received=2_700_000, previous_received=1_000_000, previous_ref=_previous_ref(cur, p, TODAY)) == (
        "За эту неделю (пн–ср) пришло 2 700 000 сум — на 170% больше, чем за те же дни прошлой недели.")
    monday = D(2026, 10, 19)
    cur2, _ = prev(D(2026, 10, 19), D(2026, 10, 25), today=monday)
    assert _lead(cur2, monday) == "За эту неделю (пн)"
    cur, p = prev(D(2026, 10, 1), D(2026, 10, 31))
    assert _lead(cur, TODAY) == "За октябрь (по 21-е)"
    assert _previous_ref(cur, p, TODAY) == "за те же дни сентября" and _previous_label(cur, p, TODAY) == "те же дни сентября"
    assert tx.short_span_text(p.first, p.last) == "1–21 сент."                              # the hint with the dates
    cur, p = prev(D(2026, 9, 1), D(2026, 9, 30))                                           # a closed month / week keep their names
    assert _lead(cur, TODAY) == "За сентябрь" and _previous_label(cur, p, TODAY) == "август"
    cur, p = prev(D(2026, 10, 12), D(2026, 10, 18))
    assert _lead(cur, TODAY) == "За неделю 12–18 октября" and _previous_label(cur, p, TODAY) == "5–11 окт."


# ---- S1: where the money went -------------------------------------------------------------------------------------


def test_the_kinds_of_a_payment_split_by_the_current_allocation():
    from types import SimpleNamespace as NS

    from src.auth import billing_ledger as bl

    def alloc(credit, bucket, segment, amount):
        return NS(credit_id=credit, bucket=bucket, segment_id=segment, amount=amount)

    segments = [NS(id="s1", begun=True), NS(id="s2", begun=True), NS(id="s3", begun=True), NS(id="s4", begun=False)]      # s3 starts after the payment but has begun since
    ledger = NS(allocations=[alloc("p1", bl.PAST_DEBT, "s1", 100), alloc("p1", bl.CURRENT, "s2", 200), alloc("p1", bl.ADVANCE, "s3", 300),
                             alloc("p1", bl.ADVANCE, "s4", 25), alloc("p1", bl.ADVANCE, bl.ADVANCE_SEGMENT, 15), alloc("p2", bl.CURRENT, "s2", 999)],
                segments=segments, refunded_from={"p1": 10}, transferred_from={"p1": 5})
    got = split_kinds(ledger, "p1")
    assert got == {"old_debt": 100, "current": 200, "forward": 300, "advance": 40, "refunded": 10, "transferred": 5}     # advance = not-begun period 25 + no period 15
    assert sum(got.values()) == 655                                                         # pieces + refunded + transferred = the payment
    parts = strip_parts(got, only_nonzero=True)
    assert [(x.kind, x.label, x.amount) for x in parts] == [
        ("old_debt", "на старые долги", 100), ("current", "за текущий период", 200), ("forward", "вперёд (за будущие периоды)", 300),
        ("advance", "пока не потрачено (аванс)", 40), ("refunded", "возвращено", 10), ("transferred", "перенесено в другую группу", 5)]
    assert [x.kind for x in strip_parts({"current": 5}, only_nonzero=False)] == ["old_debt", "current", "forward", "advance", "refunded", "transferred"]
    assert [x.kind for x in strip_parts({"current": 5}, only_nonzero=True)] == ["current"]
