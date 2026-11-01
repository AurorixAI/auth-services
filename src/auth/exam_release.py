"""Wave B P4 (B8 / SV-15, LD-23): the release of the exams of a cancelled lesson is asked of exam-service AFTER the commit, outside every lock.

Before: ``cancel_lesson`` / the reschedule asked exam-service (``release``, a 5 s call) while the group row and the journal row were locked and the
transaction was still open - a slow exam-service stalled the group (start, roster) and a failed commit left the exams released for a lesson that stayed.
Now the transaction only writes the marker ``group_attendance.exam_release_pending`` on the cancelled row; after the commit the road calls
``release_pending`` which asks exam-service, one lesson at a time, and clears the marker on an answer:

* exam-service answers: the marker is cleared;
* exam-service refuses (409: a student started the exam meanwhile): nothing can be released any more - the marker is cleared and the owner is told
  (``EXAM_NOT_RELEASED``), a retry would only repeat the refusal;
* exam-service does not answer: the marker stays, the lesson stays cancelled, the answer carries ``EXAM_RELEASE_PENDING``; the NEXT write to the group
  (a cancellation, a move, a decision) retries the old markers first (LD-23, lazy, no cron); ``scripts/sweep_exam_releases.py`` does the same by hand.

The CHECK (``cancellation-check``) is not here: it stays fail-closed and is asked BEFORE the locks by ``Substitutions.exam_preflight``.
A lesson that is given back loses its row, so its marker is gone with it.  Nothing here holds a lock while the network is called: the read transaction is
ended first, every marker is cleared in its own short transaction.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .models import GroupAttendance

logger = logging.getLogger(__name__)

PENDING = "EXAM_RELEASE_PENDING"
NOT_RELEASED = "EXAM_NOT_RELEASED"
PENDING_TEXT = "Экзамен урока вернётся в черновик позже: сервис экзаменов сейчас не отвечает. Урок отменён"
LIMIT = 20            # markers one call handles (one centre never has that many at once; the rest wait for the next write)


def mark(row: GroupAttendance) -> None:
    """In the transaction of the cancellation: this cancelled lesson still has to be released in exam-service."""
    row.exam_release_pending = True


def release_pending(svc: Any, org_id: Any, group_id: Optional[Any] = None, *, limit: int = LIMIT) -> List[Dict[str, str]]:
    """Ask exam-service to release the exams of every cancelled lesson with the marker (of one group, or of the whole centre).  Call it AFTER the commit.
    Returns the warnings for the answer: ``EXAM_RELEASE_PENDING`` (it did not answer), ``EXAM_NOT_RELEASED`` (it refused)."""
    db = svc.db
    query = db.query(GroupAttendance.id, GroupAttendance.group_id, GroupAttendance.lesson_id, GroupAttendance.lesson_date,
                     GroupAttendance.cancellation_reason).filter(
        GroupAttendance.org_id == org_id, GroupAttendance.exam_release_pending.is_(True), GroupAttendance.status == "cancelled")
    if group_id is not None:
        query = query.filter(GroupAttendance.group_id == group_id)
    rows = query.order_by(GroupAttendance.created_at, GroupAttendance.id).limit(limit).all()
    db.rollback()                       # ends the read transaction: no connection is held while exam-service is called
    warnings: List[Dict[str, str]] = []
    for row in rows:
        try:
            svc._sync_exam_lesson_cancellation(phase="release", org_id=str(org_id), group_id=str(row.group_id), lesson_id=row.lesson_id,
                                               lesson_date=row.lesson_date, reason=row.cancellation_reason)
        except RuntimeError as exc:     # exam-service does not answer: stop (the rest would only wait as long), the markers stay
            logger.warning("exam release of lesson %s/%s %s is pending: %s", row.group_id, row.lesson_id, row.lesson_date, exc)
            warnings.append({"code": PENDING, "message": PENDING_TEXT})
            break
        except ValueError as exc:       # exam-service refuses: the exam cannot be returned any more
            logger.warning("exam release of lesson %s/%s %s refused: %s", row.group_id, row.lesson_id, row.lesson_date, exc)
            warnings.append({"code": NOT_RELEASED, "message": f"Урок отменён, но экзамен урока вернуть в черновик нельзя: {exc}"})
        clear(db, row.id)
    return warnings


def clear(db: Any, attendance_id: Any) -> None:
    """The marker of one row off, in its own short transaction (the row may be gone: a restored lesson)."""
    db.query(GroupAttendance).filter(GroupAttendance.id == attendance_id).update(
        {GroupAttendance.exam_release_pending: False, GroupAttendance.updated_at: GroupAttendance.updated_at}, synchronize_session=False)
    db.commit()
