"""
ALGO - Payroll engine (N13 P3).  Pure functions, no database, no clock.

A payroll LINE is one membership (student x group) of one teacher in one calendar month:

    amount = round_half_up( sum over the paid lessons of price_lesson * p / 100 / D_lesson )

* ``price_lesson`` - the effective price of the student (group price or his individual one, ``pricing.py``) on the
  business day of THAT lesson (ROADMAP #105): a price raised from the 15th splits the month correctly.  Every mark of
  a line carries it (``Mark.price``); a line saved before #105 has marks without a price and uses ``source_fee`` for
  all of them.  The owner may overwrite the price on the sheet: one sum for every lesson of the line;
* ``p`` - the teacher's percent for the kind of lessons of the group, 0..100, at most 2 decimals;
* ``D_lesson`` - the divisor of ONE lesson: the centre's divisor (12 by default) for a lesson paid by
  a calendar payment, the size of the covering package for a lesson taken from a lessons package.  A
  line may mix both (a student who switched from months to packages), so the value of each lesson is
  ``fee * p / 100 / D_lesson`` and the line adds them up and rounds ONCE, to a whole sum, half up;
* the lessons: the ones the payments module counts (completed, the student is in the roster snapshot,
  present / late / absent, or an excused mark without a confirmed excuse) taught by this teacher, and
  covered by a payment.  A counted lesson that no payment covers is "не оплачено": it is shown but is
  not in the sum unless the owner ticks ``include_unpaid`` on the line.

Arithmetic.  Exact: percent is a ``Decimal``, money an ``int``, and the line is evaluated as ONE
rational number (``Fraction``) - every multiplication first, the divisions last, so 1/12 is never
truncated and a result that is exactly "xxx.5" is never turned into "xxx.4999...".  This is the
Decimal formula of the plan without its rounding noise.  The total of a sheet is the sum of the lines
(each already a whole sum).

The functions work on plain dicts (the JSON of a saved sheet): a saved line carries its marks, so a
change of fee / percent / "include unpaid" on a saved sheet is computed from the saved line alone
(``recompute_line``) and never reads attendance or payments again.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

LESSON_TYPES = ("group", "mini_group", "pair", "individual")
LESSON_TYPE_LABELS = {
    "group": "Группа",
    "mini_group": "Мини-группа",
    "pair": "Парный",
    "individual": "Индивидуально",
}

DEFAULT_DIVISOR = 12
MIN_DIVISOR = 1
MAX_DIVISOR = 31
MAX_FEE = 10_000_000_000  # the same bound as a payment amount

# State of one lesson column of a line.
COUNTED = "counted"
EXCUSED_CONFIRMED = "excused_confirmed"
EXCUSED_PENDING = "excused_pending"
NOT_IN_GROUP = "not_in_group"
UNPAID = "unpaid"
STATES = (COUNTED, EXCUSED_CONFIRMED, EXCUSED_PENDING, NOT_IN_GROUP, UNPAID)

# Warnings of a line.
NO_FEE = "NO_FEE"
EXCUSE_PENDING = "EXCUSE_PENDING"
UNPAID_LESSONS = "UNPAID_LESSONS"
PERCENT_REQUIRED = "PERCENT_REQUIRED"


class PercentError(ValueError):
    """The percent is not a number between 0 and 100 with at most two decimals."""


# ---------------------------------------------------------------- numbers ----


def parse_percent(value: Any) -> Decimal:
    """``"62.5"`` / ``62.5`` / ``50`` -> ``Decimal``; 0..100, at most 2 decimals, finite."""
    if isinstance(value, bool) or value is None:
        raise PercentError("Процент должен быть числом от 0 до 100")
    if isinstance(value, Decimal):
        number = value
    else:
        text = str(value).strip().replace(",", ".")
        if not text:
            raise PercentError("Укажите процент")
        try:
            number = Decimal(text)
        except InvalidOperation as exc:
            raise PercentError("Процент должен быть числом от 0 до 100") from exc
    if not number.is_finite():
        raise PercentError("Процент должен быть числом от 0 до 100")
    if number < 0 or number > 100:
        raise PercentError("Процент должен быть от 0 до 100")
    if number != number.quantize(Decimal("0.01")):
        raise PercentError("Процент — не более двух знаков после запятой")
    return number


def format_percent(percent: Decimal) -> str:
    """``Decimal('50.00')`` -> ``'50'``, ``Decimal('62.50')`` -> ``'62.5'`` (never an exponent)."""
    text = f"{percent.normalize():f}"
    return text if text not in ("", "-0") else "0"


def round_half_up(value: Fraction) -> int:
    """Whole sum nearest to ``value``, a half goes up (``floor(value + 1/2)``, exact)."""
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def line_amount(fee: Optional[int], percent: Decimal, divisors: Iterable[int]) -> int:
    """Amount of a line: ``fee * p / 100`` per lesson divided by that lesson's divisor, summed, rounded once.

    ``divisors`` has one entry per lesson that is in the sum.
    """
    if not fee:
        return 0
    per_lesson = Fraction(fee) * Fraction(percent) / 100
    total = Fraction(0)
    for divisor in divisors:
        if divisor < 1:
            raise ValueError("divisor must be positive")
        total += per_lesson / divisor
    return round_half_up(total)


def lessons_amount(percent: Decimal, lessons: Iterable[Tuple[Optional[int], int]]) -> int:
    """Amount of a line whose lessons may have DIFFERENT prices: ``sum(price * p / 100 / D)`` over ``(price, D)``,
    rounded once.  A lesson without a price (None) or with price 0 adds nothing."""
    share = Fraction(percent) / 100
    total = Fraction(0)
    for fee, divisor in lessons:
        if not fee:
            continue
        if divisor < 1:
            raise ValueError("divisor must be positive")
        total += Fraction(fee) * share / divisor
    return round_half_up(total)


# ---------------------------------------------------------------- lessons ----


@dataclass(frozen=True)
class Mark:
    """One lesson column of a line."""

    day: date
    lesson_id: str
    state: str
    divisor: Optional[int] = None  # D of the lesson when it can be in the sum (counted / pending / unpaid)
    # The price of the student on the day of THIS lesson (ROADMAP #105).  ``priced`` says the mark carries one
    # (``price`` None = not set that day); a mark of a sheet saved before #105 is not priced and the line's
    # ``source_fee`` stands for it.  ``price_source``: group | override | none.
    price: Optional[int] = None
    priced: bool = False
    price_source: Optional[str] = None

    @property
    def mark(self) -> Optional[int]:
        """The cell of the partner's table: 1 counted, 0 not counted (confirmed excuse), empty - not in the group."""
        if self.state == NOT_IN_GROUP:
            return None
        return 0 if self.state == EXCUSED_CONFIRMED else 1

    def as_dict(self) -> Dict[str, Any]:
        out = {
            "date": self.day.isoformat(),
            "lesson_id": self.lesson_id,
            "mark": self.mark,
            "state": self.state,
            "divisor": self.divisor,
        }
        if self.priced:
            out["price"] = self.price
            out["price_source"] = self.price_source
        return out

    @staticmethod
    def from_dict(item: Mapping[str, Any]) -> "Mark":
        return Mark(
            day=date.fromisoformat(item["date"]),
            lesson_id=str(item.get("lesson_id") or ""),
            state=item["state"],
            divisor=item.get("divisor"),
            price=item.get("price"),
            priced="price" in item,
            price_source=item.get("price_source"),
        )

    @property
    def carries_money(self) -> bool:
        """The lesson can be in the sum (counted / pending excuse) or is waiting for a payment (unpaid)."""
        return self.state in (COUNTED, EXCUSED_PENDING, UNPAID)


