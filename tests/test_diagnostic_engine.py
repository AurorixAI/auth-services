"""
ALGO V1 - Test Suite for Professional Diagnostic Engine
Tests for diagnostic phases, skill mastery detection, and engine initialization
Updated 18.02.2026 (session 7) — synced with actual API signatures

Key differences fixed:
- MasteryLevel enum: MASTERED, PROFICIENT, DEVELOPING, STRUGGLING, GAP, UNKNOWN
- SkillNode: requires name_ru (str), level is str (L1-L4), no 'dependents' or 'mastery' field
- TaskCandidate: task_id str, difficulty str (A/B/C), irt_params required
- SessionState: no grade, no theta_estimate, no skill_states, no response_history
- DiagnosticResult: started_at required, skill_levels not skill_masteries
- Engine constructor: skill_graph + task_repository (not knowledge_repo/task_repo)
"""

import pytest
from datetime import datetime
from unittest.mock import Mock
from src.diagnostic.irt_engine import IRTTaskParams
from src.models.dataclasses import (
    DiagnosticPhase,
    MasteryLevel,
    SkillNode,
    TaskCandidate,
    DiagnosticResult,
)


# ==============================================================================
# DiagnosticPhase Tests
# ==============================================================================


class TestDiagnosticPhase:
    """Tests for diagnostic phase transitions"""

    def test_phase_values(self):
        """Test phase enum values"""
        assert DiagnosticPhase.LIFT.value == "lift"
        assert DiagnosticPhase.ADAPTIVE.value == "adaptive"
        assert DiagnosticPhase.DEEP_SCAN.value == "deep_scan"
        assert DiagnosticPhase.SUMMARY.value == "summary"
        assert DiagnosticPhase.COMPLETED.value == "completed"

    def test_phase_ordering(self):
        """Test phases follow logical order"""
        phases = list(DiagnosticPhase)
        assert phases[0] == DiagnosticPhase.LIFT
        # COMPLETED is last
        assert phases[-1] == DiagnosticPhase.COMPLETED

    def test_all_phases_present(self):
        """All 5 phases exist"""
        assert len(DiagnosticPhase) == 5


# ==============================================================================
# MasteryLevel Tests
# ==============================================================================


class TestMasteryLevel:
    """Tests for mastery level enum"""

    def test_mastery_values(self):
        """Test mastery level values"""
        assert MasteryLevel.UNKNOWN.value == "unknown"
        assert MasteryLevel.GAP.value == "gap"
        assert MasteryLevel.STRUGGLING.value == "struggling"
        assert MasteryLevel.DEVELOPING.value == "developing"
        assert MasteryLevel.PROFICIENT.value == "proficient"
        assert MasteryLevel.MASTERED.value == "mastered"

    def test_from_probability_mastered(self):
        """p >= 0.9 → MASTERED"""
        assert MasteryLevel.from_probability(0.95) == MasteryLevel.MASTERED
        assert MasteryLevel.from_probability(0.9) == MasteryLevel.MASTERED

    def test_from_probability_proficient(self):
        """0.7 <= p < 0.9 → PROFICIENT"""
        assert MasteryLevel.from_probability(0.8) == MasteryLevel.PROFICIENT
        assert MasteryLevel.from_probability(0.7) == MasteryLevel.PROFICIENT

    def test_from_probability_developing(self):
        """0.5 <= p < 0.7 → DEVELOPING"""
        assert MasteryLevel.from_probability(0.6) == MasteryLevel.DEVELOPING
        assert MasteryLevel.from_probability(0.5) == MasteryLevel.DEVELOPING

    def test_from_probability_struggling(self):
        """0.3 <= p < 0.5 → STRUGGLING"""
        assert MasteryLevel.from_probability(0.4) == MasteryLevel.STRUGGLING
        assert MasteryLevel.from_probability(0.3) == MasteryLevel.STRUGGLING

    def test_from_probability_gap(self):
        """p < 0.3 → GAP"""
        assert MasteryLevel.from_probability(0.2) == MasteryLevel.GAP
        assert MasteryLevel.from_probability(0.0) == MasteryLevel.GAP


# ==============================================================================
# SkillNode Tests
# ==============================================================================


