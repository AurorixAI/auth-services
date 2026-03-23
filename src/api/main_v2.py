"""
ALGO V1 - FastAPI REST API (Production Ready)
Веб-API для платформы диагностики и тестирования с реальной интеграцией.
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

# Analytics
from src.api.analytics_router import router as analytics_router

# CAT Engine
from src.api.cat_router import router as cat_router

# Centralised database config (single source of truth)
from src.core.database import get_db

# Textbook Exam API (Module 2b)
from src.api.textbook_exam import router as textbook_exam_router

# Textbook API
from src.api.textbook_router import router as textbook_router

# Dev (quick login)
from src.auth.dev_router import router as dev_router

# Auth
from src.auth.router import router as auth_router

# Настройка логирования
logging.basicConfig(level=logging.INFO)
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
    logger.info("Starting ALGO V1 API...")

    # Инициализация моделей (только если таблицы не существуют)
    # from src.models import Base
    # Base.metadata.create_all(bind=engine)

    yield

    # Shutdown
    logger.info("Shutting down ALGO V1 API...")


# ============================================================================
# ПРИЛОЖЕНИЕ
# ============================================================================

app = FastAPI(
    title="ALGO V1 API",
    description="Профессиональный API для диагностики математических навыков и генерации тестов",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# CORS
_allowed_origins = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
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
app.include_router(cat_router, prefix="/api/v1")
app.include_router(textbook_router, prefix="/api/v1")
app.include_router(textbook_exam_router, prefix="/api/v1")
app.include_router(analytics_router, prefix="/api/v1")

# ─── Dev Router (quick login) — only in development ───
if os.getenv("APP_ENV", "production") == "development":
    app.include_router(dev_router, prefix="/api/v1")
    logger.warning("⚠️  Dev router enabled — disable in production (APP_ENV=production)")

# ─── Exam Router ───
from src.api.exam_router import router as exam_router

app.include_router(exam_router, prefix="/api/v1")

# ─── Skills Router ───
from src.api.skills_router import router as skills_router

app.include_router(skills_router, prefix="/api/v1")


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


@app.get("/health", response_model=HealthResponse, tags=["System"])
async def health_check(db: Session = Depends(get_db)):
    """Проверка здоровья сервиса"""
    try:
        # Проверяем подключение к БД
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


# ============================================================================

# ============================================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=settings.DEBUG)