def price_steps(marks: Iterable[Mark]) -> List[List[Any]]:
    """``[[first day, price], ...]`` - where the price of the money lessons CHANGES along the month.

    Only priced marks that carry money take part; one entry per change (the first lesson at a new price).  A
    single entry (or none) means the price was the same for the whole line.
    """
    steps: List[List[Any]] = []
    last: Any = object()
    for mark in sorted((m for m in marks if m.priced and m.carries_money), key=lambda m: (m.day, m.lesson_id)):
        if not steps or mark.price != last:
            steps.append([mark.day.isoformat(), mark.price])
            last = mark.price
    return steps


def _warning(code: str, message: str) -> Dict[str, str]:
    return {"code": code, "message": message}


def compute_line(
    identity: Mapping[str, Any],
    marks: Sequence[Mark],
    *,
    source_fee: Optional[int],
    fee_override: Optional[int] = None,
    include_unpaid: bool = False,
    percent: Optional[Decimal] = None,
    pending_excused: int = 0,
    default_divisor: int = DEFAULT_DIVISOR,
    price_source: Optional[str] = None,
) -> Dict[str, Any]:
    """The JSON line of a sheet.

    ``identity``: membership_id, student_id, student_name, display_id, group_id, group_name,
    lesson_type, lesson_time.  ``source_fee`` is the price of the line on its reference day (the last lesson
    that carries money, else the last lesson) and ``price_source`` where it came from; the prices of the lessons
    themselves are on the marks.  ``fee_override`` replaces the price of EVERY lesson of the line on this sheet.
    ``percent`` None = not given: the amount is 0 and the line says why.
    """
    paid = [m for m in marks if m.state in (COUNTED, EXCUSED_PENDING)]
    unpaid = [m for m in marks if m.state == UNPAID]
    in_sum = paid + (unpaid if include_unpaid else [])
    money = sorted(paid + unpaid, key=lambda m: (m.day, m.lesson_id))

    def lesson_fee(mark: Mark) -> Optional[int]:
        if fee_override is not None:
            return fee_override
        return mark.price if mark.priced else source_fee

    last = money[-1] if money else None
    if fee_override is not None:
        fee = fee_override
    elif last is not None and last.priced:
        fee, price_source = last.price, last.price_source
    else:
        fee = source_fee
    steps = [] if fee_override is not None else price_steps(marks)
    no_price = sum(1 for m in money if lesson_fee(m) is None)
    unpaid_due = sum(1 for m in unpaid if lesson_fee(m) != 0)  # a free lesson (price 0) is paid by nobody
    amount = (
        lessons_amount(percent, [(lesson_fee(m), m.divisor or default_divisor) for m in in_sum])
        if percent is not None
        else 0
    )

    warnings: List[Dict[str, str]] = []
    if percent is None:
        warnings.append(_warning(PERCENT_REQUIRED, "Не указан процент преподавателя"))
    if fee is None or no_price:
        warnings.append(_warning(NO_FEE, "Не задана оплата в месяц"))
    if pending_excused:
        warnings.append(
            _warning(EXCUSE_PENDING, f"Уважительных пропусков ожидает подтверждения: {pending_excused}")
        )
    if unpaid_due:  # a free student (price 0) pays nothing, so "not paid" is no news
        tail = " (включены в расчёт)" if include_unpaid else ""
        warnings.append(_warning(UNPAID_LESSONS, f"Не оплачено уроков: {unpaid_due}{tail}"))

    used = {m.divisor or default_divisor for m in in_sum}
    line = dict(identity)
    line.update(
        {
            "monthly_fee": fee,
            "source_fee": source_fee,
            "fee_overridden": fee_override is not None,
            # where the price of the line comes from (group | override | none) and whether it changed inside the
            # month (then ``price_steps`` lists the first lesson ``date`` of every new price)
            "price_source": price_source,
            "price_changed": len(steps) > 1,
            "price_steps": [{"date": day, "amount": amount_} for day, amount_ in steps] if len(steps) > 1 else [],
            "no_price_lessons": no_price,
            "unpaid_due": unpaid_due,
            "percent": format_percent(percent) if percent is not None else None,
            # one D for the whole line, or None when lessons of both kinds are in it (each mark has its own)
            "divisor": next(iter(used)) if len(used) == 1 else (default_divisor if not used else None),
            "marks": [m.as_dict() for m in marks],
            "counted": len(paid),
            "unpaid": len(unpaid),
            "pending_excused": pending_excused,
            "include_unpaid": bool(include_unpaid),
            "amount": amount,
            "warnings": warnings,
        }
    )
    return line