class TestSkillNode:
    """Tests for skill node structure"""

    def test_skill_node_creation(self):
        """Test creating skill node with correct fields"""
        node = SkillNode(
            skill_id="algebra_linear",
            name="Linear Equations",
            name_ru="Линейные уравнения",
            level="L4",
            prerequisites=["arithmetic_operations"],
        )
        assert node.skill_id == "algebra_linear"
        assert node.level == "L4"
        assert len(node.prerequisites) == 1
        assert node.mastery_level == MasteryLevel.UNKNOWN  # default
        assert node.mastery_probability == 0.5  # default
        assert node.is_tested is False
        assert node.is_gap is False
        assert node.is_root_gap is False

    def test_skill_node_default_values(self):
        """Test default field values"""
        node = SkillNode(
            skill_id="test",
            name="Test",
            name_ru="Тест",
            level="L3",
        )
        assert node.prerequisites == []
        assert node.prerequisite_weights == {}
        assert node.theta is None
        assert node.theta_se is None
        assert node.attempts == 0
        assert node.correct == 0
        assert node.last_attempt is None

    def test_skill_node_mastery_update(self):
        """Test updating skill mastery"""
        node = SkillNode(
            skill_id="test",
            name="Test",
            name_ru="Тест",
            level="L4",
        )

        # Simulate correct answer
        node.attempts += 1
        node.correct += 1
        node.mastery_probability = 0.8
        node.mastery_level = MasteryLevel.PROFICIENT

        assert node.attempts == 1
        assert node.correct == 1
        assert node.mastery_level == MasteryLevel.PROFICIENT

    def test_skill_node_gap_flags(self):
        """Test gap detection flags"""
        node = SkillNode(
            skill_id="fractions",
            name="Fractions",
            name_ru="Дроби",
            level="L4",
            mastery_probability=0.2,
            mastery_level=MasteryLevel.GAP,
            is_gap=True,
            is_root_gap=True,
        )
        assert node.is_gap is True
        assert node.is_root_gap is True
        assert node.mastery_level == MasteryLevel.GAP


# ==============================================================================
# TaskCandidate Tests
# ==============================================================================


class TestTaskCandidate:
    """Tests for task candidate selection"""

    def test_task_candidate_creation(self):
        """Test creating task candidate with correct fields"""
        irt_params = IRTTaskParams(
            task_id="task_1",
            skill_id="algebra_linear",
            discrimination=1.2,
            difficulty=0.5,
            guessing=0.25,
        )
        candidate = TaskCandidate(
            task_id="task_1",
            skill_id="algebra_linear",
            difficulty="B",  # A, B, C — not a float
            irt_params=irt_params,
            information_gain=0.8,
            priority_score=0.75,
        )
        assert candidate.task_id == "task_1"
        assert candidate.difficulty == "B"
        assert candidate.information_gain == 0.8
        assert candidate.priority_score == 0.75
        assert candidate.already_used is False
        assert candidate.similar_used is False

    def test_candidate_comparison(self):
        """Test comparing candidates by priority"""
        irt1 = IRTTaskParams(task_id="t1", skill_id="s1")
        irt2 = IRTTaskParams(task_id="t2", skill_id="s2")

        c1 = TaskCandidate(
            task_id="t1",
            skill_id="s1",
            difficulty="A",
            irt_params=irt1,
            priority_score=0.6,
        )
        c2 = TaskCandidate(
            task_id="t2",
            skill_id="s2",
            difficulty="B",
            irt_params=irt2,
            priority_score=0.8,
        )
        assert c2.priority_score > c1.priority_score


# ==============================================================================
# SessionState Tests
# ==============================================================================


class TestSessionState:
    """Tests for session state management"""

    def test_session_state_creation(self):
        """Test creating session state with correct fields"""
        state = SessionState(
            session_id="test-123",
            student_id="student_1",
        )
        assert state.session_id == "test-123"
        assert state.student_id == "student_1"
        assert state.current_phase == DiagnosticPhase.LIFT
        assert state.tasks_in_phase == 0
        assert state.total_tasks == 0
        assert state.responses == []
        assert state.used_task_ids == set()
        assert state.tested_skills == set()

    def test_session_state_update(self):
        """Test updating session state counters"""
        state = SessionState(
            session_id="test-123",
            student_id="student_1",
        )

        state.total_tasks += 1
        state.tasks_in_phase += 1
        state.tested_skills.add("algebra")
        state.used_task_ids.add("task_1")

        assert state.total_tasks == 1
        assert state.tasks_in_phase == 1
        assert "algebra" in state.tested_skills
        assert "task_1" in state.used_task_ids

    def test_session_phase_transition(self):
        """Test phase transitions"""
        state = SessionState(session_id="s1", student_id=None)
        assert state.current_phase == DiagnosticPhase.LIFT

        state.current_phase = DiagnosticPhase.ADAPTIVE
        state.tasks_in_phase = 0
        assert state.current_phase == DiagnosticPhase.ADAPTIVE


