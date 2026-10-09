"""Step 31 Fake 回滚后集群事实；没有实现 Kubernetes 写操作。"""

import json

from app.connectors.kubernetes.fake import sample_snapshot
from app.connectors.kubernetes.models import KubernetesSnapshot


def verification_snapshot(*, recovered: bool = True) -> KubernetesSnapshot:
    data = sample_snapshot().model_dump(mode="json", by_alias=True)
    deployment = data["deployments"][0]
    deployment["status"]["readyReplicas"] = 3 if recovered else 2
    deployment["status"]["availableReplicas"] = 3 if recovered else 2
    for pod in data["pods"]:
        pod["spec"]["containers"][0]["image"] = "registry.example.invalid/payment:v2.3.6"
        pod["status"]["conditions"][0]["status"] = "True" if recovered else "False"
        pod["status"]["containerStatuses"][0]["ready"] = recovered
    data["events"] = []
    return KubernetesSnapshot.model_validate_json(json.dumps(data))
