"""
ALGO V1 — Skeleton Textbook Mapper
src/pipeline/classification.py

Дуальный маппинг задачи:
  • skill_id  — привязка к L4-навыку скелета (Gemini Pro)
  • toc_id    — привязка к параграфу учебника (уже задан из TaskExtractor)
  • tags      — обогащённые теги
"""

import json
import logging
from typing import List

from src.domain.pipeline.gemini_client import (
    call_gemini,
    get_api_key,
    get_model,
    parse_json_response,
)
from src.domain.pipeline.models import ExtractedTask

log = logging.getLogger("pipeline")


class SkeletonTextbookMapper:
    """Привязывает задачу к скелету навыков (skill_id) через Gemini Pro."""

    def __init__(self, api_key: str = "", skills_json: str = ""):
        self.api_key = api_key or get_api_key()
        self.model = get_model("pro")
        self.skills_json = skills_json

    def load_skills_from_db(self, db_url: str, class_level: int = 5) -> None:
        """Загружает скелет навыков из БД для класса."""
        try:
            from sqlalchemy import create_engine, text

            engine = create_engine(db_url)
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT id, level, parent_id, name_ru, description,"
                        "       class_level_start, class_level_end, origin, is_advanced "
                        "FROM knowledge_hierarchy "
                        "WHERE class_level_start <= :cl AND class_level_end >= :cl "
                        "  AND is_active = TRUE "
                        "ORDER BY level, sequence_order"
                    ),
                    {"cl": class_level},
                ).fetchall()

            skills = [
                {
                    "id": r[0],
                    "level": r[1],
                    "parent_id": r[2],
                    "name_ru": r[3],
                    "description": r[4],
                    "origin": r[7],
                    "is_advanced": r[8],
                }
                for r in rows
            ]
            self.skills_json = json.dumps(skills, ensure_ascii=False)
            log.info("Загружено %d навыков для класса %d", len(skills), class_level)
        except Exception as e:
            log.error("Ошибка загрузки скелета: %s", e)

    def map_task(self, task: ExtractedTask) -> ExtractedTask:
        """Определяет skill_id для задачи через Gemini Pro."""
        if not self.skills_json:
            log.warning("Скелет навыков не загружен, пропуск маппинга")
            return task

        prompt = (
            "Ты — эксперт по структуре математических навыков.\n\n"
            f"Задача:\n"
            f"  Номер: {task.exercise_number}\n"
            f"  Текст: {task.question_text}\n"
            f"  LaTeX: {task.question_latex}\n"
            f"  Сложность: {task.difficulty}\n"
            f"  Параграф: §{task.paragraph_number} «{task.paragraph_title}»\n\n"
            f"Скелет навыков:\n{self.skills_json[:6000]}\n\n"
            'Определи ОДИН L4-навык (level="L4").\n'
            'Верни JSON: {"skill_id": "...", "confidence": 0.92, "reasoning": "..."}\n'
            "Только JSON."
        )
        try:
            raw = call_gemini(
                prompt,
                api_key=self.api_key,
                model=self.model,
                temperature=0.0,
                max_tokens=512,
            )
            data = parse_json_response(raw)
            if isinstance(data, dict):
                sid = data.get("skill_id", "") or None
                conf = data.get("confidence", 0.0)
                # Низкая уверенность → считаем что нет маппинга
                if sid and conf >= 0.5:
                    task.skill_id = sid
                    task.mapping_confidence = conf
                else:
                    task.skill_id = None
                    task.mapping_confidence = 0.0
                    log.info(
                        "Task %s: textbook-only (no core mapping, confidence=%.2f)",
                        task.temp_id,
                        conf,
                    )
        except Exception as e:
            log.warning("Mapper error (%s): %s", task.temp_id, e)
        return task

    def map_batch(self, tasks: List[ExtractedTask]) -> List[ExtractedTask]:
        return [self.map_task(t) for t in tasks]
