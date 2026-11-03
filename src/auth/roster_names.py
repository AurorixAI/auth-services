"""The name of a student as the journals show it: «Фамилия Имя», trimmed, «Ученик» when both are empty. One rule for every roster / list built outside the
large service module (SV-13 of wave E; the copies in ``service.py`` take it in E-D)."""
from __future__ import annotations

from typing import Any, Optional

FALLBACK_NAME = "Ученик"


def student_full_name(student: Optional[Any]) -> str:
    if student is None:
        return FALLBACK_NAME
    return f"{student.last_name or ''} {student.first_name or ''}".strip() or FALLBACK_NAME


def roster_payload(student: Any, *, id_key: str = "student_id") -> dict:
    """The six fields of a student in a roster / journal list.  ``id_key``: the snapshot of a lesson says ``student_id``, the lists of the group pages say ``id``
    (two contracts, kept apart on purpose)."""
    return {
        id_key: str(student.id),
        "first_name": student.first_name,
        "last_name": student.last_name,
        "full_name": student_full_name(student),
        "class_level": student.class_level,
        "display_id": student.display_id,
    }
