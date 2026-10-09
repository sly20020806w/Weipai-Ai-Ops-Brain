# Step 46：AI Chat API

本步按 AGENTS.md、SPEC.md、实际计划文件 plans.md 及权威原设计实施。
新增主 Agent 对话 SSE 与回答查询；本步不建立前端工程。

## 自己跑一遍

先启动 Docker Desktop。根目录 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
. .\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw '本机依赖启动失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\check-chat.ps1
if ($LASTEXITCODE -ne 0) { throw '聊天专项失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-chat.ps1 -Interactive
if ($LASTEXITCODE -ne 0) { throw 'SSE 演示失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

演示自动准备临时库、API、本人临时账户与隔离 Worker。可以输入一个支付场景问题，
也可以按回车使用默认问题。演示使用有限 Fake 脚本，问题可以自填，事实与回答范围
仍为 payment-service 的固定支付故障样例；它不表示真实网关的开放语义能力已经联调。

应逐段看到带 `[Evidence:UUID]` 的回答，以及以下结果：

1. 首轮问答为 Human 任务，经过 Runbook、主 Agent、Reviewer 和独立回答核验后 CLOSED。
2. 原请求重发时，Task ID 与回答引用保持一致，新增任务数 0。
3. 追问使用同一服务的上一轮任务作背景，本轮重新查询，引用不同的真实 Evidence ID。
4. “请回滚”动作请求生成 L3 计划，Policy 为 need_approval，任务 WAITING_APPROVAL。
5. 审批前实际运维动作数为 0；脚本不批准这个回滚。
6. 最后显示“Step 46 AI Chat API Fake 演示全部通过”和“临时测试库已清理”。

省略 `-Interactive` 使用固定问题自动演示。无需提供公司凭证、手动启动 API 或 Worker。
每个 Evidence ID 都由脚本通过 `/api/evidence/{id}` 读回并核对任务归属。

完整数据库/Temporal 回归：

```powershell
. .\use-local-temporal.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库与 Temporal 回归失败' }
```

## 接口与输入

`POST /api/chat` 需要有效会话 Cookie、`X-CSRF-Token`，沿用已有来源校验。
请求模型会出现在 `/openapi.json`；不能传 actor、approved、execution_enabled 或风险等级。

```json
{
  "request_id": "f851b4fc-d465-4abc-a036-9a42b43a29dc",
  "service_name": "payment-service",
  "message": "payment-service 为什么出现 5xx？请引用证据。",
  "mode": "question",
  "previous_task_id": null,
  "start": "2026-10-01T01:00:00Z",
  "end": "2026-10-01T02:00:00Z"
}
```

`request_id` 每个新问题使用新 UUID；重试保留原 UUID 与完整内容。相同 UUID 修改
消息、服务、模式、时间窗或上一轮任务返回 409。操作人由会话派生并写本人审计。

`mode=question` 默认只读；即使文本要求写操作，也只能交付调查回答。
`mode=task` 表示将请求交给统一处置引擎。它仍需当前 Reviewer、结构化计划、Policy、
精确哈希审批、Executor 与独立恢复验证，不构成批准信号，也不能选择执行凭证。

`start/end` 可同时省略，使用事件接纳时刻前一小时；显式窗口必须带时区、长度大于 0
且不超过 24 小时，统一转换为 UTC。Fake 样例使用上面的固定时间窗。
`message` 最多 4000 字符。`previous_task_id` 必须已有有效回答且属于同操作人、同服务。
历史回答仅作为调查背景，不能作为本轮成功查询证据，也不能提供动作权限。

`GET /api/chat/{task_id}` 返回已持久化回答及 pending、status、引用和计划/Policy 信息。
无对应对话返回 404，无效 UUID 返回 422。它只读数据库，不要求 Temporal 当前在线。
原有 `/api/tasks/{id}`、`/api/tasks/{id}/interaction`、证据/审计/历史与审批接口继续可用。

## SSE 协议

使用带 Cookie/CSRF 的流式 POST 客户端；浏览器后续可通过 fetch 读取响应流。
服务端发送 `Content-Type: text/event-stream`、`X-Accel-Buffering: no` 和 `Cache-Control: no-store`。

| 事件 | 内容 | 含义 |
| --- | --- | --- |
| task | event_id、task_id、workflow_id、duplicate | 输入已持久化并派发统一任务 |
| evidence | evidence_ids | 本轮主 Agent 成功查询并复核的真实引用 |
| delta | text | 已核验回答的文字分段 |
| done | ChatAnswer | 当前状态、完整回答、结论/复核/计划 ID 与 Policy 判定 |
| error | task_id、message | 无可交付回答或临时故障；不输出内部异常/凭证 |

每个事件使用递增序号并以空行结束；等待时每 10 秒发送 SSE 注释心跳。
心跳只等待同一个 Temporal Update，不轮询数据库或自建任务状态机。
模型调查期间不发送未经核验的生产结论；回答通过引用核验后再按段流式发送。
任务转人工时，done 的 answer 为 null，状态明确为 ESCALATED 或 AUTOMATION_ABORTED。

`CHAT_STREAM_TIMEOUT_SECONDS` 由环境注入，默认 120 秒，可设为 1–600 秒。
SSE 超时或断开只终止当前连接的等待，已接纳任务继续由 Temporal 管理。
客户端可用 task_id 查进度，或用相同 request_id 重发；当前版本重发完整回答，
不把 `Last-Event-ID` 当成局部文字恢复游标。

## 统一链路与边界

输入经 ChatIngestionWorkflow 的 Activity 在同一事务里创建 OpsEvent/manual、
source=Human 任务、输入 Evidence 与本人审计，再由既有 event.start_task 启动
AITaskWorkflow。查询全部复用主 Agent 自研循环与唯一 Dispatcher。

只读对话同样优先匹配 Runbook，再调查和独立反证。独立 `verify_chat` 为 L0 Tool，
经 Dispatcher/Policy 留证审计，核对任务、版本、结论、本人输入、成功查询引用与复核。
只有独立 Verifier 持验证权限才可设置 RESOLVED，随后 LEARNING→CLOSED。
此处完成的是回答交付，不能据此声称生产故障恢复；没有生产 Action Plan 或执行。

动作对话直接复用已有规划/审批/执行/验证流程。参数变化使旧审批失效；
普通对话、追问和问答信号都不能批准动作。默认支付回滚 L3，进入 WAITING_APPROVAL。
Policy deny、伪造引用、预算耗尽和查询失败都不交付未经验证的结论或执行动作。

行锁、OpsEvent 指纹与已提交检查点保证并发/重投只保存一次输入、审计及主 Agent 查询。
Worker 重启或提交后丢响应由 Temporal 恢复；只追加 Ledger 提供回答和重连依据。
没有新增依赖、迁移或中间件，数据库 head 仍为 0016_catalog_audit。
沿用现有 local/test + Fake Worker 门禁，真实系统、飞书通知及网关未生产联调。

## 自检记录

2026-10-08 自检：聊天专项 **42 passed**（32 项禁真实网络单测、10 项本机数据库/Temporal）。
最终源码统一检查 **2166 passed / 590 skipped**，ruff、格式、mypy（464 个源文件）、
Connector 导入边界和 Git 环境检查全部通过。依赖测试由独立入口执行，
最终完整数据库/Temporal 回归 **577 passed / 3 skipped**；三个既有时间跳跃测试由
定时专项 **20 passed** 覆盖，旧 Workflow 专项 **36 passed**。

真实回环 HTTP 的自动演示和中文交互演示均通过；证据逐项读回、重复请求、追问、
L3 审批前零执行符合预期。所有本步临时测试库和演示 API 已清理，新增 PowerShell
脚本 UTF-8 BOM 和语法检查通过。

自检修复类型/格式、HTTP 严格模型解码、OpenAPI 公共错误声明、追问重投的父轮校验时序，
以及 Fake 回答不确定性中的过时阶段文字。前端工程按 Step 47 建立，本次只实施 Step 46。
