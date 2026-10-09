"""变更事件去重存储；原始事实和配置值留在源系统。"""

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime


class ChangeEvent(Base):
    __tablename__ = "change_events"
    __table_args__ = (
        UniqueConstraint("service_name", "source", "kind", "source_ref"),
        CheckConstraint("length(trim(service_name)) > 0", name="service_not_blank"),
        CheckConstraint("length(trim(source_ref)) > 0", name="reference_not_blank"),
        CheckConstraint(
            "(source IN ('gitlab', 'github') AND kind IN ('Commit', 'Merge')) OR "
            "(source IN ('jenkins', 'gitlab_ci') AND kind = 'Build') OR "
            "(source = 'argocd' AND kind = 'Sync') OR "
            "(source = 'config_center' AND kind = 'Config') OR "
            "(source = 'kubernetes' AND kind IN ('Image', 'Deploy', 'KubernetesEvent')) OR "
            "(source = 'alibaba_cloud' AND kind = 'CloudEvent')",
            name="source_kind",
        ),
        Index("ix_change_events_service_time", "service_name", "occurred_at"),
    )

    service_name: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revision: Mapped[str | None] = mapped_column(String(512), nullable=True)
