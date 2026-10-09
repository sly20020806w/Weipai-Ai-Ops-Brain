"""源事件适配与验签；HTTP 层只负责状态码映射。"""

import hashlib
import hmac
import json
import time
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from app.connectors.kubernetes.models import Event
from app.tasks.states import TaskSource
from app.triggers.config import TriggerConfig
from app.triggers.schemas import EventOrigin, NormalizedEvent


class SignatureError(ValueError):
    pass


class PayloadError(ValueError):
    pass


def verify_signature(
    config: TriggerConfig,
    origin: str,
    body: bytes,
    timestamp: str,
    signature: str,
    *,
    now: float | None = None,
) -> None:
    secret = config.webhook_secrets.get(origin)
    if (
        secret is None
        or not timestamp.isascii()
        or not timestamp.isdecimal()
        or len(timestamp) > 12
    ):
        raise SignatureError("Webhook 签名无效")
    if (
        abs((time.time() if now is None else now) - int(timestamp))
        > config.signature_max_age_seconds
    ):
        raise SignatureError("Webhook 签名无效")
    expected = (
        "sha256="
        + hmac.new(
            secret.get_secret_value().encode(), timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest()
    )
    if not hmac.compare_digest(expected.encode(), signature.encode("utf-8")):
        raise SignatureError("Webhook 签名无效")


class Alert(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    status: Literal["firing", "resolved"]
    labels: dict[str, str]
    starts_at: AwareDatetime = Field(alias="startsAt")


class AlertmanagerPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    version: Literal["4"] = "4"
    alerts: list[Alert] = Field(max_length=100)
    truncated_alerts: int = Field(default=0, alias="truncatedAlerts", ge=0)


class SourcePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    external_id: str
    service_name: str
    title: str
    occurred_at: AwareDatetime
    source: TaskSource


def normalize_webhook(origin: EventOrigin, body: bytes) -> list[NormalizedEvent]:
    try:
        if origin == "prometheus":
            payload = AlertmanagerPayload.model_validate_json(body)
            if payload.truncated_alerts:
                raise ValueError("截断的告警批次不完整")
            result = []
            for alert in payload.alerts:
                if alert.status == "resolved":
                    continue
                name, service = alert.labels.get("alertname", ""), alert.labels.get("service", "")
                if not name or not service:
                    raise ValueError("告警缺少 alertname/service 标签")
                # 标签序稳定，startsAt 区分同标签的下一次告警；忽略发送方 fingerprint。
                identity = json.dumps(alert.labels, sort_keys=True, ensure_ascii=False)
                from datetime import UTC

                external_id = hashlib.sha256(
                    (identity + alert.starts_at.astimezone(UTC).isoformat()).encode()
                ).hexdigest()
                result.append(
                    NormalizedEvent(
                        origin=origin,
                        source=TaskSource.ALERT,
                        external_id=external_id,
                        service_name=service,
                        title=f"告警：{name}",
                        occurred_at=alert.starts_at,
                    )
                )
            return result
        payload2 = SourcePayload.model_validate_json(body)
        allowed = {
            "ops_platform": {TaskSource.TICKET},
            "git": {TaskSource.RELEASE},
            "ci": {TaskSource.RELEASE},
            "argocd": {TaskSource.RELEASE},
            "config_center": {TaskSource.RELEASE},
            "cloud": {TaskSource.ALERT},
            "manual": {TaskSource.HUMAN},
        }
        if payload2.source not in allowed.get(origin, set()):
            raise ValueError("事件来源与任务来源不匹配")
        return [NormalizedEvent(origin=origin, **payload2.model_dump())]
    except (ValidationError, ValueError, TypeError):
        raise PayloadError("事件内容无效，请检查来源、服务、标识及带时区的发生时间") from None


def normalize_kubernetes(
    cluster: str, event: Event, *, service_name: str | None = None
) -> NormalizedEvent | None:
    if event.type != "Warning":
        return None
    occurred_at = event.first_timestamp or event.event_time or event.last_timestamp
    if occurred_at is None:
        raise PayloadError("K8s Warning 缺少源系统时间，不能伪造发生时间")
    return NormalizedEvent(
        origin="kubernetes",
        source=TaskSource.ALERT,
        external_id=f"{cluster}/{event.metadata.namespace}/{event.metadata.uid}",
        service_name=service_name
        or event.metadata.labels.get("app.kubernetes.io/name")
        or f"{event.metadata.namespace}/{event.involved_object.kind}/{event.involved_object.name}",
        title=f"K8s 告警：{event.reason}"[:500],
        occurred_at=occurred_at,
    )
