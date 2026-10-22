"""ALGO - IA fix pack: the edit of a student's profile (``PUT /org/students/{id}``).

* ``update_data``: which fields the request changes.  An omitted field stays; an explicit ``null`` (or ``""``) CLEARS the phone and the note (the two
  clearable fields); ``null`` of any other field is ignored (as before).  ``model_fields_set`` tells "omitted" from "null" - ``exclude_none`` could not.
* ``journal``: name, class, phone or note changed = one ``student_audit`` row: who, when, and ``old -> new`` per field.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from src.models.auth import StudentAudit

CLEARABLE = ("student_phone", "comment")
JOURNALED = ("first_name", "last_name", "class_level", "student_phone", "comment")
ACTION = "profile_updated"


def update_data(request) -> Dict[str, Any]:
    """The fields to write: the ones the caller sent (``model_fields_set``); ``None`` / ``""`` of a clearable field = clear it."""
    out: Dict[str, Any] = {}
    for key in request.model_fields_set:
        value = getattr(request, key)
        if key in CLEARABLE:
            out[key] = None if value is None or (isinstance(value, str) and not value.strip()) else value
        elif value is not None:
            out[key] = value
    return out


def snapshot(student) -> Dict[str, Any]:
    return {key: getattr(student, key) for key in JOURNALED}


def journal(db, student, before: Dict[str, Any], actor_id: Optional[str], actor_role: Optional[str]) -> Optional[StudentAudit]:
    """One journal row when a journaled field changed (no commit: the caller's transaction).  Nothing without an actor."""
    changes = {key: {"old": before[key], "new": getattr(student, key)} for key in JOURNALED if before[key] != getattr(student, key)}
    if not changes or not actor_id:
        return None
    row = StudentAudit(org_id=student.org_id, student_id=student.id, actor_id=actor_id,
                       actor_role=actor_role if actor_role in ("owner", "admin", "teacher", "superadmin") else "admin", action=ACTION, changes=changes)
    db.add(row)
    return row
