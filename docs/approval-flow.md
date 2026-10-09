# Step 30：审批流

依据完整阅读的 AGENTS.md、SPEC.md、plans.md 实现第一个未完成项 Step 30。
实际计划文件为 `plans.md`；长版《最终设计方案 V1.0》仍未提供，沿用已有规格和本步
明确要求。本次只实现审批授权交接，Verifier、Executor、熔断仍按 Step 31–33 实施。

## 行为与边界

有真实 Action Plan 的任务进入 WAITING_APPROVAL 后，`approval.notify` Activity
从只追加 Ledger 读取当前计划，验证同任务、紧邻规划版本、当前 Reviewer 和 Policy。
生成审批单，通过既有飞书 Connector 发送“批准／拒绝”卡片。卡片展示目标环境、服务、
参数、L0–L5 风险、前置条件、回滚参数与触发条件、验证检查及成功／失败标准。
卡片超出既有容量限制时失败并转人工，不能截断待批准内容。

审批单绑定任务 ID、WAITING_APPROVAL 等待版本、Action Plan Evidence ID 和 SHA-256
动作哈希。哈希覆盖整个不可变计划：全部动作、目标、参数、风险、回滚、验证、依据、
环境和逐项 Policy；对象键排序并固定 JSON 编码，不受 PostgreSQL JSONB 键重排影响。
审批一个计划意味着批准卡片中的整组动作，修改任一动作须重新规划和审批。

信号 `approve_actions(ApprovalResponse)` 只接受当前审批身份和哈希的首个有效决定。
错误任务、旧版本、错误审批 ID、错误哈希、空操作人、无效决定被忽略。
普通 `human_response` 布尔信号和 Step 29 问答不能批准动作。
审批信号的操作人来自受信任的交互适配层，本步只在本机 Fake/Temporal 验收；
真实飞书回调验签、单用户鉴权和 HTTP 操作入口仍按 Step 43/44 实现。

| 决定 | 状态与效果 |
| --- | --- |
| approved | 原子保存决定与操作人审计，再经 tasks 服务进入 EXECUTING，完成本步授权交接 |
| rejected | 保存拒绝记录与审计，进入 ESCALATED，不执行动作 |
| expired | Temporal 等待超时，保存超时记录与审计，进入 ESCALATED，不执行动作 |

当前 EXECUTING 表示已批准、等待 Step 32 Executor 接入。该 Workflow 在授权交接后
结束，不调用占位执行／验证，不声称回滚成功或任务修复。Step 32 接入时须在该分支
继续真实执行与验证编排。Policy allow 计划沿用 WAITING_INFORMATION 等待 Executor；
deny 计划直接转人工，不生成可批准的审批单。

## 留证、门禁与恢复

审批单保存为 `approval.request` Evidence，决定保存为 `approval.decision` Evidence，
两者都有既有 `approval` 类型审计。批准／拒绝记录操作人，超时记录 `workflow`；
采集时间与审计时间均为带时区 UTC。复用现有追加保护和表结构，没有新迁移或依赖，
head 保持 `0011_human_interaction`。

同一任务行锁串行化通知和决定。重复通知使用稳定 UUID 与飞书通知幂等协议，
并发、提交后丢响应、Worker 重启复用原证据；已提交的不同决定不能覆盖首个决定。
审计失败时决定和证据整体回滚。Workflow 用 `action-approval-v1` patch 保留旧历史路径。

tasks 服务对拥有真实计划的 PLANNING／WAITING_APPROVAL → EXECUTING 添加审批门禁。
need_approval 计划缺少当前动作哈希的批准及操作人审计时拒绝迁移；旧审批不能用于新版本。
`ApprovalStore.is_approved(prompt, current_plan)` 同时核对完整哈希、原计划、任务状态版本、
批准证据、审计和当前 Policy，供后续 Executor 使用；参数、目标、环境、风险、回滚或
验证改变以及有效 Policy 判定改变均会使审批失效。Executor 仍须在实际执行事务中
重新核对授权并使用短时动作级凭证，此部分留给 Step 32。

## 自行验收

Docker Desktop 与本项目 PostgreSQL / Temporal 已运行时，在根目录 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-approval.ps1
if ($LASTEXITCODE -ne 0) { throw '审批专项失败' }
.\demo-approval.ps1 -Interactive
if ($LASTEXITCODE -ne 0) { throw '审批交互演示失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
. .\use-local-temporal.ps1
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库/Temporal 回归失败' }
```

专项应显示 `40 passed` 和“Step 30 审批流验收全部通过”。交互演示先展示
`payment-service v2.3.7 → v2.3.6` 的 L3 卡片，由你输入“批准”或“拒绝”；随后自动
演示拒绝和两秒超时。批准应显示 `approved → EXECUTING`，拒绝与超时应显示
`rejected/expired → ESCALATED`。每项打印审批 Evidence ID、操作人、动作参数修改后
原审批失效以及实际执行次数 0，最后显示“Step 30 审批流 Fake 演示全部通过”。
省略 `-Interactive` 自动演示三个分支。脚本启动隔离 Worker、创建独立临时库和队列，
结束清理，无需启动 API、常驻 Worker 或填写公司凭证。

若本机依赖未启动，但已按 deploy/README.md 配置过：

```powershell
.\use-local-deps.ps1
docker compose -f .\deploy\docker-compose.yml up -d --wait
if ($LASTEXITCODE -ne 0) { throw '本机依赖启动失败' }
.\check-deps.ps1
```

前端目录仍是 Step 47 的预留，没有可执行的前端 lint/typecheck/test，本步不提前建立前端。
所有验证使用 Fake 外部系统与本机依赖，没有向真实飞书发送消息或执行生产运维写操作。

## 自检记录（2026-10-07）

最终审批专项 `40 passed`（23 项禁真实网络单测、13 项隔离 PostgreSQL、4 项本机
Temporal），包括操作人首尾空白规范化后批准与审计一致、并发、提交后丢响应和
Worker 重启。自动三分支演示及 `-Interactive` 实际输入中文“批准”均通过。

统一检查 ruff、格式、mypy（286 个源文件）、Connector 边界与 Git 环境检查通过，
pytest `1630 passed, 345 skipped`；跳过的依赖测试通过专项和隔离本机回归执行。
完整 PostgreSQL/Temporal 回归 `332 passed, 3 skipped`，3 项为既有定时驱动
时间跳跃测试；既有完整 Workflow 专项 `36 passed`。
旧 Action Plan 演示及 Step 30 接入前完成历史 Replay 实际通过。

自检修复 Fake 图准备遗漏、类型／格式、审批与 Tool 审计计数混淆，以及操作人
规范化不一致。新增 PowerShell 语法通过；最终临时测试库数量为 0，四个本机依赖
healthy。本步没有修改 SPEC 或 AGENTS，也没有进入 Step 31。
