# Step 5：Evidence Ledger 与审计日志

依据 `AGENTS.md`、`SPEC.md` 和实际计划文件 `plans.md`，本步骤实现证据与审计的持久化和追加保护。目录中未提供规格引用的原始设计全文，实施范围为当前 SPEC 和计划明确的要求。

## 数据与查询

迁移 `0003_evidence_ledger` 在 Step 4 基础上创建两张表：

| 表 | 内容 |
| --- | --- |
| `evidence_ledger` | UUID 证据 ID、任务外键、来源 Tool、JSONB 参数、结果快照或源系统引用、UTC 采集时间及公共时间字段 |
| `audit_log` | UUID 审计 ID、任务外键、事件类型、操作人、操作名称、结果、JSONB 详情、可选证据 ID、UTC 发生时间及公共时间字段 |

证据至少包含一个结果快照或一个非空源系统引用。空对象、空数组、`false`、`0` 和空字符串也是有效快照；Python `None` 表示不存快照，此时必须提供引用。参数与审计详情必须为 JSON 对象；快照可为 JSON 对象、数组或标量。拒绝 NaN、Infinity、非字符串对象键及非 JSON 数据；保存前复制嵌套值，调用方修改原入参不会改变记录。只存必要证据快照或引用，原始指标、日志、Trace 留在源系统，凭证与密钥不得传入这些字段。

`LedgerService.get_evidence(id)` 精确引用证据，不存在时抛出 `EvidenceNotFound`。`evidence_for_task(task_id)` 按 `collected_at, id` 升序返回，`audits_for_task(task_id)` 按 `occurred_at, id` 升序返回；时间相同时按 UUID 稳定排序，只返回指定任务的数据。审计引用证据时，复合外键强制证据属于同一任务。

审计事件类型固定为 `tool_call`、`state_transition`、`approval`、`execution`。本步骤建立四类记录的存储与追加入口，Tool Dispatcher、审批业务和 Executor 在对应后续步骤接入。

## 事务与只追加保护

写入须先开启事务；服务会 flush，调用方负责提交或回滚。追加证据和相关审计应共用同一外层事务。

任务创建和每次成功状态迁移已接入审计：分别使用 `task.create`、`task.transition`，类型为 `state_transition`，记录原状态、目标状态、版本、原因和原有调用角色，发生时间与状态历史相同。状态、历史和审计在任务服务已有的 SAVEPOINT 内一起写入。任一写入失败都回滚本次操作，即使调用方捕获异常后提交外层事务，也不会留下缺失审计的任务或状态。非法迁移不会追加成功审计；升级前已有的历史不做事实回填。

ORM 更新与删除、Session 的 ORM/Core 批量更新与删除抛出 `AppendOnlyViolation`。Alembic 同时建立 PostgreSQL 语句级触发器：原始 SQL、引擎连接执行的 `UPDATE`、`DELETE` 和 `TRUNCATE`（含 CASCADE）均报 `append-only ledger` 错误。修正记录须追加新记录并引用原 ID，不能覆盖旧证据或审计。

数据库保护通过迁移安装，应用不使用 `create_all()` 代替迁移。触发器不会限制数据库管理员修改 schema 或禁用触发器；生产角色与权限按 Step 55 落实。所有时间列为 `timestamp with time zone`，应用读写规范为带时区的 UTC，无时区时间被拒绝。

## 服务调用示例

以下演示存储 Fake 查询结果，不执行 Tool 或访问外部运维系统：

```python
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.tasks.service import TaskService
from app.tasks.states import TaskSource

async with database.session() as session, session.begin():
    task = await TaskService(session).create(
        source=TaskSource.HUMAN, title="本地证据验收", reason="Fake 数据检查"
    )
    ledger = LedgerService(session)
    evidence = await ledger.append_evidence(
        task_id=task.id,
        source_tool="query_metrics",
        parameters={"service": "payment-service"},
        result_snapshot={"error_rate": 0.05},
        source_reference="fake://metrics/payment-service",
    )
    await ledger.append_audit(
        task_id=task.id,
        event_type=AuditEventType.TOOL_CALL,
        actor="fake-agent",
        operation="query_metrics",
        outcome="succeeded",
        details={"risk_level": "L0"},
        evidence_id=evidence.id,
    )

async with database.session() as session:
    ledger = LedgerService(session)
    exact = await ledger.get_evidence(evidence.id)
    ordered = await ledger.evidence_for_task(task.id)
    audits = await ledger.audits_for_task(task.id)
```

## 自行验收

本机 Docker Desktop 和项目依赖容器运行时，在 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '证据与审计数据库验收失败' }
```

预期：

- `check.ps1`：ruff、格式、mypy、pytest 和 Git 环境文件检查全部通过；pytest 为 `349 passed, 123 skipped`，最后输出「统一检查全部通过」。数据库测试在此入口明确跳过。
- `check-db.ps1`：`123 passed`，随后输出「临时测试库已清理」和「数据库基础层、AI Task、Evidence Ledger 与审计验收全部通过」。其中 33 项为本步骤新增 PostgreSQL 集成测试。
- 集成验收实际执行空库升降级、metadata 检查、跨会话证据读取、任务过滤、UTC/时间排序、四类审计、跨任务证据引用拒绝、ORM/批量/原始 SQL 修改删除拒绝、TRUNCATE 拒绝、证据与审计外层回滚、任务创建/迁移的审计失败回滚。

测试仅在新建的随机 `weipai_db_test_<UUID>` 本地临时库内读写，完成或失败后尝试清理；现有 `weipai` 和 Temporal 库不参与测试，不连接真实运维系统。前端按 Step 47 建立工程后再检查。

依赖停止时，先启动 Docker Desktop，再执行：

```powershell
.\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw '本地依赖启动失败' }
.\check-db.ps1
```

检查本地应用库的表与版本：

```powershell
. .\use-local-db.ps1
. .\scripts\project.ps1
$uvPath = Get-ProjectUv
& $uvPath run --frozen --directory backend alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw '迁移失败' }
& $uvPath run --frozen --directory backend alembic current
docker compose -f deploy/docker-compose.yml exec -T postgres psql -U $env:POSTGRES_USER -d weipai -c '\dt'
```

预期为 `0003_evidence_ledger (head)`，表清单包含 `ai_tasks`、`ai_task_status_history`、`evidence_ledger`、`audit_log` 和 `alembic_version`。应用库只升级，破坏性降级验收由脚本在临时库执行。

当前交付为后端数据库与业务服务，证据 API 和页面按 Step 44、48 实现，本步骤通过以上脚本确认可用。

## 自检记录（2026-10-06）

统一检查通过，mypy 检查 44 个源文件，pytest 为 349 passed、123 skipped。数据库验收全部 123 项通过，临时库已清理。初次检查发现的格式与迁移列泛型标注问题已修复。本地应用库升级至 `0003_evidence_ledger`，两张新表及两个只追加触发器已检查。

实现参考 [PostgreSQL 触发器官方文档](https://www.postgresql.org/docs/current/plpgsql-trigger.html) 和 [SQLAlchemy Session Events 官方文档](https://docs.sqlalchemy.org/en/20/orm/session_events.html)。
