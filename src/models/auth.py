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
    LargeBinary,
    BigInteger,
    SmallInteger,
    Date,
    text,
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

    # Префикс для публичных ID учеников (display_id вида "NC-001").
    # Если не задан — инициалы из name, уникальные между орг-ми (NC → NC1 → NC2);
    # см. student_id_service.ensure_org_prefix.
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
    # "Ученик ушёл из центра": when (naive UTC, the instant the memberships were closed) and why the
    # student went to the archive; both are cleared on return.  Students archived before the action existed
    # were stamped by the migration (``archived_at`` = their ``updated_at`` in naive UTC, no reason).
    archived_at = Column(DateTime, nullable=True)
    archive_reason = Column(String(300), nullable=True)
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
    # N13 P3: the kind of lessons (the payroll percent is set per teacher and kind).  Existing
    # groups are "group".  Changing it is written to ``group_audit`` like any other field.
    lesson_type = Column(String(16), nullable=False, default="group", server_default="group")

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    organization = relationship("Organization", back_populates="groups")
    teacher = relationship("User", foreign_keys=[teacher_id])
    student_memberships = relationship(
        "StudentGroup", back_populates="group", lazy="dynamic"
    )

    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')"),
        CheckConstraint(
            "lesson_type IN ('group', 'mini_group', 'pair', 'individual')",
            name="ck_groups_lesson_type",
        ),
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
            "description": self.description,
            "schedule": self.schedule or [],
            "status": self.status,
            "max_students": self.max_students,
            "lesson_type": self.lesson_type or "group",
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class GroupAudit(Base):
    """Immutable provenance for group creation and future lifecycle events."""

    __tablename__ = "group_audit"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    group_id = Column(
        UUID(as_uuid=True),
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    actor_role = Column(String(20), nullable=False)
    action = Column(String(30), nullable=False)
    assigned_teacher_id = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    snapshot = Column(JSONB, nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "action IN ('created', 'updated', 'teacher_changed', 'student_archived', 'student_restored')",
            name="ck_group_audit_action",
        ),
        CheckConstraint(
            "actor_role IN ('owner', 'admin', 'teacher', 'superadmin')",
            name="ck_group_audit_actor_role",
        ),
        Index("idx_group_audit_group", "group_id", "created_at"),
        Index("idx_group_audit_org", "org_id", "created_at"),
    )


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

    # This acknowledgement belongs to the concrete membership, rather than
    # to the student globally: a transfer must not silently hide the initial
    # diagnostic from the teacher of the new group.
    initial_diagnostic_reviewed_invite_id = Column(UUID(as_uuid=True))
    initial_diagnostic_reviewed_at = Column(DateTime)
    initial_diagnostic_reviewed_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))

    # "Оплата в месяц" in whole sums.  NULL = not set, 0 = free.  Money is
    # never returned by student/group endpoints - only by the payment API.
    monthly_fee = Column(BigInteger, nullable=True)
    monthly_fee_updated_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    monthly_fee_updated_at = Column(DateTime, nullable=True)

    # Set when a transfer makes an *already active* membership the primary one:
    # its joined_at must stay (rosters of past lessons depend on it), so the
    # journal reads this instant to recognise the transfer.
    transferred_at = Column(DateTime, nullable=True)

    student = relationship("Student", back_populates="group_memberships")
    group = relationship("Group", back_populates="student_memberships")

    __table_args__ = (
        UniqueConstraint("student_id", "group_id", name="uq_student_group"),
        CheckConstraint("monthly_fee >= 0", name="ck_student_groups_monthly_fee"),
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

    # Legacy VARCHAR(5), never filled and too short for the level codes; kept as is.
    recommended_level = Column(String(5))
    # Level decided once by diagnostic-service (level_policy) and stored as-is.
    # NULL on rows saved before the contract: readers fall back to theta
    # (src/auth/diagnostic_level.py).
    overall_level = Column(String(16))
    level_status = Column(String(16))
    class_equivalent = Column(String(64))
    level_provisional = Column(Boolean)
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
            "overall_level": self.overall_level,
            "level_status": self.level_status,
            "class_equivalent": self.class_equivalent,
            "level_provisional": self.level_provisional,
            "diagnosed_at": self.diagnosed_at.isoformat()
            if self.diagnosed_at
            else None,
        }


