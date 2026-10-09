# Step 38：工单场景

依据 `AGENTS.md`、`SPEC.md`、`plans.md` Step 38 和长版设计第 24 节实现。
本次只完成工单场景，Step 39 及后续步骤保持未开始。

## 已实现链路

源工单经已有 `ops_platform` 事件归一化为 OpsEvent，去重后创建 source=Ticket 的
AI Task，启动同一个 `AITaskWorkflow`。没有新增队列、调度器或另一套任务状态。

主 Agent 分类为 SQL、权限、资源、配置、咨询、故障、发布七类；自动查询工单详情、
服务、业务归属和负责人。权限工单的用户、资源、权限、有效期、业务理由任一缺失时
进入 `WAITING_INFORMATION`，Fake 飞书收到问题，版本绑定回答信号恢复原阶段，
回答及 Knowledge 草稿沿用 Step 29。只能补缺失字段，不能改写源工单已有申请。

随后优先经 Dispatcher 检索并校验 Runbook，复用主 Agent 的
Think→Plan→Tool→Observe→Reason 循环与持久化模型/观察检查点。
结论逐项校验真实成功查询 Evidence ID。独立权限 Reviewer 重新读取工单与现有权限，
尝试发现错误请求人、错误范围、期限冲突及重复授权；有冲突不得进入 PLANNING。
权限范围超出宿主白名单时进入 `NEED_HUMAN_JUDGMENT`，回答不会改变执行权限。

当前可安全执行的工单动作是短期权限申请和工单回填关闭。其他六类完成分类及 Context
获取后转业务判断/人工处理；SQL、资源、发布等具体写动作不因分类而获得执行权限，
其适配器须沿后续计划及已有 Executor 白名单接入。

权限处理为 L4，回填关闭为 L1；二者以结构化 Action Plan 逐项交 Policy 判定，
默认都需审批。复用完整计划哈希、审批单、操作人审计与 Temporal 信号，拒绝或超时
转 ESCALATED、零签发零执行。回滚方案是转人工后另行审批撤销权限/重开工单，
旧审批不会授权反向操作。

执行顺序为：授予权限 → VERIFYING → 独立读回用户/资源/权限/期限 →
经 Executor 带 Evidence 回填关闭 → 再独立读回权限与工单关闭结果 →
RESOLVED → LEARNING → 待审核 Runbook Draft → CLOSED。
权限未恢复时回到 INVESTIGATING，关闭动作不执行。
执行、查询与聚合验证全部经唯一 Dispatcher 留证/审计；只有独立 Verifier
持当前任务、版本和事实证据权限，才能经 tasks 服务设置 RESOLVED。

## 权限与恢复

Reader 不暴露写方法；写端凭证默认 60 秒、最多 300 秒，精确绑定命令哈希、任务、
计划、动作、服务、工单和参数。过期、伪造、换服务或换参数均拒绝。
Executor 每个动作再次核对 Policy、审批、Runbook、权限绑定和熔断锁存。
执行意图先提交，外部动作以固定 execution_id 幂等；审计失败后 Temporal 重试仍使用
同一命令。已提交结果重投与 Dispatcher Replay 不新增副作用。

Fake 源平台的 `TicketState` 可由重启后的隔离 Worker 共享，模拟源系统持久化；
它不替代 PostgreSQL，也不把生产权限、凭证或宿主规则存入本平台数据库。
HTTP 动作适配器只允许 MockTransport；公司签发端、动作参数限制、幂等协议尚未
联调，真实生产写入关闭。本次所有执行和飞书通知均为 Fake。

## 配置

全部来自环境变量。默认 `TICKET_CONFIG.enabled=false`，没有隐式权限绑定；
事件只有显式开启该配置才走工单场景，其他既有入口行为保持兼容。
示例（仅本机 Fake）：

```powershell
$env:TICKET_CONFIG = '{"enabled":true,"credential_ttl_seconds":60,"max_permission_seconds":86400,"bindings":[{"service_name":"payment-service","requester_id":"requester-sample","subject_id":"user-sample","resource":"payment-logs","permission":"read"}]}'
$env:EXECUTION_CONFIG = '{"enabled":true}'
```

演示脚本内部使用上述等效配置，无需手工设置。身份不匹配、超期、已授权或非 open
工单不能直接执行；有效期和所有持久化时间均归一化为 UTC。
工单 Webhook 复用 Step 21 的签名入口，`external_id` 必须等于源工单 ID。
HTTP 查询和页面按 Step 44/45/47/51 实现；前端当前仅预留目录，本步无前端检查项。

## 你可以自行验收

先打开 Docker Desktop。在项目根目录 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
. .\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw '本机依赖启动失败' }

powershell -NoProfile -ExecutionPolicy Bypass -File .\check-tickets.ps1
if ($LASTEXITCODE -ne 0) { throw '工单专项失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-tickets.ps1 -Interactive
if ($LASTEXITCODE -ne 0) { throw '工单演示失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

第一次输入「批准」，应看到 `工单状态：closed`、`任务状态：CLOSED`、
`本工单写入：2`（权限一次、回填关闭一次）以及真实 Evidence ID 和学习记录。
第二个工单先打印 `WAITING_INFORMATION`、当前工单写入 0；输入缺失信息 JSON，
或直接回车使用显示的样例，再输入「批准」，应同样完成 CLOSED。
输入「拒绝」则该工单保持 open、任务 ESCALATED、本工单写入 0。
省略 `-Interactive` 自动完成两条批准场景。
末尾应打印「Step 38 工单场景 Fake 演示全部通过」「临时测试库已清理」。

脚本自动建立独立临时库和 Worker，运行真实本机 Temporal 并回放历史；
无需 API、手动数据库操作或公司凭证。本步没有新增依赖、数据库迁移或中间件，
应用库 head 保持 `0013_runbook_maturity`。

完整回归在同一个窗口执行 `. .\use-local-temporal.ps1` 后运行 `check-db.ps1`；
既有 Workflow 回归入口为 `check-workflow.ps1`。

## 本次自检记录

- 工单专项：29 passed（21 项禁止真实网络单测、8 项本机 PostgreSQL/Temporal）。
- 最终统一检查：1849 passed / 466 skipped；ruff、格式、mypy（369 个文件）、
  Connector 导入边界和 Git 环境文件检查通过。
- 全量数据库/Temporal 回归：451 passed / 3 skipped；既有 Workflow 专项 36 passed。
  3 项跳过为既有定时驱动时间跳跃测试，沿原独立入口执行。
- 共享验证结果接入成熟度后：最终工单专项新增成功/失败两项场景通过；
  既有成熟度专项 64 passed。同任务同 Runbook 内容版本只计一次，Replay 不刷计数。
- 自动演示与交互演示均实际运行通过，中文输入批准、补充信息及再次批准正常，
  每工单写入两次，工单/任务关闭且回填包含同任务真实 Evidence ID。
- 两个交付脚本语法及 UTF-8 BOM 检查通过；测试和演示临时库全部自动清理。

测试和本地运行仅用 Fake 外部系统；前端按 Step 47 尚未建立，没有本步前端测试项。
