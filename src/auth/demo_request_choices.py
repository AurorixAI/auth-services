"""The closed lists of the demo request (codes and Russian words): one place for the validator, the letter and the console."""
from __future__ import annotations

from typing import Dict, Optional

ORG_TYPE_LABELS: Dict[str, str] = {  # the six types ``organizations`` accepts; the words of ``orgTypeLabel`` of the front
    "center": "Учебный центр",
    "private_school": "Частная школа",
    "university": "Университет",
    "tutoring_center": "Репетиторский центр",
    "tutor": "Репетитор",
    "other": "Другое",
}
POSITION_LABELS: Dict[str, str] = {"owner": "Владелец", "director": "Директор", "admin": "Администратор", "other": "Другое"}
NEEDS_LABELS: Dict[str, str] = {
    "payments": "Оплаты и долги",
    "attendance": "Посещаемость",
    "payroll": "Зарплата учителей",
    "diagnostics": "Диагностика",
    "exams": "Экзамены",
}
TOOL_LABELS: Dict[str, str] = {"excel": "Excel", "notebook": "Тетрадь", "other_system": "Другая система", "none": "Ничего"}
LOCALES = ("ru", "uz", "en")
LOCALE_LABELS: Dict[str, str] = {"ru": "Русский", "uz": "Узбекский", "en": "Английский"}

ORG_TYPES = tuple(ORG_TYPE_LABELS)
POSITIONS = tuple(POSITION_LABELS)
NEEDS = tuple(NEEDS_LABELS)
TOOLS = tuple(TOOL_LABELS)


def label(labels: Dict[str, str], code: Optional[str]) -> str:
    """The Russian word of a code; an unknown or empty code is never shown as Latin letters."""
    return labels.get(code or "", "")
