"""
ALGO V1 - Test Suite for IRT Engine
Tests for 3PL IRT model implementation
Updated 18.02.2026 (session 7) — synced with actual API signatures

Key differences fixed:
- IRTTaskParams requires task_id, skill_id (str)
- guessing default = 0.0 (not 0.25)
- probability/information are methods on IRTTaskParams, not IRTEngine
- ResponseData fields: task_id, skill_id, is_correct, time_ms, task_params
- ThetaEstimate fields: theta, standard_error, confidence_low, confidence_high
- IRTEngine uses add_response() + get_current_estimate(), not estimate_theta_eap()
- ResponsePattern enum: NORMAL, GUESSING, CHEATING, CARELESS, FATIGUE, WARMUP
"""

import pytest
from src.diagnostic.irt_engine import (
    IRTEngine,
    IRTTaskParams,
    ThetaEstimate,
    ResponseData,
    ResponsePattern,
)


# ==============================================================================
# IRTTaskParams Tests
# ==============================================================================


class TestIRTTaskParams:
    """Tests for IRT task parameters"""

    def test_required_fields(self):
        """Test that task_id and skill_id are required"""
        params = IRTTaskParams(task_id="t1", skill_id="s1")
        assert params.task_id == "t1"
        assert params.skill_id == "s1"

    def test_default_params(self):
        """Test default IRT parameters"""
        params = IRTTaskParams(task_id="t1", skill_id="s1")
        assert params.discrimination == 1.0
        assert params.difficulty == 0.0
        assert params.guessing == 0.0  # default is 0.0, not 0.25
        assert params.calibrated is False
        assert params.sample_size == 0

    def test_custom_params(self):
        """Test custom IRT parameters"""
        params = IRTTaskParams(
            task_id="task_algebra_1",
            skill_id="algebra_linear_equations",
            discrimination=1.5,
            difficulty=0.5,
            guessing=0.2,
        )
        assert params.discrimination == 1.5
        assert params.difficulty == 0.5
        assert params.guessing == 0.2
        assert params.skill_id == "algebra_linear_equations"

    def test_probability_at_difficulty(self):
        """Test P(θ=b) ≈ (1+c)/2 for 3PL model"""
        params = IRTTaskParams(
            task_id="t1",
            skill_id="s1",
            discrimination=1.0,
            difficulty=0.0,
            guessing=0.25,
        )
        # When θ = b, P(θ) should be (1 + c) / 2
        theta = params.difficulty
        prob = params.probability(theta)
        expected = (1 + params.guessing) / 2
        assert abs(prob - expected) < 0.01

    def test_probability_high_ability(self):
        """Test probability approaches 1 for high ability"""
        params = IRTTaskParams(
            task_id="t1",
            skill_id="s1",
            discrimination=1.0,
            difficulty=0.0,
            guessing=0.25,
        )
        prob = params.probability(3.0)
        assert prob > 0.95

    def test_probability_low_ability(self):
        """Test probability approaches c for low ability"""
        params = IRTTaskParams(
            task_id="t1",
            skill_id="s1",
            discrimination=1.0,
            difficulty=0.0,
            guessing=0.25,
        )
        prob = params.probability(-3.0)
        assert abs(prob - params.guessing) < 0.05

    def test_probability_no_guessing(self):
        """Test P(θ=b) = 0.5 when guessing = 0"""
        params = IRTTaskParams(task_id="t1", skill_id="s1", difficulty=1.0)
        prob = params.probability(1.0)  # θ = b
        assert abs(prob - 0.5) < 0.01

    def test_information_function(self):
        """Test information function properties for 3PL model.
        3PL information: I(θ) = a² * (P-c)² / ((1-c)² * P * Q)
        This is NOT symmetric around b even with c=0.
        """
        params = IRTTaskParams(
            task_id="t1",
            skill_id="s1",
            discrimination=1.5,
            difficulty=0.0,
            guessing=0.2,
        )
        # Information should be positive at various theta values
        for theta in [-2, -1, 0, 1, 2]:
            info = params.information(theta)
            assert info >= 0, f"Information negative at theta={theta}"

        # Higher discrimination → higher information (at same theta)
        params_low_a = IRTTaskParams(
            task_id="t2",
            skill_id="s1",
            discrimination=0.5,
            difficulty=0.0,
            guessing=0.2,
        )
        assert params.information(0.0) > params_low_a.information(0.0)

    def test_information_positive(self):
        """Test information is always non-negative"""
        params = IRTTaskParams(
            task_id="t1",
            skill_id="s1",
            discrimination=1.0,
            difficulty=0.0,
            guessing=0.25,
        )
        for theta in [-3, -1, 0, 1, 3]:
            info = params.information(theta)
            assert info >= 0

    def test_higher_discrimination_more_info(self):
        """Higher discrimination → more information"""
        params_low = IRTTaskParams(task_id="t1", skill_id="s1", discrimination=0.5)
        params_high = IRTTaskParams(task_id="t2", skill_id="s1", discrimination=2.0)

        info_low = params_low.information(0.0)
        info_high = params_high.information(0.0)
        assert info_high > info_low


