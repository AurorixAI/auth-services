"""
ALGO V1 — Tests for LLM Templatizer + Template Models

Covers:
  1. template_models — TaskTemplate, ParamInfo dataclasses
  2. llm_templatizer — prompt building, response parsing, verification,
                       TEMPLATABLE_TYPES filter
  3. Integration with mocked Gemini calls
"""

from unittest.mock import patch

from src.exam_builder.template_models import TaskTemplate, ParamInfo
from src.exam_builder.llm_templatizer import (
    templatize_via_llm,
    TEMPLATABLE_TYPES,
    _build_prompt,
    _parse_response,
    _verify_formula,
)


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _make_param(name, val, mn=-50, mx=50):
    return ParamInfo(name=name, original_value=val, min_val=mn, max_val=mx)


def _sample_gemini_response():
    """Пример корректного ответа Gemini для 15 + 27 = 42."""
    return {
        "task_type": "arithmetic",
        "text_template": "Вычислите {a} + {b}",
        "params": {
            "a": {
                "original_value": 15,
                "min": 1,
                "max": 100,
                "type": "int",
                "exclude": [],
            },
            "b": {
                "original_value": 27,
                "min": 1,
                "max": 100,
                "type": "int",
                "exclude": [],
            },
        },
        "answer_formula": "a + b",
        "constraints": ["a > 0", "b > 0"],
    }


# ═══════════════════════════════════════════════════════════════════════════
# 1. Template Models
# ═══════════════════════════════════════════════════════════════════════════


class TestTemplateModels:
    def test_param_info_defaults(self):
        p = ParamInfo(name="x", original_value=5)
        assert p.min_val == -50
        assert p.max_val == 50
        assert p.value_type == "int"
        assert p.exclude == []

    def test_param_info_custom(self):
        p = ParamInfo(
            name="a",
            original_value=3.14,
            min_val=1,
            max_val=10,
            value_type="float",
            exclude=[0],
        )
        assert p.original_value == 3.14
        assert p.exclude == [0]

    def test_task_template_param_names(self):
        tpl = TaskTemplate(
            task_type="arithmetic",
            text_template="{a} + {b}",
            params={"a": _make_param("a", 5), "b": _make_param("b", 3)},
            original_values={"a": 5, "b": 3},
            answer_formula_repr="a + b",
            answer_formula_text="a + b",
            constraints_repr=[],
        )
        assert tpl.param_names == ["a", "b"]

    def test_task_template_quality_score_default(self):
        tpl = TaskTemplate(
            task_type="linear",
            text_template="{a}x + {b} = {c}",
            params={},
            original_values={},
            answer_formula_repr="",
            answer_formula_text="(c - b) / a",
            constraints_repr=[],
        )
        assert tpl.quality_score == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# 2. TEMPLATABLE_TYPES
# ═══════════════════════════════════════════════════════════════════════════


class TestTemplatableTypes:
    def test_exact_number_is_templatable(self):
        assert "exact_number" in TEMPLATABLE_TYPES

    def test_decimal_is_templatable(self):
        assert "decimal" in TEMPLATABLE_TYPES

    def test_fraction_is_templatable(self):
        assert "fraction" in TEMPLATABLE_TYPES

    def test_multiple_choice_not_templatable(self):
        assert "multiple_choice" not in TEMPLATABLE_TYPES

    def test_text_not_templatable(self):
        assert "open_text" not in TEMPLATABLE_TYPES

    def test_true_false_not_templatable(self):
        assert "true_false" not in TEMPLATABLE_TYPES


# ═══════════════════════════════════════════════════════════════════════════
# 3. Prompt building
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildPrompt:
    def test_contains_task_text(self):
        p = _build_prompt("Вычислите 15 + 27", "", "42", "exact_number")
        assert "Вычислите 15 + 27" in p

    def test_contains_answer(self):
        p = _build_prompt("Вычислите 15 + 27", "", "42", "exact_number")
        assert "42" in p

    def test_contains_latex_when_provided(self):
        p = _build_prompt("test", "\\frac{1}{2}", "0.5", "fraction")
        assert "\\frac{1}{2}" in p

    def test_contains_answer_type(self):
        p = _build_prompt("test", "", "42", "exact_number")
        assert "exact_number" in p


# ═══════════════════════════════════════════════════════════════════════════
# 4. Response parsing
# ═══════════════════════════════════════════════════════════════════════════


class TestParseResponse:
    def test_valid_response(self):
        data = _sample_gemini_response()
        tpl = _parse_response(data)
        assert tpl is not None
        assert tpl.task_type == "arithmetic"
        assert tpl.text_template == "Вычислите {a} + {b}"
        assert tpl.answer_formula_text == "a + b"
        assert "a" in tpl.params
        assert "b" in tpl.params
        assert tpl.original_values == {"a": 15, "b": 27}

    def test_params_have_correct_ranges(self):
        data = _sample_gemini_response()
        tpl = _parse_response(data)
        assert tpl.params["a"].min_val == 1
        assert tpl.params["a"].max_val == 100
        assert tpl.params["b"].value_type == "int"

    def test_missing_text_template_returns_none(self):
        data = _sample_gemini_response()
        data["text_template"] = ""
        assert _parse_response(data) is None

    def test_missing_answer_formula_returns_none(self):
        data = _sample_gemini_response()
        data["answer_formula"] = ""
        assert _parse_response(data) is None

    def test_missing_params_returns_none(self):
        data = _sample_gemini_response()
        data["params"] = {}
        assert _parse_response(data) is None

    def test_empty_dict_returns_none(self):
        assert _parse_response({}) is None

    def test_float_param(self):
        data = _sample_gemini_response()
        data["params"]["a"]["type"] = "float"
        data["params"]["a"]["original_value"] = 3.14
        tpl = _parse_response(data)
        assert tpl.params["a"].value_type == "float"
        assert tpl.params["a"].original_value == 3.14

    def test_constraints_preserved(self):
        data = _sample_gemini_response()
        tpl = _parse_response(data)
        assert tpl.constraints_repr == ["a > 0", "b > 0"]


