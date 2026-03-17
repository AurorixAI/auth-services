"""
ALGO V1 — Skills Graph API
Эндпоинты для работы с графом навыков.
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional
from sqlalchemy.orm import Session

from src.api.database import get_db

router = APIRouter(tags=["Skills"])


# ============================================================================
# PYDANTIC МОДЕЛИ
# ============================================================================

class SkillNodeResponse(BaseModel):
    """Узел в графе навыков"""
    skill_id: str
    name: str
    name_ru: str
    level: str
    parent_id: Optional[str]
    prerequisites: List[str]
    class_level_start: Optional[int]
    class_level_end: Optional[int]
    importance: int


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.get(
    "/skills",
    response_model=List[SkillNodeResponse],
    summary="Получить все навыки",
)
async def get_all_skills(
    level: Optional[str] = None,
    class_level: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """Получает все навыки из графа"""
    from src.models.repositories import KnowledgeRepository

    repo = KnowledgeRepository(db)

    if class_level:
        skills = repo.get_skills_by_class(class_level)
    elif level:
        skills = repo.get_skills_by_level(level)
    else:
        hierarchy = repo.get_full_hierarchy()
        skills = list(hierarchy.values())

    return [
        SkillNodeResponse(
            skill_id=s.id,
            name=s.name,
            name_ru=s.name_ru,
            level=s.level,
            parent_id=s.parent_id,
            prerequisites=[p.prerequisite_id for p in s.prerequisites] if hasattr(s, 'prerequisites') else [],
            class_level_start=s.class_level_start,
            class_level_end=s.class_level_end,
            importance=s.importance or 5,
        )
        for s in skills
    ]


@router.get(
    "/skills/{skill_id}",
    response_model=SkillNodeResponse,
    summary="Получить навык по ID",
)
async def get_skill(skill_id: str, db: Session = Depends(get_db)):
    """Получает информацию о конкретном навыке"""
    from src.models.repositories import KnowledgeRepository

    repo = KnowledgeRepository(db)
    skill = repo.get_skill_by_id(skill_id)

    if not skill:
        raise HTTPException(status_code=404, detail="Навык не найден")

    return SkillNodeResponse(
        skill_id=skill.id,
        name=skill.name,
        name_ru=skill.name_ru,
        level=skill.level,
        parent_id=skill.parent_id,
        prerequisites=[p.prerequisite_id for p in skill.prerequisites],
        class_level_start=skill.class_level_start,
        class_level_end=skill.class_level_end,
        importance=skill.importance or 5,
    )


@router.get(
    "/skills/{skill_id}/prerequisites",
    summary="Получить пререквизиты навыка",
)
async def get_skill_prerequisites(skill_id: str, db: Session = Depends(get_db)):
    """Получает пререквизиты навыка с весами"""
    from src.models.repositories import PrerequisiteRepository

    repo = PrerequisiteRepository(db)
    prereqs = repo.get_prerequisites(skill_id)

    return [
        {
            "prerequisite_id": p.prerequisite_id,
            "dependency_type": p.dependency_type,
            "weight": float(p.weight or 1.0),
            "criticality": p.criticality,
            "discovery_source": p.discovery_source,
        }
        for p in prereqs
    ]
