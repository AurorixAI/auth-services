"""
ALGO V1 - Платформа для диагностики и тестирования математических навыков

Модули:
- pipeline: Конвейер оцифровки контента
- diagnostic: Алгоритм диагностики (Модуль 1)
- graph: Граф навыков и топологическая сортировка
- exam_builder: Генератор тестов (Модуль 2)
"""

__version__ = "1.0.0"
__author__ = "ALGO Team"

# Pipeline
from src.pipeline.orchestrator import DigitizationPipeline

# Diagnostic (Module 1)
from src.diagnostic.cat_engine import ShadowCATEngine
from src.models.dataclasses import TaskCandidate, SkillNode, DiagnosticPhase, MasteryLevel

# Exam Builder (Module 2)
from src.exam_builder.exam_generator import ExamGenerator, ExamConfig


__all__ = [
    # Pipeline
    "DigitizationPipeline",
    
    # Diagnostic
    "ShadowCATEngine",
    "TaskCandidate",
    "SkillNode",
    "DiagnosticPhase",
    "MasteryLevel",
    
    # Exam Builder
    "ExamGenerator",
    "ExamConfig",
]
