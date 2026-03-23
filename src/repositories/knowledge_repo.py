"""
ALGO V1 — Knowledge & Prerequisite Repositories  (Step 10)
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from sqlalchemy import select

from src.models.knowledge import KnowledgeHierarchy, SkillPrerequisite
from src.repositories.base import BaseRepository

try:
    from src.models.dataclasses import SkillNode
except ImportError:
    # If dataclasses aren't moved yet, we can skip or mockup.
    SkillNode = None


class KnowledgeRepo(BaseRepository[KnowledgeHierarchy]):
    model = KnowledgeHierarchy

    def get_skills_by_level(self, level: str) -> List[KnowledgeHierarchy]:
        stmt = (
            select(KnowledgeHierarchy)
            .where(
                KnowledgeHierarchy.level == level,
                KnowledgeHierarchy.is_active.is_(True),
            )
            .order_by(KnowledgeHierarchy.sequence_order)
        )
        return list(self.session.scalars(stmt))

    def get_skills_by_class(self, class_level: int) -> List[KnowledgeHierarchy]:
        stmt = select(KnowledgeHierarchy).where(
            KnowledgeHierarchy.level == "L4",
            KnowledgeHierarchy.class_level_start <= class_level,
            KnowledgeHierarchy.class_level_end >= class_level,
            KnowledgeHierarchy.is_active.is_(True),
        )
        return list(self.session.scalars(stmt))

    def get_children(self, parent_id: str) -> List[KnowledgeHierarchy]:
        stmt = (
            select(KnowledgeHierarchy)
            .where(
                KnowledgeHierarchy.parent_id == parent_id,
                KnowledgeHierarchy.is_active.is_(True),
            )
            .order_by(KnowledgeHierarchy.sequence_order)
        )
        return list(self.session.scalars(stmt))


class PrerequisiteRepo(BaseRepository[SkillPrerequisite]):
    model = SkillPrerequisite

    def get_prerequisites(self, skill_id: str) -> List[SkillPrerequisite]:
        stmt = select(SkillPrerequisite).where(SkillPrerequisite.skill_id == skill_id)
        return list(self.session.scalars(stmt))

    def get_dependents(self, skill_id: str) -> List[SkillPrerequisite]:
        stmt = select(SkillPrerequisite).where(
            SkillPrerequisite.prerequisite_id == skill_id
        )
        return list(self.session.scalars(stmt))

    def get_all_prerequisites(self) -> Dict[Tuple[str, str], float]:
        prereqs = self.list_all(limit=10000)
        return {
            (p.prerequisite_id, p.skill_id): float(p.weight or 1.0) for p in prereqs
        }
