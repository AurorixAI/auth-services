"""
ALGO V1 — Auth ORM Models (moved from src/auth/models.py)

Tables: organizations, users, organization_members, invitations,
        students, groups, student_groups, diagnostic_invites,
        student_diagnostic_results
"""

from uuid import uuid4

from sqlalchemy import (
    Column,
    String,
    Integer,
    Boolean,
    DateTime,
    Text,
    ForeignKey,
    Index,
    CheckConstraint,
    UniqueConstraint,
    Numeric,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from src.models.base import Base


# ============================================================================
# ORGANIZATION
# ============================================================================


class Organization(Base):
    """Организация (учебный центр, школа, репетитор)"""

    __tablename__ = "organizations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String(300), nullable=False)
    org_type = Column(String(20), nullable=False, default="private_school")
    city = Column(String(200))
    address = Column(Text)
    phone = Column(String(30))
    email = Column(String(255))
    logo_url = Column(String(500))

    owner_id = Column(UUID(as_uuid=True), ForeignKey("users.id", use_alter=True))

    status = Column(String(20), nullable=False, default="pending")
    approved_by = Column(UUID(as_uuid=True))
    approved_at = Column(DateTime)
    rejection_reason = Column(Text)

    activation_token = Column(String(500), nullable=True)
    activation_token_expires_at = Column(DateTime, nullable=True)

    settings = Column(JSONB, default={})
    max_students = Column(Integer, default=500)
    max_teachers = Column(Integer, default=50)

    # Префикс для публичных ID учеников (display_id вида "AL15-001").
    # Если не задан — выводится из name (см. student_id_service.resolve_prefix).
    student_id_prefix = Column(String(16), nullable=True)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    members = relationship(
        "OrganizationMember", back_populates="organization", lazy="dynamic"
    )
    students = relationship("Student", back_populates="organization", lazy="dynamic")
    groups = relationship("Group", back_populates="organization", lazy="dynamic")
    invitations = relationship(
        "Invitation", back_populates="organization", lazy="dynamic"
    )

    __table_args__ = (
        CheckConstraint(
            "org_type IN ('center', 'private_school', 'university', 'tutoring_center', 'tutor', 'other')"
        ),
        CheckConstraint("status IN ('pending', 'active', 'suspended', 'rejected')"),
        # Префикс для публичных ID уникален между организациями (NC → NC1 → NC2 …).
        UniqueConstraint("student_id_prefix", name="uq_org_student_id_prefix"),
    )

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "name": self.name,
            "org_type": self.org_type,
            "city": self.city,
            "phone": self.phone,
            "email": self.email,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ============================================================================
# USER
# ============================================================================


class User(Base):
    """
    Сотрудник организации.
    Роли: owner, admin, teacher (ученики — отдельная таблица students)
    """

    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)

    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)

    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=False)
    patronymic = Column(String(100))

    role = Column(String(20), nullable=False, default="teacher")
    default_org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"))

    subjects = Column(JSONB, default=[])
    refresh_token_hash = Column(String(255))

    # Set on accounts provisioned with a temporary password (bulk import) —
    # the user must set their own password before using the app.
    must_change_password = Column(Boolean, nullable=False, default=False)

    is_active = Column(Boolean, default=True)
    is_verified = Column(Boolean, default=False)

    last_login_at = Column(DateTime)
    login_count = Column(Integer, default=0)
    language = Column(String(5), default="ru")
    settings = Column(JSONB, default={})

    # Legacy (backward compat)
    class_level = Column(Integer)
    school_name = Column(String(300))

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    default_org = relationship("Organization", foreign_keys=[default_org_id])
    memberships = relationship(
        "OrganizationMember",
        back_populates="user",
        foreign_keys="OrganizationMember.user_id",
        lazy="dynamic",
    )

    __table_args__ = (
        CheckConstraint("role IN ('owner', 'admin', 'teacher', 'superadmin')"),
        Index("idx_users_role", "role"),
        Index("idx_users_active", "is_active"),
        Index("idx_users_org", "default_org_id"),
    )

    @property
    def full_name(self) -> str:
        parts = [self.last_name, self.first_name]
        if self.patronymic:
            parts.append(self.patronymic)
        return " ".join(parts)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "email": self.email,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "full_name": self.full_name,
            "role": self.role,
            "default_org_id": str(self.default_org_id) if self.default_org_id else None,
            "language": self.language,
            "is_active": self.is_active,
            "is_verified": self.is_verified,
            "must_change_password": bool(self.must_change_password),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def to_dict_with_org(self, org_role: str = None, org_name: str = None) -> dict:
        d = self.to_dict()
        d["org_role"] = org_role
        d["org_name"] = org_name
        return d


