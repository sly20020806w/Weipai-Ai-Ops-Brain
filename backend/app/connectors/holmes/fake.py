"""Fake Holmes 服务仅依据实际传入的快照生成意见。"""

import json

from app.connectors.holmes.client import HolmesConnector, HolmesError
from app.connectors.holmes.models import HolmesRequest, HolmesResponse


class FakeHolmesConnector(HolmesConnector):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[HolmesRequest] = []
        self.closed = False

    async def analyze(self, request: HolmesRequest) -> HolmesResponse:
        if self.closed:
            raise HolmesError("Fake Holmes 已关闭")
        request = HolmesRequest.model_validate_json(request.model_dump_json())
        self.calls.append(request)
        snapshots = request.context.get("evidence")
        if not isinstance(snapshots, list) or not snapshots:
            raise HolmesError("Holmes 缺少快照")
        ids = [item["id"] for item in snapshots if isinstance(item, dict) and "id" in item]
        return HolmesResponse(
            analysis=json.dumps(
                {
                    "assessment": {
                        "statement": f"{request.service_name} 快照提示需核查连接池与数据库容量。",
                        "evidence_ids": ids,
                    },
                    "findings": [],
                    "confidence": 0.65,
                    "uncertainties": ["这是快照 RCA 意见，仍需主 Agent 验证因果与替代原因。"],
                },
                ensure_ascii=False,
            )
        )

    async def aclose(self) -> None:
        self.closed = True
