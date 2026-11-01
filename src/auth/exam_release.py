"""Wave B P4 (B8 / SV-15, LD-23): the release of the exams of a cancelled lesson is asked of exam-service AFTER the commit, outside every lock.

Before: ``cancel_lesson`` / the reschedule asked exam-service (``release``, a 5 s call) while the group row and the journal row were locked and the
transaction was still open - a slow exam-service stalled the group (start, roster) and a failed commit left the exams released for a lesson that stayed.
Now the transaction only writes the marker ``group_attendance.exam_release_pending`` on the cancelled row; after the commit the road calls
``release_pending`` which asks exam-service, one lesson at a time, and clears the marker on an answer:

* exam-service answers: the marker is cleared;
* exam-service refuses (409: a student started the exam meanwhile): nothing can be released any more - the marker is cleared and the owner is told
  (``EXAM_NOT_RELEASED``), a retry would only repeat the refusal;
* exam-service does not answer (or answers 5xx): the marker stays, the lesson stays cancelled, the answer carries ``EXAM_RELEASE_PENDING``; the NEXT write to the group
  (a cancellation, a move, a decision) retries the old markers first (LD-23, lazy); the background task of auth (``exam_release_worker``, every 15 minutes, one executor under an advisory lock)
  and ``scripts/sweep_exam_releases.py`` do the same for every centre.

The CHECK (``cancellation-check``) is not here: it stays fail-closed and is asked BEFORE the locks by ``Substitutions.exam_preflight``.
A lesson that is given back loses its row, so its marker is gone with it.  Nothing here holds a lock while the network is called: the read transaction is
ended first, every marker is cleared in its own short transaction.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import func

from . import business_clock as bc
from .models import GroupAttendance, GroupAudit

logger = logging.getLogger(__name__)

PENDING = "EXAM_RELEASE_PENDING"
NOT_RELEASED = "EXAM_NOT_RELEASED"
PENDING_TEXT = "Экзамен урока вернётся в черновик позже: сервис экзаменов сейчас не отвечает. Урок отменён"
LIMIT = 20            # markers one call handles (one centre never has that many at once; the rest wait for the next write)
ACTION_PENDING = "exam_release_pending"      # ``group_audit.action``: the cancellation is committed, the exam is NOT released yet (R3 5)
ACTION_DONE = "exam_release_done"            # ... the release is over: ``snapshot.outcome`` = ``released`` | ``refused``


class ExamAnswerError(RuntimeError):
    """exam-service ANSWERED the release with a status that is neither 200 nor 409 (``status``): 5xx is a trouble of the moment (retried later), a 4xx is its verdict about THIS lesson (R3 6)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def mark(row: GroupAttendance) -> None:
    """In the transaction of the cancellation: this cancelled lesson still has to be released in exam-service."""
    row.exam_release_pending = True


def _dm(lesson_date: str) -> str:
    return f"{lesson_date[8:10]}.{lesson_date[5:7]}"


def _trail(db: Any, row: Any, action: str, outcome: str, message: str) -> None:
    """One row of ``group_audit`` for the owner's side: the cancellation was committed but the exam is (not) released.  Written in the short transaction that also clears the marker."""
    if row.marked_by is None:
        return
    db.add(GroupAudit(org_id=row.org_id, group_id=row.group_id, actor_id=row.marked_by, actor_role="admin", action=action, assigned_teacher_id=None,
                      snapshot={"attendanceId": str(row.id), "lessonId": row.lesson_id, "lessonDate": row.lesson_date, "outcome": outcome, "message": message},
                      created_at=bc.utc_now()))


def _has_pending_trail(db: Any, row: Any) -> bool:
    return db.query(GroupAudit.id).filter(GroupAudit.group_id == row.group_id, GroupAudit.action == ACTION_PENDING,
                                          GroupAudit.snapshot["attendanceId"].astext == str(row.id)).first() is not None


