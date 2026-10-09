# Step 33：自动熔断

本步依据 `AGENTS.md`、`SPEC.md` 和 `plans.md`，在现有任务引擎中实现六类熔断。原长版设计文件仍未提供，沿用前面步骤记录的来源限制。没有进入 Step 34，没有新增中间件、依赖或迁移；本机数据库 head 仍为 `0011_human_interaction`。前端工程留给 Step 47。

## 自己跑一遍

在项目根目录的 PowerShell 中运行，先确保 Docker Desktop 已启动、Step 2 本地依赖容器正在运行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
. .\use-local-deps.ps1
.\check-deps.ps1
.\check-safety.ps1
.\demo-safety.ps1
.\check.ps1
```

`check-safety.ps1` 自动加载已有本机 PostgreSQL/Temporal 配置，不打印密码；新建独立 `weipai_db_test_*` 临时库，结束后自动删除。它检查六类判据、真实持久化状态/证据/审计、批准动作拦截、并发去重、通知发送后事务失败、旧审批、Action 预算，以及本机 Temporal 的失败重试和历史 Replay。全程只有 Fake 运维系统；集成测试仅连接本机数据库和 Temporal。无须启动 API、Worker 或配置真实飞书。

`demo-safety.ps1` 注入六组明确标注的 Fake 已采集证据，逐项展示判定和通知。每一项都应出现：

```text
（对应原因）：AUTOMATION_ABORTED
任务 ID：…；熔断 Evidence ID：…
后续动作：拒绝；凭证签发/实际 Fake 运维执行次数：0/0；接管通知：1
```

最后应出现“Step 33 六种熔断 Fake 演示全部通过”和“临时测试库已清理”。通知内容含任务 ID、原因、熔断 Evidence ID 和人工接管提示。演示的六组事实是隔离样例；实际动作端失败与 Temporal 重试由专项测试验证。

`check.ps1` 应输出“统一检查全部通过”。完整旧功能回归可另行运行：

```powershell
. .\use-local-temporal.ps1
.\check-db.ps1
.\check-workflow.ps1
```

## 判据与默认值

所有检测依据当前任务的已提交 Ledger 和调用审计，不接受 Agent 传入的“已熔断”“已恢复”布尔标记。

| 原因 | 判据 |
| --- | --- |
| 连续操作失败 | `execute_action` 的 live 失败调用审计连续达到 3 次；成功清零，Policy 拒绝、只读失败和 Replay 不算操作失败。 |
| 指标继续恶化 | 同服务、同指标、同标签至少 3 个真实采样点，间隔最多 120 秒；连续恶化达到最小变化量且突破阈值，或相同序列两个不重叠窗口的均值继续恶化。5xx 阈值 1%、最小增量 2 个百分点；P99 阈值 500ms、最小增量 100ms；成功率阈值 99%、最小降幅 2 个百分点。健康、恢复、平稳异常、缺点、超大采样间隔不构成此判据。 |
| 影响范围扩大 | 两个同服务、不重叠的 Trace 窗口中，真实失败 Span 的服务集合严格扩大；拓扑邻居数量变化或同大小集合替换不算扩大。 |
| 证据冲突 | 已留证的 Reviewer 报告反驳主 Agent 结论，且引用当前任务中真实成功查询证据。新 Workflow 首次确认冲突便熔断。 |
| Runbook 连续失败 | 当前任务中同一个已匹配 Runbook 的独立诊断/验证失败连续达到 2 次；同阶段的多个失败步骤只算一次，只有独立 Verifier 确认成功才能清零。未匹配、排除、不适用和检索拒绝不算 Runbook 执行失败。跨任务成熟度及全局成功/失败计数仍属于 Step 35。 |
| 超过最大 Action 次数 | 每任务最多 3 个独立执行意图，重投同计划/同动作不重复计数。准备下一动作前预判是否超限，超限时不保存新执行意图、不签发凭证。未确认结果的已有意图也占用预算。 |

阈值只来自环境变量 `SAFETY_CONFIG`，例如：

```powershell
$env:SAFETY_CONFIG = '{"max_actions":2,"max_consecutive_execution_failures":2}'
```

配置对象和密钥不入库；报告只保存配置 SHA-256 指纹、判据及其 Evidence/Audit ID。

## 停止、通知与恢复

熔断检查在任务行锁下运行，`safety.abort` 证据、`AUTOMATION_ABORTED` 状态、状态历史和熔断审计同事务提交。Executor 在每个新意图前检查全部判据，失败调用提交后再次检查连续失败次数；Verifier 在设置恢复/重新调查状态前检查；主 Workflow 在 Runbook、调查、Reviewer、执行、验证和异常出口检查。所有状态仍只由 `tasks/` 迁移，`RESOLVED` 权限仍由独立 Verifier 控制。

持久化的 `safety.abort` 是锁存记录。Executor 授权、Dispatcher 的 live 写入口以及 `EXECUTING` 状态迁移均检查它；旧审批、Activity 重试、Worker 重启，甚至记录接管后迁移为 `ESCALATED`，都不会自动清除这道门禁。历史 Replay 仍可回放原结果，不签发凭证、不调用写客户端。已经提交给外部动作端的操作无法撤销，熔断阻止后续动作和重试；没有把跨系统副作用伪装为数据库原子事务。

先持久化停止，再由 Temporal `safety.notify` Activity 发送接管通知。通知 ID 绑定任务与熔断证据，发送后数据库事务失败时仍使用同一个 ID；Fake 发件箱去重。通知异常由 Temporal 有限重试；耗尽后任务仍为 `AUTOMATION_ABORTED`，Workflow 查询的 `takeover_notification_state` 为 `failed`，不能因通知失败重新放行动作。成功为 `sent`。通知回执和发送审计独立追加，不能把尚未发送的通知写成成功。

本步只发送接管提示，不实现接管 HTTP 接口、页面或自动复位。后续接管操作接口属于 Step 43/44，页面属于 Step 49；Postmortem 与 Runbook 晋级属于 Step 34/35。生产执行仍沿用 Step 32 的关闭限制，测试和演示没有触达真实生产系统。

## 本次自检结果

- `check-safety.ps1`：36 项通过，其中 21 项离线单测、15 项 PostgreSQL/Temporal 验收。
- `check.ps1`：Connector 边界、ruff、格式、mypy（319 个源文件）、1711 项单元测试和 Git 环境检查通过；402 项依赖测试按既有约定在单独入口运行。
- 加载 `use-local-temporal.ps1` 后的 `check-db.ps1`：388 项通过，3 项既有时间跳跃测试沿用独立入口跳过；新增 Verifier 熔断场景在最终专项中通过。
- `check-workflow.ps1`：36 项通过，覆盖既有暂停/恢复、超时、重试和历史 Replay。
- `demo-safety.ps1`：六种原因全部触发、动作拒绝、各一条 Fake 接管通知。
- 两个新增 PowerShell 脚本语法通过；本机四个常驻依赖 healthy、pgvector 可用、Temporal SERVING/UI HTTP 200；临时测试库为 0。

自检修复了循环导入、类型/格式、旧内存 Ledger 和 Worker 注册契约、拓扑测试门禁隔离、Reviewer 分支兼容与独立验证熔断重投问题。前端目录仍只有预留文件，没有越过 Step 47 初始化前端工程，因此本步没有前端 lint/typecheck/test 入口。