# ============================================================================
# GROUP ATTENDANCE
# ============================================================================


class GroupAttendance(Base):
    """Посещаемость уроков группы по датам"""

    __tablename__ = "group_attendance"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    group_id = Column(
        UUID(as_uuid=True),
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
    )
    lesson_date = Column(String(10), nullable=False)  # "YYYY-MM-DD"
    lesson_id = Column(String(100))
    topic = Column(String(300))
    status = Column(String(20), default="in_progress", nullable=False)  # "in_progress" | "completed" | "cancelled"
    records = Column(JSONB, default={})  # { student_id: "present" | "absent" | "late" | "excused" }
    # Immutable composition of the class at the moment the lesson begins.
    # Membership can change later, but it must never rewrite past attendance.
    roster_snapshot = Column(JSONB, nullable=True, default=list)
    marked_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    # The group's teacher at the moment the lesson row was created.  Groups
    # carry no teacher history, so this snapshot is the only durable record of
    # who taught a lesson; changing ``Group.teacher_id`` never rewrites it.
    teacher_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    # Server timestamp of the explicit teacher action "Start lesson".  It is
    # deliberately distinct from ``created_at`` so future analytics never
    # mistake a draft/scheduled occurrence for delivered teaching time.
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    cancellation_reason = Column(String(500), nullable=True)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    group = relationship("Group")

    __table_args__ = (
        UniqueConstraint("group_id", "lesson_date", "lesson_id", name="uq_group_lesson_date_id"),
        CheckConstraint(
            "status IN ('in_progress', 'completed', 'cancelled')",
            name="ck_group_attendance_status",
        ),
        Index("idx_attendance_group", "group_id"),
        Index("idx_attendance_org", "org_id"),
        Index("idx_attendance_teacher", "org_id", "teacher_id", "lesson_date"),
    )

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "org_id": str(self.org_id),
            "group_id": str(self.group_id),
            "lesson_date": self.lesson_date,
            "lesson_id": self.lesson_id,
            "topic": self.topic,
            "status": self.status or "completed",
            "records": self.records or {},
            "roster_snapshot": self.roster_snapshot or [],
            "marked_by": str(self.marked_by) if self.marked_by else None,
            "teacher_id": str(self.teacher_id) if self.teacher_id else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "cancellation_reason": self.cancellation_reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class GroupAttendanceAudit(Base):
    """Неизменяемый журнал создания и корректировок посещаемости."""

    __tablename__ = "group_attendance_audit"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    attendance_id = Column(
        UUID(as_uuid=True),
        ForeignKey("group_attendance.id", ondelete="CASCADE"),
        nullable=False,
    )
    org_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    group_id = Column(
        UUID(as_uuid=True),
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    action = Column(String(30), nullable=False)  # started | reconciliation_started | updated | corrected | cancelled | excuse_*
    before_status = Column(String(20))
    after_status = Column(String(20), nullable=False)
    before_records = Column(JSONB)
    after_records = Column(JSONB, nullable=False)
    correction_reason = Column(String(500))
    cancellation_reason = Column(String(500))
    # Structured facts of the event (excuse decisions, files); ids only, no names.
    details = Column(JSONB, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "action IN ('started', 'reconciliation_started', 'created', 'updated', 'corrected', 'cancelled', 'reconciliation_cancelled', "
            "'excuse_reviewed', 'excuse_reset', 'excuse_removed', 'excuse_file_added', 'excuse_file_removed')",
            name="ck_group_attendance_audit_action",
        ),
        Index("idx_group_attendance_audit_attendance", "attendance_id"),
        Index("idx_group_attendance_audit_group", "group_id"),
    )


