"""
ALGO V1 — Tests for Variant Engine

Covers:
  1. StepSolver — solve, step generation
  2. StepDistractorEngine — generate distractors
  3. VariantOrchestrator — full variant generation cycle
"""

import math
import pytest
from typing import Dict, List, Optional
from unittest.mock import patch

from src.exam_builder.template_models import TaskTemplate, ParamInfo


# ─────────────────────────────────────────────────────────────────
# 1. StepSolver tests
# ─────────────────────────────────────────────────────────────────

class TestStepSolver:
    """Tests for src.exam_builder.sympy_step_solver.StepSolver"""

    def _solver(self):
        from src.exam_builder.sympy_step_solver import StepSolver
        return StepSolver()

    def test_solve_generic(self):
        solver = self._solver()
        steps = solver.solve("arithmetic", "a + b", {"a": 3, "b": 5}, "Вычислите {a} + {b}")
        assert len(steps) >= 2
        assert steps[-1].result == "8"

    def test_solve_linear(self):
        solver = self._solver()
        steps = solver.solve("linear", "(c - b) / a", {"a": 3, "b": 7, "c": 19}, "Решите: {a}x + {b} = {c}")
        assert len(steps) >= 2
        # Последний шаг = ответ 4
        last_result = steps[-1].result
        assert "4" in last_result

    def test_solve_returns_nonempty_descriptions(self):
        solver = self._solver()
        steps = solver.solve("arithmetic", "a * b", {"a": 6, "b": 7}, "{a} * {b}")
        for step in steps:
            assert step.description != ""
            assert step.step_number >= 0

    def test_solve_quadratic(self):
        solver = self._solver()
        # x² - 5x + 6 = 0, D = 25-24 = 1, x = (5±1)/2 → x=2 or x=3
        steps = solver.solve(
            "quadratic",
            "(-b + sqrt(b**2 - 4*a*c)) / (2*a)",
            {"a": 1, "b": -5, "c": 6},
            "x² + {b}x + {c} = 0"
        )
        assert len(steps) >= 2

    def test_solve_unknown_type_falls_back_to_generic(self):
        solver = self._solver()
        steps = solver.solve("unknown_type", "a + b", {"a": 1, "b": 2}, "{a} + {b}")
        assert len(steps) >= 1


# ─────────────────────────────────────────────────────────────────
# 3. StepDistractorEngine tests
# ─────────────────────────────────────────────────────────────────

class TestStepDistractorEngine:
    """Tests for src.exam_builder.sympy_step_solver.StepDistractorEngine"""

    def _engine(self):
        from src.exam_builder.sympy_step_solver import StepDistractorEngine
        return StepDistractorEngine()

    def _make_steps(self):
        from src.exam_builder.sympy_step_solver import StepSolver
        solver = StepSolver()
        return solver.solve("arithmetic", "a + b", {"a": 10, "b": 25}, "{a} + {b}")

    def test_generate_returns_distractors(self):
        engine = self._engine()
        steps = self._make_steps()
        distractors = engine.generate(steps, 35.0, "arithmetic", count=3)
        assert len(distractors) >= 1

    def test_distractors_not_equal_to_correct(self):
        engine = self._engine()
        steps = self._make_steps()
        distractors = engine.generate(steps, 35.0, "arithmetic", count=3)
        for d in distractors:
            # Дистрактор не должен быть равен правильному ответу
            try:
                assert abs(float(d.value) - 35.0) > 0.01
            except ValueError:
                pass  # Non-numeric distractor — that's ok

    def test_distractors_have_metadata(self):
        engine = self._engine()
        steps = self._make_steps()
        distractors = engine.generate(steps, 35.0, "arithmetic", count=3)
        for d in distractors:
            assert d.error_type != ""
            assert 0.0 <= d.plausibility <= 1.0

    def test_distractors_are_unique(self):
        engine = self._engine()
        steps = self._make_steps()
        distractors = engine.generate(steps, 35.0, "arithmetic", count=3)
        values = [d.value for d in distractors]
        assert len(values) == len(set(values))

    def test_generate_with_empty_steps(self):
        engine = self._engine()
        distractors = engine.generate([], 42.0, "arithmetic", count=3)
        # Should still produce fallback distractors
        assert len(distractors) >= 1


# ─────────────────────────────────────────────────────────────────
# 4. VariantOrchestrator tests
# ─────────────────────────────────────────────────────────────────