def marks_of(line: Mapping[str, Any]) -> List[Mark]:
    return [Mark.from_dict(item) for item in line.get("marks", ())]


def recompute_line(
    line: Mapping[str, Any],
    *,
    percent: Optional[Decimal],
    fee_override: Optional[int],
    reset_fee: bool = False,
    include_unpaid: Optional[bool] = None,
    default_divisor: int = DEFAULT_DIVISOR,
) -> Dict[str, Any]:
    """The same line after the owner changed its fee / percent / "include unpaid" - from the saved marks only.

    ``fee_override`` None keeps the saved fee (overwritten or not); ``reset_fee`` drops an overwrite and
    goes back to the fee of the membership as it was when the line was calculated (``source_fee``).
    """
    if reset_fee:
        override = None
    elif fee_override is not None:
        override = fee_override
    else:
        override = line.get("monthly_fee") if line.get("fee_overridden") else None
    identity = {
        key: line.get(key)
        for key in (
            "membership_id", "student_id", "student_name", "display_id",
            "group_id", "group_name", "lesson_type", "lesson_time",
        )
    }
    return compute_line(
        identity,
        marks_of(line),
        source_fee=line.get("source_fee"),
        fee_override=override,
        include_unpaid=bool(line.get("include_unpaid")) if include_unpaid is None else include_unpaid,
        percent=percent,
        pending_excused=int(line.get("pending_excused") or 0),
        default_divisor=default_divisor,
        price_source=line.get("price_source"),
    )


# ----------------------------------------------------------------- sheets ----


def sheet_total(lines: Iterable[Mapping[str, Any]]) -> int:
    return sum(int(line["amount"]) for line in lines)