# ═══════════════════════════════════════════════════════════════════════════
# 5. Verification
# ═══════════════════════════════════════════════════════════════════════════


class TestVerifyFormula:
    def test_addition_passes(self):
        assert _verify_formula("a + b", {"a": 15, "b": 27}, 42.0) is True

    def test_subtraction_passes(self):
        assert _verify_formula("a - b", {"a": 100, "b": 37}, 63.0) is True

    def test_multiplication_passes(self):
        assert _verify_formula("a * b", {"a": 8, "b": 7}, 56.0) is True

    def test_division_passes(self):
        assert _verify_formula("a / b", {"a": 100, "b": 4}, 25.0) is True

    def test_wrong_formula_fails(self):
        assert _verify_formula("a + b", {"a": 15, "b": 27}, 99.0) is False

    def test_complex_formula(self):
        # (c - b) / a — linear equation
        assert _verify_formula("(c - b) / a", {"a": 3, "b": 7, "c": 19}, 4.0) is True

    def test_invalid_formula_fails(self):
        assert _verify_formula("???invalid", {"a": 1}, 1.0) is False


# ═══════════════════════════════════════════════════════════════════════════
# 6. Full templatize_via_llm (mocked Gemini)
# ═══════════════════════════════════════════════════════════════════════════


class TestTemplatizeViaLLM:
    @patch("src.pipeline.gemini_client.get_model", return_value="test-model")
    @patch("src.pipeline.gemini_client.parse_json_response")
    @patch("src.pipeline.gemini_client.call_gemini")
    def test_success_path(self, mock_call, mock_parse, mock_model):
        """Успешная шаблонизация через LLM."""
        mock_call.return_value = '{"json": true}'
        mock_parse.return_value = _sample_gemini_response()

        tpl = templatize_via_llm("Вычислите 15 + 27", "", "42", "exact_number")

        assert tpl is not None
        assert tpl.task_type == "arithmetic"
        assert tpl.answer_formula_text == "a + b"
        assert tpl.quality_score == 0.95
        assert tpl.original_values == {"a": 15, "b": 27}
        mock_call.assert_called_once()

    @patch("src.pipeline.gemini_client.get_model", return_value="test-model")
    @patch("src.pipeline.gemini_client.parse_json_response")
    @patch("src.pipeline.gemini_client.call_gemini")
    def test_empty_text_returns_none(self, mock_call, mock_parse, mock_model):
        tpl = templatize_via_llm("", "", "42", "exact_number")
        assert tpl is None
        mock_call.assert_not_called()

    @patch("src.pipeline.gemini_client.get_model", return_value="test-model")
    @patch("src.pipeline.gemini_client.parse_json_response")
    @patch("src.pipeline.gemini_client.call_gemini")
    def test_empty_answer_returns_none(self, mock_call, mock_parse, mock_model):
        tpl = templatize_via_llm("Вычислите 2 + 2", "", "", "exact_number")
        assert tpl is None
        mock_call.assert_not_called()

    @patch("src.pipeline.gemini_client.get_model", return_value="test-model")
    @patch("src.pipeline.gemini_client.parse_json_response")
    @patch("src.pipeline.gemini_client.call_gemini")
    def test_non_numeric_answer_returns_none(self, mock_call, mock_parse, mock_model):
        tpl = templatize_via_llm("Вычислите", "", "текст", "exact_number")
        assert tpl is None
        mock_call.assert_not_called()

    @patch("src.pipeline.gemini_client.get_model", return_value="test-model")
    @patch("src.pipeline.gemini_client.parse_json_response")
    @patch("src.pipeline.gemini_client.call_gemini")
    def test_verification_failure_retries(self, mock_call, mock_parse, mock_model):
        """Если формула не верифицируется — пробует ещё раз."""
        bad_resp = _sample_gemini_response()
        bad_resp["answer_formula"] = "a * b"  # неправильная для 15+27=42

        good_resp = _sample_gemini_response()
        good_resp["answer_formula"] = "a + b"

        mock_call.return_value = '{"json": true}'
        mock_parse.side_effect = [bad_resp, good_resp]

        tpl = templatize_via_llm("Вычислите 15 + 27", "", "42", "exact_number")
        assert tpl is not None
        assert tpl.answer_formula_text == "a + b"
        assert mock_call.call_count == 2

    @patch("src.pipeline.gemini_client.get_model", return_value="test-model")
    @patch("src.pipeline.gemini_client.parse_json_response")
    @patch("src.pipeline.gemini_client.call_gemini")
    def test_gemini_exception_returns_none(self, mock_call, mock_parse, mock_model):
        mock_call.side_effect = RuntimeError("API error")
        tpl = templatize_via_llm("Вычислите 15 + 27", "", "42", "exact_number")
        assert tpl is None

    @patch("src.pipeline.gemini_client.get_model", return_value="test-model")
    @patch("src.pipeline.gemini_client.parse_json_response")
    @patch("src.pipeline.gemini_client.call_gemini")
    def test_non_dict_response_returns_none(self, mock_call, mock_parse, mock_model):
        mock_call.return_value = "[]"
        mock_parse.return_value = ["not", "a", "dict"]
        tpl = templatize_via_llm("Вычислите 2 + 3", "", "5", "exact_number")
        assert tpl is None
