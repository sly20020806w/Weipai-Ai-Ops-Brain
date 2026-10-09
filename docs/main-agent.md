# Step 24：Codex Main Agent

本次依据 `AGENTS.md`、`SPEC.md` 和实际计划文件 `plans.md`，只完成首个未完成项 Step 24。两份规格引用的完整 V1.0 原方案仍未在目录中提供，沿用项目已记录的 SPEC 与既有状态约束，不增添新的任务状态。没有进入 Step 25 及以后的 Runbook、专家、Reviewer、Action Plan、审批、Executor 或真实 Verifier。

## 自己运行一遍

本机项目的 PostgreSQL、Temporal、Temporal UI 已在运行。关闭过 Docker 的话，先启动 Docker Desktop，再按 [本地依赖说明](../deploy/README.md) 启动原有 Compose 服务。无需启动 API、手动运行 Worker 或填写生产凭证。

在 PowerShell 进入项目目录，按顺序执行；每条都检查退出码：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'

.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }

.\check-agent.ps1
if ($LASTEXITCODE -ne 0) { throw '主 Agent 专项失败' }

.\demo-agent.ps1
if ($LASTEXITCODE -ne 0) { throw '主 Agent 演示失败' }
```

通过标准：

1. `check.ps1`：导入边界、ruff、格式、mypy、pytest 与 Git 环境文件检查通过，最后显示「统一检查全部通过」。数据库/Temporal 项在这里会跳过，由专项入口执行。
2. `check-agent.ps1`：输出 `34 passed` 和「Step 24 主 Agent 验收全部通过」，随后临时测试库清理成功。
3. `demo-agent.ps1`：正常支付 5xx 场景依次调用 `get_service_context`、`get_recent_changes`、`query_metrics`、`query_logs`；打印四个本次真实生成的 Evidence ID 和一个结构化结论 Evidence ID，并逐条从数据库读回验证。状态经过 `INVESTIGATING → RCA → WAITING_INFORMATION`。伪造 Evidence ID 的场景被拒并进入 `ESCALATED`；最大步数为 3 的场景只完成一次查询，然后进入 `ESCALATED`。最后显示「Step 24 主 Agent 演示全部通过」。

UUID 每次随机生成，不应与文档比较具体值。演示结束自动终止仍在等待的隔离 Workflow，删除临时库；不会往应用库添加演示任务、恢复默认 Schedule 或修改生产系统。Temporal 历史仍保留便于观察，历史中引用的临时数据库记录已清理。

可以从脚本输出复制 Workflow ID，在 [本机 Temporal UI](http://localhost:8080) 搜索；如果本机 UI 使用了自定义端口，以 `docker compose ps` 的绑定为准。正常场景历史应包含 `agent.investigate`、`agent.validate_conclusion` 与状态迁移 Activity；演示清理后显示 Terminated 是预期结果。两个拒绝场景以 ESCALATED 状态完成，没有 EXECUTING 或 RESOLVED。

## 实现边界

`app/agent/investigation.py` 自研调查循环。每轮 LLM 根据当前对话和高级 Tool schema 做计划，Tool Call 交给注入的宿主，观察包含调用状态、Policy 结果、Evidence ID 和结果快照，再进入下一轮。没有 LangChain/LangGraph，也没有新队列或调度器。

`app/tools/runtime.py` 组合现有图查询、变更查询和可观测性 Tool；Connector 创建与关闭在这个组合根完成。Agent 不调用 Connector 或 Tool handler，只调用 `ToolDispatcher.dispatch()`。风险、环境和执行身份来自宿主，模型传入的 `approved`/风险字段不能赋予权限。

步数同时计算 LLM 调用和每个 Tool Call。默认最大 20 步，上限配置范围 1–100；支付脚本使用 5 个 LLM 轮次与 4 个 Tool Call，共 9 步。单轮多个 Tool Call 也逐一计数，达到预算后立即停止，不执行余下调用。预算在重试时依照同一份持久化对话重建。

最终输出为严格 JSON：

- `root_cause`：根因或待验证线索，含非空 `statement` 和非空 `evidence_ids`。
- `findings`：逐条发现，每条独立引用 Evidence ID。
- `confidence`：有限数值，范围 0–1。
- `uncertainties`：尚未证实的假设或限制。

引用首先必须属于本次成功观察；RCA 阶段再核对数据库中的观察记录、同任务归属、真实证据及成功 Tool 调用审计。不存在的 ID、其他任务的 ID、模型响应检查点 ID、失败/拒绝的调用、伪造的观察清单均不能成为结论依据。缺少引用、协议截断、重复 Tool Call ID 和模型 refusal 也会拒绝。引用校验说明证据来源有效，语义与因果反证仍属于后续 Reviewer。

## Temporal 与重试

`AITaskWorkflow` 在 INVESTIGATING 调用 `agent.investigate`，调查成功后经唯一 tasks 服务进入 RCA，再调用 `agent.validate_conclusion`。有效结论写入只追加 Ledger，Workflow query 可读 `conclusion_json` 和 `conclusion_evidence_id`。随后进入 WAITING_INFORMATION，等待后续业务阶段；等待超时或收到当前版本人工信号时转交人工，不推进到占位执行/验证闭环。

非法结论、步数超限是不可重试业务错误，直接进入 ESCALATED。其他 Activity 错误仅由 Temporal 按现有配置有限重试，耗尽后转交人工。状态、历史与状态审计仍由 tasks 服务原子写入。

Ledger 保存 `agent.think` 响应、`agent.observe` 观察检查点和 `agent.conclusion` 已接受结论。检查点以任务、调查状态版本、步序和规范化请求哈希定位；持有任务行锁写入，避免并发重复。已提交 LLM 响应、Tool 证据与审计、最终结论在丢响应后的重试中复用。Tool 的 Evidence、成功审计与观察检查点在同一事务提交；观察写入失败时全部回滚。JSON 采用稳定键顺序，避免 PostgreSQL JSONB 重排造成检查点冲突。未提交的查询可能在重试时重新执行，因此本阶段只注册只读 Tool。

没有新增数据库迁移，head 保持 `0009_state_prediction`。本阶段没有依赖或前端工程变更；frontend 仍按 Step 47 预留，当前无前端 lint/typecheck/test 入口。

## 环境配置与 Fake

```powershell
$env:AGENT_CONFIG = '{"enabled":true,"max_steps":20}'
```

`enabled` 默认为 false，保留早期步骤的本地占位验收模式。设为 true 后，新 OpsEvent 会把服务、标题和事件时间窗传给统一 AI Task Workflow，直接运行主 Agent；已启动的历史 Workflow 输入不变。事件时间窗为发生前一小时至发生时刻（包含该时刻）。事件、定时、状态与预测都复用该入口，不新建另一条引擎。无须手工拼装任务上下文。

人工演示通过独立 Workflow 输入启用调查分支，配置只在脚本进程内有效。Worker 继续只允许 local/test + Fake Connector + Fake LLM，生产执行能力尚未开放。

K8s Warning 若尚未关联可查询的服务名，会保留 CONTEXT_BUILDING 后的持久化 WAITING_INFORMATION，等待补全服务上下文；不会因调查入参校验失败而卡在 NEW。

Fake LLM 采用有限脚本，动态引用本次 Dispatcher 返回的 UUID；默认脚本只演示 payment-service，有事实缺失或查询失败时拒绝结论。真实 LLM 的宿主使用已有公司 AI 网关客户端，base_url、模型名和凭证均由已有环境配置提供，本次未连接公司网关或真实运维系统。

## 自检记录

2026-10-06：34 项专项（23 项禁止真实 HTTP/DNS/socket 的离线测试、6 项隔离 PostgreSQL 测试、5 项本机 Temporal 测试）通过。覆盖支付四查询、真实证据/审计、伪造与跨任务引用、严格输出、Policy 拒绝、风险/审批字段绕过、预算、多 Tool Call、事务回滚、并发去重、调查与结论提交后丢响应、未关联服务等待、Worker 重启和历史回放。

统一检查：ruff、格式、mypy（226 个源文件）、导入边界和 Git 环境文件检查通过，`1462 passed, 238 skipped`。数据库回归 212 项、既有 Workflow 36 项和事件接入 40 项通过；依赖测试分别走隔离入口，全部临时库已清理。三场景人工演示实际运行通过，RCA 结论逐条读库核对；两个新增 PowerShell 入口通过语法检查。

实际发现并修复了 JSONB 键顺序引发的并发检查点冲突、未关联服务派发边界、事件窗口与 Fake 指标采样的对齐，以及静态类型/格式问题。没有连接生产系统或发送真实通知。
