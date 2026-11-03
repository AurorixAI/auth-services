"""The name of a student as the journals show it: «Фамилия Имя», trimmed, «Ученик» when both are empty. One rule for every roster / list built outside the
large service module (SV-13 of wave E; the copies in ``service.py`` take it in E-D)."""
from __future__ import annotations

from typing import Any, Optional

FALLBACK_NAME = "Ученик"


def student_full_name(student: Optional[Any]) -> str:
    if student is None:
        return FALLBACK_NAME
    return f"{student.last_name or ''} {student.first_name or ''}".strip() or FALLBACK_NAME
