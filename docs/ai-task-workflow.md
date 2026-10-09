# Step 17：AI Task Temporal Workflow

本步骤依据完整读取的 `AGENTS.md`、`SPEC.md` 与实际计划文件 `plans.md` 实施。目录中没有 `plan.md`，也未提供规格所引用的 V1.0 原文；沿用已有 17 个状态、合法迁移表与 Verifier 角色约束，没有增加状态或开展 Step 18。

## 本步骤交付

- 同一后端 Python 包提供 `api` 和 `worker` 两个入口；Worker 使用官方 `temporalio` SDK，锁定版本为 1.34.0。
- `AITaskWorkflow` 只负责确定性编排。数据库 I/O、状态迁移和占位阶段工作全部在 Activity 中完成；暂停、定时器与有限重试由 Temporal 负责。
- `TaskActivityStore` 通过既有 `TaskService`，在一个事务中写入状态、历史与审计。任务 UUID 和迁移序号识别重复请求；同序号的来源状态、目标状态、原因或角色不一致时拒绝。Activity 提交成功后丢失响应，再次执行不会重复写入。
- `human_response` 信号绑定当前等待状态和状态版本；只接受首个有效回答，错误版本、错误等待类型、重复或非等待阶段信号无效。
- 三个等待状态独立：补充信息在 `CONTEXT_BUILDING` 后暂停并恢复到该阶段；人工判断在 `INVESTIGATING` 后暂停并恢复到该阶段；审批在 `PLANNING` 后暂停，通过演示信号后进入占位 `EXECUTING`。拒绝或超时均转为 `ESCALATED`，Workflow 返回转人工结果，不继续执行。
- 占位 `verifier.placeholder` 位于独立的 `verifier/`，只有它使用 Verifier 角色将 `VERIFYING` 迁移到 `RESOLVED`。普通状态 Activity 不能设置 `RESOLVED`。
- 每个任务的 Workflow ID 固定为 `ai-task-<任务 UUID>`，已经运行或已完成的 ID 都拒绝重复启动。

正常占位路径为 11 条历史（含初始创建）：

```text
NEW → CONTEXT_BUILDING → RUNBOOK_MATCHING → INVESTIGATING → RCA → PLANNING
→ EXECUTING → VERIFYING → RESOLVED → LEARNING → CLOSED
```

所有阶段逻辑仍是占位。没有调用 Agent、Runbook、外部 Connector、Tool、LLM 或生产写操作；审批信号只演示编排，不产生 Step 30 的正式审批授权记录。真实验证、审批、执行和学习分别按后续步骤接入。占位 Worker 与 Verifier 拒绝 staging/production，只允许本地/测试环境、Fake Connector/Fake LLM 和回环 Temporal 地址。

## 自动验收

本机 Docker Desktop 与项目 PostgreSQL/Temporal 容器应已运行。在 PowerShell 中执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-workflow.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 17 专项失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库回归失败' }
```

本次实际结果：

| 命令 | 预期结果 |
| --- | --- |
| `check-workflow.ps1` | `36 passed`，临时库已清理，`Step 17 Temporal Workflow 验收全部通过` |
| `check.ps1` | 导入边界、ruff、格式、mypy、Git 环境文件检查通过；`1275 passed, 178 skipped`，统一检查全部通过 |
| `check-db.ps1` | `168 passed`，临时库已清理，数据库与 Workflow Activity 验收全部通过 |

专项的 36 项包括 20 项离线契约测试、6 项 PostgreSQL Activity 测试和 10 项 Temporal 闭环测试。统一入口明确跳过需要本地依赖的测试，前端目前只有预留目录，其工程和 lint/typecheck/test 在 Step 47 接入。

Temporal 测试使用 `WorkflowEnvironment.from_client()` 连接既有回环服务，采用随机任务队列和任务 ID；真实短时定时器验收超时，不下载测试服务、不连接外网或真实运维系统。数据库测试使用脚本自动新建的 `weipai_db_test_<随机 UUID>` 临时库，结束后删除，不修改本地应用库与 Temporal 数据库结构。Temporal 的测试执行历史保留在本地 `default` 命名空间，数据库测试记录随临时库清理；它们可在 UI 中查看。

验收实际检查完整状态序列、数据库历史/审计/Temporal Activity 结果逐条一致、UTC、历史 Replay 的确定性、三个等待状态的恢复与超时、Worker 停止期间收信号并重启恢复、审批拒绝不执行、旧信号拒绝、重复 Workflow 拒绝、提交后丢响应重试只写一次，以及 Activity 重试耗尽转人工。

容器已停止时，在同一 PowerShell 窗口先执行：

```powershell
.\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw '本地依赖恢复失败' }
```

首次依赖部署参见 [deploy/README.md](../deploy/README.md)。

## 自己启动并观察

窗口 A，启动常驻本地占位 Worker：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\run-worker.ps1
```

