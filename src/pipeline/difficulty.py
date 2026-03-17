"""
ALGO V1 — Hybrid Difficulty Assessor
src/pipeline/difficulty.py

Гибридная оценка сложности задачи (3 источника):
  1. Алгоритмический анализ (SymPy): шаги, числа, операции, вложенность
  2. AI-коррекция (Gemini Flash): контекст, нестандартность, ловушки
  3. IRT-калибровка из исторических данных

Выход: difficulty_abc (A/B/C), irt_difficulty, irt_discrimination,
       cognitive_load, estimated_time_sec

Перенесено из content_pipeline.py (уникальный класс).
"""

import json
import logging
import math
import os
import re
from typing import Any, Dict, Optional

from src.pipeline.config import CFG

log = logging.getLogger("pipeline")


class HybridDifficultyAssessor:
    """Гибрид SymPy + AI + IRT для оценки сложности задачи."""

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or CFG.get("gemini", {}).get(
            "api_key", os.getenv("GEMINI_API_KEY", "")
        )

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def assess(
        self,
        task_data: Dict[str, Any],
        sympy_solution: Optional[str] = None,
        historical_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Гибридная оценка сложности.

        Returns:
            ``{difficulty_abc, irt_difficulty, irt_discrimination,
              cognitive_load, estimated_time_sec, components}``
        """
        algo = self._algorithmic(task_data, sympy_solution)
        ai = self._ai_correction(task_data, algo)
        irt_b = (
            self._calibrate(historical_data)
            if historical_data
            else algo["estimated_irt_b"]
        )
        return {
            "difficulty_abc": self._to_abc(irt_b),
            "irt_difficulty": round(irt_b, 2),
            "irt_discrimination": round(algo["estimated_irt_a"], 2),
            "cognitive_load": ai.get("cognitive_load", algo["cognitive_load"]),
            "estimated_time_sec": ai.get("time_sec", algo["time_sec"]),
            "components": {"algo": algo, "ai": ai},
        }

    # ------------------------------------------------------------------
    # 1. Algorithmic
    # ------------------------------------------------------------------

    @staticmethod
    def _algorithmic(task_data: Dict[str, Any], solution: Optional[str]) -> Dict[str, Any]:
        latex = task_data.get("latex", "")
        text = task_data.get("text", "")

        num_ops = len(re.findall(r"[+\-*/^=]", latex))
        has_fractions = "\\frac" in latex or "/" in latex
        has_sqrt = "\\sqrt" in latex or "sqrt" in text
        has_powers = "^" in latex or "**" in text
        has_abs = "|" in text or "\\left|" in latex
        num_vars = len(set(re.findall(r"[a-z]", latex.lower())) - {"i", "n", "e"})

        steps = (solution.count("=") + solution.count("→") + 1) if solution else max(1, num_ops // 2)

        numbers = re.findall(r"\d+", text + latex)
        max_digits = max((len(n) for n in numbers), default=1)

        score = 0.0
        score += min(3.0, steps * 0.5)
        score += min(2.0, max_digits * 0.5)
        score += 1.0 if has_fractions else 0
        score += 0.5 if has_sqrt else 0
        score += 0.5 if has_powers else 0
        score += 0.5 if has_abs else 0
        score += min(2.0, (num_vars - 1) * 0.7)

        irt_b = (score / 10) * 4 - 2
        irt_a = 1.0 + 0.2 * min(3, steps)
        cognitive = min(5, max(1, round(score / 2)))
        time_sec = max(15, steps * 20 + max_digits * 5 + (30 if has_fractions else 0))

        return {
            "score": round(score, 2),
            "steps": steps,
            "max_digits": max_digits,
            "has_fractions": has_fractions,
            "has_sqrt": has_sqrt,
            "num_variables": num_vars,
            "estimated_irt_b": round(irt_b, 2),
            "estimated_irt_a": round(irt_a, 2),
            "cognitive_load": cognitive,
            "time_sec": time_sec,
        }

    # ------------------------------------------------------------------
    # 2. AI correction
    # ------------------------------------------------------------------

    def _ai_correction(self, task_data: Dict[str, Any], algo: Dict[str, Any]) -> Dict[str, Any]:
        if not self.api_key:
            return {}
        try:
            from google import genai

            client = genai.Client(api_key=self.api_key)
            prompt = (
                "Оцени сложность математической задачи.\n\n"
                f"Текст: {task_data.get('text', '')}\n"
                f"LaTeX: {task_data.get('latex', '')}\n\n"
                f"АЛГОРИТМ: шаги={algo['steps']}, IRT b={algo['estimated_irt_b']}, "
                f"дроби={algo['has_fractions']}, корни={algo['has_sqrt']}\n\n"
                "Скорректируй и верни JSON:\n"
                '{"corrected_irt_b":0.5,"cognitive_load":3,"time_sec":90,"reasoning":"..."}'
            )
            response = client.models.generate_content(
                model="gemini-2.5-flash-preview-04-17",
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "temperature": 0.2,
                },
            )
            return json.loads(response.text)
        except Exception as e:
            log.error("AI difficulty assessment error: %s", e)
            return {}

    # ------------------------------------------------------------------
    # 3. IRT calibration
    # ------------------------------------------------------------------

    @staticmethod
    def _calibrate(historical: Dict[str, Any]) -> float:
        correct = historical.get("correct", 0)
        total = historical.get("total", 1)
        p = max(0.01, min(0.99, correct / max(1, total)))
        return -math.log(p / (1 - p))

    @staticmethod
    def _to_abc(irt_b: float) -> str:
        if irt_b < -0.5:
            return "A"
        if irt_b < 1.0:
            return "B"
        return "C"
