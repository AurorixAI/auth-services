"""
ALGO Auth Service — FastAPI REST API
Аутентификация, организации, студенты, группы, diagnostic invites.
"""

from dotenv import load_dotenv

load_dotenv()  # Загрузить .env до чтения os.getenv()

import logging
import os
import traceback
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

# Centralised database config (single source of truth)
from src.core.database import get_db

# Dev (quick login)
from src.auth.dev_router import router as dev_router

# Auth
from src.auth.router import router as auth_router
from src.auth.attendance_excuses_router import router as attendance_excuses_router
from src.auth.payments_router import router as payments_router
from src.api.internal_router import router as internal_router

# Настройка логирования через structlog
from src.core.middleware import configure_logging, RequestIDMiddleware
configure_logging(is_development=os.getenv("APP_ENV", "production") == "development")
logger = logging.getLogger(__name__)


# ============================================================================
# КОНФИГУРАЦИЯ
# ============================================================================


class Settings:
    """Настройки приложения (non-DB settings live here; DB is in database.py)"""

    MATHPIX_APP_ID: str = os.getenv("MATHPIX_APP_ID", "")
    MATHPIX_APP_KEY: str = os.getenv("MATHPIX_APP_KEY", "")
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"


settings = Settings()


# ============================================================================
# LIFESPAN
# ============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle управление приложением"""
    # Startup
    logger.info("Starting ALGO Auth Service...")

    # Применяем миграции Alembic (только новые, идемпотентно)
    from alembic.config import Config
    from alembic import command

    alembic_cfg = Config("alembic.ini")
    try:
        command.upgrade(alembic_cfg, "head")
        logger.info("Alembic migrations applied (up to head).")
    except Exception as e:
        logger.error("Alembic migration failed: %s", e)
        raise

    # Ensure all newly added tables are created
    try:
        from src.core.database import engine
        from src.models.auth import Base
        Base.metadata.create_all(bind=engine)
        logger.info("Database tables verified/created.")
    except Exception as e:
        logger.error("Failed to verify/create database tables: %s", e)

    yield

    # Shutdown
    logger.info("Shutting down ALGO Auth Service...")


# ============================================================================
# ПРИЛОЖЕНИЕ
# ============================================================================

_is_dev = os.getenv("APP_ENV", "production") == "development"
app = FastAPI(
    title="ALGO Auth Service",
    description="Аутентификация, управление организациями, студентами, группами и diagnostic invites.",
    version="1.0.0",
    docs_url="/docs" if _is_dev else None,
    redoc_url="/redoc" if _is_dev else None,
    lifespan=lifespan,
)

# CORS — use core settings (handles JSON list from .env correctly)
from src.core.config import get_settings as _get_settings
_allowed_origins = _get_settings().cors_origins
app.add_middleware(RequestIDMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Prometheus metrics
from prometheus_fastapi_instrumentator import Instrumentator
Instrumentator(excluded_handlers=["/metrics", "/health/live", "/health/ready"]).instrument(app).expose(app)


def _cors_headers(request) -> dict:
    """Build CORS headers for error responses so the browser shows the real error."""
    origin = request.headers.get("origin", "")
    if origin in _allowed_origins or "*" in _allowed_origins:
        return {
            "Access-Control-Allow-Origin": origin or _allowed_origins[0],
            "Access-Control-Allow-Credentials": "true",
        }
    return {}


# Handle HTTPException with CORS headers (401, 403, 404 etc.)
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    # exc.headers carries Retry-After (429) / WWW-Authenticate (401): the CORS headers must not replace them
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers={**(exc.headers or {}), **_cors_headers(request)},
    )


# Handle domain/service errors (InvalidCredentialsError, etc.)
# Detail is {code, message} so the frontend can branch on `code` instead of
# pattern-matching the (Russian) message text.
from src.core.exceptions import AlgoException, EXCEPTION_STATUS_MAP


@app.exception_handler(AlgoException)
async def algo_exception_handler(request, exc: AlgoException):
    status_code = EXCEPTION_STATUS_MAP.get(type(exc), 400)
    return JSONResponse(
        status_code=status_code,
        content={"detail": {"code": exc.code, "message": exc.message}},
        headers=_cors_headers(request),
    )


# Global exception handler — show traceback in dev only
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    logger.error(f"Unhandled error: {''.join(tb)}")
    detail = {"detail": str(exc)}
    if settings.DEBUG:
        detail["traceback"] = tb[-3:]
    return JSONResponse(status_code=500, content=detail, headers=_cors_headers(request))


# ─── Routers ───
app.include_router(auth_router, prefix="/api/v1")
app.include_router(attendance_excuses_router, prefix="/api/v1")
app.include_router(payments_router, prefix="/api/v1")
app.include_router(internal_router, prefix="/api/v1")
# analytics_router removed — analytics live in diagnostic-service (port 8002)

# ─── Dev Router (quick login) — dev environment only, never in staging/prod ───
if _is_dev:
    app.include_router(dev_router, prefix="/api/v1")



# ============================================================================
# PYDANTIC МОДЕЛИ (только для оставшихся эндпоинтов)
# ============================================================================


class HealthResponse(BaseModel):
    """Проверка здоровья"""

    status: str
    version: str
    database: str
    timestamp: datetime


# ============================================================================
# ENDPOINTS: HEALTH
# ============================================================================


@app.get("/health/live", tags=["System"], summary="Liveness probe")
def health_live():
    """Always 200 — no dependency checks."""
    return {"status": "alive"}


@app.get("/version", tags=["System"], summary="Deployed release metadata")
def release_version():
    """Return non-secret metadata for the exact deployed auth-service release.

    This endpoint deliberately does not replace ``/health``: health probes
    answer whether dependencies are available, while release metadata answers
    which immutable source revision is currently running.
    """
    return {
        "service": "auth-service",
        "app_version": "1.0.0",
        "release_sha": os.getenv("RELEASE_SHA", "unknown"),
        "release_ref": os.getenv("RELEASE_REF", "unknown"),
        "released_at": os.getenv("RELEASED_AT", "unknown"),
    }


@app.get("/health/ready", tags=["System"], summary="Readiness probe")
async def health_ready(db: Session = Depends(get_db)):
    """Returns 200 if DB reachable, 503 otherwise."""
    db_ok = True
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("Readiness DB check failed: %s", exc)
        db_ok = False

    payload = {
        "status": "ready" if db_ok else "not_ready",
        "version": "1.0.0",
        "database": "ok" if db_ok else "error",
    }
    return JSONResponse(status_code=200 if db_ok else 503, content=payload)


@app.get("/health", response_model=HealthResponse, tags=["System"], summary="Health check (legacy)")
async def health_check(db: Session = Depends(get_db)):
    """Backward-compatible health check."""
    try:
        db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"

    return HealthResponse(
        status="healthy" if db_status == "connected" else "degraded",
        version="1.0.0",
        database=db_status,
        timestamp=datetime.now(),
    )

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=settings.DEBUG)
