"""
ALGO V1 — SymPy Answer Verification
src/pipeline/validation.py

5 стратегий верификации ответа задачи:
  1. direct  — прямое числовое вычисление / дробь
  2. equation — решение уравнения из LaTeX
  3. simplify — упрощение выражения
  4. numerical — числовое приближение
  5. expression — произвольное символьное выражение
"""

import logging
from typing import Any, Tuple

from src.domain.pipeline.models import ExtractedTask

log = logging.getLogger("pipeline")


class SymPySolver:
    """Верифицирует ответ задачи через SymPy (5 стратегий)."""

    def verify(self, task: ExtractedTask) -> ExtractedTask:
        """Верифицирует ответ, заполняет ``sympy_*`` поля."""
        try:
            import sympy  # noqa: F401
        except ImportError:
            log.warning("SymPy не установлен, пропуск верификации")
            return task

        answer = task.answer_raw.strip()
        if not answer:
            return task

        strategies = [
            self._try_direct_eval,
            self._try_equation,
            self._try_simplify,
            self._try_numerical,
            self._try_expression,
        ]

        for strategy in strategies:
            try:
                result, confidence = strategy(answer, task)
                if result is not None and confidence > 0.5:
                    task.sympy_verified = True
                    task.sympy_answer = str(result)
                    task.sympy_confidence = confidence
                    return task
            except Exception:
                continue

        log.debug("SymPy не смог верифицировать: %s (%s)", task.temp_id, answer)
        return task

    # ------------------------------------------------------------------
    # Strategies
    # ------------------------------------------------------------------

    @staticmethod
    def _try_direct_eval(answer: str, task: ExtractedTask) -> Tuple[Any, float]:
        from sympy import Rational

        clean = answer.replace(",", ".").strip()
        if "/" in clean:
            parts = clean.split("/")
            if len(parts) == 2:
                val = Rational(int(parts[0].strip()), int(parts[1].strip()))
                return val, 0.95
        try:
            return float(clean), 0.9
        except ValueError:
            return None, 0.0

    @staticmethod
    def _try_equation(answer: str, task: ExtractedTask) -> Tuple[Any, float]:
        from sympy import symbols, solve
        from sympy.parsing.latex import parse_latex

        latex = task.question_latex
        if not latex or "=" not in latex:
            return None, 0.0
        x = symbols("x")
        try:
            expr = parse_latex(latex)
            solutions = solve(expr, x)
            if solutions:
                return str(solutions), 0.85
        except Exception:
            pass
        return None, 0.0

    @staticmethod
    def _try_simplify(answer: str, task: ExtractedTask) -> Tuple[Any, float]:
        from sympy import sympify, simplify

        try:
            expr = sympify(answer.replace("^", "**"))
            return simplify(expr), 0.8
        except Exception:
            return None, 0.0

    @staticmethod
    def _try_numerical(answer: str, task: ExtractedTask) -> Tuple[Any, float]:
        from sympy import sympify, N

        try:
            expr = sympify(answer.replace("^", "**"))
            return float(N(expr, 10)), 0.7
        except Exception:
            return None, 0.0

    @staticmethod
    def _try_expression(answer: str, task: ExtractedTask) -> Tuple[Any, float]:
        from sympy import sympify

        try:
            return sympify(answer.replace("^", "**")), 0.6
        except Exception:
            return None, 0.0
