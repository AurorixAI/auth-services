"""
ALGO V1 — Application Settings (Pydantic BaseSettings)

Usage:
    from src.core.config import get_settings
    settings = get_settings()         # cached singleton

    # As a FastAPI dependency:
    from fastapi import Depends
    def my_route(s: Settings = Depends(get_settings)): ...
"""

from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # silently ignore unknown env vars
    )

    # ── Application ───────────────────────────────────────────────────────────
    app_name: str = "ALGO Auth Service"
    app_version: str = "1.0.0"
    app_env: str = Field(
        default="production",
        pattern="^(development|staging|production|testing)$",
    )
    debug: bool = False

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = "postgresql://postgres:password@localhost:5432/algo_v1"

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379"
    redis_host: str = "localhost"
    redis_port: int = 6379

    # ── Auth — JWT ────────────────────────────────────────────────────────────
    secret_key: str = Field(
        default="change-me-in-production-must-be-at-least-32-chars",
        min_length=32,
    )
    access_token_expire_minutes: int = 60
    refresh_token_expire_days: int = 30
    algorithm: str = "HS256"

    # ── External APIs ─────────────────────────────────────────────────────────
    mathpix_app_id: str = ""
    mathpix_app_key: str = ""
    gemini_api_key: str = ""
    # Exam-service receives authoritative lifecycle notifications from this
    # service after an attendance-backed lesson cancellation.
    exam_service_url: str = "http://exam-service:8003/api/v1"
    internal_service_token: str = ""

    # ── Organization onboarding ───────────────────────────────────────────────
    # When True, a new org application is activated immediately (owner can log
    # in right away with the credentials they registered with) instead of
    # sitting in "pending" for manual admin review. The manual review path
    # (approve_organization / approve_org.sh) stays fully functional — flip
    # this back to False to require it again, no code changes needed.
    auto_approve_organizations: bool = Field(default=True)

    # ── CORS ──────────────────────────────────────────────────────────────────
    cors_origins: List[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    # ── Derived properties ────────────────────────────────────────────────────
    @property
    def is_development(self) -> bool:
        return self.app_env == "development"

    @property
    def is_testing(self) -> bool:
        return self.app_env == "testing"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    """
    Cached settings singleton.

    The .env file is parsed exactly once at startup.
    Missing required fields raise a ValidationError immediately.
    """
    return Settings()
