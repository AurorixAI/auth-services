"""Regression tests for the attendance data contract and access helpers."""

from types import SimpleNamespace
import unittest
from datetime import date
from uuid import uuid4

from pydantic import ValidationError

from src.auth.attendance_access import is_org_admin
from src.auth.schemas import (
    GroupAttendanceResponse,
    LessonAttendanceStatus,
    OpenAttendanceActionResponse,
    ReconcileMissedLessonRequest,
    SaveAttendanceRequest,
    StudentAttendanceStatus,
)
from src.auth.service import AuthService
from src.models.auth import Group


class _GroupsQuery:
    def __init__(self, groups):
        self.groups = groups

    def filter(self, *_args):
        return self

    def order_by(self, *_args):
        return self

    def all(self):
        return self.groups

    def first(self):
        return self.groups[0] if self.groups else None


class _ReadOnlyScheduleDB:
    def __init__(self, groups):
        self.groups = groups
        self.commit_calls = 0

    def query(self, model):
        # Schedule overrides are absent in these pure read tests.
        return _GroupsQuery(self.groups if model is Group else [])

    def commit(self):
        self.commit_calls += 1


class AttendanceContractTests(unittest.TestCase):
    def test_roster_snapshot_preserves_only_real_student_ids(self):
        first = uuid4()
        second = uuid4()
        snapshot = [
            {"student_id": str(first), "full_name": "Первый ученик"},
            {"student_id": str(second), "full_name": "Второй ученик"},
            {"student_id": ""},
            "corrupt legacy value",
        ]
        self.assertEqual(
            AuthService._roster_ids_from_snapshot(snapshot),
            {str(first), str(second)},
        )
        self.assertEqual(AuthService._roster_ids_from_snapshot(None), set())

    def test_attendance_response_exposes_frozen_roster_transfer_and_diagnostic_notice(self):
        student_id = str(uuid4())
        response = GroupAttendanceResponse(
            groupId=str(uuid4()),
            rosterStudentIds={"2026-08-20_lesson-1": [student_id]},
            historicalStudents=[{
                "id": student_id,
                "full_name": "Переведённый ученик",
                "transferred_to_group_name": "Новая группа",
            }],
            membershipEvents=[{
                "student_id": student_id,
                "kind": "transferred",
                "joined_at": "2026-08-21T09:00:00",
                "source_group_name": "Предыдущая группа",
            }],
            diagnosticReportNotices=[{
                "student_id": student_id,
                "invite_id": str(uuid4()),
            }],
        )
        self.assertEqual(len(response.rosterStudentIds), 1)
        self.assertEqual(response.historicalStudents[0]["full_name"], "Переведённый ученик")
        self.assertEqual(response.historicalStudents[0]["transferred_to_group_name"], "Новая группа")
        self.assertEqual(response.membershipEvents[0]["kind"], "transferred")
        self.assertEqual(response.diagnosticReportNotices[0]["student_id"], student_id)

    def test_attendance_request_is_strict_and_uses_full_snapshot(self):
        student_id = uuid4()
        request = SaveAttendanceRequest(
            lessonDate="2026-08-20",
            lessonId="lesson-1",
            status="in_progress",
            records={str(student_id): "excused"},
        )

        self.assertEqual(request.lessonDate.isoformat(), "2026-08-20")
        self.assertIs(request.status, LessonAttendanceStatus.IN_PROGRESS)
        self.assertIs(request.records[student_id], StudentAttendanceStatus.EXCUSED)

        with self.assertRaises(ValidationError):
            SaveAttendanceRequest(
                lessonDate="20.08.2026",
                records={str(student_id): "present"},
            )
        with self.assertRaises(ValidationError):
            SaveAttendanceRequest(
                lessonDate="2026-08-20",
                status="scheduled",
                records={str(student_id): "present"},
            )
        with self.assertRaises(ValidationError):
            SaveAttendanceRequest(
                lessonDate="2026-08-20",
                lessonId="lesson-1",
                records={str(student_id): "unknown"},
            )

    def test_cancellation_has_a_reason_and_uses_no_separate_illness_status(self):
        cancellation = SaveAttendanceRequest(
            lessonDate="2026-08-20",
            lessonId="lesson-1",
            status="cancelled",
            records={},
            cancellationReason="Урок перенесён по просьбе группы",
        )
        self.assertIs(cancellation.status, LessonAttendanceStatus.CANCELLED)
        self.assertEqual(cancellation.cancellationReason, "Урок перенесён по просьбе группы")

        with self.assertRaises(ValidationError):
            SaveAttendanceRequest(
                lessonDate="2026-08-20",
                lessonId="lesson-1",
                records={str(uuid4()): "ill"},
            )
        with self.assertRaises(ValidationError):
            SaveAttendanceRequest(
                lessonDate="2026-08-20",
                lessonId="lesson-1",
                status="cancelled",
                records={},
                cancellationReason="ok",
            )

    def test_missed_lesson_reconciliation_is_explicit_and_requires_a_reason_to_cancel(self):
        action = OpenAttendanceActionResponse(
            actionType="missed",
            groupId=str(uuid4()),
            groupName="10-А",
            classLevel=10,
            lessonId="lesson-1",
            lessonDate="2026-08-20",
            subject="Алгебра",
            startTime="10:00",
            endTime="11:30",
        )
        self.assertEqual(action.actionType, "missed")

        cancellation = ReconcileMissedLessonRequest(
            lessonDate="2026-08-20",
            lessonId="lesson-1",
            cancellationReason="Преподаватель заболел",
        )
        self.assertEqual(cancellation.lessonDate.isoformat(), "2026-08-20")
        with self.assertRaises(ValidationError):
            ReconcileMissedLessonRequest(
                lessonDate="2026-08-20",
                lessonId="lesson-1",
                cancellationReason="ok",
            )

    def test_schedule_time_validation_is_strict_and_ordered(self):
        self.assertEqual(AuthService._validate_lesson_time_range("09:00", "10:30"), (540, 630))
        with self.assertRaises(ValueError):
            AuthService._validate_lesson_time_range("9:00", "10:30")
        with self.assertRaises(ValueError):
            AuthService._validate_lesson_time_range("10:30", "10:30")
        with self.assertRaises(ValueError):
            AuthService._validate_lesson_time_range("10:30", "10:00")

    def test_schedule_get_is_pure_read_and_does_not_create_default_lessons(self):
        group = SimpleNamespace(
            id=uuid4(),
            name="10-А",
            class_level=10,
            schedule=[
                {
                    "id": "lesson-1",
                    "dayOfWeek": 1,
                    "startTime": "10:00",
                    "endTime": "11:30",
                    "subject": "Алгебра",
                    "room": "201",
                    "format": "offline",
                    "color": "blue",
                    "topic": "Квадратные уравнения",
                }
            ],
        )
        db = _ReadOnlyScheduleDB([group])

        lessons = AuthService(db).get_schedule(str(uuid4()))

        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0].id, "lesson-1")
        self.assertEqual(db.commit_calls, 0)

    def test_resolved_occurrence_keeps_auditable_recurring_and_one_off_layers(self):
        group = SimpleNamespace(
            id=uuid4(),
            name="10-А",
            class_level=10,
            schedule=[{
                "id": "lesson-1", "dayOfWeek": 1,
                "startTime": "10:00", "endTime": "11:30",
                "subject": "Алгебра", "room": "201", "format": "offline",
                "color": "blue", "topic": "Функции",
            }],
        )
        recurring = SimpleNamespace(
            lesson_id="lesson-1", effective_date="2026-08-18",
            payload={
                "dayOfWeek": 3, "startTime": "12:00", "endTime": "13:30",
                "subject": "Алгебра", "room": "202", "format": "offline",
                "color": "blue", "topic": "Функции",
            },
        )
        transfer = SimpleNamespace(
            payload={
                "dayOfWeek": 3, "startTime": "14:00", "endTime": "15:30",
                "subject": "Алгебра", "room": "203", "format": "online",
                "color": "blue", "topic": "Функции",
            },
            reason="Аудитория занята",
        )
        service = AuthService(_ReadOnlyScheduleDB([]))
        service._schedule_overrides_for_group = lambda *_args, **_kwargs: (
            {"lesson-1": [recurring]},
            {("lesson-1", "2026-08-19"): transfer},
            {},
        )

        occurrences = service._resolved_occurrences_for_group(
            group,
            date_from=date(2026, 8, 17),
            date_to=date(2026, 8, 23),
        )

        self.assertEqual(len(occurrences), 2)
        self.assertEqual(occurrences[0]["occurrenceDate"], "2026-08-17")
        self.assertFalse(occurrences[0]["isRescheduled"])
        self.assertEqual(occurrences[1]["occurrenceDate"], "2026-08-19")
        self.assertTrue(occurrences[1]["isRescheduled"])
        self.assertEqual(occurrences[1]["originalStartTime"], "12:00")
        self.assertEqual(occurrences[1]["startTime"], "14:00")
        self.assertEqual(occurrences[1]["room"], "203")

    def test_resolved_occurrences_stop_at_recurring_end_without_erasing_past(self):
        group = SimpleNamespace(
            id=uuid4(),
            name="Математика-1",
            class_level=10,
            schedule=[{
                "id": "lesson-1", "dayOfWeek": 1,
                "startTime": "10:00", "endTime": "11:30",
                "subject": "Алгебра", "room": "201", "format": "offline",
                "color": "blue", "topic": "Функции",
            }],
        )
        recurring_end = SimpleNamespace(effective_date="2026-08-24")
        service = AuthService(_ReadOnlyScheduleDB([]))
        service._schedule_overrides_for_group = lambda *_args, **_kwargs: (
            {},
            {},
            {"lesson-1": recurring_end},
        )

        occurrences = service._resolved_occurrences_for_group(
            group,
            date_from=date(2026, 8, 17),
            date_to=date(2026, 8, 31),
        )

        # The scheduled fact before the end date remains resolvable; the two
        # future Mondays are not generated at all.
        self.assertEqual([item["occurrenceDate"] for item in occurrences], ["2026-08-17"])

    def test_group_access_allows_only_assigned_teacher_or_org_manager(self):
        teacher_id = uuid4()
        group = SimpleNamespace(id=uuid4(), teacher_id=teacher_id)
        service = AuthService(_ReadOnlyScheduleDB([group]))

        self.assertIs(
            service._require_group_access(
                str(uuid4()), str(group.id), actor_id=str(teacher_id), is_org_admin=False
            ),
            group,
        )
        self.assertIs(
            service._require_group_access(
                str(uuid4()), str(group.id), actor_id=str(uuid4()), is_org_admin=True
            ),
            group,
        )
        with self.assertRaises(PermissionError):
            service._require_group_access(
                str(uuid4()), str(group.id), actor_id=str(uuid4()), is_org_admin=False
            )

    def test_org_admin_helper_uses_org_role_not_spoofable_query_parameters(self):
        self.assertTrue(is_org_admin({"org_role": "owner", "role": "teacher"}))
        self.assertTrue(is_org_admin({"org_role": "admin", "role": "teacher"}))
        self.assertFalse(is_org_admin({"org_role": "teacher", "role": "teacher"}))
