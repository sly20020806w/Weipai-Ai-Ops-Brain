# Step 4：AI Task 模型与状态迁移

Step 5 已在任务创建与迁移事务内接入只追加审计。下文的 Step 4 测试数量保留为当时的自检记录；当前检查结果为 `349 passed, 123 skipped`，数据库验收为 `123 passed`，最后输出「数据库基础层、AI Task、Evidence Ledger 与审计验收全部通过」。当前应用库 head 为 `0003_evidence_ledger`，详情见 [Step 5 验收说明](evidence-ledger.md)。

本步骤依据 `AGENTS.md`、`SPEC.md` 和实际计划文件 `plans.md` 实施。目录未提供两份规格引用的《Weipai AI Ops Brain 最终设计方案 V1.0》原文；SPEC 已明确全部状态与闭环要求，以下逐条迁移表是这些要求的实现细化，未增加状态。若后续补充原文中的额外规则，应对照本表核验。

## 数据模型

迁移 `0002_ai_tasks` 创建两张业务表：

| 表 | 内容 |
| --- | --- |
| `ai_tasks` | UUID 主键、标题、来源、当前状态、状态版本、UTC 创建与更新时间 |
| `ai_task_status_history` | UUID 主键、任务外键、连续序号、原状态、目标状态、非空原因、调用角色、UTC 迁移时间及公共时间字段 |

来源严格为 `Alert`、`Ticket`、`Schedule`、`State`、`Prediction`、`Release`、`Human`、`AI`。来源与状态使用字符串枚举和 PostgreSQL CHECK 约束，数据库也拒绝未声明的值。所有时间列为 `timestamp with time zone`；应用层读写规范为带时区的 UTC。

新任务由 `TaskService.create()` 创建，初始状态为 `NEW`、版本为 0，并写一条 `sequence=0`、`from_status=NULL` 的创建历史。每次成功迁移递增版本并追加一条对应序号的历史；`UNIQUE(task_id, sequence)` 防止重复序号。历史按序号读取，确保时间相同时仍有稳定顺序。

## 完整迁移表

下表“异常出口”也是合法边。表内未出现的边全部拒绝，包括同状态迁移。共 17 个状态、74 条合法边和 215 个非法状态组合。

| 当前状态 | 正常、等待或恢复出口 | 异常出口 |
| --- | --- | --- |
| `NEW` | `CONTEXT_BUILDING` | `FAILED`、`ESCALATED` |
| `CONTEXT_BUILDING` | `RUNBOOK_MATCHING`、`WAITING_INFORMATION` | `FAILED`、`ESCALATED`、`AUTOMATION_ABORTED` |
| `RUNBOOK_MATCHING` | `INVESTIGATING`、`WAITING_INFORMATION` | `FAILED`、`ESCALATED`、`AUTOMATION_ABORTED` |
| `INVESTIGATING` | `RCA`、`NEED_HUMAN_JUDGMENT`、`WAITING_INFORMATION` | `FAILED`、`ESCALATED`、`AUTOMATION_ABORTED` |
| `RCA` | `PLANNING`、`INVESTIGATING`、`NEED_HUMAN_JUDGMENT`、`WAITING_INFORMATION` | `FAILED`、`ESCALATED`、`AUTOMATION_ABORTED` |
| `PLANNING` | `EXECUTING`、`WAITING_APPROVAL`、`NEED_HUMAN_JUDGMENT`、`WAITING_INFORMATION`、`INVESTIGATING` | `FAILED`、`ESCALATED`、`AUTOMATION_ABORTED` |
| `NEED_HUMAN_JUDGMENT` | `CONTEXT_BUILDING`、`RUNBOOK_MATCHING`、`INVESTIGATING`、`RCA`、`PLANNING` | `FAILED`、`ESCALATED`、`AUTOMATION_ABORTED` |
| `WAITING_INFORMATION` | `CONTEXT_BUILDING`、`RUNBOOK_MATCHING`、`INVESTIGATING`、`RCA`、`PLANNING` | `FAILED`、`ESCALATED`、`AUTOMATION_ABORTED` |
| `WAITING_APPROVAL` | `EXECUTING`、`PLANNING` | `FAILED`、`ESCALATED`、`AUTOMATION_ABORTED` |
| `EXECUTING` | `VERIFYING`、`INVESTIGATING` | `FAILED`、`ESCALATED`、`AUTOMATION_ABORTED` |
| `VERIFYING` | `RESOLVED`、`INVESTIGATING` | `FAILED`、`ESCALATED`、`AUTOMATION_ABORTED` |
| `RESOLVED` | `LEARNING` | 无 |
| `FAILED` | `LEARNING`、`ESCALATED` | 无 |
| `AUTOMATION_ABORTED` | `ESCALATED` | 无 |
| `ESCALATED` | `INVESTIGATING`、`LEARNING` | 无 |
| `LEARNING` | `CLOSED` | `FAILED`、`ESCALATED` |
| `CLOSED` | 无 | 无 |