应输出 `占位 Worker 已启动：namespace=default task_queue=weipai-ai-tasks`。保持该窗口运行。按 `Ctrl+C` 停止；等待中的工作流及其定时器仍由 Temporal 持有，重新执行命令即可恢复处理。

本地应用库应为既有的 `0004_context_graph`；本步骤没有新表或数据库迁移。如果本地库尚未升级，在另一个窗口先执行：

```powershell
.\use-local-db.ps1
. .\scripts\project.ps1
$uvPath = Get-ProjectUv
& $uvPath run --frozen --directory backend alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw '迁移失败' }
```

窗口 B，按次运行三个演示：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\demo-workflow.ps1 -Mode normal
if ($LASTEXITCODE -ne 0) { throw '正常闭环失败' }
.\demo-workflow.ps1 -Mode signal
if ($LASTEXITCODE -ne 0) { throw '信号恢复失败' }
.\demo-workflow.ps1 -Mode timeout
if ($LASTEXITCODE -ne 0) { throw '等待超时失败' }
```

| 演示 | 预期输出与最终任务状态 |
| --- | --- |
| `normal` | 完整 11 条状态历史，最终 `CLOSED` |
| `signal` | 显示 `已暂停：WAITING_APPROVAL，版本 6`，发送匹配信号，最终 `CLOSED` |
| `timeout` | 人工等待显式设为 1 秒，`WAITING_APPROVAL → ESCALATED`，历史不含 `EXECUTING` |

每次命令都创建一个新的、本地应用库中的 Human 演示任务并打印 Workflow ID；脚本跨会话读取数据库历史，核对与 Workflow 返回记录一致。这些演示任务及审计会保留，便于人工复查。Worker 未运行时不会自动完成，应先确认窗口 A 已启动；演示客户端等待结果最多 60 秒，超过后退出，Temporal 中的任务仍按自身配置运行。

打开 [本地 Temporal UI](http://127.0.0.1:8080)，选择 `default` 命名空间，在 Workflows 列表按输出的 `ai-task-...` 查找；点开 History 应能看到 Activity、Timer、Signal 和工作流完成记录。`timeout` 的 Temporal 执行状态也是 Completed，返回的业务任务状态为 `ESCALATED`：完成的是转人工编排。查看审批演示的 `WorkflowExecutionSignaled` 事件，可核对信号的等待类型、版本与回答。

本次自检已实际使用这三个命令完成演示，并从 Temporal UI 的 API 确认执行记录可查询。这里验证的是后端占位 Workflow，后续阶段功能与前端页面尚未实现。

## 环境配置与编程入口

配置与凭证仅来自进程环境，不写本地配置文件或数据库。`use-local-db.ps1` 从本项目 PostgreSQL 容器加载本机地址，`use-local-temporal.ps1` 从本项目 Temporal 容器读取实际回环端口；默认使用 `default` 命名空间与 `weipai-ai-tasks` 队列。

直接运行 `uv run --frozen --directory backend worker` 时，需要 `APP_ENV=local/test`、`DATABASE_URL`、Fake 模式和可达的本地 Temporal。`TEMPORAL_CONFIG` 可用 JSON 配置：

```json
{
  "address": "127.0.0.1:7233",
  "namespace": "default",
  "task_queue": "weipai-ai-tasks",
  "human_timeout_seconds": 3600,
  "activity_timeout_seconds": 30,
  "activity_max_attempts": 3
}
```

每个 Activity 使用 start-to-close 超时与 Temporal RetryPolicy，重试最多 1–10 次；参数校验、任务不存在、版本冲突和非法迁移为不可重试错误。重试耗尽后尝试按合法边转人工；数据库仍不可用或状态被外部改变时，Workflow 报错保留执行历史，不伪造数据库成功。

调用方先经 `TaskService.create()` 提交 NEW 任务，再用 `configured_workflow_input(task_id, settings.temporal_config, waits=[...])` 固化超时和重试参数，通过 `start_task_workflow()` 启动；`progress` query 返回当前快照及历史。发送 `HumanResponse(wait_status, wait_version, accepted)` 驱动演示等待。真实 OpsEvent 创建/触发入口按 Step 21 接入，HTTP API 与鉴权按后续计划接入。

SDK 信号、Activity 重试、测试环境与 Replay 的使用参考 [Temporal Python SDK 官方说明](https://github.com/temporalio/sdk-python/blob/main/README.md)。