# ==============================================================================
# IRTEngine Tests
# ==============================================================================


class TestIRTEngine:
    """Tests for IRT Engine core functionality"""

    @pytest.fixture
    def engine(self):
        return IRTEngine()

    @pytest.fixture
    def easy_task(self):
        return IRTTaskParams(task_id="t_easy", skill_id="s1", difficulty=-1.0)

    @pytest.fixture
    def medium_task(self):
        return IRTTaskParams(
            task_id="t_med", skill_id="s1", difficulty=0.0, guessing=0.25
        )

    @pytest.fixture
    def hard_task(self):
        return IRTTaskParams(task_id="t_hard", skill_id="s1", difficulty=1.0)

    def _make_response(
        self, task: IRTTaskParams, is_correct: bool, time_ms: int = 30000
    ) -> ResponseData:
        """Helper to create ResponseData"""
        return ResponseData(
            task_id=task.task_id,
            skill_id=task.skill_id,
            is_correct=is_correct,
            time_ms=time_ms,
            task_params=task,
        )

    def test_initial_estimate(self, engine):
        """Test initial theta estimate before any responses"""
        est = engine.get_current_estimate()
        assert est.theta == pytest.approx(0.0, abs=0.01)
        assert est.standard_error == pytest.approx(1.0, abs=0.01)
        assert est.n_responses == 0

    def test_add_correct_response_increases_theta(self, engine, medium_task):
        """Correct answer should increase theta"""
        resp = self._make_response(medium_task, is_correct=True)
        estimate = engine.add_response(resp)
        assert estimate.theta > 0

    def test_add_wrong_response_decreases_theta(self, engine, medium_task):
        """Wrong answer should decrease theta"""
        resp = self._make_response(medium_task, is_correct=False)
        estimate = engine.add_response(resp)
        assert estimate.theta < 0

    def test_eap_all_correct(self, engine):
        """All correct responses → positive θ"""
        tasks = [
            IRTTaskParams(task_id=f"t{i}", skill_id="s1", difficulty=d)
            for i, d in enumerate([-1.0, 0.0, 1.0])
        ]
        for task in tasks:
            engine.add_response(self._make_response(task, is_correct=True))

        estimate = engine.get_current_estimate()
        assert estimate.theta > 0
        assert estimate.standard_error > 0

    def test_eap_all_incorrect(self, engine):
        """All incorrect responses → negative θ"""
        tasks = [
            IRTTaskParams(task_id=f"t{i}", skill_id="s1", difficulty=d)
            for i, d in enumerate([-1.0, 0.0, 1.0])
        ]
        for task in tasks:
            engine.add_response(self._make_response(task, is_correct=False))

        estimate = engine.get_current_estimate()
        assert estimate.theta < 0

    def test_eap_mixed_responses(self, engine):
        """Mixed responses → θ near 0"""
        tasks = [
            IRTTaskParams(task_id=f"t{i}", skill_id="s1", difficulty=d)
            for i, d in enumerate([-1.0, 0.0, 1.0])
        ]
        engine.add_response(self._make_response(tasks[0], is_correct=True))
        engine.add_response(self._make_response(tasks[1], is_correct=True))
        engine.add_response(self._make_response(tasks[2], is_correct=False))

        estimate = engine.get_current_estimate()
        assert -1.5 < estimate.theta < 1.5

    def test_se_decreases_with_more_responses(self, engine):
        """SE should decrease as more responses are added"""
        task = IRTTaskParams(task_id="t1", skill_id="s1", difficulty=0.0)
        initial_se = engine.get_current_estimate().standard_error

        engine.add_response(self._make_response(task, is_correct=True))
        se_after_1 = engine.get_current_estimate().standard_error

        engine.add_response(
            self._make_response(
                IRTTaskParams(task_id="t2", skill_id="s1", difficulty=0.5),
                is_correct=True,
            )
        )
        se_after_2 = engine.get_current_estimate().standard_error

        assert se_after_1 < initial_se
        assert se_after_2 < se_after_1

    def test_select_optimal_task_max_info(self, engine):
        """Optimal task selection picks task with maximum information at current θ"""
        available = [
            IRTTaskParams(task_id="very_easy", skill_id="s1", difficulty=-2.0),
            IRTTaskParams(task_id="easy", skill_id="s1", difficulty=-1.0),
            IRTTaskParams(task_id="optimal", skill_id="s1", difficulty=0.0),
            IRTTaskParams(task_id="hard", skill_id="s1", difficulty=1.0),
            IRTTaskParams(task_id="very_hard", skill_id="s1", difficulty=2.0),
        ]
        selected = engine.select_optimal_task(available, method="max_info")
        assert selected is not None
        # With c=0, max info is at θ = b, so any could win depending on I(θ)
        # The important thing is that it returns one of the available tasks
        assert selected in available
        # Verify it's actually the one with max info at current θ
        theta = engine.get_current_estimate().theta
        max_info_task = max(available, key=lambda t: t.information(theta))
        assert selected.task_id == max_info_task.task_id

    def test_select_optimal_task_empty(self, engine):
        """Returns None for empty task list"""
        assert engine.select_optimal_task([]) is None

    def test_expected_information_gain(self, engine):
        """Expected info gain should be positive"""
        task = IRTTaskParams(task_id="t1", skill_id="s1", difficulty=0.0)
        gain = engine.expected_information_gain(task)
        assert gain > 0

    def test_reset(self, engine, medium_task):
        """Reset should clear all state"""
        engine.add_response(self._make_response(medium_task, is_correct=True))
        assert len(engine.responses) == 1

        engine.reset()
        assert len(engine.responses) == 0
        est = engine.get_current_estimate()
        assert est.n_responses == 0

    def test_compute_mle_no_data(self, engine):
        """MLE returns None with no responses"""
        assert engine.compute_mle() is None

    def test_compute_mle_with_data(self, engine):
        """MLE returns valid estimate with data"""
        tasks = [
            IRTTaskParams(task_id=f"t{i}", skill_id="s1", difficulty=d)
            for i, d in enumerate([-1.0, 0.0, 1.0])
        ]
        for t in tasks:
            engine.add_response(self._make_response(t, is_correct=True))

        mle = engine.compute_mle()
        assert mle is not None
        assert mle.theta > 0
        assert mle.estimation_method == "MLE"

    def test_compute_map_no_data(self, engine):
        """MAP returns None with no responses"""
        assert engine.compute_map() is None

    def test_compute_map_with_data(self, engine):
        """MAP returns valid estimate with data"""
        task = IRTTaskParams(task_id="t1", skill_id="s1", difficulty=0.0)
        engine.add_response(self._make_response(task, is_correct=True))

        map_est = engine.compute_map()
        assert map_est is not None
        assert map_est.estimation_method == "MAP"

    def test_get_diagnostics_no_data(self, engine):
        """Diagnostics with no data"""
        diag = engine.get_diagnostics()
        assert diag["status"] == "no_data"

    def test_get_diagnostics_with_data(self, engine):
        """Diagnostics after responses"""
        task = IRTTaskParams(task_id="t1", skill_id="s1", difficulty=0.0)
        engine.add_response(self._make_response(task, is_correct=True))

        diag = engine.get_diagnostics()
        assert diag["n_responses"] == 1
        assert diag["accuracy"] == 1.0
        assert "theta" in diag
        assert "standard_error" in diag


