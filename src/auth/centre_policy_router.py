"""LESSON-RULES С0: the routes of the rules of the lessons of a centre (mounted into the auth router: ``/api/v1/auth/org/settings/lessons``).  The owner's alone, like the payment settings."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from src.core.database import get_db

from . import centre_policy
from .centre_policy import CentrePolicy
from .dependencies import require_payments_settings

router = APIRouter(tags=["Centre rules"])


class LessonRulesUpdate(BaseModel):
    """``PUT /org/settings/lessons``: an absent field stays."""
    model_config = ConfigDict(extra="forbid")
    teacher_lesson_window_days: Optional[int] = None
    past_cancel_needs_confirmation: Optional[bool] = None


@router.get("/org/settings/lessons")
def get_lesson_rules(user: dict = Depends(require_payments_settings), db: Session = Depends(get_db)):
    """The rules of the lessons: the window of a teacher's own actions on a past lesson, the confirmation of a past cancellation (the value, the default, the allowed values, the words)."""
    return CentrePolicy.of(db, user["org_id"]).view()


@router.put("/org/settings/lessons")
def put_lesson_rules(request: LessonRulesUpdate, user: dict = Depends(require_payments_settings), db: Session = Depends(get_db)):
    """Change the rules (an absent field stays); acts for the actions after the change; every change goes to the journal."""
    return centre_policy.update(db, user["org_id"], user["user_id"], request.model_dump(exclude_none=True))


@router.get("/org/settings/lessons/history")
def lesson_rules_history(limit: int = Query(50, ge=1, le=200), user: dict = Depends(require_payments_settings), db: Session = Depends(get_db)):
    """The journal of the rules of the lessons: who, when, «было → стало»."""
    return centre_policy.history(db, user["org_id"], limit=limit)