# ============================================================================
# ORGANIZATION MEMBER
# ============================================================================


class OrganizationMember(Base):
    __tablename__ = "organization_members"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    org_role = Column(String(20), nullable=False, default="teacher")
    is_active = Column(Boolean, default=True)
    joined_at = Column(DateTime, default=func.now())
    invited_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))

    organization = relationship("Organization", back_populates="members")
    user = relationship("User", back_populates="memberships", foreign_keys=[user_id])

    __table_args__ = (
        CheckConstraint("org_role IN ('owner', 'admin', 'teacher')"),
        UniqueConstraint("org_id", "user_id", name="uq_org_member"),
    )


# ============================================================================
# INVITATION
# ============================================================================


class Invitation(Base):
    __tablename__ = "invitations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    invited_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    email = Column(String(255))
    org_role = Column(String(20), nullable=False, default="teacher")
    code = Column(String(20), nullable=False, unique=True)
    status = Column(String(20), nullable=False, default="pending")
    expires_at = Column(DateTime, nullable=False)
    accepted_at = Column(DateTime)
    accepted_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at = Column(DateTime, default=func.now())

    organization = relationship("Organization", back_populates="invitations")

    __table_args__ = (
        CheckConstraint("org_role IN ('admin', 'teacher')"),
        CheckConstraint("status IN ('pending', 'accepted', 'expired', 'cancelled')"),
    )

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "code": self.code,
            "email": self.email,
            "org_role": self.org_role,
            "status": self.status,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ============================================================================
# STUDENT
# ============================================================================


class Student(Base):
    """Ученик — запись без логина. Создаётся админом/учителем."""

    __tablename__ = "students"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )

    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=False)
    class_level = Column(Integer, nullable=False)

    group_id = Column(UUID(as_uuid=True), ForeignKey("groups.id", ondelete="SET NULL"))
    student_number = Column(String(30))
    student_phone = Column(String(30))  # optional — bulk import may omit it
    comment = Column(Text)

    # Публичный человекочитаемый ID для «zero-friction» лобби.
    # sequence_number — порядковый номер внутри организации (max+1).
    # display_id      — форматированный ID, напр. "AL15-001".
    # Оба заполняются в момент фактического зачисления (импорт / assign-to-group),
    # а не при создании в буфере, поэтому nullable. См. student_id_service.py.
    sequence_number = Column(Integer, nullable=True)
    display_id = Column(String(32), nullable=True)

    status = Column(String(20), nullable=False, default="new")
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    organization = relationship("Organization", back_populates="students")
    primary_group = relationship("Group", foreign_keys=[group_id])
    group_memberships = relationship(
        "StudentGroup", back_populates="student", lazy="dynamic"
    )
    diagnostic_results = relationship(
        "StudentDiagnosticResult", back_populates="student", lazy="dynamic"
    )
    diagnostic_invites = relationship(
        "DiagnosticInvite", back_populates="student", lazy="dynamic"
    )

    __table_args__ = (
        CheckConstraint("class_level >= 1 AND class_level <= 11"),
        CheckConstraint("status IN ('new', 'diagnosed', 'active', 'archived')"),
        Index("idx_students_org", "org_id"),
        Index("idx_students_status", "status"),
        Index("idx_students_display_id", "display_id"),
        # Порядковый номер и публичный ID уникальны в пределах организации.
        # NULL-ы в Postgres считаются различными → ученики в буфере (ещё без ID)
        # ограничение не нарушают.
        UniqueConstraint("org_id", "sequence_number", name="uq_student_org_sequence"),
        UniqueConstraint("org_id", "display_id", name="uq_student_org_display_id"),
    )

    @property
    def full_name(self) -> str:
        return f"{self.last_name} {self.first_name}"

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "org_id": str(self.org_id),
            "first_name": self.first_name,
            "last_name": self.last_name,
            "full_name": self.full_name,
            "class_level": self.class_level,
            "group_id": str(self.group_id) if self.group_id else None,
            "student_number": self.student_number,
            "display_id": self.display_id,
            "sequence_number": self.sequence_number,
            "student_phone": self.student_phone,
            "comment": self.comment,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ============================================================================
# GROUP
# ============================================================================


