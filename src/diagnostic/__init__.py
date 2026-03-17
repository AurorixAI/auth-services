"""
ALGO V1 - Diagnostic Package

Modules:
- irt_engine: 3PL IRT model, EAP/MLE estimation, Fisher Information
- cat_engine: Shadow CAT with content balancing, Sympson-Hetter exposure control
- integrity_service: Anomaly detection (l_z, Guttman, RT analysis, theta jumps)
"""

from .irt_engine import IRTEngine, IRTTaskParams, ResponseData, ThetaEstimate, SkillIRTEstimator
from .cat_engine import ShadowCATEngine, CATConfig, CATPhase, CATTask, ContentBlueprint, ExposureController, get_exposure_controller
from .integrity_service import IntegrityService, IntegrityReport, AnomalyType
from src.models.dataclasses import DiagnosticResult

__all__ = [
    "IRTEngine", "IRTTaskParams", "ResponseData", "ThetaEstimate", "SkillIRTEstimator",
    "ShadowCATEngine", "CATConfig", "CATPhase", "CATTask", "ContentBlueprint", "ExposureController", "get_exposure_controller",
    "IntegrityService", "IntegrityReport", "AnomalyType",
    "DiagnosticResult",
]
