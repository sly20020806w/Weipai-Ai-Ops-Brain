"""首次 ACK 上线的只读发现模式，仍使用同一镜像和 Temporal Worker 入口。"""

from datetime import timedelta
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

from app.config import Settings
from app.connectors.models import ConnectorMode
from app.db.session import Database
from app.graph.discovery.activities import DiscoveryActivities
from app.graph.discovery.workflow import DiscoveryWorkflow

READY_PATH = Path("/tmp/weipai-worker-ready")


def validate_readonly_settings(settings: Settings) -> None:
    if settings.app_env not in {"staging", "production"}:
        raise ValueError("只读云端 Worker 仅允许 staging/production；本地测试仍使用 Fake")
    if not settings.read_only or settings.worker_mode != "readonly_discovery":
        raise ValueError("只读 Worker 要求 OPS_READ_ONLY=true 与 readonly_discovery 模式")
    if settings.connector_mode is not ConnectorMode.REAL:
        raise ValueError("只读云端 Worker 必须使用真实只读 Connector")
    # 再次校验，避免 model_copy 绕过宿主门禁。
    validated = Settings.model_validate(
        {
            field.validation_alias: getattr(settings, name)
            for name, field in Settings.model_fields.items()
        }
    )
    validated.require_database_url()
    validated.require_ops_platform_config()
    kubernetes = validated.require_kubernetes_config()
    if kubernetes.namespace_allowlist is None:
        raise ValueError("只读上线必须设置 KUBERNETES_CONFIG.namespace_allowlist")
    validated.require_arms_config()
    validated.require_git_config()
    validated.require_cloud_config()
    required = {"ops_platform", "arms", "git", "cloud"}
    if validated.kubernetes_reader_token_file is None:
        required.add("kubernetes")
    supplied = set(validated.connector_reader_tokens)
    if supplied != required:
        raise ValueError("CONNECTOR_READER_TOKENS 必须恰好包含 Discovery 的只读来源身份")
    if bool(validated.kubernetes_reader_ca_file) != bool(validated.kubernetes_reader_token_file):
        raise ValueError("投射的 Kubernetes Reader token 与 CA 必须同时配置")


def create_readonly_worker(client: Client, database: Database, settings: Settings) -> Worker:
    validate_readonly_settings(settings)
    return Worker(
        client,
        task_queue=settings.temporal_config.task_queue,
        workflows=[DiscoveryWorkflow],
        activities=[DiscoveryActivities(database, settings).refresh],
        graceful_shutdown_timeout=timedelta(seconds=10),
    )
