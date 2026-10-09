"""可手动运行的离线验收样例。"""

import asyncio
import json

from app.config import Settings
from app.connectors.ops_platform.factory import create_ops_platform_connector


async def main() -> None:
    settings = Settings(
        APP_ENV="local",
        CONNECTOR_MODE="fake",
        CONNECTOR_READER_TOKENS={},
        OPS_PLATFORM_CONFIG=None,
        PROMETHEUS_CONFIG=None,
        SLS_CONFIG=None,
        ARMS_CONFIG=None,
        DATABASE_URL=None,
        LLM_MODE="fake",
    )
    async with create_ops_platform_connector(settings) as connector:
        application = await connector.get_application("payment-service")
        business = next(
            node
            for node in await connector.list_service_tree()
            if node.id == application.business_id
        )
        owners = await connector.list_owners(application.service_name)
        tickets = await connector.list_tickets(service_name=application.service_name, status="open")
        assert application.business_id == "payment" and owners[0].id == "owner-payment"
        assert len(tickets) == 1 and tickets[0].id == "TICKET-1001"
        assert await connector.get_ticket(tickets[0].id) == tickets[0]
        print(
            json.dumps(
                {
                    "mode": "fake",
                    "service": application.service_name,
                    "business": business.name,
                    "owners": [owner.model_dump(mode="json") for owner in owners],
                    "tickets": [ticket.model_dump(mode="json") for ticket in tickets],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    print("Step 11 Fake 样例验收通过（未连接真实系统）")


if __name__ == "__main__":
    asyncio.run(main())
