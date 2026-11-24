"""PAY-TAILS Б11.33: ONE rule for the year in the name of a period (rows, rests and the advance are named by ``period_label``): the year is said when and only when the year of the period is NOT the year of the working day the probe is counted for."""
from datetime import date
from types import SimpleNamespace

from src.auth.payments_views import period_label

D = date


def month(year, m):
    first = D(year, m, 1)
    last = D(year + (m == 12), m % 12 + 1, 1).fromordinal(D(year + (m == 12), m % 12 + 1, 1).toordinal() - 1)
    return SimpleNamespace(kind="period", period_start=first, period_end=last, by_cell=True, package_no=None)


def test_the_year_is_said_only_when_it_is_not_the_year_of_the_working_day():
    dec, jan = month(2026, 12), month(2027, 1)
    today = D(2026, 12, 10)
    assert (period_label(dec, today), period_label(jan, today), period_label(jan, today, advance=True)) == ("Декабрь", "Январь 2027", "Аванс на январь 2027")
    today = D(2027, 1, 12)
    assert (period_label(dec, today), period_label(jan, today), period_label(dec, today, advance=True), period_label(jan, today, advance=True)) == (
        "Декабрь 2026", "Январь", "Аванс на декабрь 2026", "Аванс на январь")
