"""设计第 26/29 节：17 类巡检、四类治理的固定检查目录。"""

from dataclasses import dataclass
from typing import Literal

Category = Literal["stability", "capacity", "security", "cost"]
Mode = Literal["inspection", "capacity", "governance"]
Area = Literal[
    "service",
    "kubernetes",
    "cloud",
    "database",
    "redis",
    "mq",
    "disk",
    "network",
    "monitoring",
    "alerts",
    "logs",
    "traces",
    "certificate",
    "dns",
    "capacity",
    "cost",
    "security",
]
Outcome = Literal["healthy", "abnormal", "unknown"]


@dataclass(frozen=True)
class Check:
    id: str
    area: Area
    category: Category
    label: str
    operator: Literal["true", "false", "min", "max"]
    threshold: float = 0
    governance: bool = False


CHECKS = (
    Check("service_health", "service", "stability", "服务健康", "true"),
    Check("k8s_health", "kubernetes", "stability", "K8s 健康", "true"),
    Check("cloud_health", "cloud", "stability", "云资源健康", "true"),
    Check("database_health", "database", "stability", "数据库健康", "true"),
    Check("redis_health", "redis", "stability", "Redis 健康", "true"),
    Check("mq_health", "mq", "stability", "MQ 健康", "true"),
    Check("disk_usage", "disk", "capacity", "磁盘使用率", "max", 0.85),
    Check("network_health", "network", "stability", "网络健康", "true"),
    Check("monitoring_present", "monitoring", "stability", "监控覆盖", "true"),
    Check("alerts_healthy", "alerts", "stability", "告警健康", "true"),
    Check("logs_healthy", "logs", "stability", "日志异常检查", "true"),
    Check("traces_healthy", "traces", "stability", "Trace 异常检查", "true"),
    Check("certificate_days", "certificate", "security", "证书有效期", "min", 7),
    Check("dns_healthy", "dns", "stability", "DNS 健康", "true"),
    Check("capacity_usage", "capacity", "capacity", "资源容量使用率", "max", 0.85),
    Check("cost_daily_growth", "cost", "cost", "日成本异常增长", "max", 0.20),
    Check("security_healthy", "security", "security", "安全基线", "true"),
    Check("single_point", "service", "stability", "单点风险", "false", governance=True),
    Check("pdb_present", "kubernetes", "stability", "缺少 PDB", "true", governance=True),
    Check("hpa_present", "kubernetes", "stability", "缺少 HPA", "true", governance=True),
    Check("replicas", "kubernetes", "stability", "副本不足", "min", 1, True),
    Check("runbook_present", "service", "stability", "Runbook 覆盖", "true", governance=True),
    Check("capacity_growth", "capacity", "capacity", "容量增长趋势", "max", 0.20, True),
    Check("capacity_exhaustion_days", "capacity", "capacity", "预计容量耗尽", "min", 7, True),
    Check("permissions_excessive", "security", "security", "权限过大", "false", governance=True),
    Check("credential_risk", "security", "security", "凭证风险", "false", governance=True),
    Check("public_exposure", "network", "security", "公网暴露", "false", governance=True),
    Check("security_group_risk", "network", "security", "安全组风险", "false", governance=True),
    Check("ecs_idle", "cloud", "cost", "闲置 ECS", "false", governance=True),
    Check("low_utilization", "cloud", "cost", "资源低利用率", "false", governance=True),
    Check("overprovisioned", "cloud", "cost", "资源过度配置", "false", governance=True),
    Check("temporary_unreclaimed", "cloud", "cost", "临时资源未回收", "false", governance=True),
)
BY_ID = {item.id: item for item in CHECKS}


def selected_checks(mode: Mode) -> tuple[Check, ...]:
    if mode not in {"inspection", "capacity", "governance"}:
        raise ValueError("巡检类型无效")
    return tuple(item for item in CHECKS if mode != "capacity" or item.category == "capacity")


def outcome(check: Check, value: bool | float | None, threshold: float) -> Outcome:
    if value is None:
        return "unknown"
    if check.operator in {"true", "false"}:
        if type(value) is not bool:
            return "unknown"
        return "healthy" if value == (check.operator == "true") else "abnormal"
    if type(value) is not float:
        return "unknown"
    passed = value > threshold if check.operator == "min" else value < threshold
    return "healthy" if passed else "abnormal"
