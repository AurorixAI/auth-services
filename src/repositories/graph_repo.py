"""
ALGO V1 — Graph Update Log Repository  (Step 10)
"""

from __future__ import annotations

from typing import List, Optional
from datetime import datetime

from sqlalchemy import select

from src.models.graph import GraphUpdateLog
from src.repositories.base import BaseRepository


class GraphUpdateRepo(BaseRepository[GraphUpdateLog]):
    model = GraphUpdateLog

    def get_pending_reviews(self, limit: int = 50) -> List[GraphUpdateLog]:
        stmt = select(GraphUpdateLog).where(
            GraphUpdateLog.requires_review.is_(True),
            GraphUpdateLog.review_status == 'pending'
        ).order_by(GraphUpdateLog.created_at).limit(limit)
        return list(self.session.scalars(stmt))

    def approve_update(self, update_id: int, reviewed_by: str) -> bool:
        log_entry = self.get_by_id(update_id)
        if not log_entry:
            return False
        
        log_entry.review_status = 'approved'
        log_entry.reviewed_by = reviewed_by
        log_entry.reviewed_at = datetime.now()
        self.session.flush()
        return True

    def reject_update(self, update_id: int, reviewed_by: str, reason: Optional[str] = None) -> bool:
        log_entry = self.get_by_id(update_id)
        if not log_entry:
            return False
        
        log_entry.review_status = 'rejected'
        log_entry.reviewed_by = reviewed_by
        log_entry.reviewed_at = datetime.now()
        if reason:
            log_entry.change_reason = reason
        self.session.flush()
        return True
