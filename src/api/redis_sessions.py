"""
Redis-backed CAT Session Storage

Хранение CAT-сессий в Redis с TTL, сериализацией и rate limiting.
Fallback на in-memory dict если Redis недоступен.

Security: используется HMAC-подписанный pickle вместо raw pickle,
чтобы предотвратить RCE при компрометации Redis.
"""

import hashlib
import hmac
import os
import pickle
import time
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# HMAC key для подписи сериализованных данных
_SIGNING_KEY = os.getenv("SECRET_KEY", "algo-v1-dev-secret-key-change-in-prod").encode()


def _sign_data(data: bytes) -> bytes:
    """Подписывает данные HMAC-SHA256 и возвращает signature + data."""
    sig = hmac.new(_SIGNING_KEY, data, hashlib.sha256).digest()
    return sig + data  # 32 bytes signature + pickle payload


def _verify_and_load(raw: bytes) -> Any:
    """Проверяет HMAC-подпись и десериализует данные.

    Raises:
        ValueError: если подпись невалидна (данные скомпрометированы).
    """
    if len(raw) < 32:
        raise ValueError("Signed data too short")
    sig = raw[:32]
    payload = raw[32:]
    expected_sig = hmac.new(_SIGNING_KEY, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected_sig):
        raise ValueError("HMAC signature verification failed — possible data tampering")
    return pickle.loads(payload)


# ============================================================================
# REDIS CLIENT (lazy init)
# ============================================================================

_redis_client = None
_redis_available = None  # None = not checked yet


def _get_redis():
    """Ленивое подключение к Redis"""
    global _redis_client, _redis_available

    if _redis_available is False:
        return None

    if _redis_client is not None:
        return _redis_client

    try:
        import redis

        url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        _redis_client = redis.Redis.from_url(
            url,
            decode_responses=False,  # pickle бинарный
            socket_timeout=2,
            socket_connect_timeout=2,
            retry_on_timeout=True,
        )
        _redis_client.ping()
        _redis_available = True
        logger.info(f"Redis connected: {url}")
        return _redis_client
    except Exception as e:
        _redis_available = False
        logger.warning(f"Redis unavailable, using in-memory fallback: {e}")
        return None


# ============================================================================
# CAT SESSION STORE
# ============================================================================

# Fallback in-memory store
_memory_sessions: Dict[str, Dict[str, Any]] = {}

# Константы
SESSION_PREFIX = "cat:session:"
RATE_PREFIX = "cat:rate:"
SESSION_TTL = int(os.getenv("REDIS_SESSION_TTL", "3600"))  # 1 час
RATE_LIMIT_WINDOW = 60  # 1 минута
RATE_LIMIT_MAX_STARTS = 5  # Макс. 5 сессий/мин на пользователя


