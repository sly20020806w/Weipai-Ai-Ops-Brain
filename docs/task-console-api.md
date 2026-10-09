# Step 44：任务闭环 API

本步骤只实现任务闭环的 HTTP 查询与人工操作。依据 AGENTS.md、SPEC.md、plans.md，
以及权威原设计第 10、14、17、19–22、31、34 节；未进入 Step 45。
无新增依赖或数据库迁移，head 保持 `0015_single_user_auth`。

## 自己运行一次

先启动 Docker Desktop，保持本项目已有 PostgreSQL/Temporal 依赖运行。
在 Windows PowerShell 中执行下面三条命令，每条成功后再执行下一条：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
. .\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw '本机依赖启动失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\check-console.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 44 专项失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-console.ps1 -Interactive
if ($LASTEXITCODE -ne 0) { throw 'Step 44 HTTP 演示失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

演示会显示支付服务 `v2.3.7 → v2.3.6` 的 L3 审批，输入 **approve**。
预期看到：

- API 审批 `202 → CLOSED`，打印真实审批 Evidence ID。
- Fake 回滚次数 **1**，相同审批再提交仍为 **1**。
- 判断与补充信息各 `202 → CLOSED`，回答留证并保存 Knowledge 草稿。
- 人工接管 `202 → ESCALATED`，对应 Workflow 取消，后续自动化被禁止。
- 「Step 44 任务闭环 API Fake 演示全部通过」「临时测试库已清理」。

输入 **reject** 会验证拒绝分支：`ESCALATED`，Fake 回滚次数 **0**。
省略 `-Interactive` 自动批准 Fake 动作。脚本自动创建临时数据库、隔离 Worker、
随机账户及回环 HTTP API；结束后清理 API 进程、未结束的演示任务和临时数据库。
无需公司凭证或另行启动 API/Worker，全部运维系统与 LLM 使用 Fake。

完整数据库/Temporal 回归：

```powershell
. .\use-local-temporal.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '完整数据库/Temporal 回归失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\check-schedules.ps1
if ($LASTEXITCODE -ne 0) { throw '时间跳跃专项失败' }
```

## 查询接口

所有路径都有 `/api` 前缀，并复用 Step 43 的 Cookie 会话鉴权。
列表返回 `{items, total, limit, offset}`；默认 50 条，`limit` 为 1–100，
`offset` 非负。状态历史按任务完整返回，按 `sequence` 升序。

| 方法 | 路径 | 结果/筛选 |
| --- | --- | --- |
| GET | `/tasks` | 任务分页；`status`、`source` 精确筛选 |
| GET | `/tasks/{task_id}` | 任务状态、版本、来源及 UTC 时间 |
| GET | `/tasks/{task_id}/interaction` | 当前审批单或判断/补充信息问题 |
| GET | `/events` | 事件分页；`source`、`origin`、`service_name` 筛选 |
| GET | `/events/{event_id}` | OpsEvent 详情、指纹及关联任务 |
| GET | `/evidence` | 证据分页；可按 `task_id` 筛选 |
| GET | `/tasks/{task_id}/evidence` | 同任务证据分页，按采集时间/ID 排序 |
| GET | `/evidence/{evidence_id}` | 按 Evidence ID 精确返回原快照/来源引用 |
| GET | `/tool-calls` | Tool 调用分页；可按 `task_id` 筛选 |
| GET | `/tasks/{task_id}/tool-calls` | 同任务 Tool 调用分页，包含拒绝/失败/回放 |
| GET | `/tool-calls/{call_id}` | 调用审计详情、参数/结果引用及 Evidence ID |
| GET | `/tasks/{task_id}/status-history` | 同任务状态迁移历史 |
| GET | `/status-history/{history_id}` | 单条状态历史 |
| GET | `/incidents` | 事故复盘分页；`service_name` 筛选 |
| GET | `/incidents/{incident_id}` | 复盘详情、十三章、Timeline 与证据引用 |

`incident_id` 是复盘的 **Evidence ID**；详情中 `report.task_id` 是事故任务 ID。
Incident 复用既有只追加 `postmortem` 证据，不新造事故表。
普通证据不能作为 Incident，状态/审批审计不能作为 Tool 调用，两者均返回 404。
有效任务的空列表为 200；不存在的任务、证据或记录为 404。
UUID、分页、枚举无效为 422。源系统原始日志/指标/Trace 仍保留在源系统，API 查询本库
已经采集的引用/证据快照，不发起实时 Connector 查询。

`interaction` 将 `NEED_HUMAN_JUDGMENT`、`WAITING_INFORMATION`、`WAITING_APPROVAL`
分别保留。状态提交到通知 Activity 完成前，`approval`/`question` 可能短暂为 null；
此时不能构造审批。旧巡检/占位等待可返回 `recovery` 与 `recovery_question_id`：
以该问题 ID 和 `status_version` 提交恢复回答，平台先留存问题、回答与知识草稿，
再派发原有恢复信号。只有 Workflow 确认为旧等待模式才接受此适配；
正式问题正在生成时不会改用恢复信号。

## 人工操作接口

四个 POST 均要求会话、同源校验及 `X-CSRF-Token`，操作人由已验证会话派生，
请求体不允许 `actor`/`respondent` 等额外字段。

| 路径 | 必填请求体 |
| --- | --- |
| `/tasks/{task_id}/approval` | `approval_id`、`wait_version`、`action_hash`、`decision` |
| `/tasks/{task_id}/judgment` | `question_id`、`wait_version`、`answer` |
| `/tasks/{task_id}/information` | `question_id`、`wait_version`、`answer` |
| `/tasks/{task_id}/takeover` | `expected_version`、`reason` |

审批字段从 `interaction.approval` 获取；`decision` 只能是 `approved`/`rejected`。
回答身份从 `interaction.question` 获取，版本是 `question.task.version`；
判断不能通过补充信息路径回答，也不能批准动作。
旧等待使用 `recovery_question_id` 与 `status_version`，仍提交 `answer` 正文；
巡检恢复后重新读取实际数据，回答本身不能证明资源或指标已经恢复。
接管版本使用任务当前 `status_version`。

成功返回 **202**，包含 `task_id`、`operation_id`、`evidence_id`、`outcome`。
这表示已保存决定/回答/接管记录，且信号或取消请求已由 Temporal 接收；
业务执行继续异步进行，最终状态通过任务查询确认。

不存在的任务/问题/审批单为 404；旧状态、错哈希、错问题类型、冲突决定为 409；
输入不合法为 422；未登录为 401；缺 CSRF 或跨站请求为 403；
存储/Temporal 暂时不可用或提交尚未确认为 503，响应隐藏底层错误。
遇到 503 使用**完全相同请求体**重试，已提交记录保持幂等。

### 持久化与安全边界

HTTP 层只适配参数和状态码。查询在 `tasks/console_queries.py`，
操作在 `tasks/control.py`。同一后端 Worker 注册 `TaskControlWorkflow`，
由 Temporal 管理保存记录、信号派发、有限重试和 Worker 重启恢复。
没有自建队列、定时器或业务轮询状态机。

命令身份覆盖任务、类型、操作人、完整参数；运行中/已完成命令复用结果，
失败命令允许同身份重新启动，数据库行锁与既有审批/问答服务确保记录不重复。
审批继续校验 Reviewer、Policy、当前 Runbook、完整动作哈希与等待版本；
Workflow 再消费相同决定，Executor 继续执行原有授权门禁。
派发前查询 Workflow 当前问题/审批单，避免通知 Activity 尚未被消费时丢弃信号。
信号派发后丢响应的重试检查已消费证据，避免重复决定。

人工接管在任务锁下原子追加 `human.takeover` Evidence、本人操作审计和状态历史，
由 TaskService 置 `ESCALATED`，再通过 Temporal 请求取消对应 AI Task Workflow。
持久化接管记录参与 TaskService、统一写入口/Executor 的自动化门禁与审批有效性校验；
旧审批、后续信号或 Worker 重启均不能恢复该任务的自动化。
`CLOSED`/`RESOLVED` 等没有合法接管边的状态返回 409。
接管会停止后续自动动作；此前已发往外部的动作不能撤回，须查看已有执行证据确认结果。

本步骤没有开放真实生产写入；API 操作仍沿用 Worker 的 local/test + Fake 限制。
前端目录仅预留，lint/typecheck/test 在 Step 47 建立，未提前实施。

## 手动调用已有本机 API

如需操作本机应用库中的既有任务，可在一个 PowerShell 窗口启动 API：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
. .\use-local-db.ps1
. .\use-local-temporal.ps1
$env:APP_ENV = 'local'
. .\use-local-auth.ps1 -Username 'owner'
.\run-api.ps1
```

账户配置只保留在当前进程。另一个窗口执行 `run-worker.ps1`，使用本机 Fake Worker。
应用库须已经执行既有 `alembic upgrade head`；本步没有新增迁移。
打开 `http://127.0.0.1:8000/docs` 可查看全部 schema。

第三个窗口登录并查询，密码通过隐藏输入读取：

```powershell
$consoleSecret = Read-Host '输入刚设置的密码' -AsSecureString
$consolePassword = [System.Net.NetworkCredential]::new('', $consoleSecret).Password
try {
    $consoleLogin = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/auth/login' `
        -ContentType 'application/json' -Headers @{'X-Ops-Login'='1'} -SessionVariable consoleSession `
        -Body (@{username='owner';password=$consolePassword} | ConvertTo-Json)
} finally { $consolePassword = $null; $consoleSecret.Dispose() }
$consoleTasks = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/tasks?status=WAITING_APPROVAL' `
    -WebSession $consoleSession
$consoleTasks.items
```

选一个真实存在的等待任务，用其当前审批数据批准：

```powershell
$consoleTaskId = $consoleTasks.items[0].id
$consolePending = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/tasks/$consoleTaskId/interaction" `
    -WebSession $consoleSession
$consoleTicket = $consolePending.approval
if ($null -eq $consoleTicket) { throw '当前没有可审批的单据，请刷新状态' }
$consoleTicket.plan.actions  # 检查完整动作、风险、参数、回滚和验证方案
$consoleBody = @{
    approval_id=$consoleTicket.approval_id
    wait_version=$consoleTicket.wait_version
    action_hash=$consoleTicket.action_hash
    decision='approved'
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/tasks/$consoleTaskId/approval" `
    -ContentType 'application/json' -WebSession $consoleSession `
    -Headers @{'X-CSRF-Token'=$consoleLogin.csrf_token} -Body $consoleBody
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/tasks/$consoleTaskId" -WebSession $consoleSession
```

应用库没有等待任务时，优先运行上方隔离演示；它自动准备完整 Fake 场景。

## 2026-10-08 最终自检记录

- Step 44 专项：**65 passed**（49 项禁止真实网络单测、16 项本机集成）。
- 统一检查：**2072 passed、560 skipped**；ruff/格式/mypy、Connector 边界与 Git 检查通过。
- 完整 PostgreSQL/Temporal 回归：**547 passed、3 skipped**；三项既有时间跳跃测试
  由定时专项 **20 passed** 覆盖，旧 Workflow 专项 **36 passed**。
- 真实回环 HTTP 自动演示及交互 `approve` 演示通过，Fake 回滚恰好一次，
  重复审批同回执；审批闭环、两类回答及接管输出均符合上文。
- 新增 PowerShell 脚本 UTF-8 BOM 与语法通过；临时测试库数量为 0，
  本次演示 API 进程及端口已关闭，应用库 head 仍为 `0015_single_user_auth`。

自检修复了类型/格式、旧 Mock 夹具、问答样例来源、通知/信号时序、失败命令恢复
与旧等待适配。一次完整回归运行中补充查询契约，旧进程混用了版本；已停止该隔离
pytest 子进程，由父脚本清理临时库，并以 Worker 身份中的 PID 精确清理 13 个遗留
Workflow。固定最终源码、使用全新进程后的完整回归全部通过。

本次只完成 Step 44，`plans.md` 已标为完成，Step 45 仍未开始。
