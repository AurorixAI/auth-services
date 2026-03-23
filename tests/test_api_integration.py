"""
ALGO V1 — Integration Tests
Tests real API endpoints via FastAPI TestClient + PostgreSQL.

Requirements:
- PostgreSQL running with algo_v1 database
- Tables created via migrations

Run:
    python -m pytest tests/test_api_integration.py -v
"""

import os

# Ensure we're in development mode so dev_router is available
os.environ.setdefault("APP_ENV", "development")

from fastapi.testclient import TestClient
from src.api.main_v2 import app

client = TestClient(app)


# ============================================================================
# HEALTH
# ============================================================================


class TestHealth:
    """Health endpoint."""

    def test_health_returns_200(self):
        r = client.get("/health")
        assert r.status_code == 200

    def test_health_has_status_field(self):
        data = client.get("/health").json()
        assert "status" in data
        assert data["status"] in ("healthy", "degraded")

    def test_health_reports_database(self):
        data = client.get("/health").json()
        assert data["database"] == "connected"


# ============================================================================
# SKILLS
# ============================================================================


class TestSkills:
    """Skill graph endpoints."""

    def test_list_all_skills(self):
        r = client.get("/api/v1/skills")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_filter_skills_by_class(self):
        r = client.get("/api/v1/skills", params={"class_level": 7})
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)

    def test_get_nonexistent_skill(self):
        r = client.get("/api/v1/skills/NONEXISTENT_SKILL_ID_12345")
        assert r.status_code == 404

    def test_skill_prerequisites(self):
        # First, get a real skill to test against
        skills = client.get("/api/v1/skills").json()
        if skills:
            skill_id = skills[0]["skill_id"]
            r = client.get(f"/api/v1/skills/{skill_id}/prerequisites")
            assert r.status_code == 200
            assert isinstance(r.json(), list)

    def test_skill_response_shape(self):
        skills = client.get("/api/v1/skills").json()
        if skills:
            s = skills[0]
            assert "skill_id" in s
            assert "name" in s
            assert "name_ru" in s
            assert "level" in s
            assert "prerequisites" in s


# ============================================================================
# EXAMS
# ============================================================================


class TestExams:
    """Exam CRUD endpoints."""

    def test_list_exams(self):
        r = client.get("/api/v1/exams")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_join_with_bad_code(self):
        r = client.get("/api/v1/exams/by-code/INVALID_CODE_999")
        assert r.status_code == 404

    def test_analytics_nonexistent_exam(self):
        r = client.get("/api/v1/exams/NONEXISTENT/analytics")
        assert r.status_code == 404


# ============================================================================
# CAT (Diagnostic)
# ============================================================================


class TestCAT:
    """CAT adaptive testing endpoints."""

    def test_start_cat_session(self):
        r = client.post(
            "/api/v1/cat/start",
            json={
                "class_level": 7,
                "max_items": 10,
            },
        )
        # Should succeed or return 422 if no tasks for class 7
        assert r.status_code in (200, 201, 422)

    def test_get_nonexistent_session(self):
        r = client.get("/api/v1/cat/session/NONEXISTENT_SESSION")
        assert r.status_code == 404


# ============================================================================
# AUTH
# ============================================================================


class TestAuth:
    """Authentication endpoints."""

    def test_login_missing_fields(self):
        r = client.post("/api/v1/auth/login", json={})
        assert r.status_code == 422  # Validation error

    def test_login_wrong_credentials(self):
        r = client.post(
            "/api/v1/auth/login",
            json={
                "email": "nonexistent@test.com",
                "password": "wrong_password",
            },
        )
        # Should return 401 or 404
        assert r.status_code in (401, 404)


# ============================================================================
# TEXTBOOKS
# ============================================================================


class TestTextbooks:
    """Textbook endpoints."""

    def test_list_textbooks(self):
        r = client.get("/api/v1/textbooks/")
        assert r.status_code == 200
        data = r.json()
        # Response is {"textbooks": [...], "total": N}
        assert "textbooks" in data
        assert isinstance(data["textbooks"], list)


# ============================================================================
# CORS
# ============================================================================


class TestCORS:
    """CORS headers on error responses."""

    def test_cors_on_404(self):
        r = client.get(
            "/api/v1/exams/by-code/NO_SUCH_CODE",
            headers={"Origin": "http://localhost:3000"},
        )
        assert r.status_code == 404
        # CORS headers should be set
        assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"
