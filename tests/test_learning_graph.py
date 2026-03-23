"""
ALGO V1 - Test Suite for Learning Graph Updater
Tests for self-learning correlation discovery
Updated 18.02.2026 (session 7) — synced with actual API signatures

Key differences fixed:
- SkillPairStats.phi_coefficient() not .calculate_phi()
- DiscoveredCorrelation: correlation, p_value, relationship_type, direction, strength,
  confidence, discovery_source — not phi_coefficient, suggested_weight, discovery_method
- ErrorPattern: pattern_type, pattern_description, occurrence_count, occurrence_rate,
  root_cause_skills — not error_type, frequency, related_skill, confidence
- LearningGraphUpdater: skill_ids (Set), existing_prerequisites (Dict), batch_size
  — not min_sample_size, correlation_threshold (those are class constants)
"""

import pytest
from datetime import datetime
from src.graph.learning_updater import (
    LearningGraphUpdater,
    SkillPairStats,
    DiscoveredCorrelation,
    ErrorPattern,
    DiscoverySource,
    RelationshipType,
    AnswerEvent,
)


# ==============================================================================
# SkillPairStats Tests
# ==============================================================================


class TestSkillPairStats:
    """Tests for skill pair statistics"""

    def test_stats_creation(self):
        """Test creating skill pair stats"""
        stats = SkillPairStats(
            skill_a="fractions",
            skill_b="decimals",
            both_correct=50,
            a_correct_b_wrong=10,
            a_wrong_b_correct=8,
            both_wrong=32,
        )
        assert stats.skill_a == "fractions"
        assert stats.total == 100

    def test_phi_coefficient_perfect_positive(self):
        """Test phi coefficient = 1.0 for perfect positive correlation"""
        stats = SkillPairStats(
            skill_a="a",
            skill_b="b",
            both_correct=50,
            a_correct_b_wrong=0,
            a_wrong_b_correct=0,
            both_wrong=50,
        )
        phi = stats.phi_coefficient()  # NOT .calculate_phi()
        assert phi == 1.0

    def test_phi_coefficient_perfect_negative(self):
        """Test phi coefficient = -1.0 for perfect negative correlation"""
        stats = SkillPairStats(
            skill_a="a",
            skill_b="b",
            both_correct=0,
            a_correct_b_wrong=50,
            a_wrong_b_correct=50,
            both_wrong=0,
        )
        phi = stats.phi_coefficient()
        assert phi == -1.0

    def test_phi_no_correlation(self):
        """Test phi coefficient ≈ 0 for independent skills"""
        stats = SkillPairStats(
            skill_a="a",
            skill_b="b",
            both_correct=25,
            a_correct_b_wrong=25,
            a_wrong_b_correct=25,
            both_wrong=25,
        )
        phi = stats.phi_coefficient()
        assert abs(phi) < 0.01

    def test_phi_zero_denominator(self):
        """Test phi returns 0 when denominator is 0"""
        stats = SkillPairStats(
            skill_a="a",
            skill_b="b",
            both_correct=0,
            a_correct_b_wrong=0,
            a_wrong_b_correct=0,
            both_wrong=0,
        )
        phi = stats.phi_coefficient()
        assert phi == 0.0

    def test_conditional_probability(self):
        """Test conditional probability P(B|A)"""
        stats = SkillPairStats(
            skill_a="a",
            skill_b="b",
            both_correct=40,
            a_correct_b_wrong=10,
            a_wrong_b_correct=20,
            both_wrong=30,
        )
        p_b_given_a_correct, p_b_given_a_wrong = (
            stats.conditional_probability_b_given_a()
        )
        # P(B correct | A correct) = 40 / (40 + 10) = 0.8
        assert p_b_given_a_correct == pytest.approx(0.8)
        # P(B correct | A wrong) = 20 / (20 + 30) = 0.4
        assert p_b_given_a_wrong == pytest.approx(0.4)

    def test_conditional_probability_empty(self):
        """Test conditional probability returns 0.5 for empty data"""
        stats = SkillPairStats(skill_a="a", skill_b="b")
        p_correct, p_wrong = stats.conditional_probability_b_given_a()
        assert p_correct == 0.5
        assert p_wrong == 0.5

    def test_total_calculation(self):
        """Test total sample size calculation"""
        stats = SkillPairStats(
            skill_a="a",
            skill_b="b",
            both_correct=10,
            a_correct_b_wrong=20,
            a_wrong_b_correct=30,
            both_wrong=40,
        )
        assert stats.total == 100


# ==============================================================================
# DiscoveredCorrelation Tests
# ==============================================================================


