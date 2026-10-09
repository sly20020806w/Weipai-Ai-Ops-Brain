"""标准 apps/v1 Deployment 和 core/v1 Pod/Event 只读 API；不自建重试或 Watch。"""

import ssl
from abc import abstractmethod

import httpx2 as httpx
from pydantic import TypeAdapter, ValidationError

from app.connectors.base import ReadOnlyConnector
from app.connectors.kubernetes.config import KubernetesConfig
from app.connectors.kubernetes.models import (
    Deployment,
    Event,
    Namespace,
    NamespaceRecord,
    Pod,
    ResourceList,
    ServiceName,
)
from app.connectors.kubernetes.watch import EventWatchBatch, read_event_watch
from app.connectors.models import ReaderCredentials

_namespace = TypeAdapter(Namespace)
_service = TypeAdapter(ServiceName)


def checked_query(namespace: str, service_name: str | None) -> None:
    _namespace.validate_python(namespace, strict=True)
    if service_name is not None:
        _service.validate_python(service_name, strict=True)


class KubernetesError(RuntimeError):
    """异常只输出固定消息，不带 token、URL 或源系统正文。"""


class KubernetesResponseError(KubernetesError):
    pass


class KubernetesTimeout(KubernetesError):
    pass


class KubernetesTransportError(KubernetesError):
    pass


class KubernetesHTTPError(KubernetesError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"Kubernetes 返回 HTTP {status_code}")


def service_events(
    events: tuple[Event, ...], deployments: tuple[Deployment, ...], pods: tuple[Pod, ...]
) -> tuple[Event, ...]:
    objects: tuple[Deployment | Pod, ...] = (*deployments, *pods)
    targets = {
        (item.metadata.uid, item.kind, item.metadata.namespace, item.metadata.name)
        for item in objects
    }
    # Event 通常没有服务标签；必须用 UID，不能把同名旧 Pod 的 Event 归到新 Pod。
    return tuple(
        event
        for event in events
        if (
            event.involved_object.uid,
            event.involved_object.kind,
            event.involved_object.namespace,
            event.involved_object.name,
        )
        in targets
    )


class KubernetesConnector(ReadOnlyConnector):
    async def watch_events(
        self, namespace: str, *, resource_version: str = "", timeout_seconds: int = 20
    ) -> EventWatchBatch:
        raise NotImplementedError("该 Connector 未实现 Event Watch")

    @abstractmethod
    async def list_namespaces(self) -> tuple[str, ...]: ...

    @property
    @abstractmethod
    def cluster_name(self) -> str: ...

    @abstractmethod
    async def list_deployments(
        self, namespace: str, *, service_name: str | None = None
    ) -> tuple[Deployment, ...]: ...

    @abstractmethod
    async def list_pods(
        self, namespace: str, *, service_name: str | None = None
    ) -> tuple[Pod, ...]: ...

    @abstractmethod
    async def list_events(
        self, namespace: str, *, service_name: str | None = None
    ) -> tuple[Event, ...]: ...


