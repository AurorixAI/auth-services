"""
ALGO V1 — Graph & IRT Calibration ORM Models

Tables: graph_updates_log, irt_calibration_history
"""

from sqlalchemy import (
    Column, String, Integer, Boolean, Text, ForeignKey,
    DateTime, Numeric, BigInteger, Index, CheckConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from src.models.base import Base


class GraphUpdateLog(Base):
    """Лог изменений графа знаний"""
    __tablename__ = 'graph_updates_log'

    id = Column(BigInteger, primary_key=True)

    update_type = Column(String(30), nullable=False)
    entity_type = Column(String(20), nullable=False)
    entity_id = Column(String(50), nullable=False)

    old_value = Column(JSONB)
    new_value = Column(JSONB)
    change_reason = Column(Text)

    triggered_by = Column(String(30), nullable=False)

    requires_review = Column(Boolean, default=False)
    reviewed_by = Column(String(100))
    reviewed_at = Column(DateTime)
    review_status = Column(String(20))

    created_at = Column(DateTime, default=func.now())

    __table_args__ = (
        CheckConstraint("update_type IN ('new_correlation', 'strength_update', 'correlation_removed', 'prerequisite_added', 'prerequisite_removed', 'irt_calibration', 'benchmark_update')"),
        CheckConstraint("triggered_by IN ('ml_system', 'statistical_batch', 'admin_manual', 'expert_review')"),
        CheckConstraint("review_status IN ('pending', 'approved', 'rejected')"),
        Index('idx_gul_type', 'update_type'),
        Index('idx_gul_entity', 'entity_type', 'entity_id'),
        Index('idx_gul_created', 'created_at'),
    )


class IRTCalibrationHistory(Base):
    """История калибровки IRT параметров"""
    __tablename__ = 'irt_calibration_history'

    id = Column(Integer, primary_key=True)
    task_id = Column(String(30), ForeignKey('tasks_master.id'), nullable=False)

    old_discrimination = Column(Numeric(6, 4))
    old_difficulty = Column(Numeric(6, 4))
    old_guessing = Column(Numeric(6, 4))

    new_discrimination = Column(Numeric(6, 4))
    new_difficulty = Column(Numeric(6, 4))
    new_guessing = Column(Numeric(6, 4))

    sample_size = Column(Integer, nullable=False)
    fit_statistic = Column(Numeric(8, 4))

    calibration_method = Column(String(30))
    calibrated_at = Column(DateTime, default=func.now())

    task = relationship("TaskMaster")

    __table_args__ = (
        Index('idx_ich_task', 'task_id'),
        Index('idx_ich_date', 'calibrated_at'),
    )
