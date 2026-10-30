"""
Tests for auth-service — JWT, bcrypt, RBAC, rate limiting
Coverage target: ≥70%

Run:
    cd auth-service
    pip install pytest pytest-cov
    pytest tests/ -v --cov=src --cov-report=term-missing
"""
import os
import time
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

# ── Patch env before importing service modules ────────────────────────────────
os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

from src.auth.service import (  # noqa: E402
    hash_password,
    verify_password,
    hash_token,
    generate_code,
    create_access_token,
    create_refresh_token,
    decode_token,
    verify_access_token,
    verify_refresh_token,
)
from src.api.rate_limit import _parse_rate, _check_memory, _memory_store  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════
# PASSWORD HASHING
# ═══════════════════════════════════════════════════════════════════════════


class TestPasswordHashing:
    def test_hash_password_returns_string(self):
        result = hash_password("mypassword123")
        assert isinstance(result, str)
        assert len(result) > 20

    def test_hash_password_is_bcrypt(self):
        result = hash_password("mypassword123")
        assert result.startswith("$2b$") or result.startswith("$2a$")

    def test_hash_is_not_plaintext(self):
        result = hash_password("mypassword123")
        assert result != "mypassword123"

    def test_same_password_different_hash(self):
        """bcrypt uses salt — each hash is unique"""
        h1 = hash_password("same")
        h2 = hash_password("same")
        assert h1 != h2

    def test_verify_correct_password(self):
        hashed = hash_password("correct_password")
        assert verify_password("correct_password", hashed) is True

    def test_verify_wrong_password(self):
        hashed = hash_password("correct_password")
        assert verify_password("wrong_password", hashed) is False

    def test_verify_empty_password_fails(self):
        hashed = hash_password("nonempty")
        assert verify_password("", hashed) is False


# ═══════════════════════════════════════════════════════════════════════════
# TOKEN UTILS
# ═══════════════════════════════════════════════════════════════════════════


class TestTokenUtils:
    def test_hash_token_is_deterministic(self):
        assert hash_token("abc") == hash_token("abc")

    def test_hash_token_different_inputs(self):
        assert hash_token("abc") != hash_token("xyz")

    def test_hash_token_is_hex_64_chars(self):
        result = hash_token("test")
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_generate_code_default_digits_only(self):
        code = generate_code(6)
        assert len(code) == 6
        assert code.isdigit()

    def test_generate_code_custom_length(self):
        code = generate_code(10)
        assert len(code) == 10

    def test_generate_code_alphanumeric(self):
        code = generate_code(8, digits_only=False)
        assert len(code) == 8
        # Should contain letters or digits (alphanumeric)
        assert code.isalnum()

    def test_generate_code_is_random(self):
        codes = {generate_code(8) for _ in range(20)}
        # With 8-digit codes, chance of all being same is astronomically low
        assert len(codes) > 1


# ═══════════════════════════════════════════════════════════════════════════
# JWT — CREATE & DECODE
# ═══════════════════════════════════════════════════════════════════════════