class TestVariantOrchestrator:
    """Tests for src.exam_builder.variant_orchestrator.VariantOrchestrator"""

    def _orchestrator(self):
        from src.exam_builder.variant_orchestrator import VariantOrchestrator
        return VariantOrchestrator()

    def _sample_tasks(self) -> List[Dict]:
        return [
            {
                "task_id": "T001",
                "question_text": "Вычислите: 15 + 27",
                "correct_answer": "42",
                "answer_type": "exact_number",
                "skill_id": "ARITH_ADD",
                "skill_name": "Сложение",
                "difficulty": "easy",
            },
            {
                "task_id": "T002",
                "question_text": "Вычислите: 8 * 7",
                "correct_answer": "56",
                "answer_type": "exact_number",
                "skill_id": "ARITH_MUL",
                "skill_name": "Умножение",
                "difficulty": "easy",
            },
            {
                "task_id": "T003",
                "question_text": "Найдите разность 100 и 37",
                "correct_answer": "63",
                "answer_type": "exact_number",
                "skill_id": "ARITH_SUB",
                "skill_name": "Вычитание",
                "difficulty": "easy",
            },
        ]

    @staticmethod
    def _mock_template(text, latex="", answer="", answer_type="exact_number", **kw):
        """Fake templatize_via_llm: returns a simple TaskTemplate."""
        if not text or not answer:
            return None
        # Simple addition template
        if "15" in text and "27" in text:
            return TaskTemplate(
                task_type="arithmetic",
                text_template="Вычислите: {a} + {b}",
                params={
                    "a": ParamInfo("a", 15, 1, 100),
                    "b": ParamInfo("b", 27, 1, 100),
                },
                original_values={"a": 15, "b": 27},
                answer_formula_repr="a + b",
                answer_formula_text="a + b",
                constraints_repr=["a > 0", "b > 0"],
                quality_score=0.95,
            )
        if "8" in text and "7" in text:
            return TaskTemplate(
                task_type="arithmetic",
                text_template="Вычислите: {a} * {b}",
                params={
                    "a": ParamInfo("a", 8, 2, 20),
                    "b": ParamInfo("b", 7, 2, 20),
                },
                original_values={"a": 8, "b": 7},
                answer_formula_repr="a * b",
                answer_formula_text="a * b",
                constraints_repr=["a > 0", "b > 0"],
                quality_score=0.95,
            )
        if "100" in text and "37" in text:
            return TaskTemplate(
                task_type="arithmetic",
                text_template="Найдите разность {a} и {b}",
                params={
                    "a": ParamInfo("a", 100, 50, 200),
                    "b": ParamInfo("b", 37, 1, 99),
                },
                original_values={"a": 100, "b": 37},
                answer_formula_repr="a - b",
                answer_formula_text="a - b",
                constraints_repr=["a > b"],
                quality_score=0.95,
            )
        return None

    @patch("src.exam_builder.variant_orchestrator.templatize_via_llm")
    def test_generate_variants_returns_report(self, mock_llm):
        mock_llm.side_effect = self._mock_template
        orch = self._orchestrator()
        report = orch.generate_variants(self._sample_tasks(), num_variants=2, seed=42)
        assert report.total_variants == 2
        assert report.tasks_total == 3
        assert len(report.variants) == 2

    @patch("src.exam_builder.variant_orchestrator.templatize_via_llm")
    def test_variants_have_correct_structure(self, mock_llm):
        mock_llm.side_effect = self._mock_template
        orch = self._orchestrator()
        report = orch.generate_variants(self._sample_tasks(), num_variants=2, seed=42)
        for v in report.variants:
            assert v.variant_number > 0
            assert len(v.tasks) == 3
            for t in v.tasks:
                assert t.task_id != ""
                assert t.question_text != ""
                assert t.correct_answer != ""

    @patch("src.exam_builder.variant_orchestrator.templatize_via_llm")
    def test_variant_to_dict_jsonb_compatible(self, mock_llm):
        mock_llm.side_effect = self._mock_template
        orch = self._orchestrator()
        report = orch.generate_variants(self._sample_tasks(), num_variants=1, seed=42)
        d = report.variants[0].to_dict()
        assert "variant_number" in d
        assert "tasks" in d
        assert isinstance(d["tasks"], list)
        for t in d["tasks"]:
            assert "task_id" in t
            assert "question_text" in t
            assert "correct_answer" in t
            assert "answer_type" in t
            assert "skill_id" in t

    @patch("src.exam_builder.variant_orchestrator.templatize_via_llm")
    def test_variants_differ(self, mock_llm):
        """Разные варианты должны иметь разные задачи (хотя бы частично)."""
        mock_llm.side_effect = self._mock_template
        orch = self._orchestrator()
        report = orch.generate_variants(self._sample_tasks(), num_variants=3, seed=42)
        if report.tasks_templatized > 0:
            answers_per_variant = []
            for v in report.variants:
                answers = tuple(t.correct_answer for t in v.tasks)
                answers_per_variant.append(answers)
            unique_variants = set(answers_per_variant)
            assert len(unique_variants) >= 2

    def test_fallback_preserves_original(self):
        """Задача без чисел должна остаться оригинальной (answer_type не в TEMPLATABLE_TYPES)."""
        orch = self._orchestrator()
        tasks = [
            {
                "task_id": "T_TEXT",
                "question_text": "Опишите свойства параллелограмма",
                "correct_answer": "текстовый ответ",
                "answer_type": "text",
                "skill_id": "GEOM",
                "difficulty": "medium",
            },
        ]
        report = orch.generate_variants(tasks, num_variants=2, seed=42)
        assert report.tasks_fallback >= 1
        for v in report.variants:
            assert v.tasks[0].is_original is True
            assert v.tasks[0].question_text == "Опишите свойства параллелограмма"

    def test_empty_tasks_returns_empty_report(self):
        orch = self._orchestrator()
        report = orch.generate_variants([], num_variants=2, seed=42)
        assert report.total_variants == 2
        assert report.tasks_total == 0
        assert all(len(v.tasks) == 0 for v in report.variants)

    @patch("src.exam_builder.variant_orchestrator.templatize_via_llm")
    def test_max_variants_cap(self, mock_llm):
        """num_variants > 30 должен ограничиться 30."""
        mock_llm.side_effect = self._mock_template
        orch = self._orchestrator()
        report = orch.generate_variants(self._sample_tasks(), num_variants=50, seed=42)
        assert report.total_variants == 30

    @patch("src.exam_builder.variant_orchestrator.templatize_via_llm")
    def test_seed_reproducibility(self, mock_llm):
        """Одинаковый seed → одинаковый результат."""
        mock_llm.side_effect = self._mock_template
        orch = self._orchestrator()
        r1 = orch.generate_variants(self._sample_tasks(), num_variants=3, seed=123)

        mock_llm.side_effect = self._mock_template
        r2 = orch.generate_variants(self._sample_tasks(), num_variants=3, seed=123)

        for v1, v2 in zip(r1.variants, r2.variants):
            for t1, t2 in zip(v1.tasks, v2.tasks):
                assert t1.question_text == t2.question_text
                assert t1.correct_answer == t2.correct_answer


