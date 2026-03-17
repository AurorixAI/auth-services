"""
ALGO V1 — Pipeline Configuration
src/pipeline/config.py

Единая точка загрузки конфигурации из scripts/pipeline_config.json.
"""

import json
import logging
from pathlib import Path

log = logging.getLogger("pipeline")

_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent.parent / "scripts" / "pipeline_config.json"
)


def load_config() -> dict:
    """Загружает конфигурацию конвейера."""
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    log.warning("pipeline_config.json не найден, значения по умолчанию")
    return {}


CFG: dict = load_config()