`NEED_HUMAN_JUDGMENT` 用于业务判断，`WAITING_APPROVAL` 用于授权已有动作，两者始终独立。等待判断或信息时，可恢复到需要补充信息的原阶段；具体恢复阶段由后续 Temporal Workflow 持有和选择。

`EXECUTING` 不能直接进入 `RESOLVED`；必须进入 `VERIFYING`。`VERIFYING → RESOLVED` 要求内部调用角色为 `TransitionActor.VERIFIER`，普通 Workflow 调用会抛出 `VerificationRequired`。本步骤仅建立此内部调用契约；独立验证逻辑按 Step 31 接入，角色参数本身不承担对外身份认证。

验证失败可以回到 `INVESTIGATING`。熔断状态只可转入 `ESCALATED`；后续 Workflow 负责通知接管和控制人工恢复，不能以执行重试直接跳出熔断。成功任务经 `LEARNING` 才能进入 `CLOSED`。关键任务的 Reviewer、Policy、审批和熔断触发条件按对应后续步骤接入。

## 服务与事务

业务代码使用 `app/tasks/service.py` 的 `TaskService`。调用方必须先开启事务，并显式提交或回滚；服务自身不提交外层事务。

```python
async with database.session() as session, session.begin():
    service = TaskService(session)
    task = await service.create(
        source=TaskSource.HUMAN,
        title="本地任务模型验收",
        reason="人工提出的验收请求",
    )
    task = await service.transition(
        task.id,
        TaskStatus.CONTEXT_BUILDING,
        expected_status=task.status,
        expected_version=task.status_version,
        reason="开始构建上下文",
    )
```

迁移时对任务执行 `SELECT ... FOR UPDATE`，同时重新加载 identity map 中的状态；`expected_status` 和 `expected_version` 必须与数据库最新值一致。旧请求抛出 `TaskStateConflict`，不存在的任务抛出 `TaskNotFound`。合法状态转换也必须有非空原因。

状态更新和历史插入在同一个 SAVEPOINT 内完成，插入历史失败会回滚本次状态改动；即使调用方捕获错误后提交外层事务，也不会留下缺失历史的状态。外层事务回滚同样撤销任务和历史写入。

公开 `status` 和 `status_version` 属性只读；修改内部状态属性或通过 ORM/SQLAlchemy Core 的 Session 批量写入任务表会抛出 `TaskServiceRequired`。任务和新历史记录也必须通过服务创建。该约束属于应用代码边界；持有数据库管理凭证的原始 SQL 操作仍需遵守工程契约。

这是持久化模型与迁移服务；任务运行、等待信号、超时与重试按 Step 17 使用 Temporal，本步骤没有运行中的任务 Workflow，也没有新增任务 API、页面、外部 Connector 或 L1+ 运维操作。

## 自行验收

在 PowerShell 中执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库与状态机验收失败' }
```

`check.ps1` 应通过 ruff、格式、mypy、pytest 和 Git 环境文件检查。pytest 为 `321 passed, 90 skipped`；其中 302 项为 Step 4 单元测试，数据库测试在此入口明确跳过。前端工程按 Step 47 建立后纳入检查。

`check-db.ps1` 应显示 `90 passed`，最后输出「临时测试库已清理」和「数据库基础层与 AI Task 状态机验收全部通过」。它从本项目已有本地容器加载凭证，在新建的 `weipai_db_test_<随机 UUID>` 临时库中验证：

- 空库 upgrade → metadata check → 降到 Step 3 → upgrade → downgrade base → 再次 upgrade；任务表及历史表按版本建立或移除。
- 8 个来源的任务跨会话读回，UUID、UTC、创建历史完整。
- 74 条合法边全部实际提交，每条恰好增加一条带原因与 UTC 时间的历史。
- 非法迁移、外层回滚、模拟历史插入失败均保留原状态及历史。
- 两个同时持有旧状态的会话竞争迁移，恰好一个成功，另一个报状态版本冲突。
- ORM 属性修改与 Session 批量写入不能绕过服务。

这些测试只使用本机临时 PostgreSQL 数据库。现有 `weipai` 和 Temporal 数据库不用于测试读写。

本地容器已停止时，先启动 Docker Desktop，然后在同一个 PowerShell 窗口恢复依赖：

```powershell
.\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw '本地依赖启动失败' }
.\check-db.ps1
```

需要检查本地应用库中的实际表与版本时：

```powershell
. .\use-local-db.ps1
. .\scripts\project.ps1
$uvPath = Get-ProjectUv
& $uvPath run --frozen --directory backend alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw '迁移失败' }
& $uvPath run --frozen --directory backend alembic current
docker compose -f deploy/docker-compose.yml exec -T postgres psql -U $env:POSTGRES_USER -d weipai -c '\dt ai_*'
```

当前预期版本为 `0003_evidence_ledger (head)`，任务表清单包含 `ai_tasks` 和 `ai_task_status_history`。本地应用库执行升级即可；破坏性降级验收仅在临时测试库中进行。

并发控制及 Session 写入拦截的使用参考 [SQLAlchemy 官方 Session Events 文档](https://docs.sqlalchemy.org/en/20/orm/session_events.html)。