class CATSessionStore:
    """
    Хранилище CAT-сессий с Redis и in-memory fallback.

    Сериализация: pickle (т.к. ShadowCATEngine содержит сложные объекты).
    TTL: 1 час по умолчанию (каждый ответ продлевает).
    Rate limiting: 5 стартов/мин на пользователя.
    """

    def save(self, session_id: str, data: Dict[str, Any]) -> None:
        """Сохранить сессию (HMAC-signed pickle)"""
        r = _get_redis()
        if r:
            try:
                pickled = pickle.dumps(data)
                serialized = _sign_data(pickled)
                r.setex(f"{SESSION_PREFIX}{session_id}", SESSION_TTL, serialized)
                logger.info(
                    f"Session {session_id[:8]}... saved to Redis ({len(pickled)} bytes)"
                )
                return
            except Exception as e:
                logger.error(
                    f"Redis save FAILED {session_id[:8]}...: {type(e).__name__}: {e}",
                    exc_info=True,
                )
        else:
            logger.warning(
                f"Redis unavailable, falling back to memory for {session_id[:8]}..."
            )

        # Fallback
        data["_expires_at"] = time.time() + SESSION_TTL
        _memory_sessions[session_id] = data
        logger.info(f"Session {session_id[:8]}... saved to in-memory fallback")

    def get(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Получить сессию"""
        r = _get_redis()
        if r:
            try:
                raw = r.get(f"{SESSION_PREFIX}{session_id}")
                if raw:
                    data = _verify_and_load(raw)
                    # Продлеваем TTL при каждом доступе
                    r.expire(f"{SESSION_PREFIX}{session_id}", SESSION_TTL)
                    logger.debug(f"Session {session_id[:8]}... loaded from Redis")
                    return data
                logger.warning(
                    f"Session {session_id[:8]}... NOT found in Redis, checking memory..."
                )
                # Сессия могла сохраниться в memory если pickle упал при save
                mem_data = _memory_sessions.get(session_id)
                if mem_data:
                    if time.time() > mem_data.get("_expires_at", float("inf")):
                        del _memory_sessions[session_id]
                        return None
                    mem_data["_expires_at"] = time.time() + SESSION_TTL
                    logger.info(
                        f"Session {session_id[:8]}... found in memory fallback (Redis miss)"
                    )
                    return mem_data
                return None
            except ValueError as e:
                logger.error(
                    f"Redis session verification failed {session_id[:8]}...: {e}"
                )
                r.delete(f"{SESSION_PREFIX}{session_id}")
                return None
            except Exception as e:
                logger.error(
                    f"Redis get FAILED {session_id[:8]}...: {type(e).__name__}: {e}",
                    exc_info=True,
                )
        else:
            logger.warning(f"Redis unavailable for get {session_id[:8]}...")

        # Fallback
        data = _memory_sessions.get(session_id)
        if data:
            if time.time() > data.get("_expires_at", float("inf")):
                del _memory_sessions[session_id]
                return None
            data["_expires_at"] = time.time() + SESSION_TTL
            logger.info(f"Session {session_id[:8]}... loaded from memory fallback")
        else:
            logger.warning(f"Session {session_id[:8]}... NOT found anywhere")
        return data

    def exists(self, session_id: str) -> bool:
        """Проверить существование"""
        r = _get_redis()
        if r:
            try:
                return bool(r.exists(f"{SESSION_PREFIX}{session_id}"))
            except Exception:
                pass
        return session_id in _memory_sessions

    def delete(self, session_id: str) -> None:
        """Удалить сессию"""
        r = _get_redis()
        if r:
            try:
                r.delete(f"{SESSION_PREFIX}{session_id}")
            except Exception:
                pass
        _memory_sessions.pop(session_id, None)

    def check_rate_limit(self, user_id: str) -> bool:
        """
        Rate limiting: не более RATE_LIMIT_MAX_STARTS стартов за RATE_LIMIT_WINDOW.

        Returns:
            True если в рамках лимита, False если превышен
        """
        r = _get_redis()
        if r:
            try:
                key = f"{RATE_PREFIX}{user_id}"
                pipe = r.pipeline()
                pipe.incr(key)
                pipe.expire(key, RATE_LIMIT_WINDOW)
                results = pipe.execute()
                count = results[0]
                return count <= RATE_LIMIT_MAX_STARTS
            except Exception as e:
                logger.error(f"Redis rate limit error: {e}")
                return True  # При ошибке пропускаем

        # In-memory rate limiting
        return self._memory_rate_check(user_id)

    def _memory_rate_check(self, user_id: str) -> bool:
        """In-memory rate limiting fallback"""
        key = f"_rate_{user_id}"
        now = time.time()
        entry = _memory_sessions.get(key, {"count": 0, "window_start": now})

        if now - entry["window_start"] > RATE_LIMIT_WINDOW:
            entry = {"count": 1, "window_start": now}
        else:
            entry["count"] += 1

        _memory_sessions[key] = entry
        return entry["count"] <= RATE_LIMIT_MAX_STARTS

    def cleanup_expired(self) -> int:
        """Очистка просроченных in-memory сессий"""
        now = time.time()
        expired = [
            k
            for k, v in _memory_sessions.items()
            if isinstance(v, dict) and now > v.get("_expires_at", float("inf"))
        ]
        for k in expired:
            del _memory_sessions[k]
        return len(expired)

    def active_count(self) -> int:
        """Количество активных сессий (SCAN вместо KEYS для production)"""
        r = _get_redis()
        if r:
            try:
                count = 0
                cursor = 0
                while True:
                    cursor, keys = r.scan(cursor, match=f"{SESSION_PREFIX}*", count=100)
                    count += len(keys)
                    if cursor == 0:
                        break
                return count
            except Exception:
                pass
        return len([k for k in _memory_sessions if not k.startswith("_rate_")])


# Singleton
session_store = CATSessionStore()
