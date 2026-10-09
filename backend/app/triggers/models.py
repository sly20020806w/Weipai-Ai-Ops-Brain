"""每个指纹唯一对应事件与任务，UTC 时间复用公共基类。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime


class OpsEvent(Base):
    __tablename__ = "ops_events"
    __table_args__ = (
        UniqueConstraint("fingerprint"),
        UniqueConstraint("task_id"),
        CheckConstraint("fingerprint ~ '^[0-9a-f]{64}$'", name="fingerprint_format"),
        CheckConstraint(
            "source IN ('Alert','Ticket','Schedule','State','Prediction','Release','Human','AI')",
            name="source_valid",
        ),
        CheckConstraint(
            "length(trim(external_id)) > 0 AND length(trim(service_name)) > 0 "
            "AND length(trim(title)) > 0",
            name="required_text",
        ),
        CheckConstraint(
            "origin IN ('prometheus','kubernetes','ops_platform','git','ci','argocd',"
            "'config_center','cloud','manual','schedule','state','prediction','learning')",
            name="origin_valid",
        ),
        Index("ix_ops_events_service_time", "service_name", "occurred_at"),
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    service_name: Mapped[str] = mapped_column(String(256), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("ai_tasks.id"), nullable=False)