class TestJWT:
    USER_ID = "user-123"
    ROLE = "teacher"
    ORG_ID = "org-456"
    ORG_ROLE = "teacher"

    def test_create_access_token_returns_string(self):
        token = create_access_token(self.USER_ID, self.ROLE)
        assert isinstance(token, str)
        assert len(token) > 50

    def test_access_token_has_correct_payload(self):
        token = create_access_token(self.USER_ID, self.ROLE, self.ORG_ID, self.ORG_ROLE)
        payload = decode_token(token)
        assert payload is not None
        assert payload["sub"] == self.USER_ID
        assert payload["role"] == self.ROLE
        assert payload["org_id"] == self.ORG_ID
        assert payload["org_role"] == self.ORG_ROLE
        assert payload["type"] == "access"

    def test_access_token_without_org(self):
        token = create_access_token(self.USER_ID, self.ROLE)
        payload = decode_token(token)
        assert payload is not None
        assert "org_id" not in payload
        assert "org_role" not in payload

    def test_create_refresh_token_returns_string(self):
        token = create_refresh_token(self.USER_ID)
        assert isinstance(token, str)

    def test_refresh_token_has_correct_type(self):
        token = create_refresh_token(self.USER_ID)
        payload = decode_token(token)
        assert payload is not None
        assert payload["type"] == "refresh"
        assert payload["sub"] == self.USER_ID

    def test_refresh_token_has_jti(self):
        """Each refresh token must have a unique jti for revocation"""
        t1 = create_refresh_token(self.USER_ID)
        t2 = create_refresh_token(self.USER_ID)
        p1 = decode_token(t1)
        p2 = decode_token(t2)
        assert p1["jti"] != p2["jti"]

    def test_decode_invalid_token_returns_none(self):
        assert decode_token("not.a.valid.token") is None

    def test_decode_empty_string_returns_none(self):
        assert decode_token("") is None

    def test_decode_tampered_token_returns_none(self):
        token = create_access_token(self.USER_ID, self.ROLE)
        tampered = token[:-5] + "XXXXX"
        assert decode_token(tampered) is None

    def test_verify_access_token_accepts_access(self):
        token = create_access_token(self.USER_ID, self.ROLE)
        payload = verify_access_token(token)
        assert payload is not None
        assert payload["sub"] == self.USER_ID

    def test_verify_access_token_rejects_refresh(self):
        """Must NOT accept a refresh token as an access token"""
        token = create_refresh_token(self.USER_ID)
        assert verify_access_token(token) is None

    def test_verify_refresh_token_accepts_refresh(self):
        token = create_refresh_token(self.USER_ID)
        payload = verify_refresh_token(token)
        assert payload is not None
        assert payload["sub"] == self.USER_ID

    def test_verify_refresh_token_rejects_access(self):
        """Must NOT accept an access token as a refresh token"""
        token = create_access_token(self.USER_ID, self.ROLE)
        assert verify_refresh_token(token) is None

    def test_expired_token_returns_none(self):
        """Token expired 1 second ago must be rejected"""
        token = create_access_token(
            self.USER_ID, self.ROLE, expires_delta=timedelta(seconds=-1)
        )
        assert decode_token(token) is None

    def test_custom_expiry_respected(self):
        """Token with long expiry must decode successfully"""
        token = create_access_token(
            self.USER_ID, self.ROLE, expires_delta=timedelta(hours=24)
        )
        payload = decode_token(token)
        assert payload is not None


# ═══════════════════════════════════════════════════════════════════════════
# RBAC — RoleChecker
# ═══════════════════════════════════════════════════════════════════════════