class TestDiscoveredCorrelation:
    """Tests for discovered correlations with actual field names"""

    def test_correlation_creation(self):
        """Test creating discovered correlation with correct fields"""
        corr = DiscoveredCorrelation(
            skill_a="fractions",
            skill_b="decimals",
            correlation=0.75,
            p_value=0.001,
            sample_size=150,
            relationship_type=RelationshipType.PREREQUISITE,
            direction="a_to_b",
            strength=8.0,
            confidence=0.95,
            discovery_source=DiscoverySource.STATISTICAL,
        )
        assert corr.correlation == 0.75
        assert corr.p_value == 0.001
        assert corr.relationship_type == RelationshipType.PREREQUISITE
        assert corr.direction == "a_to_b"
        assert corr.strength == 8.0
        assert corr.is_validated is False
        assert corr.requires_review is True

    def test_correlation_significance(self):
        """Test correlation significance threshold"""
        # Significant correlation (|phi| > 0.3, n >= 100)
        significant = DiscoveredCorrelation(
            skill_a="a",
            skill_b="b",
            correlation=0.45,
            p_value=0.01,
            sample_size=100,
            relationship_type=RelationshipType.COREQUISITE,
            direction="bidirectional",
            strength=5.0,
            confidence=0.95,
            discovery_source=DiscoverySource.STATISTICAL,
        )
        assert abs(significant.correlation) > LearningGraphUpdater.CORRELATION_THRESHOLD
        assert significant.sample_size >= LearningGraphUpdater.MIN_SAMPLE_SIZE

        # Not significant — below threshold
        weak = DiscoveredCorrelation(
            skill_a="a",
            skill_b="b",
            correlation=0.15,
            p_value=0.3,
            sample_size=50,
            relationship_type=RelationshipType.COREQUISITE,
            direction="bidirectional",
            strength=2.0,
            confidence=0.6,
            discovery_source=DiscoverySource.STATISTICAL,
        )
        assert abs(weak.correlation) < LearningGraphUpdater.CORRELATION_THRESHOLD

    def test_relationship_types(self):
        """Test all relationship types"""
        assert RelationshipType.PREREQUISITE.value == "prerequisite"
        assert RelationshipType.COREQUISITE.value == "corequisite"
        assert RelationshipType.DIAGNOSTIC.value == "diagnostic"
        assert RelationshipType.TRANSFER.value == "transfer"

    def test_discovery_sources(self):
        """Test all discovery sources"""
        assert DiscoverySource.EXPERT.value == "expert"
        assert DiscoverySource.STATISTICAL.value == "statistical"
        assert DiscoverySource.ML.value == "ml"
        assert DiscoverySource.PATTERN_MINING.value == "pattern_mining"


# ==============================================================================
# ErrorPattern Tests
# ==============================================================================


class TestErrorPattern:
    """Tests for error pattern detection with actual field names"""

    def test_error_pattern_creation(self):
        """Test creating error pattern with correct fields"""
        pattern = ErrorPattern(
            skill_id="fractions_addition",
            pattern_type="common_denominator_error",
            pattern_description="Student fails to find common denominator before adding",
            occurrence_count=45,
            occurrence_rate=0.15,
            root_cause_skills=["fractions_basics", "lcm"],
        )
        assert pattern.skill_id == "fractions_addition"
        assert pattern.pattern_type == "common_denominator_error"
        assert pattern.occurrence_count == 45
        assert pattern.occurrence_rate == 0.15
        assert len(pattern.root_cause_skills) == 2

    def test_pattern_defaults(self):
        """Test error pattern default values"""
        pattern = ErrorPattern(
            skill_id="test",
            pattern_type="unknown",
            pattern_description="Unknown error",
        )
        assert pattern.occurrence_count == 0
        assert pattern.occurrence_rate == 0.0
        assert pattern.root_cause_skills == []

    def test_pattern_priority_by_count(self):
        """Test error pattern priority by occurrence count"""
        patterns = [
            ErrorPattern("a", "type1", "desc1", occurrence_count=10),
            ErrorPattern("a", "type2", "desc2", occurrence_count=50),
            ErrorPattern("a", "type3", "desc3", occurrence_count=25),
        ]
        sorted_patterns = sorted(
            patterns, key=lambda p: p.occurrence_count, reverse=True
        )
        assert sorted_patterns[0].pattern_type == "type2"
        assert sorted_patterns[-1].pattern_type == "type1"


# ==============================================================================
# LearningGraphUpdater Tests
# ==============================================================================


