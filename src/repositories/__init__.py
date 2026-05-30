"""
ALGO V1 — Repositories Package

Re-exports all repository classes for convenient imports:

    from src.repositories import UserRepo, OrgRepo, TaskRepo, ...
"""

# Auth domain
from src.repositories.user_repo import UserRepo, OrgMemberRepo
from src.repositories.org_repo import OrgRepo, InvitationRepo
from src.repositories.student_repo import (
    StudentRepo,
    DiagnosticInviteRepo,
    StudentDiagnosticResultRepo,
)
from src.repositories.group_repo import GroupRepo, StudentGroupRepo

__all__ = [
    "UserRepo",
    "OrgMemberRepo",
    "OrgRepo",
    "InvitationRepo",
    "StudentRepo",
    "DiagnosticInviteRepo",
    "StudentDiagnosticResultRepo",
    "GroupRepo",
    "StudentGroupRepo",
]