class AttendanceExcuse(Base):
    """Причина уважительного пропуска и решение администратора по ней.

    Запись создаётся при отметке ``excused`` и живёт, пока ученик остаётся
    ``excused`` на этом уроке: смена отметки удаляет запись вместе с файлами.
    """

    __tablename__ = "attendance_excuses"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    attendance_id = Column(
        UUID(as_uuid=True),
        ForeignKey("group_attendance.id", ondelete="CASCADE"),
        nullable=False,
    )
    student_id = Column(UUID(as_uuid=True), nullable=False)
    reason = Column(String(300), nullable=False)
    reason_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    reason_at = Column(DateTime, nullable=False)
    status = Column(String(12), nullable=False, default="pending", server_default="pending")
    reviewed_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    review_note = Column(String(500), nullable=True)

    files = relationship(
        "AttendanceExcuseFile",
        back_populates="excuse",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint("attendance_id", "student_id", name="uq_attendance_excuse_student"),
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'rejected')",
            name="ck_attendance_excuse_status",
        ),
        Index("idx_attendance_excuses_org_status", "org_id", "status"),
    )


class AttendanceExcuseFile(Base):
    """Справка к уважительному пропуску (JPEG/PNG/PDF, до 2 МБ, хранится в БД)."""

    __tablename__ = "attendance_excuse_files"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id = Column(UUID(as_uuid=True), nullable=False)
    excuse_id = Column(
        UUID(as_uuid=True),
        ForeignKey("attendance_excuses.id", ondelete="CASCADE"),
        nullable=False,
    )
    filename = Column(String(200))
    content_type = Column(String(50), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    data = Column(LargeBinary, nullable=False)
    uploaded_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    uploaded_at = Column(DateTime, nullable=False)

    excuse = relationship("AttendanceExcuse", back_populates="files")

    __table_args__ = (
        CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 2097152",
            name="ck_attendance_excuse_file_size",
        ),
        Index("idx_attendance_excuse_files_excuse", "excuse_id"),
    )


class GroupScheduleOverride(Base):
    """Версия, перенос или управляемое прекращение регулярного расписания.

    Исходный ``Group.schedule`` остаётся базовым шаблоном. Эта таблица хранит
    только изменения, поэтому завершённые занятия и их посещаемость никогда не
    переписываются при переносе или изменении регулярного времени.
    """

    __tablename__ = "group_schedule_overrides"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    group_id = Column(UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    lesson_id = Column(String(100), nullable=False)
    # ``recurring_end`` is a dated tombstone.  It never removes the base slot:
    # past attendance and audit history remain resolvable while future
    # occurrences of this recurring lesson stop being generated.
    scope = Column(String(20), nullable=False)  # occurrence | recurring | recurring_end
    effective_date = Column(String(10), nullable=False)  # YYYY-MM-DD
    payload = Column(JSONB, nullable=False)
    reason = Column(String(500), nullable=False)
    changed_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "group_id", "lesson_id", "scope", "effective_date",
            name="uq_group_schedule_override_scope_date",
        ),
        CheckConstraint(
            "scope IN ('occurrence', 'recurring', 'recurring_end')",
            name="ck_group_schedule_override_scope",
        ),
        Index("idx_group_schedule_override_lookup", "group_id", "lesson_id", "effective_date"),
        Index("idx_group_schedule_override_org", "org_id"),
    )