# ==============================================================================
# DiagnosticResult Tests
# ==============================================================================


class TestDiagnosticResult:
    """Tests for diagnostic result structure"""

    def test_result_creation(self):
        """Test creating diagnostic result with correct fields"""
        result = DiagnosticResult(
            session_id="test-123",
            student_id="student_1",
            started_at=datetime.now(),
            overall_theta=0.5,
            overall_theta_se=0.3,
            skill_levels={
                "algebra": MasteryLevel.MASTERED,
                "fractions": MasteryLevel.DEVELOPING,
            },
            identified_gaps=["fractions"],
            root_gaps=["fraction_basics"],
            learning_path=["fraction_basics", "fractions"],
            total_tasks=15,
            duration_seconds=900,
        )
        assert result.overall_theta == 0.5
        assert len(result.skill_levels) == 2
        assert len(result.identified_gaps) == 1
        assert result.total_tasks == 15

    def test_result_defaults(self):
        """Test result default values"""
        result = DiagnosticResult(
            session_id="test",
            student_id=None,
            started_at=datetime.now(),
        )
        assert result.total_tasks == 0
        assert result.correct_answers == 0
        assert result.accuracy == 0.0
        assert result.overall_theta == 0.0
        assert result.overall_theta_se == 1.0
        assert result.response_pattern == "normal"
        assert result.identified_gaps == []
        assert result.root_gaps == []
        assert result.learning_path == []


# ==============================================================================
# ProfessionalDiagnosticEngine Tests
# ==============================================================================


class TestProfessionalDiagnosticEngine:
    """Tests for the main diagnostic engine"""

    @pytest.fixture
    def skill_graph(self):
        """Create minimal skill graph"""
        return {
            "arithmetic": SkillNode(
                skill_id="arithmetic",
                name="Arithmetic",
                name_ru="Арифметика",
                level="L4",
            ),
            "fractions": SkillNode(
                skill_id="fractions",
                name="Fractions",
                name_ru="Дроби",
                level="L4",
                prerequisites=["arithmetic"],
            ),
        }

    @pytest.fixture
    def mock_task_repository(self):
        """Create mock task repository"""
        repo = Mock()
        repo.get_tasks_for_skill = Mock(return_value=[])
        return repo

    @pytest.fixture
    def engine(self, skill_graph, mock_task_repository):
        """Create diagnostic engine with correct constructor signature"""
        return ProfessionalDiagnosticEngine(
            skill_graph=skill_graph,
            task_repository=mock_task_repository,
        )

    def test_engine_initialization(self, engine):
        """Test engine initializes correctly"""
        assert engine is not None

    def test_engine_constants(self, engine):
        """Test engine has expected constants"""
        assert engine.MASTERY_THRESHOLD == 0.7
        assert engine.GAP_THRESHOLD == 0.4
        assert engine.STOPPING_SE == 0.3
        assert engine.LIFT_TASKS_PER_SKILL == 2
        assert engine.ADAPTIVE_MAX_TASKS == 20

    def test_start_session(self, engine):
        """Test session creation returns session ID"""
        session_id = engine.start_session(student_id="test_student")
        assert session_id is not None
        assert isinstance(session_id, str)
        assert len(session_id) > 0

    def test_mastery_threshold(self):
        """Test mastery determination via MasteryLevel.from_probability"""
        # High probability → mastered
        assert MasteryLevel.from_probability(0.95) == MasteryLevel.MASTERED

        # Low probability → gap
        assert MasteryLevel.from_probability(0.2) == MasteryLevel.GAP

        # Medium → developing
        assert MasteryLevel.from_probability(0.6) == MasteryLevel.DEVELOPING


# ==============================================================================
# Integration Tests
# ==============================================================================


