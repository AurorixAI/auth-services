"""The whole /api/v1/platform/* API: one router, guarded by the console host (``console_guard``)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from . import (
    platform_audit_router,
    platform_auth_router,
    platform_demo_router,
    platform_orgs_router,
    platform_team_router,
    platform_users_router,
)
from . import support_console_router
from .platform_security import console_guard

router = APIRouter(prefix="/platform", tags=["Platform console"], dependencies=[Depends(console_guard)])
router.include_router(platform_auth_router.router)
router.include_router(platform_demo_router.router)
router.include_router(platform_orgs_router.router)
router.include_router(platform_users_router.router)
router.include_router(platform_team_router.router)
router.include_router(platform_audit_router.router)
router.include_router(support_console_router.router)
