"""与全部 Connector 相同的 Fake/Reader 选型门禁。"""

from app.config import Settings
from app.connectors.factory import ConnectorFactory
from app.connectors.inspection.client import HTTPInspectionConnector, InspectionConnector
from app.connectors.inspection.fake import FakeInspectionConnector
from app.connectors.models import ReaderCredentials


def create_inspection_connector(settings: Settings) -> InspectionConnector:
    def real(credentials: ReaderCredentials) -> InspectionConnector:
        if settings.inspection_endpoint is None:
            raise ValueError("请配置 INSPECTION_ENDPOINT")
        return HTTPInspectionConnector(settings.inspection_endpoint, credentials)

    return ConnectorFactory[InspectionConnector](
        "inspection", fake=FakeInspectionConnector, real=real
    ).create(settings)