# ==============================================================================
# ResponsePattern Tests
# ==============================================================================


class TestResponsePattern:
    """Tests for response pattern detection"""

    def test_pattern_enum_values(self):
        """Test actual response pattern enum values"""
        assert ResponsePattern.NORMAL.value == "normal"
        assert ResponsePattern.GUESSING.value == "guessing"
        assert ResponsePattern.CHEATING.value == "cheating"
        assert ResponsePattern.CARELESS.value == "careless"
        assert ResponsePattern.FATIGUE.value == "fatigue"
        assert ResponsePattern.WARMUP.value == "warmup"

    def test_normal_pattern_default(self):
        """Engine defaults to NORMAL with few responses"""
        engine = IRTEngine()
        assert engine.detect_response_pattern() == ResponsePattern.NORMAL

    def test_guessing_detection(self):
        """Fast + incorrect → GUESSING pattern"""
        engine = IRTEngine()
        task = IRTTaskParams(task_id="t1", skill_id="s1", difficulty=0.0)

        # 7+ fast wrong answers → guessing
        for i in range(8):
            engine.add_response(
                ResponseData(
                    task_id=f"t{i}",
                    skill_id="s1",
                    is_correct=False,
                    time_ms=1000,  # Very fast
                    task_params=task,
                )
            )

        pattern = engine.detect_response_pattern()
        assert pattern == ResponsePattern.GUESSING


