"""
ALGO V1 — Custom Exception Hierarchy

All domain/service errors inherit from AlgoException.
The central exception handler in src/main.py maps them to HTTP status codes.

Services raise these — no HTTP imports required in the service layer.
"""

from fastapi import status as http_status
from typing import Type


# ── Base ──────────────────────────────────────────────────────────────────────


class AlgoException(Exception):
    """Base application exception. All ALGO errors inherit from this."""

    def __init__(self, message: str, code: str = "INTERNAL_ERROR") -> None:
        self.message = message
        self.code = code
        super().__init__(message)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


# ── Domain Exceptions ─────────────────────────────────────────────────────────


class NotFoundError(AlgoException):
    """Resource does not exist."""

    def __init__(self, entity: str, entity_id: str = "") -> None:
        detail = (
            f"{entity} not found"
            if not entity_id
            else f"{entity} '{entity_id}' not found"
        )
        super().__init__(detail, "NOT_FOUND")


class ConflictError(AlgoException):
    """Resource already exists or state conflict."""

    def __init__(self, message: str) -> None:
        super().__init__(message, "CONFLICT")


class ForbiddenError(AlgoException):
    """Caller lacks permission for the action."""

    def __init__(self, message: str = "Access denied") -> None:
        super().__init__(message, "FORBIDDEN")


class UnauthorizedError(AlgoException):
    """Caller is not authenticated."""

    def __init__(self, message: str = "Authentication required") -> None:
        super().__init__(message, "UNAUTHORIZED")


class ValidationError(AlgoException):
    """Business-level validation failure (distinct from Pydantic schema errors)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, "VALIDATION_ERROR")


class ServiceUnavailableError(AlgoException):
    """External dependency (Redis, Gemini, etc.) is unavailable."""

    def __init__(self, service: str) -> None:
        super().__init__(f"Service unavailable: {service}", "SERVICE_UNAVAILABLE")


class RateLimitError(AlgoException):
    """Request rate limit exceeded."""

    def __init__(self) -> None:
        super().__init__(
            "Rate limit exceeded. Please slow down.", "RATE_LIMIT_EXCEEDED"
        )


class AccountNotFoundError(AlgoException):
    """No user exists with the given email — distinct from a wrong password."""

    def __init__(self, message: str = "Аккаунт с таким email не найден") -> None:
        super().__init__(message, "ACCOUNT_NOT_FOUND")


class InvalidCredentialsError(AlgoException):
    """Email exists but the password does not match."""

    def __init__(self, message: str = "Неверный пароль") -> None:
        super().__init__(message, "INVALID_PASSWORD")


class AccountInactiveError(AlgoException):
    """Account exists but is deactivated/pending approval."""

    def __init__(self, message: str = "Аккаунт заблокирован") -> None:
        super().__init__(message, "ACCOUNT_INACTIVE")


# ── HTTP Status Mapping ───────────────────────────────────────────────────────
# Used by the central exception handler in src/api/main_v2.py

EXCEPTION_STATUS_MAP: dict[Type[AlgoException], int] = {
    NotFoundError: http_status.HTTP_404_NOT_FOUND,
    ConflictError: http_status.HTTP_409_CONFLICT,
    ForbiddenError: http_status.HTTP_403_FORBIDDEN,
    UnauthorizedError: http_status.HTTP_401_UNAUTHORIZED,
    ValidationError: http_status.HTTP_422_UNPROCESSABLE_ENTITY,
    ServiceUnavailableError: http_status.HTTP_503_SERVICE_UNAVAILABLE,
    RateLimitError: http_status.HTTP_429_TOO_MANY_REQUESTS,
    AccountNotFoundError: http_status.HTTP_404_NOT_FOUND,
    InvalidCredentialsError: http_status.HTTP_401_UNAUTHORIZED,
    AccountInactiveError: http_status.HTTP_403_FORBIDDEN,
}
