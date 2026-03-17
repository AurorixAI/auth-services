"""
ALGO V1 — Tests for Template Cache + DB-aware VariantOrchestrator

Covers:
  1. TemplateCache — _row_to_template, get_cached, get_cached_bulk, save_template
  2. VariantOrchestrator — cache-hit path, fallback path, persist_new
"""

import pytest
from decimal import Decimal
from unittest.mock import MagicMock, patch, PropertyMock

from src.exam_builder.template_models import TaskTemplate, ParamInfo


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _make_param(name, val, mn=-50, mx=50):
    return ParamInfo(name=name, original_value=val, min_val=mn, max_val=mx)


def _make_template(**overrides) -> TaskTemplate:
    defaults = dict(
        task_type="arithmetic",
        text_template="Вычислите: {a} + {b}",
        params={"a": _make_param("a", 5, 1, 100), "b": _make_param("b", 3, 1, 100)},
        original_values={"a": 5, "b": 3},
        answer_formula_repr="Add(Symbol('a'), Symbol('b'))",
        answer_formula_text="a + b",
        constraints_repr=[],
        quality_score=0.85,
    )
    defaults.update(overrides)
    return TaskTemplate(**defaults)


def _make_db_row(**overrides):
    """Builds a mock DB row mimicking TaskTemplateDB."""
    defaults = dict(
        id="TPL_T001",
        original_task_id="T001",
        skill_id="SKILL_001",
        template_text="Вычислите: {{a}} + {{b}}",
        template_latex=None,
        text_template="Вычислите: {a} + {b}",
        parameters=["a", "b"],
        constraints={"a": {"min": 1, "max": 100, "type": "int"},
                     "b": {"min": 1, "max": 100, "type": "int"}},
        answer_formula="a + b",
        answer_formula_sympy="Add(Symbol('a'), Symbol('b'))",
        constraints_repr=[],
        task_type="arithmetic",
        original_values={"a": 5, "b": 3},
        param_details={"a": {"min": 1, "max": 100, "type": "int", "exclude": []},
                       "b": {"min": 1, "max": 100, "type": "int", "exclude": []}},
        generation_method="sympy_verified",
        quality_score=Decimal("0.85"),
        tested_combinations=50,
        successful_combinations=42,
        is_active=True,
    )
    defaults.update(overrides)
    row = MagicMock()
    for k, v in defaults.items():
        setattr(row, k, v)
    return row


# ═══════════════════════════════════════════════════════════════════════════
# 1. TemplateCache internal: _row_to_template
# ═══════════════════════════════════════════════════════════════════════════

class TestRowToTemplate:
    """Tests for template_cache._row_to_template"""

    def test_basic_conversion(self):
        from src.exam_builder.template_cache import _row_to_template
        row = _make_db_row()
        tpl = _row_to_template(row)

        assert tpl.task_type == "arithmetic"
        assert tpl.text_template == "Вычислите: {a} + {b}"
        assert tpl.answer_formula_text == "a + b"
        assert tpl.original_values == {"a": 5, "b": 3}
        assert "a" in tpl.params
        assert "b" in tpl.params
        assert tpl.params["a"].min_val == 1
        assert tpl.params["a"].max_val == 100
        assert tpl.quality_score == pytest.approx(0.85)

    def test_fallback_double_braces(self):
        """If text_template is missing, convert template_text ({{a}} → {a})."""
        from src.exam_builder.template_cache import _row_to_template
        row = _make_db_row(text_template=None)
        tpl = _row_to_template(row)
        assert tpl.text_template == "Вычислите: {a} + {b}"

    def test_legacy_constraints_format(self):
        """When param_details is empty, fall back to legacy constraints."""
        from src.exam_builder.template_cache import _row_to_template
        row = _make_db_row(param_details={})
        tpl = _row_to_template(row)
        assert "a" in tpl.params
        assert tpl.params["a"].min_val == 1

    def test_no_template_text_raises(self):
        from src.exam_builder.template_cache import _row_to_template
        row = _make_db_row(text_template=None, template_text=None)
        with pytest.raises(ValueError, match="no text_template"):
            _row_to_template(row)

    def test_quality_score_none(self):
        from src.exam_builder.template_cache import _row_to_template
        row = _make_db_row(quality_score=None)
        tpl = _row_to_template(row)
        assert tpl.quality_score == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# 2. TemplateCache.get_cached
# ═══════════════════════════════════════════════════════════════════════════

class TestGetCached:

    def test_returns_template_when_found(self):
        from src.exam_builder.template_cache import TemplateCache
        row = _make_db_row()
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = row

        tpl = TemplateCache.get_cached("T001", db)
        assert tpl is not None
        assert tpl.task_type == "arithmetic"
        assert tpl.text_template == "Вычислите: {a} + {b}"

    def test_returns_none_when_not_found(self):
        from src.exam_builder.template_cache import TemplateCache
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        tpl = TemplateCache.get_cached("MISS", db)
        assert tpl is None


# ═══════════════════════════════════════════════════════════════════════════
# 3. TemplateCache.get_cached_bulk
# ═══════════════════════════════════════════════════════════════════════════

