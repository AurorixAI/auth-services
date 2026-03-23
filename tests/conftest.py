"""
ALGO V1 - Test Suite Configuration
Pytest configuration and fixtures
Updated 18.02.2026 (session 7) — synced with actual API signatures
"""

import pytest
import asyncio
from typing import Generator
from unittest.mock import Mock, AsyncMock


# ==============================================================================
# PYTEST CONFIGURATION
# ==============================================================================


def pytest_configure(config):
    """Configure custom markers"""
    config.addinivalue_line("markers", "unit: Unit tests")
    config.addinivalue_line("markers", "integration: Integration tests")
    config.addinivalue_line("markers", "slow: Slow-running tests")
    config.addinivalue_line("markers", "database: Tests requiring database")


# ==============================================================================
# ASYNC FIXTURES
# ==============================================================================


@pytest.fixture(scope="session")
def event_loop() -> Generator:
    """Create event loop for async tests"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


# ==============================================================================
# DATABASE FIXTURES (MOCKED)
# ==============================================================================


@pytest.fixture
def mock_db_session():
    """Create mock database session"""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    return session


@pytest.fixture
def mock_knowledge_repo():
    """Create mock knowledge repository"""
    repo = Mock()
    repo.get_skills_for_grade = AsyncMock(return_value=[])
    repo.get_skill_by_id = AsyncMock(return_value=None)
    repo.get_prerequisites = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def mock_task_repo():
    """Create mock task repository with correct interface for ProfessionalDiagnosticEngine"""
    repo = Mock()
    repo.get_tasks_for_skill = Mock(return_value=[])
    repo.get_task_by_id = Mock(return_value=None)
    repo.get_tasks_by_difficulty = Mock(return_value=[])
    return repo


@pytest.fixture
def mock_session_repo():
    """Create mock session repository"""
    repo = Mock()
    repo.create_session = AsyncMock(return_value="test-session-id")
    repo.get_session = AsyncMock(return_value=None)
    repo.update_session = AsyncMock()
    return repo


# ==============================================================================
# IRT FIXTURES
# ==============================================================================


@pytest.fixture
def sample_irt_params():
    """Create sample IRT parameters for testing.
    IRTTaskParams requires: task_id (str), skill_id (str).
    Defaults: discrimination=1.0, difficulty=0.0, guessing=0.0
    """
    from src.diagnostic.irt_engine import IRTTaskParams

    return [
        IRTTaskParams(
            task_id="task_easy",
            skill_id="easy_skill",
            discrimination=1.0,
            difficulty=-1.0,
            guessing=0.25,
        ),
        IRTTaskParams(
            task_id="task_medium",
            skill_id="medium_skill",
            discrimination=1.2,
            difficulty=0.0,
            guessing=0.25,
        ),
        IRTTaskParams(
            task_id="task_hard",
            skill_id="hard_skill",
            discrimination=1.5,
            difficulty=1.0,
            guessing=0.25,
        ),
    ]


@pytest.fixture
def sample_responses(sample_irt_params):
    """Create sample response data for testing.
    ResponseData fields: task_id, skill_id, is_correct, time_ms, task_params
    """
    from src.diagnostic.irt_engine import ResponseData

    return [
        ResponseData(
            task_id=sample_irt_params[0].task_id,
            skill_id=sample_irt_params[0].skill_id,
            is_correct=True,
            time_ms=30000,
            task_params=sample_irt_params[0],
        ),
        ResponseData(
            task_id=sample_irt_params[1].task_id,
            skill_id=sample_irt_params[1].skill_id,
            is_correct=True,
            time_ms=45000,
            task_params=sample_irt_params[1],
        ),
        ResponseData(
            task_id=sample_irt_params[2].task_id,
            skill_id=sample_irt_params[2].skill_id,
            is_correct=False,
            time_ms=60000,
            task_params=sample_irt_params[2],
        ),
    ]


# ==============================================================================
# SKILL FIXTURES
# ==============================================================================


@pytest.fixture
def sample_skill_graph():
    """Create sample skill graph for testing.
    SkillNode fields: skill_id, name, name_ru (required str),
                      level (str: L1-L4),
                      mastery_level (MasteryLevel enum),
                      mastery_probability (float 0-1)
    """
    from src.models.dataclasses import SkillNode, MasteryLevel

    return {
        "arithmetic": SkillNode(
            skill_id="arithmetic",
            name="Arithmetic",
            name_ru="Арифметика",
            level="L4",
            prerequisites=[],
            mastery_level=MasteryLevel.MASTERED,
            mastery_probability=0.9,
            attempts=5,
            correct=5,
        ),
        "fractions": SkillNode(
            skill_id="fractions",
            name="Fractions",
            name_ru="Дроби",
            level="L4",
            prerequisites=["arithmetic"],
            mastery_level=MasteryLevel.DEVELOPING,
            mastery_probability=0.6,
            attempts=4,
            correct=2,
        ),
        "algebra": SkillNode(
            skill_id="algebra",
            name="Algebra",
            name_ru="Алгебра",
            level="L4",
            prerequisites=["arithmetic", "fractions"],
            mastery_level=MasteryLevel.UNKNOWN,
            mastery_probability=0.5,
            attempts=0,
            correct=0,
        ),
    }


# ==============================================================================
# API FIXTURES
# ==============================================================================


@pytest.fixture
def test_client():
    """Create test client for API testing"""
    from fastapi.testclient import TestClient
    from src.api.main_v2 import app

    return TestClient(app)
