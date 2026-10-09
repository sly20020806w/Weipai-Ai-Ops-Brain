"""Executor 精确能力、三类动作、HTTP 协议和配置的禁止真实网络验收。"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx2 as httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.connectors.kubernetes.config import KubernetesConfig
from app.connectors.kubernetes.execution import (
    FakeKubernetesWriteConnector,
    HTTPKubernetesWriteConnector,
    fake_binding,
)
from app.connectors.kubernetes.fake import FakeKubernetesConnector
from app.connectors.models import ExecutorCredentials, ReaderCredentials
from app.executor.models import ExecutionCommand, TargetQuery, command_hash
from app.executor.service import make_command, validate_action
from app.policy.models import RiskLevel
from tests.test_action_plans import draft, evaluate

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("forbid_llm_network")]


async def command(name: str = "rollback_prod") -> ExecutionCommand:
    connector = FakeKubernetesWriteConnector()
    target = await connector.inspect(
        TargetQuery.model_validate(
            {
                f: getattr(fake_binding(), f)
                for f in (
                    "service_name",
                    "cluster_name",
                    "namespace",
                    "deployment_name",
                    "container_name",
                )
            }
        )
    )
    value = draft()
    parameters = {
        "rollback_prod": {"from_version": "v2.3.7", "to_version": "v2.3.6"},
        "restart_service": {"strategy": "rolling"},
        "scale_service": {"from_replicas": 3, "to_replicas": 5},
    }[name]
    value = value.model_copy(
        update={
            "actions": (
                value.actions[0].model_copy(
                    update={
                        "name": name,
                        "parameters": parameters,
                    }
                ),
            )
        }
    )
    plan = evaluate(value)
    return make_command(plan, uuid4(), plan.actions[0].action, fake_binding(), target)


@pytest.mark.parametrize("name", ["rollback_prod", "restart_service", "scale_service"])
async def test_three_actions_change_only_approved_fields_and_deduplicate(name: str) -> None:
    client = FakeKubernetesWriteConnector()
    value = await command(name)
    credential = await client.issue(value, 60)
    result = await client.execute(value, credential)
    assert result == await client.execute(value, credential)
    assert client.execution_count == 1 and result.command_hash == command_hash(value)
    assert result.target.image == value.expected_image
    assert result.target.replicas == value.expected_replicas
    assert result.target.resource_version == "8"
    assert result.completed_at.tzinfo is UTC
    assert credential.token.get_secret_value() not in repr(credential)
    assert not hasattr(FakeKubernetesConnector(), "execute")


@pytest.mark.parametrize(
    "tamper", ["service", "namespace", "image", "task", "execution_id", "replicas"]
)
async def test_credential_cannot_authorize_any_other_command(tamper: str) -> None:
    client = FakeKubernetesWriteConnector()
    value = await command()
    credential = await client.issue(value, 60)
    data = value.model_dump(mode="json")
    if tamper in {"service", "namespace", "replicas"}:
        key = {"service": "service_name", "namespace": "namespace", "replicas": "replicas"}[tamper]
        data["target"][key] = 5 if tamper == "replicas" else "other-service"
        if tamper == "replicas":
            data["expected_replicas"] = 5
    elif tamper == "image":
        data["expected_image"] = "registry.example.invalid/other:v1"
    else:
        data["task_id" if tamper == "task" else "execution_id"] = str(uuid4())
    changed = ExecutionCommand.model_validate_json(json.dumps(data))
    with pytest.raises(PermissionError):
        await client.execute(changed, credential)
    assert client.execution_count == 0


@pytest.mark.parametrize("offset", [-1, 60, 61])
async def test_credential_not_yet_valid_or_expired(offset: int) -> None:
    now = datetime(2026, 10, 7, tzinfo=UTC)
    client = FakeKubernetesWriteConnector(clock=lambda: now)
    value = await command()
    credential = await client.issue(value, 60)
    now += timedelta(seconds=offset)
    with pytest.raises(PermissionError):
        await client.execute(value, credential)
    assert client.execution_count == 0


async def test_forged_token_and_stale_resource_refused() -> None:
    client = FakeKubernetesWriteConnector()
    value = await command()
    credential = await client.issue(value, 60)
    with pytest.raises(PermissionError):
        await client.execute(value, credential.model_copy(update={"token": SecretStr("forged")}))
    client.targets["payment-service"] = value.target.model_copy(update={"resource_version": "9"})
    with pytest.raises(ValueError, match="资源"):
        await client.execute(value, credential)
    assert client.execution_count == 0


@pytest.mark.parametrize("ttl", [0, -1, 301, True])
async def test_invalid_ttl_rejected(ttl: int) -> None:
    client = FakeKubernetesWriteConnector()
    with pytest.raises(ValueError):
        await client.issue(await command(), ttl)


@pytest.mark.parametrize(
    "params",
    [
        {"from_replicas": 3, "to_replicas": 0},
        {"from_replicas": 3, "to_replicas": 101},
        {"from_replicas": 3, "to_replicas": True},
        {"from_replicas": 3, "to_replicas": 3},
        {"to_replicas": 5},
    ],
)
async def test_scale_minimal_permission_bounds(params: dict[str, object]) -> None:
    action = draft().actions[0].model_copy(update={"name": "scale_service", "parameters": params})
    with pytest.raises(ValueError):
        validate_action(action, fake_binding())


async def test_unknown_action_risk_floor_and_binding_mismatch() -> None:
    action = draft().actions[0]
    with pytest.raises(ValueError):
        validate_action(action.model_copy(update={"name": "delete_database"}), fake_binding())
    with pytest.raises(ValueError):
        validate_action(action.model_copy(update={"risk_level": RiskLevel.L0}), fake_binding())
    value = await command()
    plan = evaluate(draft())
    with pytest.raises(ValueError):
        make_command(
            plan,
            uuid4(),
            plan.actions[0].action,
            fake_binding(),
            value.target.model_copy(update={"namespace": "other"}),
        )


async def test_execution_config_environment_and_disabled_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not Settings(APP_ENV="test").execution_config.enabled
    monkeypatch.setenv("EXECUTION_CONFIG", '{"enabled":true,"credential_ttl_seconds":30}')
    assert Settings(APP_ENV="test").execution_config.credential_ttl_seconds == 30


async def test_credentials_and_receipts_always_normalize_utc() -> None:
    from datetime import timezone

    from app.connectors.kubernetes.execution import ActionCredential, ExecutionReceipt

    value = await command()
    local_time = datetime(2026, 10, 7, 9, tzinfo=timezone(timedelta(hours=8)))
    credential = ActionCredential(
        token=SecretStr("test-token"),
        execution_id=value.execution_id,
        command_hash=command_hash(value),
        issued_at=local_time,
        expires_at=local_time + timedelta(seconds=60),
    )
    receipt = ExecutionReceipt(
        execution_id=value.execution_id,
        command_hash=command_hash(value),
        completed_at=local_time,
        target=value.target,
    )
    assert credential.issued_at.tzinfo is credential.expires_at.tzinfo is UTC
    assert receipt.completed_at.tzinfo is UTC


async def test_http_protocol_exact_scope_identity_and_no_token_leak() -> None:
    backend = FakeKubernetesWriteConnector()
    requests: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        token = request.headers["authorization"].removeprefix("Bearer ")
        requests.append((request.url.path, token))
        if request.url.path == "/execution/inspect":
            assert token == "reader-token"
            target_result = await backend.inspect(TargetQuery.model_validate_json(json.dumps(data)))
            return httpx.Response(200, json=target_result.model_dump(mode="json"))
        elif request.url.path == "/execution/credentials":
            assert token == "issuer-token"
            credential_result = await backend.issue(
                ExecutionCommand.model_validate_json(json.dumps(data["command"])),
                data["ttl_seconds"],
            )
            response = credential_result.model_dump(mode="json")
            response["token"] = credential_result.token.get_secret_value()
            return httpx.Response(200, json=response)
        else:
            result = await backend.execute(
                ExecutionCommand.model_validate_json(json.dumps(data)), backend.credentials[token]
            )
        return httpx.Response(200, json=result.model_dump(mode="json"))

    reader = ReaderCredentials(connector="kubernetes", token=SecretStr("reader-token"))
    executor = ExecutorCredentials(connector="kubernetes", token=SecretStr("issuer-token"))
    config = KubernetesConfig(cluster_name="ack-fake", base_url="https://actions.example.invalid")
    async with HTTPKubernetesWriteConnector(
        config, reader, executor, transport=httpx.MockTransport(handler)
    ) as client:
        value = await command()
        query = TargetQuery.model_validate(
            {f: getattr(value.target, f) for f in TargetQuery.model_fields}
        )
        assert await client.inspect(query) == value.target
        credential = await client.issue(value, 60)
        assert (await client.execute(value, credential)).target.image.endswith(":v2.3.6")
    assert len(requests) == 3 and backend.execution_count == 1
    assert requests[-1][1] not in {"reader-token", "issuer-token"}
    with pytest.raises(ValueError, match="真实"):
        HTTPKubernetesWriteConnector(config, reader, executor)
    with pytest.raises(ValueError, match="复用"):
        HTTPKubernetesWriteConnector(
            config,
            reader,
            ExecutorCredentials(connector="kubernetes", token=reader.token),
            transport=httpx.MockTransport(handler),
        )
    with pytest.raises(TypeError):
        HTTPKubernetesWriteConnector(config, reader, reader, transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