class GroupScheduleOneOffLesson(Base):
    """Отдельное занятие, существующее только в указанную дату.

    Разовый урок не добавляется в ``Group.schedule``: этот JSON-массив хранит
    исключительно постоянные правила. Благодаря отдельной сущности создание
    занятия на текущую неделю никогда не создаёт повторяющийся урок на
    следующих неделях.
    """

    __tablename__ = "group_schedule_one_off_lessons"

    # String ID keeps the identifier compatible with schedule occurrences,
    # attendance and exam bindings, which already use lesson_id strings.
    id = Column(String(100), primary_key=True)
    org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    group_id = Column(UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    lesson_date = Column(String(10), nullable=False)  # YYYY-MM-DD
    payload = Column(JSONB, nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_group_schedule_one_off_group_date", "group_id", "lesson_date"),
        Index("idx_group_schedule_one_off_org_date", "org_id", "lesson_date"),
    )


class GroupScheduleAudit(Base):
    """Неизменяемый аудит переносов, изменений и прекращения расписания."""

    __tablename__ = "group_schedule_audit"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    override_id = Column(UUID(as_uuid=True), ForeignKey("group_schedule_overrides.id", ondelete="CASCADE"), nullable=False)
    org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    group_id = Column(UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    lesson_id = Column(String(100), nullable=False)
    scope = Column(String(20), nullable=False)
    effective_date = Column(String(10), nullable=False)
    actor_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    before_payload = Column(JSONB, nullable=True)
    after_payload = Column(JSONB, nullable=False)
    reason = Column(String(500), nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "scope IN ('occurrence', 'recurring', 'recurring_end')",
            name="ck_group_schedule_audit_scope",
        ),
        Index("idx_group_schedule_audit_group", "group_id", "effective_date"),
        Index("idx_group_schedule_audit_override", "override_id"),
    )


# ============================================================================
# STUDENT PAYMENTS (N13 P2)
# ============================================================================


class StudentPayment(Base):
    """Одна оплата за период по одному членству ученика в группе.

    Записи не удаляются: ``voided_*`` отменяет оплату, ``replaces_payment_id``
    связывает исправление со старой записью.  Ровно одно из ``valid_to`` /
    ``lessons_count`` заполнено - это режим периода, действовавший при внесении.
    """

    __tablename__ = "student_payments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    membership_id = Column(
        UUID(as_uuid=True),
        ForeignKey("student_groups.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Copies of the membership's ids for fast queries (kept equal by the service).
    student_id = Column(
        UUID(as_uuid=True), ForeignKey("students.id", ondelete="RESTRICT"), nullable=False
    )
    group_id = Column(
        UUID(as_uuid=True), ForeignKey("groups.id", ondelete="RESTRICT"), nullable=False
    )
    amount = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False, default="UZS", server_default="UZS")
    paid_on = Column(Date, nullable=False)
    valid_from = Column(Date, nullable=False)
    valid_to = Column(Date, nullable=True)
    lessons_count = Column(SmallInteger, nullable=True)
    note = Column(String(500), nullable=True)
    replaces_payment_id = Column(UUID(as_uuid=True), ForeignKey("student_payments.id"), nullable=True)
    # N13 P2.1: a carry-over made by a transfer points at the payment it was made from
    # (amount 0, the original keeps the money).  NULL = an ordinary payment.
    carried_from_payment_id = Column(UUID(as_uuid=True), ForeignKey("student_payments.id"), nullable=True)
    # The instant this payment moved to another group with the student (naive UTC, the same
    # moment as the old membership's left_at): from then on it covers, in its own membership,
    # only what happened while the student was still in the group.  NULL = it did not move.
    carried_out_at = Column(DateTime, nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())
    voided_at = Column(DateTime, nullable=True)
    voided_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    void_reason = Column(String(500), nullable=True)
    # N13 P2.2: the receipt number, 1, 2, 3... inside the organisation (never reused, also not after a
    # void).  NULL for a carry-over (it is not money).  A replacement gets a new number.
    receipt_no = Column(Integer, nullable=True)
    # N13 P2.2: with one payment day for the centre, the payment did not cover whole periods of the
    # centre (a student who came mid-month).  Honest history: settings changes do not touch it.
    partial_period = Column(Boolean, nullable=False, default=False, server_default=text("false"))

    membership = relationship("StudentGroup")

    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_student_payments_amount"),
        CheckConstraint("lessons_count > 0", name="ck_student_payments_lessons_positive"),
        CheckConstraint(
            "(valid_to IS NULL) <> (lessons_count IS NULL)", name="ck_student_payments_period_kind"
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from", name="ck_student_payments_period_order"
        ),
        CheckConstraint(
            "(voided_at IS NULL AND voided_by IS NULL AND void_reason IS NULL) OR "
            "(voided_at IS NOT NULL AND voided_by IS NOT NULL AND void_reason IS NOT NULL)",
            name="ck_student_payments_void_all_or_none",
        ),
        Index(
            "idx_student_payments_membership",
            "org_id",
            "membership_id",
            text("valid_to DESC"),
        ),
        Index(
            "idx_student_payments_org_valid_to",
            "org_id",
            "valid_to",
            postgresql_where=text("voided_at IS NULL"),
        ),
        # Cascades (void / replace of the original) find the carry-overs through this column.
        Index(
            "idx_student_payments_carried_from",
            "carried_from_payment_id",
            postgresql_where=text("carried_from_payment_id IS NOT NULL"),
        ),
        # Receipt numbers are unique inside the organisation; the service takes max+1 and retries.
        Index(
            "uq_student_payments_receipt_no",
            "org_id",
            "receipt_no",
            unique=True,
            postgresql_where=text("receipt_no IS NOT NULL"),
        ),
        # Double-click guard: an identical live payment cannot be inserted twice.  Carry-overs
        # made from different payments are different payments (A -> B -> A -> B in one day).
        Index(
            "uq_student_payments_active_dup",
            "org_id",
            "membership_id",
            "paid_on",
            "valid_from",
            text("COALESCE(valid_to, DATE '1900-01-01')"),
            text("COALESCE(lessons_count, 0)"),
            "amount",
            text("COALESCE(carried_from_payment_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            unique=True,
            postgresql_where=text("voided_at IS NULL"),
        ),
    )


class StudentPaymentFile(Base):
    """Фото или скан чека к оплате (JPEG/PNG/PDF, до 2 МБ, до 3 на оплату, хранится в БД)."""

    __tablename__ = "student_payment_files"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id = Column(UUID(as_uuid=True), nullable=False)
    payment_id = Column(
        UUID(as_uuid=True),
        ForeignKey("student_payments.id", ondelete="CASCADE"),
        nullable=False,
    )
    filename = Column(String(200))
    content_type = Column(String(50), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    data = Column(LargeBinary, nullable=False)
    uploaded_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    uploaded_at = Column(DateTime, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 2097152",
            name="ck_student_payment_file_size",
        ),
        Index("idx_student_payment_files_payment", "payment_id"),
    )


# ============================================================================
# PAYROLL (N13 P3)
# ============================================================================


class PayrollSheet(Base):
    """Ведомость зарплаты: один преподаватель за один календарный месяц.

    ``period_month`` - первое число месяца.  ``lines`` - строки (членство x преподаватель) вместе с
    отметками по датам: ведомость самодостаточна и не меняется, когда позже правят посещаемость или
    оплаты (для этого ``source_fingerprint`` - хэш входных данных расчёта).  Каждая правка создаёт
    новую редакцию (``revision``); прежние остаются в ``payroll_sheet_revisions``.
    """

    __tablename__ = "payroll_sheets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    teacher_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    period_month = Column(Date, nullable=False)
    # {"group": "50", "individual": "60"} - decimal strings, percent per kind of lessons
    percent_inputs = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    divisor_default = Column(SmallInteger, nullable=False, default=12, server_default="12")
    lines = Column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    total_amount = Column(BigInteger, nullable=False, default=0, server_default="0")
    source_fingerprint = Column(Text, nullable=False)
    revision = Column(Integer, nullable=False, default=1, server_default="1")
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())
    updated_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    updated_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())

    revisions = relationship(
        "PayrollSheetRevision",
        back_populates="sheet",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint("org_id", "teacher_id", "period_month", name="uq_payroll_sheet_teacher_month"),
        CheckConstraint("EXTRACT(DAY FROM period_month) = 1", name="ck_payroll_sheets_first_day"),
        CheckConstraint("total_amount >= 0", name="ck_payroll_sheets_total"),
        CheckConstraint("revision >= 1", name="ck_payroll_sheets_revision"),
        CheckConstraint("divisor_default BETWEEN 1 AND 31", name="ck_payroll_sheets_divisor"),
        Index("idx_payroll_sheets_org_month", "org_id", "period_month"),
    )


class PayrollSheetRevision(Base):
    """Редакция ведомости.  Только добавление: запись никогда не меняется."""

    __tablename__ = "payroll_sheet_revisions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    sheet_id = Column(
        UUID(as_uuid=True),
        ForeignKey("payroll_sheets.id", ondelete="CASCADE"),
        nullable=False,
    )
    revision = Column(Integer, nullable=False)
    percent_inputs = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    lines = Column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    total_amount = Column(BigInteger, nullable=False, default=0, server_default="0")
    changed_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    changed_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())
    change_note = Column(String(300), nullable=True)

    sheet = relationship("PayrollSheet", back_populates="revisions")

    __table_args__ = (
        UniqueConstraint("sheet_id", "revision", name="uq_payroll_sheet_revision"),
        CheckConstraint("revision >= 1", name="ck_payroll_sheet_revisions_revision"),
    )