class TestIntegration:
    """Integration tests for diagnostic flow"""

    def test_full_diagnostic_flow_phases(self):
        """Test complete diagnostic flow phase sequence"""
        phases = [
            DiagnosticPhase.LIFT,
            DiagnosticPhase.ADAPTIVE,
            DiagnosticPhase.DEEP_SCAN,
            DiagnosticPhase.SUMMARY,
            DiagnosticPhase.COMPLETED,
        ]

        # Verify phases are distinct
        for i in range(len(phases) - 1):
            assert phases[i] != phases[i + 1]

    def test_gap_identification(self):
        """Test gap identification logic using correct MasteryLevel values"""
        skill_states = {
            "arithmetic": MasteryLevel.MASTERED,
            "fractions": MasteryLevel.GAP,
            "algebra": MasteryLevel.STRUGGLING,
        }

        gaps = [
            skill
            for skill, mastery in skill_states.items()
            if mastery in [MasteryLevel.GAP, MasteryLevel.STRUGGLING]
        ]

        assert "fractions" in gaps
        assert "algebra" in gaps
        assert "arithmetic" not in gaps

    def test_mastery_level_hierarchy(self):
        """Test mastery levels form logical progression"""
        probs = [0.95, 0.8, 0.6, 0.4, 0.2]
        expected = [
            MasteryLevel.MASTERED,
            MasteryLevel.PROFICIENT,
            MasteryLevel.DEVELOPING,
            MasteryLevel.STRUGGLING,
            MasteryLevel.GAP,
        ]
        for p, exp in zip(probs, expected):
            assert MasteryLevel.from_probability(p) == exp


# ==============================================================================
# Prerequisite Filter Tests
# ==============================================================================


class TestPrereqFilter:
    """Tests for prerequisite-aware task selection in LIFT/ADAPTIVE phases"""

    @pytest.fixture
    def prereq_skill_graph(self):
        """Skill graph with prerequisites and weights for prereq testing"""
        return {
            "arithmetic": SkillNode(
                skill_id="arithmetic",
                name="Arithmetic",
                name_ru="Арифметика",
                level="L4",
            ),
            "fractions": SkillNode(
                skill_id="fractions",
                name="Fractions",
                name_ru="Дроби",
                level="L4",
                prerequisites=["arithmetic"],
                prerequisite_weights={"arithmetic": 8.0},  # Hard dependency
            ),
            "linear_eq": SkillNode(
                skill_id="linear_eq",
                name="Linear Equations",
                name_ru="Линейные уравнения",
                level="L4",
                prerequisites=["arithmetic"],
                prerequisite_weights={"arithmetic": 9.0},  # Hard dependency
            ),
            "quadratic_eq": SkillNode(
                skill_id="quadratic_eq",
                name="Quadratic Equations",
                name_ru="Квадратные уравнения",
                level="L4",
                prerequisites=["linear_eq", "fractions"],
                prerequisite_weights={
                    "linear_eq": 9.0,
                    "fractions": 4.0,
                },  # Hard + Soft
            ),
            "geometry_basic": SkillNode(
                skill_id="geometry_basic",
                name="Basic Geometry",
                name_ru="Базовая геометрия",
                level="L4",
                prerequisites=["arithmetic"],
                prerequisite_weights={"arithmetic": 3.0},  # Soft dependency
            ),
        }

    @pytest.fixture
    def prereq_engine(self, prereq_skill_graph):
        """Engine with prerequisite-rich skill graph"""
        repo = Mock()
        repo.get_tasks_for_skill = Mock(return_value=[])
        return ProfessionalDiagnosticEngine(
            skill_graph=prereq_skill_graph,
            task_repository=repo,
        )

    def test_engine_has_prereq_constants(self, prereq_engine):
        """Engine has prerequisite-related constants"""
        assert prereq_engine.HARD_PREREQ_WEIGHT_THRESHOLD == 7.0
        assert prereq_engine.SOFT_PREREQ_PENALTY == 0.3

    def test_skill_eligible_no_prerequisites(self, prereq_engine):
        """Skill without prerequisites is always eligible"""
        eligible, penalty = prereq_engine._is_skill_eligible("arithmetic")
        assert eligible is True
        assert penalty == 1.0

    def test_skill_eligible_untested_prerequisites(self, prereq_engine):
        """Untested prerequisites do NOT block (unknown state)"""
        # arithmetic is not tested yet → fractions should be eligible
        eligible, penalty = prereq_engine._is_skill_eligible("fractions")
        assert eligible is True
        assert penalty == 1.0

    def test_skill_blocked_hard_prereq_gap(self, prereq_engine):
        """Skill blocked when hard prerequisite (weight >= 7) is confirmed gap"""
        # Mark arithmetic as tested AND a gap
        node = prereq_engine.skill_graph["arithmetic"]
        node.is_tested = True
        node.mastery_probability = 0.2  # Below GAP_THRESHOLD (0.4)

        # fractions depends on arithmetic (weight=8 → hard) → BLOCKED
        eligible, penalty = prereq_engine._is_skill_eligible("fractions")
        assert eligible is False
        assert penalty == 0.0

    def test_skill_penalized_soft_prereq_gap(self, prereq_engine):
        """Skill penalized (not blocked) when soft prereq (weight < 7) is gap"""
        # Mark arithmetic as tested and gap
        node = prereq_engine.skill_graph["arithmetic"]
        node.is_tested = True
        node.mastery_probability = 0.2

        # geometry_basic depends on arithmetic (weight=3 → soft) → PENALIZED
        eligible, penalty = prereq_engine._is_skill_eligible("geometry_basic")
        assert eligible is True
        assert penalty == prereq_engine.SOFT_PREREQ_PENALTY  # 0.3

    def test_skill_eligible_when_prereq_mastered(self, prereq_engine):
        """Skill eligible when hard prerequisite is mastered"""
        node = prereq_engine.skill_graph["arithmetic"]
        node.is_tested = True
        node.mastery_probability = 0.8  # Above GAP_THRESHOLD

        eligible, penalty = prereq_engine._is_skill_eligible("fractions")
        assert eligible is True
        assert penalty == 1.0

    def test_mixed_hard_soft_prereqs(self, prereq_engine):
        """quadratic_eq: hard prereq on linear_eq, soft on fractions"""
        # linear_eq (hard, w=9) → mastered, fractions (soft, w=4) → gap
        prereq_engine.skill_graph["linear_eq"].is_tested = True
        prereq_engine.skill_graph["linear_eq"].mastery_probability = 0.8
        prereq_engine.skill_graph["fractions"].is_tested = True
        prereq_engine.skill_graph["fractions"].mastery_probability = 0.2

        eligible, penalty = prereq_engine._is_skill_eligible("quadratic_eq")
        assert eligible is True  # Hard prereq OK
        assert penalty == prereq_engine.SOFT_PREREQ_PENALTY  # Soft prereq gap

    def test_mixed_prereqs_hard_gap_blocks(self, prereq_engine):
        """quadratic_eq blocked when hard prereq (linear_eq) is a gap"""
        prereq_engine.skill_graph["linear_eq"].is_tested = True
        prereq_engine.skill_graph["linear_eq"].mastery_probability = 0.1  # GAP
        prereq_engine.skill_graph["fractions"].is_tested = True
        prereq_engine.skill_graph["fractions"].mastery_probability = 0.8

        eligible, penalty = prereq_engine._is_skill_eligible("quadratic_eq")
        assert eligible is False

    def test_unknown_skill_eligible(self, prereq_engine):
        """Unknown skill_id returns eligible"""
        eligible, penalty = prereq_engine._is_skill_eligible("nonexistent_skill")
        assert eligible is True
        assert penalty == 1.0


