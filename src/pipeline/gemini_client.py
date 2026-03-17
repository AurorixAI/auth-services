"""
ALGO V1 — Shared Gemini API Client
src/pipeline/gemini_client.py

Единый клиент Gemini API (через REST/requests) для всех модулей конвейера.
Избавляет от дублирования кода HTTP-вызовов.
"""

import json
import logging
import os
import re
import time
from typing import Optional

from src.pipeline.config import CFG

log = logging.getLogger("pipeline")

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_api_key() -> str:
    """Возвращает API-ключ Gemini из конфигурации или окружения."""
    return CFG.get("gemini", {}).get("api_key", os.getenv("GEMINI_API_KEY", ""))


def get_model(variant: str = "flash") -> str:
    """Возвращает имя модели Gemini.

    variant: "flash" (по умолчанию) или "pro".
    """
    models = CFG.get("gemini", {})
    if variant == "pro":
        return models.get("pro_model", "gemini-2.5-pro-preview-05-06")
    return models.get("flash_model", "gemini-2.5-flash-preview-04-17")


# ---------------------------------------------------------------------------
# Main caller
# ---------------------------------------------------------------------------

def call_gemini(
    prompt: str,
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.1,
    max_tokens: int = 4096,
    json_mode: bool = True,
    timeout: int = 60,
    max_retries: int = 3,
) -> str:
    """Вызывает Gemini API и возвращает текст ответа.

    Поддерживает retry с экспоненциальным backoff при 429 / 5xx.

    Raises:
        requests.HTTPError: при ошибке HTTP после исчерпания попыток.
        RuntimeError: при пустом ответе.
    """
    import requests  # lazy — не все модули нуждаются в requests

    key = api_key or get_api_key()
    mdl = model or get_model("flash")
    url = f"{_BASE_URL}/{mdl}:generateContent"

    gen_config: dict = {
        "temperature": temperature,
        "maxOutputTokens": max_tokens,
    }
    if json_mode:
        gen_config["responseMimeType"] = "application/json"

    last_exc: Optional[Exception] = None

    for attempt in range(max_retries):
        try:
            resp = requests.post(
                url,
                params={"key": key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": gen_config,
                },
                timeout=timeout,
            )
            # Retry on 429 (rate limit) and 5xx (server errors)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 2 ** attempt  # 1, 2, 4 seconds
                log.warning(
                    "Gemini API %s (attempt %d/%d), retrying in %ds...",
                    resp.status_code, attempt + 1, max_retries, wait,
                )
                time.sleep(wait)
                last_exc = requests.HTTPError(response=resp)
                continue

            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]

        except requests.ConnectionError as exc:
            wait = 2 ** attempt
            log.warning(
                "Gemini connection error (attempt %d/%d), retrying in %ds...",
                attempt + 1, max_retries, wait,
            )
            time.sleep(wait)
            last_exc = exc

    # All retries exhausted
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Gemini API: no response after retries")


# ---------------------------------------------------------------------------
# JSON parser
# ---------------------------------------------------------------------------

def parse_json_response(text: str) -> object:
    """Парсит JSON из ответа Gemini, обрабатывая markdown code-blocks."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        raise
