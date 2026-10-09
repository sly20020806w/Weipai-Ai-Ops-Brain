"""Step 16 可运行的 Fake 通知演示；不读取宿主凭证或连接飞书。"""

import asyncio
import json

from app.connectors.feishu.fake import FakeFeishuConnector
from app.connectors.feishu.models import CardButton, CardNotification, InteractiveCard


async def main() -> None:
    card = InteractiveCard(
        title="payment-service 需要关注",
        markdown="检测到支付服务异常，请查看任务调查结果。此处为离线通知样例。",
        buttons=(
            CardButton(label="查看任务", value={"kind": "view_task", "task_id": "sample-task"}),
        ),
    )
    async with FakeFeishuConnector() as connector:
        await connector.send_text("支付业务运维通知（Fake 样例）")
        receipt = await connector.send_card(card)
        recorded = connector.get_sent(receipt.notification_id)
        assert isinstance(recorded.notification, CardNotification)
        assert recorded.notification.card == card
        assert len(connector.sent_messages) == 2
        print(
            json.dumps(
                {
                    "mode": "fake",
                    "agent_tools": [],
                    "sent_messages": [
                        record.model_dump(mode="json") for record in connector.sent_messages
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    print("Step 16 Fake 通知验收通过（卡片完整读回，未连接飞书；Tool 排除见专项测试）")


if __name__ == "__main__":
    asyncio.run(main())
