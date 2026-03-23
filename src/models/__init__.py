"""
ALGO V1 - SQLAlchemy Models
Central re-exports of all domain ORM models.
"""

from src.models.analytics import (
    AnswerEvent,
    ErrorPattern,
    SessionSummary,
    SkillCorrelation,
    SkillResponseTime,
    StudentSkillState,
)
from src.models.auth import (
    DiagnosticInvite,
    Group,
    Invitation,
    Organization,
    OrganizationMember,
    Student,
    StudentDiagnosticResult,
    StudentGroup,
    User,
)
from src.models.base import Base
from src.models.dataclasses import *  # noqa: F403
from src.models.graph import GraphUpdateLog, IRTCalibrationHistory  # noqa: F401

# Domain models
from src.models.knowledge import KnowledgeHierarchy, SkillBenchmark, SkillPrerequisite
from src.models.tasks import TaskMaster, TaskTemplateDB
from src.models.textbooks import (
    Textbook,
    TextbookSkillMap,
    TextbookStructure,
    TextbookTask,
    TextbookToc,
)

__all__ = [
    "Base",
    # Knowledge
    "KnowledgeHierarchy",
    "SkillPrerequisite",
    "SkillBenchmark",
    "ErrorPattern",
    # Tasks
    "TaskMaster",
    "TaskTemplateDB",
    # Textbooks
    "Textbook",
    "TextbookStructure",
    "TextbookTask",
    "TextbookToc",
    "TextbookSkillMap",
    # Analytics
    "AnswerEvent",
    "SkillCorrelation",
    "SessionSummary",
    "StudentSkillState",
    "SkillResponseTime",
    # Graph
    "GraphUpdateLog",
    "IrtCalibrationHistory",  # noqa: F405
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
]
