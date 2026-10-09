# Step 21：事件接入与自行验收

依据 `AGENTS.md`、`SPEC.md` 与 `plans.md` 的 Step 21 实现。本次只接入事件，不实现 Step 22 的定时驱动、Step 24 的 Agent 调查或后续处置。目录中未提供 AGENTS/SPEC 引用的完整版原始设计。

## 一键检查与实际演示

先启动 Docker Desktop 的 Linux 引擎，并确保本项目 PostgreSQL、Temporal、Temporal UI 容器正在运行。已有本地依赖可以运行 `check-deps.ps1` 检查；首次启动依赖见 `deploy/README.md`。

在 PowerShell 中执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check.ps1
.\check-events.ps1
.\demo-events.ps1
```

每条命令执行后可用 `$LASTEXITCODE` 检查，成功应为 `0`。

- `check.ps1`：Connector 导入边界、ruff、格式、mypy、全部离线测试及 Git 环境文件检查。
- `check-events.ps1`：独立临时 PostgreSQL 库、本地 Temporal 和全 Fake 的专项测试。检查并发/重复去重、事务回滚、签名与时间窗、HTTP 202/401/413/422/503、K8s Watch、丢响应后的重试及历史回放。结束显示“Step 21 事件接入验收全部通过”，并删除临时库。
- `demo-events.ps1`：自动启动一个真实的回环 HTTP API 和专用队列 Worker，实际发送带签名的 Prometheus 样例；无需手工开两个窗口，也无需提供凭证。它不会修改应用库。

实际演示应该看到：

```text
POST 告警：HTTP 202；OpsEvent=1，Alert 任务=1；状态=WAITING_INFORMATION
Workflow ID：ai-task-<UUID>
相同告警重投：HTTP 202，duplicate=true，任务 ID 不变；错误签名：HTTP 401
Fake K8s Warning：已生成 OpsEvent，关联 payment-service；演示全部通过
临时 API、Worker、演示 Workflow 已停止
临时测试库已清理
```

演示签名密钥随机生成，只在内存和子进程环境中存在。API 只绑定 `127.0.0.1` 的临时端口。演示完成后会终止本次专用队列的 Workflow，并关闭 Windows venv 启动器及其 API 子进程树。本地 [Temporal UI](http://127.0.0.1:8080) 可按输出的 Workflow ID 查看历史，清理后的状态显示 `Terminated`，属于预期。

需要完整数据库回归时另运行 `.\check-db.ps1`。数据库入口不运行 Temporal 专项；相关测试由 `check-events.ps1` 执行。前端仍按 Step 47 预留，本步骤没有可运行的前端工程。

## 接入行为

`triggers/` 定义统一 `NormalizedEvent`、`OpsEvent`、多来源适配、指纹和事务服务。`api/events.py` 只负责读取 HTTP 请求、依赖注入和错误码映射。请求校验成功后，经 `EventIngestionWorkflow` 执行 `event.persist`，同一事务写事件、AI Task、NEW 状态历史及任务创建审计，然后由 `event.start_task` 启动既有 `AITaskWorkflow`。

`ops_events` 只保存来源、源事件 ID、服务/目标、摘要、源发生时间与任务关联；不保存完整原始告警正文、日志、Trace 或签名密钥。`occurred_at`、`created_at`、`updated_at` 均为 UTC `timestamptz`。迁移 head 为 `0007_ops_events`。

指纹由平台计算，为来源系统、任务来源和源事件 ID 的 SHA-256。外部事件 ID 必须在来源系统内唯一；如源 ID 只在某仓库/租户内唯一，上游适配应将该范围加入 ID。重复投递保留首条事实及同一任务，不覆盖摘要或时间。数据库唯一约束与按固定顺序获取的事务级 advisory lock 保证并发去重，没有自建队列或业务轮询。

告警身份使用完整标签集合与 UTC `startsAt`；标签顺序、annotations、接收时间和发送方 fingerprint 不改变身份，同标签下一次告警的 `startsAt` 不同则创建新事件。一个批次按告警逐条归一化；无效告警或 `truncatedAlerts > 0` 会拒绝整个批次。`resolved` 推送不新建调查任务，也不会把任务置为 `RESOLVED`；任务成功仍必须由 Verifier 判定。

Temporal Activity 提交后丢响应时会重新执行去重，返回已有任务。派发提交后丢响应时，`ai-task-<UUID>` 与 `REJECT_DUPLICATE` 防止第二条生命周期。暂时不可用的入库/派发由 Temporal 持久重试。HTTP 等待超时返回 503，已启动的接入 Workflow 继续运行；上游重投不会新增同指纹任务。需要恢复时重启同队列 Worker，无须进程内补发轮询。

当前 Worker 仍要求 `APP_ENV=local/test`、Fake Connector、Fake LLM 与回环 Temporal。事件任务在占位 `CONTEXT_BUILDING` 后暂停到 `WAITING_INFORMATION`，等待后续真实调查能力；按既有配置超时后转 `ESCALATED`。这不是生产告警处理成功。`WAITING_APPROVAL` 与 `NEED_HUMAN_JUDGMENT` 保持原有独立语义，没有实际运维写操作或新 Agent Tool。

## Webhook 协议与配置

路径为 `POST /webhooks/{origin}`，成功返回 HTTP 202：

```json
{"events":[{"event_id":"<UUID>","task_id":"<UUID>","workflow_id":"ai-task-<UUID>","duplicate":false}]}
```

`prometheus` 接受 Alertmanager webhook 的 `alerts`、`labels`、`status`、`startsAt` 字段，其中告警必须带 `alertname` 与 `service` 标签。依据 [Alertmanager 官方 Webhook 配置](https://prometheus.io/docs/alerting/latest/configuration/#webhook_config)。

其他来源采用明确的统一接入协议，公司原生 Webhook 未提供，当前不假设原生字段或签名规则：

| origin | source | 用途 |
| --- | --- | --- |
| ops_platform | Ticket | 工单事件 |
| git / ci / argocd / config_center | Release | 代码、构建、发布、配置事件 |
| cloud | Alert | 云告警/异常事件 |
| manual | Human | 人工请求事件 |

```json
{"external_id":"<来源范围内唯一事件ID>","service_name":"payment-service","title":"发布事件","occurred_at":"2026-10-01T01:25:00Z","source":"Release"}
```

这些来源按固定映射生成任务；不能通过 manual 伪造 Schedule/Prediction 等来源。后续计划步骤产生的事件可复用统一模型与服务，本次没有实现相应驱动。

`TRIGGER_CONFIG` 由环境变量注入；`webhook_secrets` 是按来源配置的密钥字典，每个密钥至少 32 字符；无缺省密钥，未配置来源返回 401。生产凭证应通过环境变量/K8s Secret 提供，不写入配置文件、数据库或 Git。可配置 `signature_max_age_seconds`（默认 300）、`max_body_bytes`（默认 262144）和 `response_timeout_seconds`（默认 15）。

所有 Webhook 使用本平台统一的 HMAC 协议：

- `X-Ops-Timestamp`：ASCII 十进制 Unix 秒时间。
- `X-Ops-Signature`：`sha256=` 加小写十六进制 `HMAC-SHA256(secret, timestamp + "." + 原始请求体字节)`。
- 校验使用常量时间比较，同时检查时间戳有效期；必须先验签再连接 Temporal。

这不是 Alertmanager/GitHub 等系统的原生签名格式；直接接入前需在可信上游配置签名中转，或按公司已确认的协议增加适配。上游必须对实际发送的原始字节签名，不能在签名后重排 JSON。当前真实源系统未联调。

401 表示验签失败，413 表示请求体超过限制，422 表示事件协议无效，503 表示数据库/本地运行环境未配置或 Temporal 未就绪/仍在处理；重投可安全去重。

## K8s Event Watcher

HTTP LIST/Watch 仅实现于 `connectors/kubernetes/`，只使用 Reader 的 GET API。首次 LIST 验证完整分页与一致的快照 resourceVersion，然后 Watch 使用 `watch=true`、`resourceVersion`、`allowWatchBookmarks=true` 和 `timeoutSeconds`。ADDED/MODIFIED 进入归一化，DELETED/BOOKMARK 只推进游标，普通 Normal 事件不创建任务。HTTP 或流内 410 会清空游标并重新 LIST；规则依据 [Kubernetes 官方 API Watch 文档](https://kubernetes.io/docs/reference/using-api/api-concepts/#efficient-detection-of-changes)。

仅 Warning 生成 Alert 来源的 OpsEvent。事件身份含 cluster/namespace/Event UID，聚合计数或 lastTimestamp 变化不重复创建任务；优先保存首次源时间，不用采集时间替代事件发生时间。服务通过关联对象 UID 与配置服务标签查找；对象已消失时保留资源范围，不推断服务归属，后续关联变化也不改变指纹。

Watcher 默认不启用。需要常驻本地 Fake Watch 时，在已有 `TRIGGER_CONFIG` 中设 `watcher_enabled=true`、`watcher_namespaces=["payment"]`，再运行 `run-worker.ps1`。Worker 启动时幂等启动 Watch Workflow，既有游标不会因 Worker 重启而重置。`watch_timeout_seconds` 默认 20。取消 `watcher_enabled` 仅停止自动注册，已存在的 Watch 应通过 Temporal 明确终止。

`KubernetesEventWatchWorkflow` 托管连接窗口与重试，入库及任务派发成功之后才通过 ContinueAsNew 保存游标，保持历史有界。没有本地线程队列、调度器或轮询状态机。Fake 返回固定 Warning，后续窗口等待配置时长且不产生新事件；真实 Watch 协议只通过 HTTP mock 验证，没有访问实际 ACK/K8s 集群。

## 自检记录（2026-10-06）

- 33 项禁止真实网络的离线测试与 7 项本地 PostgreSQL/Temporal 专项全部通过，共 40 项。
- `check.ps1`：ruff、格式、mypy（194 个源文件）、Connector 导入边界和 Git 检查通过；1395 项单元测试通过，集成测试通过独立入口运行。
- `check-db.ps1`：201 项 PostgreSQL 回归通过，5 项 Temporal 测试按专项入口跳过。空库完整迁移升降级及 Alembic metadata 检查通过。
- 真实回环 HTTP 演示通过 202、重复去重、401 和 Fake K8s Warning；临时数据库、API 进程树及演示 Workflow 全部清理。
- 本机应用库已升级到 `0007_ops_events`，Alembic metadata 检查一致；新增 PowerShell 入口语法通过。
- 修复了静态类型、长字符串格式、K8s 重新关联服务的去重边界、Windows 子进程清理，以及测试/演示误用跟随执行链句柄的 `run_id` 而提前停止 Worker 的时序问题；改为比较 `first_execution_run_id`，等待派发与 ContinueAsNew 完成。

没有新增依赖、前端工程、生产系统访问或 L1+ 运维操作。未进入 Step 22。
