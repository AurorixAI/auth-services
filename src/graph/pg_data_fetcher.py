"""
PostgreSQL implementation of DataFetcher interface.

Wraps KnowledgeRepository, PrerequisiteRepository and AnswerEventRepository
to supply data for BatchGraphUpdater / LearningGraphUpdater.
"""

from datetime import datetime
from typing import Dict, List, Tuple

from sqlalchemy.orm import Session

from src.models.repositories import (
    KnowledgeRepository,
    PrerequisiteRepository,
    AnswerEventRepository,
)
from src.graph.learning_updater import AnswerEvent, DataFetcher


class PgDataFetcher(DataFetcher):
    """Sync PostgreSQL DataFetcher backed by existing repos."""

    def __init__(self, db: Session):
        self._db = db
        self._knowledge = KnowledgeRepository(db)
        self._prereqs = PrerequisiteRepository(db)
        self._events = AnswerEventRepository(db)

    # -- DataFetcher interface -----------------------------------------------

    def get_all_skill_ids(self) -> List[str]:
        hierarchy = self._knowledge.get_full_hierarchy()
        return list(hierarchy.keys())

    def get_all_prerequisites(self) -> Dict[Tuple[str, str], float]:
        return self._prereqs.get_all_prerequisites()

    def get_answer_events(
        self,
        since: datetime,
        until: datetime,
        offset: int = 0,
        limit: int = 10000,
    ) -> List[AnswerEvent]:
        return self._events.get_events_for_period(
            since=since,
            until=until,
            offset=offset,
            limit=limit,
        )
