"""Strict identification of a student by ID and/or full name (exam entry).

Used by ``AuthService.verify_group_student`` (exam-service asks it who is
entering a group exam).  The old matching was dangerously loose - an ID matched
by suffix and a name by any substring, silently taking the first candidate - so
that typing «1» or «ал» entered as someone else.  Here a student is identified
only when the input is *exact* after normalisation, and ambiguity is refused.

Normalisation is applied to the stored value and to what was typed alike, so
the usual keyboard/typography differences do not matter (``Ma'rufjon`` =
``Ma’rufjon`` = ``Marufjon``, ``Иванова-Петрова`` = ``Иванова Петрова``, a
Cyrillic ``ТЕ-031`` = ``TE-031``) while a different person never matches.

Pure functions over duck-typed students (``id``, ``display_id``,
``student_number``, ``first_name``, ``last_name``): no database access.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

# Invisible characters that sneak in with copy/paste: ZWSP/ZWNJ/ZWJ, LRM/RLM (copied from messengers
# and right-to-left text), word joiner, BOM, soft hyphen.
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍‎‏⁠﻿­"))
# The apostrophe family is deleted: Ma'rufjon = Ma’rufjon = Marufjon, Oʻgʻiloy = O'g'iloy.
_APOSTROPHES = dict.fromkeys(map(ord, "'’‘ʼʻ`´"))
# Hyphens and dashes read as a space in names: Иванова-Петрова = Иванова Петрова.
_DASHES = "-‐‑‒–—―−"
_DASH_TO_SPACE = {ord(ch): " " for ch in _DASHES}
# Uzbek Cyrillic letters typed on a Russian keyboard (after lower-casing and ё -> е).
_UZBEK_FOLD = str.maketrans({"ғ": "г", "қ": "к", "ҳ": "х", "ў": "у"})
# Cyrillic look-alikes of Latin letters in an ID (А В Е К М Н О Р С Т Х), lower case.
_ID_LOOKALIKES = str.maketrans({"а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t", "х": "x"})
_ID_SEPARATORS = re.compile(r"[\s_" + re.escape(_DASHES) + r"]+")
_DIGITS_ONLY = re.compile(r"[0-9]+")

# Extra words a name-only entry may carry besides the student's first and last name.
MAX_EXTRA_NAME_WORDS = 2

MSG_NEED_FULL_NAME = "Введите имя и фамилию полностью или ID"
MSG_NAMESAKES = "В группе несколько учеников с таким именем — введите ваш ID (он есть у учителя и в карточке ученика)"
MSG_AMBIGUOUS_ID = "Несколько учеников подходят под этот ID — обратитесь к преподавателю."
# A fixed hint: it must never be built from a real student's ID.
MSG_AMBIGUOUS_NUMBER = (
    "Несколько учеников подходят под этот номер — "
    "введите ID полностью, как в карточке ученика (буквы и цифры)"
)
# Name-only entry found nobody: the ID of the student card is the way out.
MSG_NAME_NOT_FOUND = (
    "Ученик с именем «{name}» не найден в группе «{group}». "
    "Пожалуйста, перепроверьте написание или введите ID из карточки ученика. "
    "Если не получается — обратитесь к преподавателю."
)
MSG_NAME_MISMATCH = (
    "Введённое имя (ФИО) или Student ID не соответствуют ученику в этой группе. "
    "Пожалуйста, перепроверьте данные или обратитесь к преподавателю."
)


def _clean(value: Optional[str]) -> str:
    """NFKC (NFD «й» becomes «й»), invisible characters and apostrophes removed."""
    text = (value or "").translate(_INVISIBLE).translate(_APOSTROPHES)
    # NFKC can itself produce one of the characters above (U+00B4 -> space + accent), so clean again.
    return unicodedata.normalize("NFKC", text).translate(_INVISIBLE).translate(_APOSTROPHES)


def _punctuation_to_space(text: str) -> str:
    """«Иванов, Иван» / «Иванов;Иван» -> words.  Apostrophes are gone already, dashes became spaces."""
    return "".join(" " if unicodedata.category(ch).startswith("P") else ch for ch in text)


def normalize_name(value: Optional[str]) -> str:
    """Comparable form of a name: see the module docstring.  Words are separated by single spaces."""
    text = _clean(value).translate(_DASH_TO_SPACE).lower().replace("ё", "е").translate(_UZBEK_FOLD)
    return " ".join(_punctuation_to_space(text).split())


def normalize_id(value: Optional[str]) -> str:
    """Comparable form of an ID.

    Case-insensitive, Cyrillic look-alikes read as Latin, and any run of spaces, ``-``, ``_``
    or dash variants is *one* ``-`` (so «TE 031» = «TE_031» = «TE-031»).  The separator is
    kept - dropping it would make «NC1-001» and «NC10-01» the same ID.
    """
    text = _clean(value).lower().translate(_ID_LOOKALIKES)
    return _ID_SEPARATORS.sub("-", text).strip("-")


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
    """Exact display_id / student_number / UUID; a bare number also reads as a display number.

    For a bare number both readings are merged, and it enters only when they point at exactly
    one student: «31» is neither TE-031 for one student and student number 31 for another.
    """
    id_norm = normalize_id(id_input)
    if not id_norm:
        return Match()
    found = [s for s in students if id_norm in _exact_ids(s)]
    if is_digits_only(id_norm):
        number = _strip_zeros(id_norm)
        seen = {id(s) for s in found}
        found += [s for s in students if id(s) not in seen and _display_number(s.display_id) == number]
        if len(found) > 1:
            return Match(message=MSG_AMBIGUOUS_NUMBER)
    elif len(found) > 1:
        return Match(message=MSG_AMBIGUOUS_ID)
    return Match(found[0]) if found else Match()


def name_matches_owner(student, name_input: str) -> bool:
    """The typed name belongs to the owner of an already matched ID.

    Either the owner's first and last name are both there (extra words, e.g. a patronymic, are
    fine - the ID is the key), or every typed word is the owner's first or last name
    (e.g. only the surname).
    """
    typed = set(name_tokens(name_input))
    if not typed:
        return False
    first, last = set(name_tokens(student.first_name)), set(name_tokens(student.last_name))
    if first and last and first <= typed and last <= typed:
        return True
    return typed <= (first | last)


def match_by_name(students: Sequence, name_input: str) -> Match:
    """Full name (first + last, any order) as whole words; exactly one student.

    Students whose name words *equal* the typed words go first, so «Али Каримов» and
    «Али Акбар Каримов» each enter with their own exact name.  Failing that, the typed words
    may hold the whole name plus at most TWO extra words: a patronymic is often two words
    («Akram o'g'li», «Akram qizi») or one («Алиевич»).  Several students on either level ask
    for the ID.
    """
    typed = name_tokens(name_input)
    if len(typed) < 2:
        return Match(message=MSG_NEED_FULL_NAME)
    typed_set = set(typed)
    exact, covering = [], []
    for s in students:
        first, last = set(name_tokens(s.first_name)), set(name_tokens(s.last_name))
        # A student without both names can only enter by ID.
        if not (first and last):
            continue
        own = first | last
        if own == typed_set:
            exact.append(s)
        elif own <= typed_set and len(typed_set - own) <= MAX_EXTRA_NAME_WORDS:
            covering.append(s)
    if len(exact) == 1:
        return Match(exact[0])
    candidates = exact + covering
    if len(candidates) == 1:
        return Match(candidates[0])
    if len(candidates) > 1:
        return Match(message=MSG_NAMESAKES)
    return Match()