class TestRBAC:
    def _make_payload(self, role: str, org_role: str = None, org_id: str = None) -> dict:
        """Build a mock JWT payload"""
        p = {"sub": "user-1", "role": role, "type": "access"}
        if org_role:
            p["org_role"] = org_role
        if org_id:
            p["org_id"] = org_id
        return p

    def _run_checker(self, checker, payload: dict):
        """Call RoleChecker with a mock payload dependency"""
        return checker(payload=payload)

    def test_owner_passes_owner_checker(self):
        from src.auth.dependencies import require_org_owner
        payload = self._make_payload("owner", org_role="owner", org_id="org-1")
        result = self._run_checker(require_org_owner, payload)
        assert result["user_id"] == "user-1"

    def test_teacher_fails_owner_checker(self):
        from src.auth.dependencies import require_org_owner
        payload = self._make_payload("teacher", org_role="teacher", org_id="org-1")
        with pytest.raises(HTTPException) as exc_info:
            self._run_checker(require_org_owner, payload)
        assert exc_info.value.status_code == 403

    def test_admin_passes_admin_checker(self):
        from src.auth.dependencies import require_org_admin
        payload = self._make_payload("admin", org_role="admin", org_id="org-1")
        result = self._run_checker(require_org_admin, payload)
        assert result["org_role"] == "admin"

    def test_teacher_fails_admin_checker(self):
        from src.auth.dependencies import require_org_admin
        payload = self._make_payload("teacher", org_role="teacher", org_id="org-1")
        with pytest.raises(HTTPException) as exc_info:
            self._run_checker(require_org_admin, payload)
        assert exc_info.value.status_code == 403

    def test_teacher_passes_teacher_checker(self):
        from src.auth.dependencies import require_org_teacher
        payload = self._make_payload("teacher", org_role="teacher", org_id="org-1")
        result = self._run_checker(require_org_teacher, payload)
        assert result["user_id"] == "user-1"

    def test_missing_org_id_fails_org_checker(self):
        from src.auth.dependencies import require_org_owner
        # owner role but no org_id
        payload = self._make_payload("owner", org_role="owner")
        with pytest.raises(HTTPException) as exc_info:
            self._run_checker(require_org_owner, payload)
        assert exc_info.value.status_code == 403

    def test_result_contains_expected_keys(self):
        from src.auth.dependencies import require_org_teacher
        payload = self._make_payload("teacher", org_role="teacher", org_id="org-5")
        result = self._run_checker(require_org_teacher, payload)
        assert set(result.keys()) == {"user_id", "role", "org_id", "org_role"}
        assert result["org_id"] == "org-5"


# ═══════════════════════════════════════════════════════════════════════════
# RATE LIMITING
# ═══════════════════════════════════════════════════════════════════════════


class TestRateLimit:
    def setup_method(self):
        """Clear in-memory store before each test"""
        _memory_store.clear()

    def test_parse_rate_minute(self):
        count, window = _parse_rate("5/minute")
        assert count == 5
        assert window == 60

    def test_parse_rate_hour(self):
        count, window = _parse_rate("3/hour")
        assert count == 3
        assert window == 3600

    def test_parse_rate_second(self):
        count, window = _parse_rate("10/second")
        assert count == 10
        assert window == 1

    def test_parse_rate_day(self):
        count, window = _parse_rate("100/day")
        assert count == 100
        assert window == 86400

    def test_parse_rate_invalid_period_raises(self):
        with pytest.raises((ValueError, KeyError)):
            _parse_rate("5/century")

    def test_memory_allows_within_limit(self):
        key = "test-key-allow"
        for _ in range(5):
            assert _check_memory(key, 5, 60) is True

    def test_memory_blocks_over_limit(self):
        key = "test-key-block"
        for _ in range(5):
            _check_memory(key, 5, 60)
        # 6th request must be blocked
        assert _check_memory(key, 5, 60) is False

    def test_memory_allows_after_window_expires(self):
        key = "test-key-expire"
        # Fill up
        for _ in range(3):
            _check_memory(key, 3, 1)
        assert _check_memory(key, 3, 1) is False

        # Simulate time passing by backdating entries
        _memory_store[key] = [t - 2 for t in _memory_store[key]]
        # Now window expired → should allow again
        assert _check_memory(key, 3, 1) is True

    def test_rate_limit_dependency_passes_within_limit(self):
        from src.api.rate_limit import rate_limit
        from unittest.mock import MagicMock

        dep = rate_limit("10/minute")
        request = MagicMock()
        request.headers.get.return_value = None
        request.client.host = "127.0.0.1"
        request.url.path = "/test"

        # Should not raise for first 10 calls
        for _ in range(10):
            dep(request)

    def test_rate_limit_dependency_raises_429_when_exceeded(self):
        from src.api.rate_limit import rate_limit
        from unittest.mock import MagicMock

        dep = rate_limit("3/minute", endpoint="/login")
        request = MagicMock()
        request.headers.get.return_value = None
        request.client.host = "10.0.0.1"
        request.url.path = "/login"

        # Exhaust limit
        for _ in range(3):
            dep(request)

        with pytest.raises(HTTPException) as exc_info:
            dep(request)
        assert exc_info.value.status_code == 429