def release_pending(svc: Any, org_id: Any, group_id: Optional[Any] = None, *, limit: int = LIMIT) -> List[Dict[str, str]]:
    """Ask exam-service to release the exams of every cancelled lesson with the marker (of one group, or of the whole centre).  Call it AFTER the commit.
    Returns the warnings for the answer: ``EXAM_RELEASE_PENDING`` (it did not answer), ``EXAM_NOT_RELEASED`` (it refused).

    Only a trouble of the NETWORK / a 5xx stops the run (the rest would wait as long); an answer that is a verdict about one lesson (409, 4xx) clears that marker and goes on
    with the next one (R3 6: one lesson that exam-service cannot handle never blocks the others).  The owner's trail (R3 5): the first failure writes ``exam_release_pending``,
    the end of a release that had failed before writes ``exam_release_done``."""
    db = svc.db
    query = db.query(GroupAttendance.id, GroupAttendance.org_id, GroupAttendance.group_id, GroupAttendance.lesson_id, GroupAttendance.lesson_date,
                     GroupAttendance.cancellation_reason, GroupAttendance.marked_by).filter(
        GroupAttendance.org_id == org_id, GroupAttendance.exam_release_pending.is_(True), GroupAttendance.status == "cancelled")
    if group_id is not None:
        query = query.filter(GroupAttendance.group_id == group_id)
    rows = query.order_by(GroupAttendance.created_at, GroupAttendance.id).limit(limit).all()
    db.rollback()                       # ends the read transaction: no connection is held while exam-service is called
    warnings: List[Dict[str, str]] = []
    for row in rows:
        outcome: Optional[str] = None
        try:
            svc._sync_exam_lesson_cancellation(phase="release", org_id=str(org_id), group_id=str(row.group_id), lesson_id=row.lesson_id,
                                               lesson_date=row.lesson_date, reason=row.cancellation_reason)
            outcome = "released"
        except ExamAnswerError as exc:
            if exc.status >= 500:       # exam-service itself is in trouble: the same as no answer
                outcome = "pending"
            else:                       # its verdict about THIS lesson: a retry would only repeat it
                logger.warning("exam release of lesson %s/%s %s answered %s: %s", row.group_id, row.lesson_id, row.lesson_date, exc.status, exc)
                warnings.append({"code": NOT_RELEASED, "message": f"Урок отменён, но экзамен урока вернуть в черновик не удалось (сервис экзаменов ответил {exc.status})"})
                outcome = "refused"
        except RuntimeError as exc:     # exam-service does not answer
            logger.warning("exam release of lesson %s/%s %s is pending: %s", row.group_id, row.lesson_id, row.lesson_date, exc)
            outcome = "pending"
        except ValueError as exc:       # exam-service refuses (409): the exam cannot be returned any more
            logger.warning("exam release of lesson %s/%s %s refused: %s", row.group_id, row.lesson_id, row.lesson_date, exc)
            warnings.append({"code": NOT_RELEASED, "message": f"Урок отменён, но экзамен урока вернуть в черновик нельзя: {exc}"})
            outcome = "refused"
        if outcome == "pending":
            warnings.append({"code": PENDING, "message": PENDING_TEXT})
            if not _has_pending_trail(db, row):
                _trail(db, row, ACTION_PENDING, "pending", f"Экзамен по уроку {_dm(row.lesson_date)} не освобождён (сервис экзаменов недоступен) — повторим автоматически")
                db.commit()
            break
        if outcome == "released" and _has_pending_trail(db, row):
            _trail(db, row, ACTION_DONE, "released", f"Экзамен по уроку {_dm(row.lesson_date)} освобождён")
        elif outcome == "refused":
            _trail(db, row, ACTION_DONE, "refused", f"Экзамен по уроку {_dm(row.lesson_date)} вернуть в черновик нельзя: он уже идёт или у него есть попытки")
        clear(db, row.id)
    return warnings


def pending_count(db: Any) -> int:
    """How many cancelled lessons still wait for the release (the whole database): what the background task logs."""
    return int(db.query(func.count(GroupAttendance.id)).filter(GroupAttendance.exam_release_pending.is_(True), GroupAttendance.status == "cancelled").scalar() or 0)


def clear(db: Any, attendance_id: Any) -> None:
    """The marker of one row off (together with the trail row added before it, one short transaction; the row may be gone: a restored lesson)."""
    db.query(GroupAttendance).filter(GroupAttendance.id == attendance_id).update(
        {GroupAttendance.exam_release_pending: False, GroupAttendance.updated_at: GroupAttendance.updated_at}, synchronize_session=False)
    db.commit()