# ==============================================================================
# Exam Prerequisite Validation Tests
# ==============================================================================


class TestExamPrereqValidation:
    """Tests for exam generator prerequisite validation"""

    def test_prerequisite_warning_creation(self):
        """Test PrerequisiteWarning dataclass"""
        from src.exam_builder.exam_generator import PrerequisiteWarning

        w = PrerequisiteWarning(
            skill_id="ALG_QUAD",
            skill_name="Квадратные уравнения",
            missing_prereq_id="ALG_LINEAR",
            missing_prereq_name="Линейные уравнения",
            dependency_type="hard",
            weight=9.0,
        )
        assert w.skill_id == "ALG_QUAD"
        assert w.dependency_type == "hard"
        assert w.weight == 9.0

    def test_generated_exam_has_warnings_field(self):
        """GeneratedExam includes prerequisite_warnings field"""
        from src.exam_builder.exam_generator import (
            ExamGenerator,
            ExamConfig,
            ExamType,
            VariantMode,
        )

        gen = ExamGenerator()
        config = ExamConfig(
            title="Test",
            exam_type=ExamType.QUIZ,
            textbook_id="TEST",
            paragraphs=["1.1"],
            tasks_count=0,  # stub repo returns no tasks
            variant_mode=VariantMode.ORIGINAL,
        )
        exam = gen.generate_exam(config)
        assert hasattr(exam, "prerequisite_warnings")
        assert isinstance(exam.prerequisite_warnings, list)

    def test_validate_without_db_returns_empty(self):
        """validate_exam_prerequisites returns [] when no DB"""
        from src.exam_builder.exam_generator import ExamGenerator

        gen = ExamGenerator()  # no db_session
        warnings = gen.validate_exam_prerequisites(["ALG_QUAD", "ALG_LINEAR"])
        assert warnings == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
