"""
ALGO V1 — API v1 Master Router

Assembles all domain routers under /api/v1.
"""

import os

from fastapi import APIRouter

from src.api.v1.auth.router import router as auth_router
from src.auth.router import router as legacy_auth_router

# Legacy routers (not yet refactored into src/api/v1/*)
from src.api.cat_router import router as cat_router
from src.api.exam_router import router as exam_router
from src.api.textbook_router import router as textbook_router
from src.api.textbook_exam import router as textbook_exam_router
from src.api.analytics_router import router as analytics_router
from src.api.skills_router import router as skills_router

v1_router = APIRouter(prefix="/api/v1")

v1_router.include_router(auth_router)
v1_router.include_router(legacy_auth_router)
v1_router.include_router(cat_router)
v1_router.include_router(exam_router)
v1_router.include_router(textbook_router)
v1_router.include_router(textbook_exam_router)
v1_router.include_router(analytics_router)
v1_router.include_router(skills_router)

# Dev-login — only in development
if os.getenv("APP_ENV", "production") == "development":
    from src.auth.dev_router import router as dev_router
    v1_router.include_router(dev_router)
# v1_router.include_router(textbook_router)
# v1_router.include_router(analytics_router)
# v1_router.include_router(skills_router)