class TestLearningGraphUpdater:
    """Tests for learning graph updater with correct constructor signature"""

    @pytest.fixture
    def updater(self):
        """Create graph updater with correct constructor"""
        return LearningGraphUpdater(
            skill_ids={"algebra", "fractions", "arithmetic", "decimals"},
            existing_prerequisites={
                ("arithmetic", "fractions"): 0.8,
                ("arithmetic", "algebra"): 0.7,
                ("fractions", "algebra"): 0.6,
            },
        )

    def test_updater_initialization(self, updater):
        """Test updater initializes correctly"""
        assert updater is not None
        # Class-level constants
        assert updater.MIN_SAMPLE_SIZE == 100
        assert updater.CORRELATION_THRESHOLD == 0.3
        assert updater.P_VALUE_THRESHOLD == 0.05

    def test_should_analyze_pair(self, updater):
        """Test pair analysis threshold against MIN_SAMPLE_SIZE"""
        # Enough samples (>= 100 = MIN_SAMPLE_SIZE)
        stats_enough = SkillPairStats("a", "b", 40, 20, 15, 35)
        assert stats_enough.total >= updater.MIN_SAMPLE_SIZE  # 110 >= 100

        # Not enough samples
        stats_few = SkillPairStats("a", "b", 5, 2, 2, 1)
        assert stats_few.total < updater.MIN_SAMPLE_SIZE

    def test_is_significant_correlation(self, updater):
        """Test significance check using phi_coefficient"""
        sig_stats = SkillPairStats("a", "b", 40, 5, 5, 50)
        phi = sig_stats.phi_coefficient()
        assert abs(phi) > updater.CORRELATION_THRESHOLD

    def test_weight_calculation_from_phi(self):
        """Test prerequisite weight derived from phi"""
        high_phi = 0.8
        weight = min(abs(high_phi), 1.0) * 0.9
        assert weight == pytest.approx(0.72)

        low_phi = 0.2
        weight = min(abs(low_phi), 1.0) * 0.9
        assert weight == pytest.approx(0.18)

    def test_class_constants(self):
        """Verify class-level constants match expected values"""
        assert LearningGraphUpdater.MIN_SAMPLE_SIZE == 100
        assert LearningGraphUpdater.CORRELATION_THRESHOLD == 0.3
        assert LearningGraphUpdater.P_VALUE_THRESHOLD == 0.05
        assert LearningGraphUpdater.WEIGHT_UPDATE_THRESHOLD == 0.1
        assert LearningGraphUpdater.CONFIDENCE_DECAY == 0.95


# ==============================================================================
# AnswerEvent Tests
# ==============================================================================


class TestAnswerEvent:
    """Tests for answer event data structure"""

    def test_answer_event_creation(self):
        """Test creating answer event"""
        event = AnswerEvent(
            session_id="session_1",
            student_id="student_1",
            skill_id="algebra",
            task_id="task_1",
            is_correct=True,
            time_ms=15000,
            submitted_at=datetime.now(),
        )
        assert event.session_id == "session_1"
        assert event.is_correct is True
        assert event.difficulty == "B"  # default

    def test_answer_event_optional_fields(self):
        """Test optional fields"""
        event = AnswerEvent(
            session_id="s1",
            student_id=None,
            skill_id="s1",
            task_id="t1",
            is_correct=False,
            time_ms=5000,
            submitted_at=datetime.now(),
            difficulty="A",
            theta_before=-0.5,
            theta_after=-0.3,
        )
        assert event.student_id is None
        assert event.difficulty == "A"
        assert event.theta_before == -0.5


# ==============================================================================
# Integration Tests
# ==============================================================================


class TestIntegration:
    """Integration tests for graph updates"""

    def test_correlation_to_weight_flow(self):
        """Test flow from correlation to weight update"""
        stats = SkillPairStats(
            skill_a="multiplication",
            skill_b="division",
            both_correct=60,
            a_correct_b_wrong=15,
            a_wrong_b_correct=10,
            both_wrong=15,
        )

        # Use phi_coefficient() — correct method name
        phi = stats.phi_coefficient()
        assert phi > 0  # Positive correlation expected

        # Determine if significant
        is_significant = (
            abs(phi) > LearningGraphUpdater.CORRELATION_THRESHOLD
            and stats.total >= LearningGraphUpdater.MIN_SAMPLE_SIZE
        )
        assert is_significant

        # Calculate suggested weight
        suggested_weight = min(abs(phi), 1.0)
        assert 0 < suggested_weight <= 1.0

    def test_batch_update_logic(self):
        """Test batch update accumulation"""
        cumulative = SkillPairStats("a", "b")

        sessions = [
            (10, 2, 3, 5),
            (15, 3, 2, 10),
            (12, 1, 1, 6),
        ]

        for bc, acbw, awbc, bw in sessions:
            cumulative.both_correct += bc
            cumulative.a_correct_b_wrong += acbw
            cumulative.a_wrong_b_correct += awbc
            cumulative.both_wrong += bw

        assert cumulative.total == 70
        assert cumulative.both_correct == 37


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