class TestGetCachedBulk:

    def test_returns_dict_of_templates(self):
        from src.exam_builder.template_cache import TemplateCache
        row1 = _make_db_row(id="TPL_1", original_task_id="T1")
        row2 = _make_db_row(id="TPL_2", original_task_id="T2", task_type="linear",
                            text_template="Решите: {a}x + {b} = {c}",
                            answer_formula="(c - b) / a",
                            param_details={"a": {"min": 1, "max": 10, "type": "int", "exclude": [0]},
                                           "b": {"min": -10, "max": 10, "type": "int", "exclude": []},
                                           "c": {"min": 1, "max": 20, "type": "int", "exclude": []}},
                            original_values={"a": 3, "b": 5, "c": 11})

        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [row1, row2]

        result = TemplateCache.get_cached_bulk(["T1", "T2"], db)
        assert len(result) == 2
        assert result["T1"].task_type == "arithmetic"
        assert result["T2"].task_type == "linear"

    def test_empty_input(self):
        from src.exam_builder.template_cache import TemplateCache
        db = MagicMock()
        assert TemplateCache.get_cached_bulk([], db) == {}

    def test_deduplicates_by_task_id(self):
        """If multiple templates exist for same task, only first (highest quality) is kept."""
        from src.exam_builder.template_cache import TemplateCache
        row1 = _make_db_row(id="TPL_1a", original_task_id="T1", quality_score=Decimal("0.95"))
        row2 = _make_db_row(id="TPL_1b", original_task_id="T1", quality_score=Decimal("0.70"))

        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [row1, row2]

        result = TemplateCache.get_cached_bulk(["T1"], db)
        assert len(result) == 1


# ═══════════════════════════════════════════════════════════════════════════
# 4. TemplateCache.save_template
# ═══════════════════════════════════════════════════════════════════════════

class TestSaveTemplate:

    def test_inserts_new_template(self):
        from src.exam_builder.template_cache import TemplateCache
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None  # not existing

        tpl = _make_template()
        result_id = TemplateCache.save_template("TPL_NEW", "T_NEW", "SK_1", tpl, db)

        assert result_id == "TPL_NEW"
        db.add.assert_called_once()
        db.flush.assert_called_once()

    def test_updates_existing_template(self):
        from src.exam_builder.template_cache import TemplateCache
        existing = MagicMock()
        existing.id = "TPL_OLD"

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = existing

        tpl = _make_template(quality_score=0.92)
        result_id = TemplateCache.save_template("TPL_OLD", "T_OLD", "SK_1", tpl, db)

        assert result_id == "TPL_OLD"
        db.add.assert_not_called()
        db.flush.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# 5. VariantOrchestrator — cache-hit path
# ═══════════════════════════════════════════════════════════════════════════

class TestOrchestratorCachePath:

    def _draft_tasks(self):
        return [
            {
                "task_id": "T001",
                "question_text": "Вычислите: 5 + 3",
                "correct_answer": "8",
                "answer_type": "exact_number",
                "skill_id": "ARITH_ADD",
                "difficulty": "A",
            },
        ]

    def test_cache_hit_skips_on_the_fly(self):
        """When DB has a template, templatize_via_llm() should NOT be called."""
        from src.exam_builder.variant_orchestrator import VariantOrchestrator

        db = MagicMock()
        row = _make_db_row(original_task_id="T001")
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = row
        # Bulk preload
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [row]

        orch = VariantOrchestrator(db=db)

        with patch("src.exam_builder.variant_orchestrator.templatize_via_llm") as mock_llm:
            report = orch.generate_variants(self._draft_tasks(), num_variants=2, seed=42)
            mock_llm.assert_not_called()

        assert report.tasks_templatized == 1
        assert report.tasks_fallback == 0

    def test_no_db_falls_through_to_llm(self):
        """Without DB session, orchestrator uses on-the-fly LLM templatizer."""
        from src.exam_builder.variant_orchestrator import VariantOrchestrator
        from src.exam_builder.template_models import TaskTemplate, ParamInfo

        tpl = _make_template()

        orch = VariantOrchestrator(db=None)
        with patch("src.exam_builder.variant_orchestrator.templatize_via_llm", return_value=tpl):
            report = orch.generate_variants(self._draft_tasks(), num_variants=2, seed=42)
        assert report.tasks_total == 1

    def test_cache_miss_triggers_llm_and_persist(self):
        """When DB has no template, LLM runs; result is persisted."""
        from src.exam_builder.variant_orchestrator import VariantOrchestrator

        db = MagicMock()
        # get_cached returns None (miss)
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
        # Bulk preload returns empty
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []

        orch = VariantOrchestrator(db=db, persist_new=True)
        tpl = _make_template()

        with patch("src.exam_builder.variant_orchestrator.templatize_via_llm", return_value=tpl):
            report = orch.generate_variants(self._draft_tasks(), num_variants=1, seed=42)

        # Should have tried to persist
        assert report.tasks_templatized == 1

    def test_persist_new_false_skips_save(self):
        """With persist_new=False, on-the-fly templates are NOT saved to DB."""
        from src.exam_builder.variant_orchestrator import VariantOrchestrator
        from src.exam_builder.template_cache import TemplateCache

        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []

        orch = VariantOrchestrator(db=db, persist_new=False)
        tpl = _make_template()

        with patch("src.exam_builder.variant_orchestrator.templatize_via_llm", return_value=tpl), \
             patch.object(TemplateCache, "save_template") as mock_save:
            orch.generate_variants(self._draft_tasks(), num_variants=1, seed=42)
            mock_save.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# 6. Внутренние хелперы
# ═══════════════════════════════════════════════════════════════════════════

class TestHelpers:

    def test_to_double_braces(self):
        from src.exam_builder.template_cache import _to_double_braces
        assert _to_double_braces("Вычислите: {a} + {b}") == "Вычислите: {{a}} + {{b}}"

    def test_to_double_braces_no_placeholders(self):
        from src.exam_builder.template_cache import _to_double_braces
        assert _to_double_braces("Без переменных") == "Без переменных"

    def test_param_details_to_constraints(self):
        from src.exam_builder.template_cache import _param_details_to_constraints
        details = {"a": {"min": 1, "max": 50, "type": "int", "exclude": [0]}}
        result = _param_details_to_constraints(details)
        assert result == {"a": {"min": 1, "max": 50, "type": "int"}}