# ─────────────────────────────────────────────────────────────────
# 5. Integration: VariantTask / GeneratedVariant serialization
# ─────────────────────────────────────────────────────────────────

class TestVariantSerialization:
    """Tests that VariantTask and GeneratedVariant serialize correctly."""

    def test_variant_task_to_dict(self):
        from src.exam_builder.variant_orchestrator import VariantTask
        vt = VariantTask(
            task_id="VT_abc",
            question_text="2 + 3",
            correct_answer="5",
            answer_type="exact_number",
            skill_id="ARITH",
            difficulty="easy",
            answer_options=["3", "5", "7", "4"],
        )
        d = vt.to_dict()
        assert d["task_id"] == "VT_abc"
        assert d["correct_answer"] == "5"
        assert len(d["answer_options"]) == 4

    def test_generated_variant_to_dict(self):
        from src.exam_builder.variant_orchestrator import VariantTask, GeneratedVariant
        gv = GeneratedVariant(
            variant_number=1,
            tasks=[
                VariantTask(task_id="VT_1", question_text="1+1", correct_answer="2"),
                VariantTask(task_id="VT_2", question_text="2+2", correct_answer="4"),
            ],
            seed=42,
        )
        d = gv.to_dict()
        assert d["variant_number"] == 1
        assert len(d["tasks"]) == 2
        assert d["seed"] == 42

    def test_generation_report_fields(self):
        from src.exam_builder.variant_orchestrator import GenerationReport
        report = GenerationReport(
            total_variants=5,
            tasks_templatized=3,
            tasks_fallback=2,
            tasks_total=5,
        )
        assert report.total_variants == 5
        assert report.tasks_templatized == 3
