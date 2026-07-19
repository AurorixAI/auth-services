"""
ALGO — Генерация публичных Display-ID учеников (переиспользуемый сервис).

Display-ID — короткий человекочитаемый идентификатор, по которому ученик заходит
в «zero-friction» лобби тестов. Формат:  ``<ПРЕФИКС_ОРГАНИЗАЦИИ>-<NNN>``  →  ``IN-001``.

**Префикс организации** — инициалы названия (первые буквы первых двух слов), напр.
``New Castle`` → ``NC``; для однословных — первые две буквы (``Algo`` → ``AL``).
Кириллица транслитерируется в латиницу. Префикс **уникален между организациями**:
если инициалы уже заняты другой организацией, добавляется числовой суффикс
(``NC`` → ``NC1`` → ``NC2`` …). Один раз назначенный префикс за организацией
закрепляется (хранится в ``Organization.student_id_prefix``) и больше не меняется.

Бизнес-правило (см. тикет): ID выдаётся строго в момент *фактического зачисления*
ученика, а не при его создании в буфере. Точки вызова:

  1. **Импорт (работает сейчас)** — по одному ID на каждого *нового* ученика при
     массовой загрузке. Уже существующие (Skip/Update) ученики НЕ перенумеровываются.
  2. **AssignToGroup (задел на будущее)** — когда админ переводит прошедшего
     диагностику ученика из буфера в реальную группу.

Нумерация ученика — сквозная в пределах организации: ``sequence_number = max(...) + 1``.
Перед чтением максимума берётся блокировка строки организации (``SELECT ... FOR UPDATE``),
поэтому два параллельных импорта не выдадут одинаковый номер.

Модуль зависит только от ORM-моделей — переиспользуется из любого flow (AC: логика
вынесена отдельно и не зашита в контроллер импорта).
"""
from __future__ import annotations

import re

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.auth import Organization, Student

# Минимальная ширина порядкового номера: 001, 002, ... После 999 растёт сам (1000).
SEQ_PAD = 3
# Запасной префикс, если из названия не выделилось двух латинских букв.
PREFIX_FALLBACK = "ST"

# Базовая транслитерация кириллицы → латиница (нужен только первый символ).
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _latin_initial(ch: str) -> str:
    """Первая латинская буква одного символа (с транслитерацией кириллицы).

    ``"N" → "N"``, ``"Ш" → "S"`` (sh→s), ``"5" → ""``.
    """
    if not ch:
        return ""
    mapped = _TRANSLIT.get(ch[:1].lower(), ch[:1])
    mapped = re.sub(r"[^A-Za-z]", "", mapped)
    return mapped[:1].upper()


def _initials_from_name(name: str) -> str:
    """Базовый 2-буквенный префикс из названия организации.

    ``"Inter nation"``     → ``"IN"``
    ``"New CastleSchool"`` → ``"NC"``
    ``"Algo"``             → ``"AL"``   (одно слово → первые две буквы)
    ``"Школа Знаний"``     → ``"SZ"``   (транслитерация)
    пусто / без латиницы   → ``PREFIX_FALLBACK`` (``"ST"``).
    """
    words = [w for w in re.split(r"[^0-9A-Za-zА-Яа-яЁё]+", name or "") if w]
    if len(words) >= 2:
        base = _latin_initial(words[0][0]) + _latin_initial(words[1][0])
    elif len(words) == 1:
        w = words[0]
        base = _latin_initial(w[0]) + _latin_initial(w[1] if len(w) > 1 else w[0])
    else:
        base = ""
    base = re.sub(r"[^A-Z]", "", base.upper())
    if len(base) < 2:
        base = (base + PREFIX_FALLBACK)[:2]  # добить до 2 символов
    return base


def _unique_prefix(db: Session, base: str, exclude_org_id) -> str:
    """base, если свободен среди других организаций; иначе base1, base2, …"""
    taken = {
        p
        for (p,) in (
            db.query(Organization.student_id_prefix)
            .filter(
                Organization.student_id_prefix.isnot(None),
                Organization.id != exclude_org_id,
            )
            .all()
        )
        if p
    }
    if base not in taken:
        return base
    i = 1
    while f"{base}{i}" in taken:
        i += 1
    return f"{base}{i}"


def ensure_org_prefix(db: Session, org: Organization) -> str:
    """Префикс организации; при отсутствии — назначает уникальный и сохраняет.

    Уже заданный префикс (явно админом или ранее сгенерированный) не меняется.
    """
    if org.student_id_prefix:
        return org.student_id_prefix
    prefix = _unique_prefix(db, _initials_from_name(org.name or ""), org.id)
    org.student_id_prefix = prefix
    db.flush()
    return prefix


def format_display_id(prefix: str, seq: int) -> str:
    """``("IN", 1) -> "IN-001"``; при seq > 999 ширина растёт естественно."""
    return f"{prefix}-{seq:0{SEQ_PAD}d}"


def _current_max_sequence(db: Session, org_id) -> int:
    """Текущий максимум sequence_number в организации (0, если учеников с ID нет)."""
    value = (
        db.query(func.max(Student.sequence_number))
        .filter(Student.org_id == org_id)
        .scalar()
    )
    return int(value) if value is not None else 0


def _lock_org(db: Session, org_id) -> Organization:
    """Строка организации под ``FOR UPDATE`` — сериализуем выдачу номеров.

    Блокировка держится до конца транзакции вызывающего кода (commit/rollback).
    """
    org = (
        db.query(Organization)
        .filter(Organization.id == org_id)
        .with_for_update()
        .first()
    )
    if org is None:
        raise ValueError("Организация не найдена")
    return org


def assign_display_id(
    db: Session, org_id, student: Student, *, force: bool = False
) -> Student:
    """Выдать и привязать Display-ID одному ученику (момент зачисления).

    Идемпотентно: если у ученика уже есть ``display_id`` — он сохраняется
    (если только не ``force=True``). Транзакцию коммитит вызывающий код.

    Готово к вызову из будущего контроллера ``AssignToGroup`` и уже используется
    в ``AuthService.add_student_to_group``.
    """
    if student.display_id and not force:
        return student
    org = _lock_org(db, org_id)
    prefix = ensure_org_prefix(db, org)
    seq = _current_max_sequence(db, org_id) + 1
    student.sequence_number = seq
    student.display_id = format_display_id(prefix, seq)
    db.flush()
    return student


class DisplayIdAllocator:
    """Выдаёт последовательные Display-ID в пределах одной организации.

    Оптимизирован под пакетную выдачу (массовый импорт): блокирует строку
    организации, назначает/читает префикс и максимум ОДИН раз при создании,
    далее каждый ``allocate()`` инкрементирует локальный счётчик — без похода
    в БД за максимумом на каждого ученика и без риска коллизии внутри пакета.

    Использовать внутри уже открытой транзакции (напр., ``import_service.commit``)::

        allocator = DisplayIdAllocator(db, org_id)
        for student in new_students:
            allocator.allocate(student)   # проставит sequence_number + display_id
        db.commit()
    """

    def __init__(self, db: Session, org_id):
        org = _lock_org(db, org_id)
        self._prefix = ensure_org_prefix(db, org)
        self._next = _current_max_sequence(db, org_id) + 1

    def allocate(self, student: Student) -> Student:
        # Никогда не перенумеровываем уже помеченного ученика (Skip/Update-семантика).
        if student.display_id:
            return student
        student.sequence_number = self._next
        student.display_id = format_display_id(self._prefix, self._next)
        self._next += 1
        return student
