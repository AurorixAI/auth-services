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
            "description": self.description,
            "schedule": self.schedule or [],
            "status": self.status,
            "max_students": self.max_students,
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
        CheckConstraint("action IN ('created')", name="ck_group_audit_action"),
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
    action = Column(String(30), nullable=False)  # started | reconciliation_started | updated | corrected | cancelled
    before_status = Column(String(20))
    after_status = Column(String(20), nullable=False)
    before_records = Column(JSONB)
    after_records = Column(JSONB, nullable=False)
    correction_reason = Column(String(500))
    cancellation_reason = Column(String(500))
    created_at = Column(DateTime, default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "action IN ('started', 'reconciliation_started', 'created', 'updated', 'corrected', 'cancelled', 'reconciliation_cancelled')",
            name="ck_group_attendance_audit_action",
        ),
        Index("idx_group_attendance_audit_attendance", "attendance_id"),
        Index("idx_group_attendance_audit_group", "group_id"),
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
