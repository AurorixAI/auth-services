"""
ALGO V1 — AI Enrichment Module
src/pipeline/enrichment.py

Обогащает задачу через Gemini Flash:
  • solution_steps — пошаговое решение
  • hints          — подсказки без ответа
  • common_mistakes — типичные ошибки

Генерирует AI-дистракторы для текстовых задач (open_text) через Gemini Pro.
"""

import json
import logging
from typing import List, Dict, Optional

from src.pipeline.gemini_client import call_gemini, get_api_key, get_model, parse_json_response
from src.pipeline.models import ExtractedTask

log = logging.getLogger("pipeline")


class AIEnricher:
    """Обогащает задачу: solution_steps, hints, common_mistakes."""

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or get_api_key()

    def enrich(self, task: ExtractedTask) -> ExtractedTask:
        prompt = (
            "Ты — математический педагог. Для задачи:\n\n"
            f"Текст: {task.question_text}\nLaTeX: {task.question_latex}\n"
            f"Ответ: {task.answer_raw}\nТип: {task.answer_type}\n"
            f"Сложность: {task.difficulty}\n\n"
            "Верни JSON:\n"
            '{"solution_steps":["Шаг 1: ...","Шаг 2: ..."],'
            '"hints":["Подсказка 1","Подсказка 2"],'
            '"common_mistakes":[{"mistake":"...","wrong_answer":"...","explanation":"..."}]}\n\n'
            "Только JSON, без обёрток."
        )
        try:
            text = call_gemini(
                prompt, api_key=self.api_key, temperature=0.2, max_tokens=2048,
            )
            data = parse_json_response(text)
            if isinstance(data, dict):
                task.solution_steps = data.get("solution_steps", [])
                task.hints = data.get("hints", [])
                task.common_mistakes = data.get("common_mistakes", [])
        except Exception as e:
            log.warning("AIEnricher error (%s): %s", task.temp_id, e)
        return task


class AIDistractorGenerator:
    """
    Генерирует дистракторы для текстовых задач (open_text) через Gemini Pro.

    Используется в двух случаях:
    1. answer_type == "open_text" — SymPy не может обработать
    2. Fallback для любого типа, если SymPy StepDistractorEngine вернул пустой список

    Формат: [{value, error_type, explanation, plausibility}]
    Модель: Gemini Pro (точнее, надёжнее для педагогических задач)
    """

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or get_api_key()

    def generate(
        self,
        question_text: str,
        correct_answer: str,
        answer_type: str = "open_text",
        count: int = 3,
    ) -> List[Dict]:
        """
        Генерирует дистракторы через Gemini Pro.

        Returns:
            Список словарей: [{value, error_type, explanation, plausibility}]
            Пустой список при ошибке.
        """
        if not question_text or not correct_answer:
            return []

        prompt = (
            "Ты — эксперт по математической педагогике и типичным ошибкам учеников.\n\n"
            f"Задача: {question_text}\n"
            f"Правильный ответ: {correct_answer}\n"
            f"Тип ответа: {answer_type}\n\n"
            f"Сгенерируй {count} правдоподобных НЕПРАВИЛЬНЫХ ответа, которые реальный ученик "
            "мог бы дать. Каждый дистрактор должен отражать конкретную типичную ошибку.\n\n"
            "Верни JSON-массив (без обёрток):\n"
            "[{\n"
            '  "value": "неправильный ответ",\n'
            '  "error_type": "тип_ошибки",  // sign_error, calculation_error, '
            "conceptual_error, partial_answer, misread_problem, unit_error\n"
            '  "explanation": "Почему ученик мог так ответить (1 предложение)",\n'
            '  "plausibility": 0.85  // 0-1, насколько правдоподобен\n'
            "}]\n\n"
            "Правила:\n"
            "- Ответы должны быть РАЗНЫМИ (не дубликаты)\n"
            "- plausibility от 0.6 до 0.95\n"
            "- error_type из списка: sign_error, calculation_error, conceptual_error, "
            "partial_answer, misread_problem, unit_error, formula_error, "
            "simplification_error, rounding_error\n"
            "- Ответы должны быть ПОХОЖИ по формату на правильный\n"
            "- Только JSON-массив, без markdown"
        )

        try:
            text = call_gemini(
                prompt,
                model=get_model("pro"),
                api_key=self.api_key,
                temperature=0.4,
                max_tokens=1024,
                json_mode=True,
            )
            data = parse_json_response(text)

            if isinstance(data, list):
                return self._validate_distractors(data, correct_answer, count)
            elif isinstance(data, dict) and "distractors" in data:
                return self._validate_distractors(data["distractors"], correct_answer, count)

        except Exception as e:
            log.warning("AIDistractorGenerator error: %s", e)

        return []

    def _validate_distractors(
        self, raw: list, correct: str, count: int
    ) -> List[Dict]:
        """Валидирует и нормализует сырые дистракторы от Gemini."""
        results: List[Dict] = []
        seen = {correct.strip().lower()}

        valid_types = {
            "sign_error", "calculation_error", "conceptual_error",
            "partial_answer", "misread_problem", "unit_error",
            "formula_error", "simplification_error", "rounding_error",
            "unknown",
        }

        for item in raw:
            if not isinstance(item, dict):
                continue
            val = str(item.get("value", "")).strip()
            if not val or val.lower() in seen:
                continue

            etype = str(item.get("error_type", "unknown")).strip()
            if etype not in valid_types:
                etype = "unknown"

            plaus = item.get("plausibility", 0.7)
            if not isinstance(plaus, (int, float)):
                plaus = 0.7
            plaus = max(0.1, min(0.95, float(plaus)))

            expl = str(item.get("explanation", ""))

            seen.add(val.lower())
            results.append({
                "value": val,
                "error_type": etype,
                "explanation": expl,
                "plausibility": plaus,
            })

            if len(results) >= count:
                break

        return results
