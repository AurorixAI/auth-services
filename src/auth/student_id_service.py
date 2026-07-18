"""
ALGO — Генерация публичных Display-ID учеников (переиспользуемый сервис).

Display-ID — короткий человекочитаемый идентификатор, по которому ученик заходит
в «zero-friction» лобби тестов. Формат:  ``<ПРЕФИКС_ОРГАНИЗАЦИИ>-<NNN>``  →  ``AL15-001``.

Бизнес-правило (см. тикет): ID выдаётся строго в момент *фактического зачисления*
ученика, а не при его создании в буфере. Точки вызова:

  1. **Импорт (работает сейчас)** — по одному ID на каждого *нового* ученика при
     массовой загрузке. Уже существующие (Skip/Update) ученики НЕ перенумеровываются.
  2. **AssignToGroup (задел на будущее)** — когда админ переводит прошедшего
     диагностику ученика из буфера в реальную группу.

Нумерация — сквозная в пределах организации: ``sequence_number = max(существующих) + 1``.
Чтобы это было безопасно при параллельных импортах, перед чтением максимума берём
блокировку строки организации (``SELECT ... FOR UPDATE``) — два параллельных коммита
не смогут выдать один и тот же номер.

Модуль намеренно НЕ зависит от контроллеров/сервисов — только от ORM-моделей, чтобы
его можно было переиспользовать из любого flow (AC: логика вынесена отдельно и не
зашита в контроллер импорта).
"""
from __future__ import annotations

import re
from typing import Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.auth import Organization, Student

# Минимальная ширина порядкового номера: 001, 002, ... После 999 растёт сам (1000).
SEQ_PAD = 3
# Запасной префикс, если из названия организации ничего пригодного не выделилось.
PREFIX_FALLBACK = "ST"
# Ограничение длины префикса (совпадает с Organization.student_id_prefix VARCHAR(16)).
PREFIX_MAXLEN = 16


def _derive_prefix_from_name(name: str) -> str:
    """Детерминированный префикс из названия, если организация не задала свой.

    Правило (совпадает с SQL-бэкофиллом в миграции):
      * берём латинские буквы (до 4) в верхнем регистре,
      * затем цифры (до 4),
      * если ничего не осталось — ``PREFIX_FALLBACK``.

    Примеры::

        "Algo Learning"  -> "ALGO"
        "Школа 15"        -> "15"     (кириллица отбрасывается, остаются цифры)
        "AL15 Center"     -> "ALCENTER"? нет — буквы "ALCENTER"[:4]="ALCE" + ""   -> "ALCE"
        ""                -> "ST"

    Это лишь безопасный дефолт: рекомендуемый путь — явно задать
    ``Organization.student_id_prefix`` (например, "AL15").
    """
    letters = re.sub(r"[^A-Za-z]", "", name or "")[:4].upper()
    digits = re.sub(r"[^0-9]", "", name or "")[:4]
    prefix = f"{letters}{digits}"
    return (prefix[:PREFIX_MAXLEN]) if prefix else PREFIX_FALLBACK


def resolve_prefix(org: Organization) -> str:
    """Префикс организации: явный ``student_id_prefix`` или вывод из названия."""
    explicit = (org.student_id_prefix or "").strip().upper()
    if explicit:
        return explicit[:PREFIX_MAXLEN]
    return _derive_prefix_from_name(org.name or "")


def format_display_id(prefix: str, seq: int) -> str:
    """``("AL15", 1) -> "AL15-001"``; при seq > 999 ширина растёт естественно."""
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
    """Берём строку организации под ``FOR UPDATE`` — сериализуем выдачу номеров.

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
    seq = _current_max_sequence(db, org_id) + 1
    student.sequence_number = seq
    student.display_id = format_display_id(resolve_prefix(org), seq)
    db.flush()
    return student


class DisplayIdAllocator:
    """Выдаёт последовательные Display-ID в пределах одной организации.

    Оптимизирован под пакетную выдачу (массовый импорт): блокирует строку
    организации и читает максимум ОДИН раз при создании, далее каждый
    ``allocate()`` инкрементирует локальный счётчик — без похода в БД за
    максимумом на каждого ученика и без риска коллизии внутри пакета.

    Использовать внутри уже открытой транзакции (напр., ``import_service.commit``)::

        allocator = DisplayIdAllocator(db, org_id)
        for student in new_students:
            allocator.allocate(student)   # проставит sequence_number + display_id
        db.commit()
    """

    def __init__(self, db: Session, org_id):
        org = _lock_org(db, org_id)
        self._prefix = resolve_prefix(org)
        self._next = _current_max_sequence(db, org_id) + 1

    def allocate(self, student: Student) -> Student:
        # Никогда не перенумеровываем уже помеченного ученика (Skip/Update-семантика).
        if student.display_id:
            return student
        student.sequence_number = self._next
        student.display_id = format_display_id(self._prefix, self._next)
        self._next += 1
        return student
