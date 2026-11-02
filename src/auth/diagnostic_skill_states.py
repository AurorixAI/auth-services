"""Per-topic skill states written from the diagnostic completion callback (DM-036).

diagnostic-service delivers the result of an attempt through a retrying outbox, so this callback is replayed:
after a timeout, after a restart of either side, after the hourly requeue.  Applying it must therefore be

* idempotent: the same payload twice leaves the same rows (the old code recomputed ``trend`` against the value it
  had just written itself and turned an "improving" topic into "stable" on the replay);
* tolerant: one malformed topic (not a dict, ``alpha`` null, a name that does not fit the column) is skipped and
  counted, instead of a 500 that makes the sender retry the same payload for a day while the result row, which is
  already committed, is fine;
* honest about time: the columns are naive UTC (see ``core/db_timezone``), so naive UTC is written.

* monotonic: a result OLDER than the one already applied to the topic is ignored.  The late replay of diagnostic No. 1
  (the sender's hourly requeue after a long outage) must not roll back the states written by diagnostic No. 2 of the
  same student.  The time of a result is the moment its session started (``session_started_at`` of the callback); it is
  stored in ``last_attempt_at`` (the column already meant "last attempt"; no migration).  Rows written before this
  change hold the time they were applied, which is later than the start of the same result, so a replay of such a result
  is ignored too, which is what a replay should be.

A database error is NOT swallowed: the sender retries and the (idempotent) replay finishes the job.
"""
from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

from sqlalchemy.orm import Session

from src.models.analytics import StudentSkillState

logger = logging.getLogger(__name__)

SKILL_ID_MAX = 30          # StudentSkillState.skill_id String(30)
THETA_LIMIT = 100          # Numeric(6, 4)
TREND_STEP = 0.05


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _finite(value: Any) -> Optional[float]:
    """A finite float, or None for null / bool / text / NaN / inf."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    """An aware datetime as naive UTC (the convention of every column); a naive one is taken as UTC already."""
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def status_for(alpha: float, is_gap: bool) -> str:
    if is_gap or alpha < -0.8:
        return "gap"
    if alpha >= 0.4:
        return "mastered"
    if alpha >= -0.2:
        return "learning"
    return "struggling"


def mastery_for(alpha: float) -> float:
    """Sigmoid-like map of alpha in [-2, 2] to [0, 1]."""
    return round(max(0.0, min(1.0, 0.5 + alpha / 4.0)), 4)


def apply_skill_states(
    db: Session,
    *,
    student_id: str,
    org_id: Optional[str],
    theta: Optional[float],
    topic_breakdown: Any,
    gaps: Set[str],
    now: Optional[datetime] = None,
    result_at: Optional[datetime] = None,
) -> Dict[str, int]:
    """Upsert one StudentSkillState per topic.

    ``result_at``: when the diagnostic that produced ``topic_breakdown`` started (naive UTC; ``None`` = unknown, the
    result counts as the newest).  Returns ``{"written": n, "unchanged": n, "skipped": n, "stale": n}``.
    """
    counts = {"written": 0, "unchanged": 0, "skipped": 0, "stale": 0}
    if not isinstance(topic_breakdown, dict):
        if topic_breakdown:
            logger.warning("diagnostic_topic_breakdown_ignored student_id=%s type=%s", student_id, type(topic_breakdown).__name__)
        return counts
    now = now or _utcnow()
    result_at = naive_utc(result_at) or now
    student_uuid = uuid.UUID(str(student_id))
    org_uuid = uuid.UUID(str(org_id)) if org_id else None
    theta_value = _finite(theta)
    if theta_value is not None and abs(theta_value) >= THETA_LIMIT:
        theta_value = None
    theta_stored = None if theta_value is None else round(theta_value, 4)

    # wave C DM-012: the states of all the topics in ONE query (was one per topic)
    wanted = [key for key in topic_breakdown if isinstance(key, str) and 0 < len(key) <= SKILL_ID_MAX]
    states = {}
    if wanted:
        states = {s.skill_id: s for s in db.query(StudentSkillState).filter(StudentSkillState.student_id == student_uuid, StudentSkillState.skill_id.in_(wanted)).all()}

    for skill_id, info in topic_breakdown.items():
        if not (isinstance(skill_id, str) and 0 < len(skill_id) <= SKILL_ID_MAX and isinstance(info, dict)):
            counts["skipped"] += 1
            continue
        raw_alpha = info.get("alpha")
        alpha = 0.0 if raw_alpha is None else _finite(raw_alpha)
        if alpha is None:
            counts["skipped"] += 1
            continue
        status = status_for(alpha, skill_id in gaps)
        mastery = mastery_for(alpha)

        existing = states.get(skill_id)
        if existing is None:
            db.add(StudentSkillState(
                student_id=student_uuid, org_id=org_uuid, skill_id=skill_id, mastery_probability=mastery,
                status=status, trend="stable", theta=theta_stored, last_attempt_at=result_at, updated_at=now,
            ))
            counts["written"] += 1
            continue
        if existing.last_attempt_at is not None and result_at < existing.last_attempt_at:
            counts["stale"] += 1                # an older result than the one already applied: never roll back
            continue
        previous = float(existing.mastery_probability if existing.mastery_probability is not None else 0.5)
        same_theta = (existing.theta is None and theta_stored is None) or (
            existing.theta is not None and theta_stored is not None and float(existing.theta) == theta_stored
        )
        if previous == mastery and existing.status == status and same_theta:
            counts["unchanged"] += 1            # a replay of what is already stored: touch nothing
            continue
        if mastery > previous + TREND_STEP:
            existing.trend = "improving"
        elif mastery < previous - TREND_STEP:
            existing.trend = "declining"
        else:
            existing.trend = "stable"
        existing.mastery_probability = mastery
        existing.status = status
        existing.theta = theta_stored
        existing.last_attempt_at = result_at
        existing.updated_at = now
        counts["written"] += 1

    if counts["stale"]:
        logger.info("diagnostic_topic_results_stale student_id=%s stale=%s", student_id, counts["stale"])
    if counts["skipped"]:
        logger.warning("diagnostic_topic_breakdown_rows_skipped student_id=%s skipped=%s", student_id, counts["skipped"])
    db.commit()
    return counts
