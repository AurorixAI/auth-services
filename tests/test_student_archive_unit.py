"""N13 «Ученик ушёл из центра»: the pure parts - request validation, the status guard, stamps, codes (no database)."""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.auth.student_archive import (
    ArchiveError, ArchiveStatusViolation, ArchiveStudentRequest, RestoreStudentRequest, SkippedGroupOut,
    actor_role_for_audit, archive_stamp, guard_generic_status_update, skip_code,
)


def student(status="active", **extra):
    base = dict(status=status, archived_at=None, archive_reason=None, updated_at=datetime(2026, 10, 1, 12, 0))
    return SimpleNamespace(**{**base, **extra})


def test_reason_is_trimmed_blank_is_none_and_the_limit_is_300():
    assert ArchiveStudentRequest().reason is None
    assert ArchiveStudentRequest(reason="   ").reason is None
    assert ArchiveStudentRequest(reason="  ушёл  ").reason == "ушёл"
    assert ArchiveStudentRequest(reason="я" * 300).reason == "я" * 300
    with pytest.raises(ValidationError):
        ArchiveStudentRequest(reason="я" * 301)


def test_restore_request_takes_a_list_of_uuids_which_may_be_empty_or_absent():
    assert RestoreStudentRequest().group_ids is None
    assert RestoreStudentRequest(group_ids=[]).group_ids == []
    one = uuid4()
    assert RestoreStudentRequest(group_ids=[str(one)]).group_ids == [one]
    with pytest.raises(ValidationError):
        RestoreStudentRequest(group_ids=["nope"])
    with pytest.raises(ValidationError):
        RestoreStudentRequest(group_ids=[str(uuid4()) for _ in range(51)])


@pytest.mark.parametrize("current, requested, refused", [
    ("active", "archived", True),
    ("new", "archived", True),
    ("diagnosed", "archived", True),
    ("archived", "active", True),
    ("archived", "new", True),
    ("archived", "diagnosed", True),
    ("archived", "archived", False),      # not a change
    ("active", "active", False),
    ("active", "diagnosed", False),       # the other statuses: as before
    ("new", "diagnosed", False),
    ("active", None, False),
    ("archived", None, False),
])
def test_the_generic_edit_guard(current, requested, refused):
    if refused:
        with pytest.raises(ArchiveStatusViolation) as raised:
            guard_generic_status_update(student(current), requested)
        assert str(raised.value) == "Используйте «В архив» / «Вернуть из архива»" and raised.value.http_status == 422
        assert isinstance(raised.value, ArchiveError) and isinstance(raised.value, ValueError)
    else:
        guard_generic_status_update(student(current), requested)


def test_the_stamp_is_empty_unless_archived_and_has_no_updated_at_fallback():
    assert archive_stamp(student("active", archived_at=datetime(2026, 1, 1), archive_reason="x")) == {
        "archived_at": None, "archive_reason": None}
    stamped = archive_stamp(student("archived", archived_at=datetime(2026, 10, 16, 8, 30, 5), archive_reason="Переехал"))
    assert stamped == {"archived_at": "2026-10-16T08:30:05", "archive_reason": "Переехал"}
    # the migration stamped the old ones; a row without a stamp (archived by an old image later) answers None
    assert archive_stamp(student("archived")) == {"archived_at": None, "archive_reason": None}
    assert archive_stamp(student("archived", updated_at=None)) == {"archived_at": None, "archive_reason": None}


def test_the_audit_role_is_always_one_the_check_accepts():
    assert actor_role_for_audit({"org_role": "owner", "role": "owner"}) == "owner"
    assert actor_role_for_audit({"org_role": "admin", "role": "teacher"}) == "admin"
    assert actor_role_for_audit({"org_role": None, "role": "superadmin"}) == "superadmin"
    assert actor_role_for_audit({"org_role": "teacher", "role": "superadmin"}) == "superadmin"
    assert actor_role_for_audit({}) == "admin"


def test_skip_codes_follow_the_messages_of_add_student_to_group():
    """The messages are the ones of ``AuthService.add_student_to_group`` / the lesson guard."""
    assert skip_code("В группе достигнут лимит учеников") == "group_full"
    assert skip_code("Нельзя зачислить ученика в архивную группу") == "group_archived"
    assert skip_code("Группа не найдена") == "not_found"
    assert skip_code("Нельзя изменить состав группы, пока идёт урок. Сначала завершите или отмените начатый урок.") == "lesson_in_progress"
    assert skip_code("что-то другое") == "refused"
    out = SkippedGroupOut(group_id="g", reason="group_full", message="В группе достигнут лимит учеников")
    assert out.model_dump() == {"group_id": "g", "group_name": None, "reason": "group_full",
                                "message": "В группе достигнут лимит учеников"}
