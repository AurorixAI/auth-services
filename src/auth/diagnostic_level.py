"""Single source of truth for reading a student's diagnostic level in auth-service.

diagnostic-service decides the level once (``level_policy.classify``) and sends the
finished codes in the completion payload; auth stores and serves them. Rows saved
before that contract carry no codes, so for them -- and only them -- the codes are
derived from theta by ``classify_theta`` below, using the same cut-offs as
diagnostic-service:

    category: theta >= 1.0 above_grade, theta >= -0.5 at_grade, else below_grade
    status:   theta < -1.4 critical, < -0.5 below, < 0.1 partial_gap,
              < 1.0 on_level, else above
"""
from __future__ import annotations

from typing import Any, Optional

OVERALL_LEVELS = ("below_grade", "at_grade", "above_grade")
LEVEL_STATUSES = ("critical", "below", "partial_gap", "on_level", "above")
CLASS_EQUIVALENT_MAX_LEN = 64


def theta_to_category(theta: float) -> str:
    if theta >= 1.0:
        return "above_grade"
    if theta >= -0.5:
        return "at_grade"
    return "below_grade"


def theta_to_status(theta: float) -> str:
    if theta < -1.4:
        return "critical"
    if theta < -0.5:
        return "below"
    if theta < 0.1:
        return "partial_gap"
    if theta < 1.0:
        return "on_level"
    return "above"


def classify_theta(theta: Optional[float]) -> tuple[Optional[str], Optional[str]]:
    """(category, status) for a theta, or (None, None) when theta is unknown."""
    if theta is None:
        return None, None
    return theta_to_category(theta), theta_to_status(theta)


def normalize_level_payload(payload: dict) -> dict:
    """Pick the level fields out of a completion payload, dropping anything invalid.

    Older diagnostic-service versions do not send them; unknown codes are dropped
    (not rejected) so a vocabulary mismatch can never block saving the result.
    """
    overall = payload.get("overall_level")
    status = payload.get("level_status")
    equivalent = payload.get("class_equivalent")
    provisional = payload.get("level_provisional")
    return {
        "overall_level": overall if overall in OVERALL_LEVELS else None,
        "level_status": status if status in LEVEL_STATUSES else None,
        "class_equivalent": (
            equivalent.strip()[:CLASS_EQUIVALENT_MAX_LEN]
            if isinstance(equivalent, str) and equivalent.strip()
            else None
        ),
        "level_provisional": provisional if isinstance(provisional, bool) else None,
    }


def level_fields(result: Any) -> dict:
    """Level fields of a StudentDiagnosticResult for API responses.

    Stored values win; missing codes fall back to theta. ``class_equivalent`` and
    ``level_provisional`` cannot be derived from theta alone, so they stay None for
    rows saved before the contract (the UI then simply omits them).
    """
    theta = getattr(result, "final_theta", None)
    theta = float(theta) if theta is not None else None
    category, status = classify_theta(theta)
    return {
        "recommended_level": getattr(result, "overall_level", None) or category,
        "level_status": getattr(result, "level_status", None) or status,
        "class_equivalent": getattr(result, "class_equivalent", None),
        "level_provisional": getattr(result, "level_provisional", None),
    }
