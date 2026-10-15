"""Strict identification of a student by ID and/or full name (exam entry).

Used by ``AuthService.verify_group_student`` (exam-service asks it who is
entering a group exam).  The old matching was dangerously loose - an ID matched
by suffix and a name by any substring, silently taking the first candidate - so
that typing «1» or «ал» entered as somebody else.  Here a student is identified
only when the input is *exact* after normalisation, and ambiguity is refused.

Pure functions over duck-typed students (``id``, ``display_id``,
``student_number``, ``first_name``, ``last_name``): no database access.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-", "_": "-"})
_DIGITS_ONLY = re.compile(r"[0-9]+")

MSG_NEED_FULL_NAME = "Введите имя и фамилию полностью или ID"
MSG_NAMESAKES = "В группе несколько учеников с таким именем — введите ваш ID (он есть у учителя и в карточке ученика)"
MSG_AMBIGUOUS_ID = "Несколько учеников подходят под этот ID — обратитесь к преподавателю."
MSG_NAME_MISMATCH = (
    "Введённое имя (ФИО) или Student ID не соответствуют ученику в этой группе. "
    "Пожалуйста, перепроверьте данные или обратитесь к преподавателю."
)


def normalize_name(value: Optional[str]) -> str:
    """trim, lowercase, ё→е, collapse whitespace."""
    return " ".join((value or "").strip().lower().replace("ё", "е").split())


def normalize_id(value: Optional[str]) -> str:
    """Like names, but spaces are dropped and ``-``/``_`` (and dash variants) are equal."""
    return "".join(normalize_name(value).split()).translate(_DASHES)


def name_tokens(value: Optional[str]) -> list[str]:
    return normalize_name(value).split()


def is_digits_only(id_norm: str) -> bool:
    return bool(_DIGITS_ONLY.fullmatch(id_norm))


def _strip_zeros(digits: str) -> str:
    return digits.lstrip("0") or "0"


def _display_number(display_id: Optional[str]) -> Optional[str]:
    """Numeric tail of ``TE-031`` -> ``31`` (leading zeros dropped); None if there is none."""
    norm = normalize_id(display_id)
    tail = norm.rsplit("-", 1)[-1] if "-" in norm else ""
    return _strip_zeros(tail) if _DIGITS_ONLY.fullmatch(tail) else None


def _exact_ids(student) -> set[str]:
    ids = {normalize_id(student.display_id), normalize_id(student.student_number), str(student.id).lower()}
    ids.discard("")
    return ids


def has_exact_id(students: Iterable, id_input: str) -> bool:
    id_norm = normalize_id(id_input)
    return bool(id_norm) and any(id_norm in _exact_ids(s) for s in students)


@dataclass
class Match:
    student: object = None
    message: Optional[str] = None  # set only when ``student`` is None and the message is specific


def match_by_id(students: Sequence, id_input: str) -> Match:
    """Exact display_id / student_number / UUID; digits-only may match a display number if unique."""
    id_norm = normalize_id(id_input)
    if not id_norm:
        return Match()
    exact = [s for s in students if id_norm in _exact_ids(s)]
    if len(exact) == 1:
        return Match(exact[0])
    if len(exact) > 1:
        return Match(message=MSG_AMBIGUOUS_ID)
    if not is_digits_only(id_norm):
        return Match()
    number = _strip_zeros(id_norm)
    numeric = sorted(
        (s for s in students if _display_number(s.display_id) == number),
        key=lambda s: normalize_id(s.display_id),
    )
    if len(numeric) == 1:
        return Match(numeric[0])
    if len(numeric) > 1:
        display = normalize_id(numeric[0].display_id)
        prefix = display.rsplit("-", 1)[0].upper() if "-" in display else "ID"
        return Match(
            message=(
                "Несколько учеников подходят под этот номер — "
                f"введите ID полностью, например {prefix}-{number.zfill(3)}"
            )
        )
    return Match()


def name_matches_owner(student, name_input: str) -> bool:
    """Every typed word must be the student's first or last name as a whole word."""
    typed = name_tokens(name_input)
    if not typed:
        return False
    own = set(name_tokens(student.first_name)) | set(name_tokens(student.last_name))
    return all(token in own for token in typed)


def match_by_name(students: Sequence, name_input: str) -> Match:
    """Full name (first + last, any order) as whole words; exactly one student."""
    typed = name_tokens(name_input)
    if len(typed) < 2:
        return Match(message=MSG_NEED_FULL_NAME)
    typed_set = set(typed)
    candidates = []
    for s in students:
        first, last = set(name_tokens(s.first_name)), set(name_tokens(s.last_name))
        # A student without both names can only enter by ID.
        if first and last and (first | last) <= typed_set:
            candidates.append(s)
    if len(candidates) == 1:
        return Match(candidates[0])
    if len(candidates) > 1:
        return Match(message=MSG_NAMESAKES)
    return Match()
