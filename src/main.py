"""
ALGO V1 — Application Entry Point  (Step 18)

Replaces src/api/main_v2.py with a clean app factory pattern.

The old main_v2.py is kept in place as a fallback during the transition.
Once all routers are moved to src/api/v1/, remove main_v2.py and update:
  - Dockerfile: CMD uvicorn src.main:app ...
  - docker-compose.*.yml: command: uvicorn src.main:app ...
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.core.config import get_settings
from src.core.exceptions import AlgoException, EXCEPTION_STATUS_MAP
from src.api.v1.router import v1_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    logger.info("Starting %s v%s (env=%s)", s.app_name, s.app_version, s.app_env)
    yield
    logger.info("Shutting down %s", s.app_name)


def create_app() -> FastAPI:
    """
    Application factory.

    Usage:
        app = create_app()         # production
        client = TestClient(create_app())  # tests
    """
    s = get_settings()

    app = FastAPI(
        title=s.app_name,
        version=s.app_version,
        description="ALGO V1 — Adaptive Math Diagnostic & Exam Platform",
        # Hide docs in production for security
        docs_url="/docs" if s.is_development else None,
        redoc_url="/redoc" if s.is_development else None,
        openapi_url="/openapi.json" if s.is_development else None,
        lifespan=lifespan,
    )

    # ── CORS ─────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Domain exception → HTTP status mapping ────────────────────────────
    @app.exception_handler(AlgoException)
    async def algo_exception_handler(request: Request, exc: AlgoException):
        status_code = EXCEPTION_STATUS_MAP.get(type(exc), 500)
        return JSONResponse(
            status_code=status_code,
            content={"detail": exc.message, "code": exc.code},
        )

    # ── Health check ──────────────────────────────────────────────────────
    @app.get("/health", tags=["System"])
    async def health():
        return {"status": "ok", "version": s.app_version, "env": s.app_env}

    # ── Routers ───────────────────────────────────────────────────────────
    app.include_router(v1_router)

    return app


# Module-level app instance for uvicorn
app = create_app()
