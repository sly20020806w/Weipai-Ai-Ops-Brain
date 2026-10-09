# Step 54：端到端闭环验收

本步只增加全 Fake 闭环测试和验收入口，不改变设计、业务实现、数据库迁移或生产权限。
计划文件在本仓库实际名为 `plans.md`。

## 自己运行一次

先启动 Docker Desktop 的 Linux 引擎。本项目的 PostgreSQL、Temporal 容器已经建立时，
在 PowerShell 中执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
. .\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw '本机依赖启动失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

统一检查包含后端 Connector 边界、ruff、格式、mypy、pytest、Git 环境检查，
**强制运行本步 E2E**，以及前端生成一致性、lint、typecheck、test 和 build。
E2E 缺少依赖、失败或跳过均导致统一检查失败。第一次建立本机依赖请按
`deploy/README.md` 配置；不要填写生产系统地址或凭证。

只检查本步时可直接执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\check-e2e.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 54 验收失败' }
```

无需手动启动 API 或 Worker。脚本在当前进程读取已有本机容器配置，创建独立临时库，
运行迁移和隔离 Worker，最后自动清理库与仍在等待的测试 Workflow；账户仅为测试账户。

## 怎么看结果

验收包含六项网络门禁测试与三个完整链路场景，共 **9 passed、零跳过**。
每个业务场景打印实际任务 ID、Temporal Workflow ID、状态、Evidence/审计数量和执行次数。

成功场景应完整显示：

```text
NEW → CONTEXT_BUILDING → RUNBOOK_MATCHING → INVESTIGATING → RCA
→ PLANNING → WAITING_APPROVAL → EXECUTING → VERIFYING
→ RESOLVED → LEARNING → CLOSED
```

同时显示十三章复盘、一个 Draft/Manual Runbook、四个改进任务和 **Fake 回滚 1 次**。
拒绝审批场景终点为 `ESCALATED`，**签发凭证与执行均为 0**。
验证未恢复场景即使回滚已执行，也不能出现 `RESOLVED`、`LEARNING`、`CLOSED` 或复盘草稿。
三个业务场景的真实运维 HTTP 与外网尝试均为 **0**。

末尾必须显示：

```text
临时测试库已清理
Step 54 端到端闭环验收全部通过（9 项，零跳过）
```

运行 `check.ps1` 时还须看到最后的“统一检查全部通过”。
JUnit 报告留在忽略的 `.cache/e2e/`；验收器检查实际测试数以及零跳过，不会仅凭 pytest 退出码标成功。

## 测试实际覆盖什么

- 先用既有 Discovery、Change Timeline 采集 Fake 事实。发布发生在 01:25 UTC，
  本步故障采样移至 01:30–01:40 UTC；测试断言发布早于故障采样，避免错误时间窗误通过。
- 告警由 FastAPI 的验签 Webhook 经内存 ASGI HTTP 接入，启动实际 EventIngestionWorkflow
  和 AITaskWorkflow。无签名返回 401，同指纹重投只保留一个 OpsEvent/Alert 任务。
- 从 NEW 开始由统一 Workflow 完成 Runbook 检索、主 Agent 调查、RCA、四类 Reviewer 反证、
  L3 回滚规划和 Policy `need_approval`。测试不会手工修改任务状态。
- 审批前零签发/零执行；哈希篡改返回 409；登录后经审批 API、TaskControlWorkflow 恢复。
  相同审批重投返回同回执，操作人审计仅一条，实际动作不会重复。
- Fake Executor 真正把目标镜像 v2.3.7 改为 v2.3.6。独立 Verifier 从实际执行回执构建
  目标和恢复窗口，核对八项事实与引用；只有 Verifier 可以设置 RESOLVED。
- 每次状态迁移的顺序、版本、前后状态、中文原因和 UTC 时间，与数据库历史、状态审计及
  Workflow 进度逐条一致。Tool 成功审计精确关联 Evidence；成功场景全部证据经 API 原样读回。
- 十三章复盘逐条引用同任务持久化证据，Draft 和改进 OpsEvent/AI Task 均在数据库中存在，
  改进任务确实派发到 Temporal；主任务及审批控制历史可以重新回放。

## 网络边界与范围

LLM、运维 Connector、动作客户端和飞书均为 Fake；HTTP 使用 ASGITransport，
真实 HTTP transport、外部 DNS 和 socket 连接在 I/O 前拒绝。门禁保留尝试计数，
业务即使捕获异常也无法通过零尝试断言。Temporal 使用 Rust transport，因此额外限制
Client.connect 只能访问检查过的本机地址。

PostgreSQL 与 Temporal 是真实**本机测试基础设施**，允许其两个精确回环端口通信。
“真实网络请求 0”指真实运维系统、网关、飞书和外网；不把本机数据库/gRPC 通信伪称为零。
迁移子进程同样只使用由验收脚本创建的本机专用临时库。

此前的数据库/Temporal 专项仍有独立入口；本步统一检查强制增加 E2E，不把全部旧专项改成统一运行。
本步不代表生产系统已联调，不进入 Step 55。

## 2026-10-09 最终自检记录

- 从 Windows PowerShell 5.1 实际运行 `check.ps1`，退出码 0，显示“统一检查全部通过”。
- Connector 边界、ruff、格式、mypy（471 个源文件）与 Git 环境检查全部通过。
- 后端 `2172 passed / 590 skipped`；590 项为此前沿独立入口执行的数据库/Temporal 专项，
  本步未全量复跑。强制 E2E 另行实际运行，`9 passed / 0 skipped`。
- 前端 OpenAPI 与 16 个生成文件逐字节一致，lint、typecheck、`132 passed` 和生产构建通过。
- 三个 E2E 业务场景真实运维 HTTP/外网尝试均为 0；成功场景 43 条 Evidence、34 条审计、
  回滚 1 次；拒绝场景回滚 0 次；未恢复场景回滚 1 次后返回 INVESTIGATING。
- PowerShell UTF-8 BOM/语法通过，临时测试库 0，本步仍运行的隔离 Workflow 0，四个依赖 healthy。
- 自检修复了门禁安装早于 Windows event loop socketpair 初始化、原 Fake 发布/故障时间窗
  不一致，以及新增测试的类型和格式问题；仅修改测试、验收入口和说明。