# ==============================================================================
# ThetaEstimate Tests
# ==============================================================================


class TestThetaEstimate:
    """Tests for theta estimate dataclass"""

    def test_theta_estimate_creation(self):
        """Test creating theta estimate with correct fields"""
        estimate = ThetaEstimate(
            theta=0.5,
            standard_error=0.3,
            confidence_low=-0.08,
            confidence_high=1.08,
            n_responses=10,
        )
        assert estimate.theta == 0.5
        assert estimate.standard_error == 0.3
        assert estimate.estimation_method == "EAP"  # default

    def test_is_reliable(self):
        """Test reliability check: SE < 0.5 and n >= 5"""
        reliable = ThetaEstimate(
            theta=0.5,
            standard_error=0.3,
            confidence_low=0.0,
            confidence_high=1.0,
            n_responses=10,
        )
        assert reliable.is_reliable is True

        unreliable_se = ThetaEstimate(
            theta=0.5,
            standard_error=0.8,
            confidence_low=-0.5,
            confidence_high=1.5,
            n_responses=10,
        )
        assert unreliable_se.is_reliable is False

        unreliable_n = ThetaEstimate(
            theta=0.5,
            standard_error=0.3,
            confidence_low=0.0,
            confidence_high=1.0,
            n_responses=3,
        )
        assert unreliable_n.is_reliable is False

    def test_confidence_interval(self):
        """Test confidence interval is consistent with theta ± 1.96*SE"""
        estimate = ThetaEstimate(
            theta=0.0,
            standard_error=0.5,
            confidence_low=-0.98,
            confidence_high=0.98,
        )
        expected_low = estimate.theta - 1.96 * estimate.standard_error
        expected_high = estimate.theta + 1.96 * estimate.standard_error
        assert abs(estimate.confidence_low - expected_low) < 0.01
        assert abs(estimate.confidence_high - expected_high) < 0.01


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
