"""人工回答形成待审核知识草稿，与正式知识和语义检索隔离。"""

from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class HumanKnowledgeDraft(Base):
    __tablename__ = "human_knowledge_drafts"
    __table_args__ = (
        UniqueConstraint("task_id", "wait_version"),
        ForeignKeyConstraint(
            ["answer_evidence_id", "task_id"],
            ["evidence_ledger.id", "evidence_ledger.task_id"],
        ),
        CheckConstraint("wait_version > 0", name="wait_version_positive"),
        CheckConstraint(
            "wait_status IN ('NEED_HUMAN_JUDGMENT','WAITING_INFORMATION')", name="wait_status"
        ),
        CheckConstraint("status = 'draft'", name="draft_status"),
        CheckConstraint("length(trim(question)) BETWEEN 1 AND 3000", name="question_length"),
        CheckConstraint("length(trim(answer)) BETWEEN 1 AND 8000", name="answer_length"),
        CheckConstraint("length(trim(respondent)) BETWEEN 1 AND 200", name="respondent_length"),
    )

    task_id: Mapped[UUID] = mapped_column(ForeignKey("ai_tasks.id"), nullable=False)
    wait_version: Mapped[int] = mapped_column(Integer, nullable=False)
    wait_status: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    respondent: Mapped[str] = mapped_column(String(200), nullable=False)
    answer_evidence_id: Mapped[UUID] = mapped_column(nullable=False)
