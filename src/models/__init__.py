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
    GroupAudit,
    StudentGroup,
    DiagnosticInvite,
    StudentDiagnosticResult,
    GroupAttendance,
    GroupAttendanceAudit,
    AttendanceExcuse,
    AttendanceExcuseFile,
    GroupScheduleOverride,
    GroupScheduleOneOffLesson,
    GroupScheduleAudit,
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
    "GroupAudit",
    "StudentGroup",
    "DiagnosticInvite",
    "StudentDiagnosticResult",
    "GroupAttendance",
    "GroupAttendanceAudit",
    "AttendanceExcuse",
    "AttendanceExcuseFile",
    "GroupScheduleOverride",
    "GroupScheduleOneOffLesson",
    "GroupScheduleAudit",
    # Analytics (skill states written by internal callback)
    "AnswerEvent",
    "SessionSummary",
    "StudentSkillState",
]