def sheet_counters(lines: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    """Totals and the counters behind the warnings of a sheet, from its lines."""
    return {
        "total_amount": sheet_total(lines),
        "counted": sum(int(line["counted"]) for line in lines),
        # lessons nobody paid for - not counting the free lessons (price 0): they owe nothing
        "unpaid": sum(_unpaid_due(line) for line in lines),
        "pending_excused": sum(int(line["pending_excused"]) for line in lines),
        # lines that have lessons to pay for but no price (a line without lessons costs nothing either way)
        "no_fee": sum(1 for line in lines if _lacks_price(line)),
    }


def _unpaid_due(line: Mapping[str, Any]) -> int:
    if line.get("unpaid_due") is not None:
        return int(line["unpaid_due"])
    return int(line["unpaid"]) if line.get("monthly_fee") != 0 else 0  # a line saved before #105


def _lacks_price(line: Mapping[str, Any]) -> bool:
    if line.get("no_price_lessons") is not None:
        return int(line["no_price_lessons"]) > 0
    return line.get("monthly_fee") is None and bool(line["counted"] or line["unpaid"])  # saved before #105


def group_blocks(lines: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Blocks of the sheet - one per group, in the order of the lines: the date columns and the subtotals."""
    blocks: Dict[str, Dict[str, Any]] = {}
    for line in lines:
        block = blocks.get(line["group_id"])
        if block is None:
            block = blocks[line["group_id"]] = {
                "group_id": line["group_id"],
                "group_name": line["group_name"],
                "lesson_type": line["lesson_type"],
                "lesson_time": line.get("lesson_time"),
                "percent": line.get("percent"),
                "dates": [],
                "line_count": 0,
                "counted": 0,
                "unpaid": 0,
                "total_amount": 0,
            }
            block["_seen"] = set()
        for mark in line.get("marks", ()):
            key = (mark["date"], mark["lesson_id"])
            if key not in block["_seen"]:
                block["_seen"].add(key)
                block["dates"].append({"date": mark["date"], "lesson_id": mark["lesson_id"]})
        block["line_count"] += 1
        block["counted"] += int(line["counted"])
        block["unpaid"] += int(line["unpaid"])
        block["total_amount"] += int(line["amount"])
    result = []
    for block in blocks.values():
        block.pop("_seen")
        block["dates"].sort(key=lambda item: (item["date"], item["lesson_id"]))
        result.append(block)
    return result


# ------------------------------------------------------------------- diff ----

_COMPARED = ("monthly_fee", "percent", "counted", "unpaid", "pending_excused", "amount", "price_steps")
_LABELS = {
    "monthly_fee": "оплата",
    "percent": "процент",
    "counted": "засчитано",
    "unpaid": "не оплачено",
    "pending_excused": "уважительные ожидают",
    "amount": "начислено",
    "price_steps": "цена по дням изменилась",  # only the per-day prices differ (same last price, same total)
}


def _value(line: Mapping[str, Any], key: str) -> Any:
    """A compared field; a sheet saved before #105 has no ``price_steps``: that is the same as no change of the price."""
    value = line.get(key)
    return (value or []) if key == "price_steps" else value


def _marks_key(line: Mapping[str, Any]) -> list:
    """The lessons that mean something for the student; a blank column (not in the group) is not a change."""
    return sorted(
        (m["date"], m["lesson_id"], m["state"], m.get("divisor") or 0)
        for m in line.get("marks", ())
        if m["state"] != NOT_IN_GROUP
    )


def diff_lines(
    saved: Sequence[Mapping[str, Any]], fresh: Sequence[Mapping[str, Any]]
) -> List[Dict[str, Any]]:
    """Lines that differ between a saved sheet and a fresh calculation, by membership.

    ``status``: ``changed`` / ``added`` (only in the fresh calculation) / ``removed`` (only in the sheet).
    Unchanged lines are not listed.
    """
    old = {line["membership_id"]: line for line in saved}
    new = {line["membership_id"]: line for line in fresh}
    out: List[Dict[str, Any]] = []

    def brief(line: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
        return None if line is None else {key: _value(line, key) for key in _COMPARED}

    for membership_id in sorted(set(old) | set(new), key=lambda k: (_sort_name(old.get(k) or new.get(k)), k)):
        a, b = old.get(membership_id), new.get(membership_id)
        if a is not None and b is not None:
            changes = [key for key in _COMPARED if _value(a, key) != _value(b, key)]
            if not changes and _marks_key(a) != _marks_key(b):
                changes = ["marks"]
            if not changes:
                continue
            status = "changed"
        else:
            status = "added" if a is None else "removed"
            changes = []
        ref = b if b is not None else a
        out.append(
            {
                "membership_id": membership_id,
                "student_name": ref.get("student_name"),
                "group_name": ref.get("group_name"),
                "status": status,
                "changes": [_LABELS.get(key, "отметки") for key in changes],
                "saved": brief(a),
                "fresh": brief(b),
                "delta_amount": int((b or {}).get("amount", 0)) - int((a or {}).get("amount", 0)),
            }
        )
    return out


def _sort_name(line: Optional[Mapping[str, Any]]) -> str:
    return f"{(line or {}).get('group_name') or ''}\u0000{(line or {}).get('student_name') or ''}"
