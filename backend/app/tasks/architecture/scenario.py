"""显式 Fake 样例：仅识别样例单点，其他维度如实保留待补充。"""

import json
from typing import Literal
from uuid import UUID

from app.agent.fake import FakeLLM, ScriptedChatStep
from app.agent.models import ChatMessage, ChatRequest, ChatResponse
from app.tasks.architecture.models import DIMENSIONS, Citation, DimensionReview, ReviewDraft

SAMPLE_PROPOSAL = (
    "payment-service 部署在 ACK，支付数据写入单点数据库 payment-db。"
    "暂未设计数据库备库和故障切换；其他设计材料待补充。"
)
SAMPLE_STANDARD = "支付核心数据库必须消除单点，配置跨可用区备库、自动故障切换，并定期演练恢复。"


def sample_response(request: ChatRequest) -> ChatResponse:
    payload = json.loads(request.messages[-1].content or "{}")
    sources = payload["sources"]
    proposal_id = UUID(sources["proposal"])
    snapshots = payload["snapshots"]
    proposal = snapshots[str(proposal_id)]["proposal"]
    matches = snapshots[sources["standards"]]["matches"]
    related = next((m for m in matches if "消除单点" in m["entry"]["content"]), None)
    dimensions = []
    for dimension in DIMENSIONS:
        citations: tuple[Citation, ...] = (
            Citation(evidence_id=proposal_id, quote=proposal[:2000]),
        )
        finding = f"{dimension}材料不足，当前仅有提交的方案摘要，待补充核实。"
        recommendation = f"补充{dimension}设计、约束、容量数据或演练证据后再评审。"
        outcome: Literal["risk", "supported", "unknown"] = "unknown"
        if dimension in {"稳定性", "高可用"} and proposal == SAMPLE_PROPOSAL:
            outcome = "risk"
            finding = "方案明确包含单点数据库，数据库故障可能中断支付链路；备库与故障切换尚未设计。"
            recommendation = "消除数据库单点，补充跨可用区备库、故障切换和恢复演练方案。"
            if related is not None:
                citations += (
                    Citation(
                        evidence_id=UUID(sources["standards"]),
                        quote=related["entry"]["content"][:2000],
                    ),
                )
        dimensions.append(
            DimensionReview(
                dimension=dimension,
                outcome=outcome,
                finding=finding,
                recommendation=recommendation,
                citations=citations,
            )
        )
    draft = ReviewDraft(dimensions=tuple(dimensions))
    return ChatResponse(
        id="fake-architecture-review",
        model="fake-architecture-v1",
        finish_reason="stop",
        message=ChatMessage(role="assistant", content=draft.model_dump_json()),
    )


def fake_review_llm() -> FakeLLM:
    return FakeLLM([ScriptedChatStep(sample_response)])