class Group(Base):
    __tablename__ = "groups"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String(200), nullable=False)
    class_level = Column(Integer)
    teacher_id = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    description = Column(Text)
    schedule = Column(JSONB)
    status = Column(String(20), default="active")
    max_students = Column(Integer, default=30)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    organization = relationship("Organization", back_populates="groups")
    teacher = relationship("User", foreign_keys=[teacher_id])
    student_memberships = relationship(
        "StudentGroup", back_populates="group", lazy="dynamic"
    )

    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')"),
        Index("idx_groups_org", "org_id"),
        Index("idx_groups_teacher", "teacher_id"),
    )

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "org_id": str(self.org_id),
            "name": self.name,
            "class_level": self.class_level,
            "teacher_id": str(self.teacher_id) if self.teacher_id else None,
            "status": self.status,
            "max_students": self.max_students,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ============================================================================
# STUDENT ↔ GROUP (M2M)
# ============================================================================


class StudentGroup(Base):
    __tablename__ = "student_groups"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    student_id = Column(
        UUID(as_uuid=True),
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
    )
    group_id = Column(
        UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), nullable=False
    )
    is_active = Column(Boolean, default=True)
    joined_at = Column(DateTime, default=func.now())
    left_at = Column(DateTime)

    student = relationship("Student", back_populates="group_memberships")
    group = relationship("Group", back_populates="student_memberships")

    __table_args__ = (
        UniqueConstraint("student_id", "group_id", name="uq_student_group"),
    )


# ============================================================================
# DIAGNOSTIC INVITE
# ============================================================================


class DiagnosticInvite(Base):
    """QR-код / ссылка на диагностику Модуль 1"""

    __tablename__ = "diagnostic_invites"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    student_id = Column(
        UUID(as_uuid=True),
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)

    code = Column(String(10), nullable=False, unique=True)
    class_level = Column(Integer, nullable=False)
    diagnostic_type = Column(String(20), default="initial")

    status = Column(String(20), nullable=False, default="pending")
    cat_session_id = Column(UUID(as_uuid=True))

    confirmed_at = Column(DateTime)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=func.now())

    student = relationship("Student", back_populates="diagnostic_invites")
    organization = relationship("Organization")

    __table_args__ = (
        CheckConstraint("diagnostic_type IN ('initial', 'recheck', 'pre_exam')"),
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'in_progress', 'completed', 'expired')"
        ),
    )

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "student_id": str(self.student_id),
            "code": self.code,
            "class_level": self.class_level,
            "diagnostic_type": self.diagnostic_type,
            "status": self.status,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ============================================================================
# STUDENT DIAGNOSTIC RESULT
# ============================================================================


class StudentDiagnosticResult(Base):
    """Результат диагностики — θ, ошибки, пробелы, рекомендации"""

    __tablename__ = "student_diagnostic_results"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    student_id = Column(
        UUID(as_uuid=True),
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
    )
    org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    invite_id = Column(UUID(as_uuid=True), ForeignKey("diagnostic_invites.id"))
    session_id = Column(UUID(as_uuid=True))

    diagnostic_type = Column(String(20), nullable=False, default="initial")

    final_theta = Column(Numeric(6, 4))
    se = Column(Numeric(6, 4))
    ci_low = Column(Numeric(6, 4))
    ci_high = Column(Numeric(6, 4))

    total_tasks = Column(Integer)
    correct_answers = Column(Integer)
    accuracy = Column(Numeric(5, 4))
    total_duration_ms = Column(Integer)

    identified_gaps = Column(JSONB, default=[])
    root_gaps = Column(JSONB, default=[])
    recommended_path = Column(JSONB, default=[])
    error_summary = Column(JSONB, default={})
    integrity_report = Column(JSONB, default={})

    recommended_level = Column(String(5))
    recommended_group_id = Column(UUID(as_uuid=True), ForeignKey("groups.id"))

    diagnosed_at = Column(DateTime, default=func.now())

    student = relationship("Student", back_populates="diagnostic_results")

    __table_args__ = (
        CheckConstraint(
            "diagnostic_type IN ('initial', 'recheck', 'pre_exam', 'exam')"
        ),
        Index("idx_diagres_student", "student_id"),
        Index("idx_diagres_org", "org_id"),
    )

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "student_id": str(self.student_id),
            "diagnostic_type": self.diagnostic_type,
            "final_theta": float(self.final_theta) if self.final_theta else None,
            "se": float(self.se) if self.se else None,
            "total_tasks": self.total_tasks,
            "correct_answers": self.correct_answers,
            "accuracy": float(self.accuracy) if self.accuracy else None,
            "identified_gaps": self.identified_gaps,
            "root_gaps": self.root_gaps,
            "error_summary": self.error_summary,
            "recommended_level": self.recommended_level,
            "diagnosed_at": self.diagnosed_at.isoformat()
            if self.diagnosed_at
            else None,
        }
