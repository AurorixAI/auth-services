"""
ALGO V1 — API v1 Master Router

Assembles all domain routers under /api/v1.
"""

from fastapi import APIRouter

from src.api.v1.auth.router import router as auth_router

# Future domain routers — uncomment as they are refactored:
# from src.api.v1.cat.router import router as cat_router
# from src.api.v1.exam.router import router as exam_router
# from src.api.v1.textbook.router import router as textbook_router
# from src.api.v1.analytics.router import router as analytics_router
# from src.api.v1.skills.router import router as skills_router

v1_router = APIRouter(prefix="/api/v1")

v1_router.include_router(auth_router)

# v1_router.include_router(cat_router)
# v1_router.include_router(exam_router)
# v1_router.include_router(textbook_router)
# v1_router.include_router(analytics_router)
# v1_router.include_router(skills_router)
