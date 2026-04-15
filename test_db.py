import asyncio
from src.core.database import SessionLocal
from src.repositories.knowledge_repo import KnowledgeRepo
from src.models.knowledge import KnowledgeHierarchy
from sqlalchemy import select

def test():
    db = SessionLocal()
    repo = KnowledgeRepo(db)
    
    # Check all active skills
    stmt1 = select(KnowledgeHierarchy).limit(5)
    skills = db.scalars(stmt1).all()
    print("Found any skills?", len(skills))
    if skills:
        print("Sample skill level:", skills[0].level, "is_active:", skills[0].is_active)
        
    # Check L4 specifically
    stmt2 = select(KnowledgeHierarchy).where(KnowledgeHierarchy.level == 'L4')
    l4_skills = db.scalars(stmt2).all()
    print("Found L4 skills?", len(l4_skills))

    # Check is_active == True
    stmt3 = select(KnowledgeHierarchy).where(KnowledgeHierarchy.level == 'L4', KnowledgeHierarchy.is_active == True)
    active_l4 = db.scalars(stmt3).all()
    print("Found Active L4?", len(active_l4))

    # Check is_active.is_(True)
    stmt4 = select(KnowledgeHierarchy).where(KnowledgeHierarchy.level == 'L4', KnowledgeHierarchy.is_active.is_(True))
    is_true_l4 = db.scalars(stmt4).all()
    print("Found is_(True) L4?", len(is_true_l4))
    
    graph = repo.get_skill_graph()
    print("Graph length:", len(graph))

test()
