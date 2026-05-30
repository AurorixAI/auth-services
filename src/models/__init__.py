"""
ALGO Auth Service — SQLAlchemy Models
Only auth-service–owned tables.
"""

from src.models.base import Base
from src.models.auth import (
    Organization,
    User,
    OrganizationMember,
    Invitation,
    Student,
    Group,
    StudentGroup,
    DiagnosticInvite,
    StudentDiagnosticResult,
)
from src.models.analytics import (
    AnswerEvent,
    SessionSummary,
    StudentSkillState,
)

__all__ = [
    "Base",
    # Auth
    "Organization",
    "User",
    "OrganizationMember",
    "Invitation",
    "Student",
    "Group",
    "StudentGroup",
    "DiagnosticInvite",
    "StudentDiagnosticResult",
    # Analytics (skill states written by internal callback)
    "AnswerEvent",
    "SessionSummary",
    "StudentSkillState",
]
