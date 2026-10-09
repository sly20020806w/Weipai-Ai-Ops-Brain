"""只通过已有公司网关客户端取向量；Fake 为显式离线关键词样例。"""

from app.agent.client import LLMClient
from app.agent.fake import EmbeddingStep, FakeLLM, create_llm_client
from app.agent.models import EmbeddingRequest, EmbeddingResponse
from app.config import Settings


def embedding_client(settings: Settings, request: EmbeddingRequest) -> LLMClient:
    if settings.llm_mode != "fake":
        return create_llm_client(settings)
    vectors = tuple(
        tuple(
            float(any(word in content.lower() for word in words))
            for words in (
                ("payment", "支付", "5xx", "连接池"),
                ("network", "网络", "dns"),
                ("capacity", "容量", "磁盘"),
            )
        )
        + (0.1,)
        for content in request.inputs
    )
    return FakeLLM(
        [EmbeddingStep(request, EmbeddingResponse(model="fake-runbooks-v1", vectors=vectors))]
    )
