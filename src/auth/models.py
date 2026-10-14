"""
Legacy Auth Models Module.
These models have been migrated to src.models.auth.
This file now simply re-exports them to prevent breaking existing imports.
"""

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
    StudentPayment,
    GroupScheduleOverride,
    GroupScheduleOneOffLesson,
    GroupScheduleAudit,
)

__all__ = [
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
    "StudentPayment",
    "GroupScheduleOverride",
    "GroupScheduleOneOffLesson",
    "GroupScheduleAudit",
]
