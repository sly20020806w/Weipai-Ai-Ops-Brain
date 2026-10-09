"""有界的标准 Kubernetes LIST/Watch；连接与源协议只留在 Connector。"""

from dataclasses import dataclass

import httpx2 as httpx
from pydantic import ValidationError

from app.connectors.kubernetes.models import Event, ResourceList


@dataclass(frozen=True)
class EventWatchBatch:
    events: tuple[Event, ...]
    resource_version: str


class WatchResponseError(RuntimeError):
    pass


async def read_event_watch(
    http: httpx.AsyncClient,
    namespace: str,
    resource_version: str,
    timeout_seconds: int,
    *,
    page_size: int,
    max_pages: int,
) -> EventWatchBatch:
    path = f"api/v1/namespaces/{namespace}/events"
    events: list[Event] = []
    cursor = resource_version
    try:
        if not cursor:
            query: dict[str, str | int] = {"limit": page_size}
            seen_tokens: set[str] = set()
            seen_ids: set[str] = set()
            for _ in range(max_pages):
                response = await http.get(path, params=query)
                response.raise_for_status()
                page = ResourceList[Event].model_validate_json(response.content)
                if (
                    page.kind != "EventList"
                    or page.api_version != "v1"
                    or not page.metadata.resource_version
                ):
                    raise WatchResponseError("K8s LIST 缺少有效快照版本")
                if cursor and cursor != page.metadata.resource_version:
                    raise WatchResponseError("K8s LIST 分页版本不一致")
                cursor = page.metadata.resource_version
                for event in page.items:
                    if event.metadata.namespace != namespace or event.metadata.uid in seen_ids:
                        raise WatchResponseError("K8s LIST 范围错误或重复对象")
                    seen_ids.add(event.metadata.uid)
                    events.append(event)
                token = page.metadata.continue_token
                if not token:
                    return EventWatchBatch(tuple(events), cursor)
                if token in seen_tokens:
                    raise WatchResponseError("K8s LIST 重复分页游标")
                seen_tokens.add(token)
                query["continue"] = token
            raise WatchResponseError("K8s LIST 超过分页上限")
        query = {
            "watch": "true",
            "resourceVersion": cursor,
            "allowWatchBookmarks": "true",
            "timeoutSeconds": timeout_seconds,
        }
        async with http.stream("GET", path, params=query, timeout=timeout_seconds + 5) as response:
            if response.status_code == 410:
                return EventWatchBatch((), "")
            response.raise_for_status()
            import json

            count = 0
            async for line in response.aiter_lines():
                if not line:
                    continue
                if len(line) > 1048576:
                    raise WatchResponseError("K8s Watch 单条事件过大")
                message = json.loads(line)
                kind, value = message["type"], message["object"]
                if kind == "ERROR":
                    if value.get("code") == 410:
                        return EventWatchBatch(tuple(events), "")
                    raise WatchResponseError("K8s Watch 返回错误事件")
                version = value["metadata"]["resourceVersion"]
                if (
                    not isinstance(version, str)
                    or not version
                    or len(version) > 256
                    or any(c.isspace() for c in version)
                ):
                    raise WatchResponseError("K8s Watch 版本无效")
                if kind in {"ADDED", "MODIFIED", "DELETED"}:
                    event = Event.model_validate_json(json.dumps(value))
                    if event.metadata.namespace != namespace:
                        raise WatchResponseError("K8s Watch 返回其他命名空间对象")
                    if kind != "DELETED":
                        events.append(event)
                elif kind != "BOOKMARK":
                    raise WatchResponseError("K8s Watch 类型无效")
                cursor = version
                count += 1
                if count >= 1000:
                    break
        return EventWatchBatch(tuple(events), cursor)
    except (httpx.HTTPError, ValidationError, ValueError, KeyError, TypeError):
        raise WatchResponseError("K8s Watch 读取失败") from None
