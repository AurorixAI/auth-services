"""INCOME-2: periods, the comparison with the previous period and the Russian sentences (pure functions, no database)."""
from datetime import date

import pytest

from src.auth import payments_texts as tx
from src.auth.payments_income import _previous_dative, month_end, previous_period, resolve_period
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
    assert text(previous_received=0) == "За октябрь пришло 7 885 000 сум; за сентябрь оплат не было."
    assert text(received=0) == "За октябрь оплат не было; за сентябрь пришло 7 040 000 сум."
    assert text(received=0, previous_received=0, collected_pct=0) == "За октябрь оплат не было. Собрано 0% стоимости месяца."
    assert tx.income_summary_text(lead="Сегодня", received=1_200_000, previous_received=1_000_000, previous_ref="вчера") == (
        "Сегодня пришло 1 200 000 сум — на 20% больше, чем вчера.")


def test_allocation_summary_joins_pieces_in_the_owners_words():
    assert tx.allocation_summary([("за сентябрь", 200_000), ("аванс", 50_000)]) == "за сентябрь — 200 000 · аванс — 50 000"
    assert tx.allocation_summary([("за сентябрь", 100_000), ("за сентябрь", 100_000), ("возвращено", 0)]) == "за сентябрь — 200 000"
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
