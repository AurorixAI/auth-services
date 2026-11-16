"""Money-2 R3 П2: a transfer always names its source (the server never guesses it).  The old tests that «just move the student» name it through this helper: the ONE active group of the student other than the target."""
from typing import Any, Optional

from src.models.auth import StudentGroup


def source_of(db: Any, student_id: Any, target_group_id: Any) -> Optional[str]:
    """The group the student is moved out of: his only active group other than the target (``None`` when he has none or several - the test then names the source itself)."""
    db.expire_all()
    rows = db.query(StudentGroup.group_id).filter(StudentGroup.student_id == student_id, StudentGroup.is_active.is_(True), StudentGroup.group_id != target_group_id).all()
    return str(rows[0][0]) if len(rows) == 1 else None