class HTTPKubernetesConnector(KubernetesConnector):
    async def watch_events(
        self, namespace: str, *, resource_version: str = "", timeout_seconds: int = 20
    ) -> EventWatchBatch:
        checked_query(namespace, None)
        self._check_namespace(namespace)
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
            raise ValueError("Watch 超时必须为 1–60 秒")
        if len(resource_version) > 256 or any(c.isspace() for c in resource_version):
            raise ValueError("Watch resourceVersion 无效")
        return await read_event_watch(
            self._http,
            namespace,
            resource_version,
            timeout_seconds,
            page_size=self._config.page_size,
            max_pages=self._config.max_pages,
        )

    def __init__(
        self,
        config: KubernetesConfig,
        credentials: ReaderCredentials,
        *,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        super().__init__(credentials)
        assert self.reader_credentials is not None
        if self.reader_credentials.connector != "kubernetes":
            raise ValueError("Kubernetes 只接受 kubernetes 的 Reader 凭证")
        token = self.reader_credentials.token.get_secret_value()
        if any(char.isspace() or ord(char) < 32 for char in token):
            raise ValueError("Kubernetes Reader token 不能包含空白或控制字符")
        self._config = KubernetesConfig.model_validate(config)
        if transport is not None and not isinstance(transport, httpx.MockTransport):
            raise TypeError("测试 transport 只接受 MockTransport")
        context = ssl.create_default_context()
        if self._config.ca_cert_pem is not None:
            try:
                context.load_verify_locations(cadata=self._config.ca_cert_pem)
            except (ssl.SSLError, ValueError):
                raise ValueError("KUBERNETES_CONFIG.ca_cert_pem 必须是有效的 PEM CA 证书") from None
        self._http = httpx.AsyncClient(
            base_url=self._config.base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=self._config.timeout_seconds,
            verify=context,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    @property
    def cluster_name(self) -> str:
        return self._config.cluster_name

    async def aclose(self) -> None:
        await self._http.aclose()

    def _check_namespace(self, namespace: str) -> None:
        allowed = self._config.namespace_allowlist
        if allowed is not None and namespace not in allowed:
            raise KubernetesError("命名空间不在 Reader 授权白名单内")

    async def _read[Record: Deployment | Pod | Event | NamespaceRecord](
        self,
        namespace: str | None,
        service_name: str | None,
        page_type: type[ResourceList[Record]],
        *,
        path: str,
        api_version: str,
        kind: str,
    ) -> tuple[Record, ...]:
        if self._http.is_closed:
            raise KubernetesError("Kubernetes Connector 已关闭")
        if namespace is not None:
            checked_query(namespace, service_name)
            self._check_namespace(namespace)
        query = {"limit": str(self._config.page_size)}
        if service_name is not None:
            query["labelSelector"] = f"{self._config.service_label_key}={service_name}"
        items: list[Record] = []
        seen_uids: set[str] = set()
        seen_names: set[str] = set()
        seen_cursors: set[str] = set()
        resource_version: str | None = None
        for page_index in range(self._config.max_pages):
            try:
                response = await self._http.get(path, params=query)
            except httpx.TimeoutException:
                raise KubernetesTimeout("Kubernetes 读取超时") from None
            except httpx.RequestError:
                raise KubernetesTransportError("Kubernetes 连接失败") from None
            if response.status_code != 200:
                raise KubernetesHTTPError(response.status_code)
            try:
                page = page_type.model_validate_json(response.content)
            except (ValidationError, ValueError):
                raise KubernetesResponseError("Kubernetes 响应不符合只读 API 协议") from None
            if page.api_version != api_version or page.kind != kind + "List":
                raise KubernetesResponseError("Kubernetes 返回的资源列表类型不符")
            if page_index == 0:
                resource_version = page.metadata.resource_version
            elif page.metadata.resource_version != resource_version:
                raise KubernetesResponseError("Kubernetes 分页资源版本不一致")
            for item in page.items:
                meta = item.metadata
                if (namespace is not None and getattr(meta, "namespace", None) != namespace) or (
                    service_name is not None
                    and getattr(meta, "labels", {}).get(self._config.service_label_key)
                    != service_name
                ):
                    raise KubernetesResponseError("Kubernetes 响应与命名空间/服务筛选条件不符")
                if meta.uid in seen_uids or meta.name in seen_names:
                    raise KubernetesResponseError("Kubernetes 分页出现重复对象")
                seen_uids.add(meta.uid)
                seen_names.add(meta.name)
                items.append(item)
            cursor = page.metadata.continue_token
            if not cursor:
                return tuple(items)
            if cursor in seen_cursors:
                raise KubernetesResponseError("Kubernetes 分页游标重复")
            seen_cursors.add(cursor)
            query["continue"] = cursor
        raise KubernetesResponseError("Kubernetes 读取超出分页上限，结果不完整")

    async def list_namespaces(self) -> tuple[str, ...]:
        records = await self._read(
            None,
            None,
            ResourceList[NamespaceRecord],
            path="api/v1/namespaces",
            api_version="v1",
            kind="Namespace",
        )
        names = tuple(record.metadata.name for record in records)
        allowed = self._config.namespace_allowlist
        if allowed is not None:
            if set(allowed) - set(names):
                raise KubernetesResponseError("Reader 配置的命名空间不存在")
            return tuple(name for name in names if name in allowed)
        return names

    async def list_deployments(
        self, namespace: str, *, service_name: str | None = None
    ) -> tuple[Deployment, ...]:
        checked_query(namespace, service_name)
        return await self._read(
            namespace,
            service_name,
            ResourceList[Deployment],
            path=f"apis/apps/v1/namespaces/{namespace}/deployments",
            api_version="apps/v1",
            kind="Deployment",
        )

    async def list_pods(
        self, namespace: str, *, service_name: str | None = None
    ) -> tuple[Pod, ...]:
        checked_query(namespace, service_name)
        return await self._read(
            namespace,
            service_name,
            ResourceList[Pod],
            path=f"api/v1/namespaces/{namespace}/pods",
            api_version="v1",
            kind="Pod",
        )

    async def list_events(
        self, namespace: str, *, service_name: str | None = None
    ) -> tuple[Event, ...]:
        checked_query(namespace, service_name)
        deployments = (
            await self.list_deployments(namespace, service_name=service_name)
            if service_name is not None
            else ()
        )
        pods = (
            await self.list_pods(namespace, service_name=service_name)
            if service_name is not None
            else ()
        )
        events = await self._read(
            namespace,
            None,
            ResourceList[Event],
            path=f"api/v1/namespaces/{namespace}/events",
            api_version="v1",
            kind="Event",
        )
        return service_events(events, deployments, pods) if service_name is not None else events
