"""
ALGO V1 — Pipeline Package
src/pipeline/

Модульный конвейер оцифровки учебников:
  models              — ExtractedTask, DistractorResult, TemplateResult
  config              — load_config, CFG
  gemini_client       — call_gemini, parse_json_response
  ocr                 — MathpixOCR
  extraction          — LegendExtractor, ParagraphSplitter, TaskExtractor
  validation          — SymPySolver
  enrichment          — AIEnricher
  classification      — SkeletonTextbookMapper
  difficulty          — HybridDifficultyAssessor
  db_writer           — DBWriter
  orchestrator        — DigitizationPipeline
  cli                 — main (pdf / json / audit / status)
"""

from src.pipeline.models import DistractorResult, ExtractedTask, TemplateResult
from src.pipeline.orchestrator import DigitizationPipeline
from src.pipeline.difficulty import HybridDifficultyAssessor

__all__ = [
    # Models
    "ExtractedTask",
    "DistractorResult",
    "TemplateResult",
    # Core modules
    "DigitizationPipeline",
    "HybridDifficultyAssessor",
]
