"""One place for the formula-injection rule of every export (MN-10, MN-43).

A text cell that starts with ``= + - @`` (or a TAB / CR, which Excel also lets through to the formula parser) must never be run as a formula
when somebody opens the file (OWASP "CSV injection").

* CSV: the cell gets a leading apostrophe (``csv_safe_row``); the rows are written with the ``csv`` module, i.e. RFC 4180 quoting.
* xlsx: a text cell is stored as a string (``t="s"``) AND carries the ``quotePrefix`` flag of its style (``xlsx_style.put``): Excel shows the
  text as it is («=Тест», «-Анвар») without any apostrophe and does not turn it into a formula when the cell is edited.

Numbers and dates are not text: they are written as numbers / dates and are not touched.
"""
from __future__ import annotations

from typing import Any, List, Sequence

FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def formula_like(value: Any) -> bool:
    """A text that a spreadsheet could read as a formula (also after leading blanks)."""
    if not isinstance(value, str) or not value:
        return False
    return value[:1] in FORMULA_STARTS or value.lstrip(" ")[:1] in FORMULA_STARTS


def csv_safe_cell(value: Any) -> Any:
    return "'" + value if formula_like(value) else value


def csv_safe_row(row: Sequence[Any]) -> List[Any]:
    """The row with every dangerous TEXT cell prefixed by an apostrophe; numbers, dates and None stay as they are."""
    return [csv_safe_cell(v) for v in row]
