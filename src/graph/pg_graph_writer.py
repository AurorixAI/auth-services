"""
PostgreSQL implementation of GraphWriter interface.

Wraps GraphUpdateRepository and PrerequisiteRepository to persist
graph changes discovered by LearningGraphUpdater.
"""

import logging
from sqlalchemy.orm import Session

from src.models.repositories import (
    GraphUpdateRepository,
    PrerequisiteRepository,
)
from src.graph.learning_updater import GraphUpdate, GraphWriter

logger = logging.getLogger(__name__)


class PgGraphWriter(GraphWriter):
    """Sync PostgreSQL GraphWriter backed by existing repos."""

    def __init__(self, db: Session):
        self._db = db
        self._log = GraphUpdateRepository(db)
        self._prereqs = PrerequisiteRepository(db)

    # -- GraphWriter interface ------------------------------------------------

    def apply_update(self, update: GraphUpdate) -> bool:
        """
        Persist *update* to the database.

        Supports two entity types:
        - ``prerequisite`` — add or update a skill prerequisite link
        - ``*`` — fallback: just log the update for manual review

        Returns True on success.
        """
        try:
            # Always log the update
            self._log.log_update(update)

            if update.requires_review:
                # Don't touch the graph — just record for human review
                self._db.commit()
                return True

            # Auto-apply known entity types
            if update.entity_type == "prerequisite" and update.new_value:
                self._apply_prerequisite(update)

            self._db.commit()
            return True
        except Exception as exc:
            logger.error("GraphWriter.apply_update failed: %s", exc)
            self._db.rollback()
            return False

    # -- private helpers ------------------------------------------------------

    def _apply_prerequisite(self, update: GraphUpdate) -> None:
        nv = update.new_value or {}
        skill_id = nv.get("skill_id", "")
        prereq_id = nv.get("prerequisite_id", "")
        weight = nv.get("weight", 1.0)

        if not skill_id or not prereq_id:
            return

        if update.update_type == "add":
            self._prereqs.add_prerequisite(
                skill_id=skill_id,
                prerequisite_id=prereq_id,
                dependency_type=nv.get("dependency_type", "soft"),
                weight=weight,
                discovery_source=update.triggered_by.value,
            )
        elif update.update_type == "update":
            self._prereqs.update_weight(
                skill_id=skill_id,
                prerequisite_id=prereq_id,
                new_weight=weight,
                learned_weight=nv.get("learned_weight"),
                sample_size=nv.get("sample_size"),
                confidence=nv.get("confidence"),
            )
