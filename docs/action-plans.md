# Step 28：Action Plan

本步依据 AGENTS.md、SPEC.md 与 plans.md Step 28 实现。实际计划文件名为
`plans.md`；AGENTS.md 引用的长版设计原文仍缺失，沿用现有规格和计划的明确要求。
本次仅实现结构化规划与 Policy 判定，后续人工回答、审批单、Verifier、Executor
分别留给 Step 29–32。前端工程按 Step 47 建立，本步通过脚本验收。

## 行为

启用主 Agent 的任务完成 RCA 和 Reviewer 后，经 tasks 服务既有复核门禁进入
PLANNING。`task.plan_actions` Temporal Activity 将当前复核后的结论、真实事实快照
交给主 Agent 生成结构化 JSON 草稿；规划模型没有任何 Tool 或执行入口。
LLM 仍经已有公司网关客户端调用，local/test 使用明确的支付场景 Fake。

每个动作包含英文 ID、动作名称、目标服务、参数、风险等级、引用 Evidence ID 的
理由、执行前提、失败后的回滚方案，以及独立验证检查、成功条件和失败处理方式。
回滚方案与验证方式缺失、空白或不符合结构时，动作无法进入计划。
回滚版本参数必须包含不同的 `from_version` 和 `to_version`，拒绝额外参数。
计划至少一个动作，最多十个，动作 ID 必须唯一。

风险由宿主保守定级：当前已知 `rollback_prod` 下限 L3；模型声明更高等级则保留。
缺省或 null 等级统一 L5，未知动作按 L5。目标环境取进程配置，模型不能选择环境。
逐项复用既有 PolicyEngine，完整保存每项 allow / need_approval / deny、原因和规则 ID。
计划整体取最严格结果：deny 优先，其次 need_approval，最后 allow。

| Policy 结果 | 当前 Workflow 行为 |
| --- | --- |
| need_approval | PLANNING → WAITING_APPROVAL，经 Step 30 的独立动作审批继续 |
| deny | PLANNING → ESCALATED，转交人工 |
| allow | PLANNING → WAITING_INFORMATION，等待后续 Executor 接入 |

等待超时转 ESCALATED。当前 Step 17 的布尔人工信号不构成动作级审批，
在正式审批等待中被忽略。Step 30 明确批准才进入 EXECUTING，拒绝转 ESCALATED；
Executor 仍留给 Step 32。WAITING_APPROVAL 与
NEED_HUMAN_JUDGMENT 始终独立。保留原本地占位流程的测试用途和 Worker 的
local/test + Fake 门禁，不能将其用于生产。

Fake 验收提出“回滚 payment-service v2.3.7 → v2.3.6”，判定 L3 / need_approval。
版本是候选方案参数，实际执行前必须重新核对当前版本、目标镜像与配置可用性、
数据库兼容性、业务影响和有效审批。规划完成不代表已回滚或已修复。

## 证据、重试与回放

草稿判断只能引用当前原结论或独立 Reviewer 报告所引用的同任务真实查询证据，
逐条核对成功 Tool 审计；不存在、跨任务或检查点 ID 被拒。
计划必须绑定紧邻 PLANNING 的 RCA 版本、原结论和复核记录。Reviewer 结果新增
规范化调查规格哈希，固定服务、时间窗及上下文，篡改输入或重新调查后旧复核不能复用。

使用既有只追加 Ledger 保存 `planner.think` 模型检查点和 `action_plan` 最终快照，
不新增数据库表或迁移。计划快照保存完整结构、逐项 Policy、环境和关联 ID。
Worker 的 progress 查询新增 `action_plan_json`、`action_plan_evidence_id`，可按 ID
精确读回。状态迁移继续由 tasks 服务完成并写历史与审计。

任务行锁、规划版本与规范化输入哈希防并发重复；模型检查点已提交后不重新调用。
最终计划提交失败时可以复用模型检查点再次保存；提交后丢响应、Worker 重启复用
已提交计划。Policy 配置或环境变化时拒绝同版本结果复用。
返回 JSON 固定键顺序，避免 JSONB 重排导致首次与重试结果不同。
Workflow 使用 `action-plan-v1` patch 保留旧历史回放路径。

没有注册新 Tool、调用外部运维系统或执行 L1+ 写操作。规划留证不是 Tool 调用，
不会伪造调用、审批或执行审计。自检同时修复了 tasks 门禁导致的既有循环导入。

## 自行验收

Docker Desktop 的 Linux 引擎与本项目本机 PostgreSQL / Temporal 依赖运行后，
在项目根目录同一个 PowerShell 窗口执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-action-plans.ps1
if ($LASTEXITCODE -ne 0) { throw 'Action Plan 专项失败' }
.\demo-action-plans.ps1
if ($LASTEXITCODE -ne 0) { throw 'Action Plan 演示失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
. .\use-local-temporal.ps1
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库回归失败' }
```

专项应显示“Step 28 Action Plan 验收全部通过”。演示应打印：

- 回滚 payment-service v2.3.7 → v2.3.6：L3 / need_approval。
- RCA → PLANNING → WAITING_APPROVAL。
- Workflow ID、计划及依据的实际 Evidence ID、回滚方案与验证方式。
- 缺少 rollback 或 verification 的动作无法进入计划：通过。
- 普通人工信号不能触发执行：通过。
- “Step 28 Action Plan Fake 演示全部通过”，随后“临时测试库已清理”。

脚本自动创建临时数据库、专用队列和隔离 Worker，结束自动清理；不需要手动启动
API/Worker、填写公司凭证或访问生产系统。演示中的人工信号是防执行检查，最后
ESCALATED 为预期；正常规划任务停在 WAITING_APPROVAL。

依赖未运行但已按 deploy/README.md 配置过时：

```powershell
.\use-local-deps.ps1
docker compose -f .\deploy\docker-compose.yml up -d --wait
if ($LASTEXITCODE -ne 0) { throw '本机依赖启动失败' }
.\check-deps.ps1
```

## 自检记录（2026-10-07）

专项 45 项通过（29 项禁止实际网络的单元测试、12 项 PostgreSQL、4 项本机
Temporal）；Fake 人工演示及两项必填拒绝、状态/历史/证据读回、信号防执行和
历史回放实际通过。统一检查通过：ruff、格式、mypy、Connector 边界与 Git 环境
文件，pytest 1578 passed, 312 skipped；依赖测试由专项和本机回归执行。
显式加载本机 Temporal 后，全量数据库/Temporal 回归 299 passed, 3 skipped，
3 项为既有定时驱动时间跳跃测试。既有 Workflow 专项 36 passed。
旧主 Agent、Runbook、专家、Reviewer 演示和 Step 28 接入前复核通过历史的 Replay
实际通过；新增 PowerShell 语法通过，测试临时库已清理。

所有演示使用隔离临时库与专用队列；无新增依赖或迁移，应用 head 保持
0010_runbook_engine。Step 29 及后续步骤保持“还没做”。
