"""The life of a staff member (LOGIC-B O2, SPEC 1 "O2" and 4.5): removal with preconditions, "hand over the groups", "return to the team".

* ``DELETE /org/members/{id}`` refuses (409 ``MEMBER_HAS_ASSIGNMENTS``, the body is the ``removal-impact``) while the member teaches an active group or
  has an in-progress lesson.  Future substitutions of his are taken off automatically; missed (never started) lessons do not block - they stay with
  the administrators (the row, once reconciled, is still his: ``teacher_id`` = who taught).
* ``POST /org/members/{id}/handover`` moves groups to other teachers atomically (all groups or none) with the overlap check over the batch.
* ``POST /org/members/{id}/restore`` (owner) reactivates member + user: the old password works, the groups do NOT come back.
* every operation writes ``member_audit`` (INV-O2-4).

Lock order (SPEC 0.4): the member row -> the groups of the member (ascending id) -> the user row.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import joinedload

from . import business_clock as bc
from . import user_sessions
from . import lesson_substitutions
from .lesson_teachers import TEACHING_ROLES, LessonTeachers, _s, _uuid
from .models import (
    Group,
    GroupAttendance,
    GroupAudit,
    LessonTeacherAssignment,
    MemberAudit,
    OrganizationMember,
    StudentGroup,
    User,
)
from .ops_rules import (
    ERROR_CATALOGUE,
    OpsError,
    conflict,
    hhmm_to_minutes,
    not_found,
    short_name,
    unprocessable,
)
from .student_archive import student_not_archived

logger = logging.getLogger(__name__)


class StaffLifecycle:
    def __init__(self, svc) -> None:
        self.svc = svc
        self.db = svc.db
        self.teachers = LessonTeachers(svc)

    # --------------------------------------------------------------------------------------------- lookup
    def _member(self, org_id: Any, member_id: Any, *, lock: bool = False) -> OrganizationMember:
        mid = _uuid(member_id)
        member = None
        if mid is not None:
            q = self.db.query(OrganizationMember).filter(OrganizationMember.id == mid, OrganizationMember.org_id == org_id)
            if lock:
                q = q.with_for_update().populate_existing()
            member = q.first()
        if member is None:
            raise not_found("MEMBER_NOT_FOUND", ERROR_CATALOGUE["MEMBER_NOT_FOUND"][1])
        return member

    def _audit(self, member: OrganizationMember, actor_id: Any, action: str, snapshot: Dict[str, Any]) -> None:
        self.db.add(
            MemberAudit(
                org_id=member.org_id, member_id=member.id, user_id=member.user_id, actor_id=_uuid(actor_id), action=action,
                snapshot=snapshot, created_at=bc.utc_now(),
            )
        )

    # ----------------------------------------------------------------------------------------------- list
    def list_members(self, org_id: Any, status: str = "active") -> List[Dict[str, Any]]:
        q = self.db.query(OrganizationMember).options(joinedload(OrganizationMember.user)).filter(OrganizationMember.org_id == org_id)
        if status == "active":
            q = q.filter(OrganizationMember.is_active.is_(True))
        elif status == "removed":
            q = q.filter(OrganizationMember.is_active.isnot(True))
        members = q.all()
        removed_at: Dict[UUID, Any] = {}
        if members:
            for member_id, created in (
                self.db.query(MemberAudit.member_id, func.max(MemberAudit.created_at))
                .filter(MemberAudit.org_id == org_id, MemberAudit.action == "removed", MemberAudit.member_id.in_([m.id for m in members]))
                .group_by(MemberAudit.member_id)
                .all()
            ):
                removed_at[member_id] = created
        out = []
        for m in members:
            if not m.user:
                continue
            d = m.user.to_dict()
            d["org_role"] = m.org_role
            d["member_id"] = str(m.id)
            d["is_active"] = bool(m.is_active)
            d["removed_at"] = removed_at[m.id].isoformat() if (not m.is_active and m.id in removed_at) else None
            out.append(d)
        return out

    # ----------------------------------------------------------------------------------------- impact
    def _teaching_groups(self, org_id: Any, user_id: Any, *, lock: bool = False) -> List[Group]:
        q = self.db.query(Group).filter(Group.org_id == org_id, Group.teacher_id == user_id, Group.status == "active")
        if lock:
            # the row locks are taken in ASCENDING ID everywhere (a lesson start / a membership change take them in the same order); the name order is for the caller
            rows = q.order_by(Group.id).with_for_update().populate_existing().all()
            return sorted(rows, key=lambda g: (g.name or "", str(g.id)))
        return q.order_by(Group.name, Group.id).all()

    def _lock_groups_of(self, org_id: Any, user_id: Any) -> List[Group]:
        """The groups he teaches AND the groups of his future substitutions, locked in ascending id (R3-4: a substitute who starts a lesson holds the group lock,
        so removal either sees his ``in_progress`` row or waits for the start); returns the groups he teaches (name order), read under the locks."""
        today = bc.business_today().isoformat()
        locked: set = set()
        for _ in range(3):
            own = {g.id for g in self._teaching_groups(org_id, user_id)}
            subs = {
                gid for (gid,) in self.db.query(LessonTeacherAssignment.group_id).filter(
                    LessonTeacherAssignment.org_id == org_id, LessonTeacherAssignment.teacher_id == user_id,
                    LessonTeacherAssignment.cancelled_at.is_(None), LessonTeacherAssignment.lesson_date >= today,
                )
            }
            want = own | subs
            if want <= locked:
                break
            self.db.query(Group).filter(Group.id.in_(list(want))).order_by(Group.id).with_for_update().populate_existing().all()
            locked |= want
        return self._teaching_groups(org_id, user_id)

    def _in_progress(self, org_id: Any, user_id: Any) -> List[Any]:
        return (
            self.db.query(GroupAttendance, Group)
            .join(Group, Group.id == GroupAttendance.group_id)
            .filter(GroupAttendance.org_id == org_id, GroupAttendance.status == "in_progress", GroupAttendance.teacher_id == user_id)
            .order_by(GroupAttendance.lesson_date, GroupAttendance.created_at)
            .all()
        )

    def _next_lesson(self, group: Group) -> Optional[Dict[str, str]]:
        today = bc.business_today()
        now = bc.business_now()
        minutes_now = now.hour * 60 + now.minute
        for occ in self.svc._resolved_occurrences_for_group(group, date_from=today, date_to=today + timedelta(days=14)):
            try:
                start = hhmm_to_minutes(occ["startTime"])
            except (KeyError, ValueError, AttributeError):
                continue
            if occ["occurrenceDate"] > today.isoformat() or start >= minutes_now:
                return {"date": occ["occurrenceDate"], "startTime": occ["startTime"]}
        return None

    def impact(self, org_id: Any, member_id: Any) -> Dict[str, Any]:
        member = self._member(org_id, member_id)
        return self._impact_of(org_id, member)

    def _impact_of(self, org_id: Any, member: OrganizationMember, groups: Optional[List[Group]] = None) -> Dict[str, Any]:
        user = self.db.query(User).filter(User.id == member.user_id).first()
        groups = groups if groups is not None else self._teaching_groups(org_id, member.user_id)
        counts = {}
        if groups:
            counts = dict(
                self.db.query(StudentGroup.group_id, func.count(StudentGroup.id))
                .filter(StudentGroup.group_id.in_([g.id for g in groups]), StudentGroup.is_active.is_(True), student_not_archived())
                .group_by(StudentGroup.group_id)
                .all()
            )
        in_progress = self._in_progress(org_id, member.user_id)
        missed = [
            a for a in self.svc.get_open_attendance_actions(org_id, actor_id=str(member.user_id), is_org_admin=False)
            if a.actionType == "missed" and a.teacherId == str(member.user_id)
        ]
        return {
            "memberId": str(member.id),
            "userId": str(member.user_id),
            "name": short_name(user),
            "orgRole": member.org_role,
            "canRemove": member.org_role != "owner" and not groups and not in_progress,
            "groups": [
                {"id": str(g.id), "name": g.name, "studentCount": int(counts.get(g.id, 0)), "nextLesson": self._next_lesson(g)}
                for g in groups
            ],
            "inProgressLessons": [
                {"groupId": str(g.id), "groupName": g.name, "lessonDate": a.lesson_date, "lessonId": a.lesson_id or ""} for a, g in in_progress
            ],
            "futureSubstitutions": {"count": self.teachers.count_future_substitutions(org_id, member.user_id)},
            "pendingSubstitutionRequests": {"count": lesson_substitutions.count_pending_of_teacher(self.db, org_id, member.user_id)},
            "missedLessons": {"count": len(missed)},
        }

    def _assignments_error(self, impact: Dict[str, Any]) -> OpsError:
        return conflict("MEMBER_HAS_ASSIGNMENTS", ERROR_CATALOGUE["MEMBER_HAS_ASSIGNMENTS"][1], **{k: v for k, v in impact.items() if k not in ("userId", "name", "orgRole")})

    # ------------------------------------------------------------------------------------------ handover
    def _teacher_target(self, org_id: Any, target: Any, *, source_user: Any) -> User:
        user_id = _uuid(target)
        row = None
        if user_id is not None and user_id != source_user:
            row = (
                self.db.query(User, OrganizationMember)
                .join(OrganizationMember, OrganizationMember.user_id == User.id)
                .filter(
                    OrganizationMember.org_id == org_id, User.id == user_id, OrganizationMember.org_role.in_(TEACHING_ROLES),
                    OrganizationMember.is_active.is_(True), User.is_active.isnot(False),
                )
                .first()
            )
        if row is None:
            raise unprocessable("TARGET_NOT_TEACHER", ERROR_CATALOGUE["TARGET_NOT_TEACHER"][1])
        return row[0]

    def handover(self, org_id: Any, member_id: Any, req, *, actor_id: Any, actor_role: str) -> Dict[str, Any]:
        member = self._member(org_id, member_id, lock=True)
        wanted = {}
        for item in req.assignments:
            gid = _uuid(item.groupId)
            if gid is None:
                raise not_found("GROUP_NOT_FOUND", ERROR_CATALOGUE["GROUP_NOT_FOUND"][1])
            wanted[gid] = item.teacherId
        # groups of the batch, locked in ascending id (the membership changes / starts take the same order)
        groups = (
            self.db.query(Group).filter(Group.org_id == org_id, Group.id.in_(list(wanted))).order_by(Group.id).with_for_update().populate_existing().all()
            if wanted else []
        )
        if len(groups) != len(wanted):
            raise not_found("GROUP_NOT_FOUND", ERROR_CATALOGUE["GROUP_NOT_FOUND"][1])
        moves: Dict[UUID, Any] = {}
        unassigned = False
        for group in groups:
            if group.teacher_id != member.user_id or group.status != "active":
                continue  # not his any more (a repeated handover): nothing to do
            target = wanted[group.id]
            if target is None:
                unassigned = True
                moves[group.id] = (group, None)
            else:
                self._teacher_target(org_id, target, source_user=member.user_id)
                moves[group.id] = (group, _uuid(target))
        if unassigned and not req.confirmUnassigned:
            raise unprocessable("UNASSIGNED_NOT_CONFIRMED", ERROR_CATALOGUE["UNASSIGNED_NOT_CONFIRMED"][1])
        self.teachers.check_new_group_teacher(org_id, {gid: pair for gid, pair in moves.items() if pair[1] is not None})
        now = bc.utc_now()
        handed: List[Dict[str, Any]] = []
        released = 0
        closed_requests = 0
        by_id = {g.id: g for g in groups}
        for gid in wanted:                                  # the answer follows the order of the request
            group = by_id.get(gid)
            if group is None or group.id not in moves:
                continue
            _group, new_teacher = moves[group.id]
            old = group.teacher_id
            group.teacher_id = new_teacher
            self.db.add(
                GroupAudit(
                    org_id=org_id, group_id=group.id, actor_id=_uuid(actor_id), actor_role=actor_role, action="teacher_changed",
                    assigned_teacher_id=new_teacher, snapshot={"teacher_id": [_s(old), _s(new_teacher)], "via": "handover"}, created_at=now,
                )
            )
            released += self.teachers.release_equal_to_group_teacher(group, actor_id)
            closed_requests += lesson_substitutions.on_group_teacher_changed(self.svc, group, actor_id, trigger="handover")
            handed.append({"groupId": str(group.id), "groupName": group.name, "fromTeacherId": _s(old), "toTeacherId": _s(new_teacher)})
        self.db.flush()
        remaining = self._teaching_groups(org_id, member.user_id)
        self.db.commit()
        return {
            "handedOver": handed,
            "remainingGroups": [{"id": str(g.id), "name": g.name} for g in remaining],
            "releasedSubstitutions": released,
            "closedSubstitutionRequests": closed_requests,
        }

    # -------------------------------------------------------------------------------------------- remove
    def remove(self, org_id: Any, member_id: Any, *, actor_id: Any) -> Dict[str, Any]:
        member = self._member(org_id, member_id, lock=True)
        if member.org_role == "owner":
            raise ValueError("Нельзя удалить владельца")
        if not member.is_active:
            self.db.rollback()
            return {"message": "Сотрудник уже удалён", "releasedSubstitutions": 0}
        groups = self._lock_groups_of(org_id, member.user_id)  # groups before the user row: the same order as a lesson start
        in_progress = self._in_progress(org_id, member.user_id)  # read AFTER the locks: a start that won the race is visible here
        if groups or in_progress:
            impact = self._impact_of(org_id, member, groups)
            self.db.rollback()
            raise self._assignments_error(impact)
        user = self.db.query(User).filter(User.id == member.user_id).with_for_update().first()        # the user row before the substitutions are released (same order as an assignment)
        released = self.teachers.release_substitutions_of(org_id, member.user_id, actor_id)
        closed_requests, reopened = lesson_substitutions.on_member_removed(self.svc, org_id, member.user_id, actor_id, self.teachers.last_released)
        member.is_active = False
        if user is not None:
            user.is_active = False
            # a removed member must not mint a fresh access token with a refresh cookie issued earlier: every session ends
            user_sessions.revoke_all(self.db, user.id, user_sessions.REVOKE_USER_BLOCKED)
        self._audit(member, actor_id, "removed", {"org_role": member.org_role, "releasedSubstitutions": released, "closedSubstitutionRequests": closed_requests,
                                                  "reopenedSubstitutionRequests": reopened})
        self.db.commit()
        out = {"message": "Сотрудник удалён", "releasedSubstitutions": released}
        if closed_requests:
            out["closedSubstitutionRequests"] = closed_requests
        return out

    # -------------------------------------------------------------------------------------------- restore
    def restore(self, org_id: Any, member_id: Any, *, actor_id: Any) -> Dict[str, Any]:
        member = self._member(org_id, member_id, lock=True)
        if member.is_active:
            self.db.rollback()
            raise conflict("NOT_REMOVED", ERROR_CATALOGUE["NOT_REMOVED"][1])
        member.is_active = True
        user = self.db.query(User).filter(User.id == member.user_id).with_for_update().first()
        if user is not None:
            user.is_active = True
        self._audit(member, actor_id, "restored", {"org_role": member.org_role})
        self.db.commit()
        return {"member_id": str(member.id), "is_active": True}

    # ---------------------------------------------------------------------------------------------- role
    def change_role(self, org_id: Any, member_id: Any, new_role: str, *, actor_id: Any) -> Dict[str, Any]:
        if new_role not in {"admin", "teacher"}:
            raise ValueError("Роль должна быть admin или teacher")
        member = self._member(org_id, member_id, lock=True)
        if member.org_role == "owner":
            raise ValueError("Нельзя изменить роль владельца")
        old_role = member.org_role
        if new_role == old_role:
            self.db.rollback()
            return {"member_id": str(member.id), "new_role": new_role}
        if old_role == "teacher" and new_role != "teacher":
            groups = self._teaching_groups(org_id, member.user_id, lock=True)
            in_progress = self._in_progress(org_id, member.user_id)
            if groups or in_progress:
                impact = self._impact_of(org_id, member, groups)
                self.db.rollback()
                raise self._assignments_error(impact)
        member.org_role = new_role
        user = self.db.query(User).filter(User.id == member.user_id).first()
        if user is not None:
            user.role = new_role
        self._audit(member, actor_id, "role_changed", {"from": old_role, "to": new_role})
        self.db.commit()
        return {"member_id": str(member.id), "new_role": new_role}
