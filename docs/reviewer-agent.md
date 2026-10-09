# Step 27：Reviewer Agent

本步依据 AGENTS.md、SPEC.md 的关键任务反证要求与 plans.md Step 27 实现。
计划的实际文件名是 `plans.md`；仓库仍未提供 AGENTS.md 引用的长版设计原文，
本步没有推测其缺失章节，也没有修改 SPEC 或基础状态枚举。

## 复核行为

主 Agent 完成 INVESTIGATING 后进入 RCA，先验证并保存原始结论，再执行独立的
`reviewer.review` Temporal Activity。Reviewer 使用独立对话，接收原结论和其证据快照，
尝试从网络、Redis、其他发布原因、第三方依赖四个方向证伪。

事实只能通过唯一 Dispatcher 查询。宿主固定白名单为 `query_metrics`、`query_logs`、
`query_traces`、`get_recent_changes`，并限制查询到本任务服务和调查时间窗。
越界、未注册或 Policy 拒绝的调用不会执行，仍保留拒绝审计。
Reviewer 不注册为 Agent Tool，不能递归调用专家或执行运维操作。

每个方向都必须提交带独立查询 Evidence ID 的判断。结论引用主 Agent 的证据、
检查点、其他任务或未实际观察的 ID 均会被拒绝。成功判断还核对同任务、当前 RCA
版本的观察清单和 `codex-reviewer` 成功调用审计。

| 复核结果 | 置信度 | Workflow 行为 |
| --- | --- | --- |
| 四个方向均未找到反证 | 原置信度 + 0.1，上限 1 | 保留复核通过结果 |
| 任一方向找到反证 | 原置信度 − 0.2，下限 0 | RCA → INVESTIGATING，向主 Agent 提供反证重新调查 |
| 覆盖不足或证据不充分 | 不提高 | ESCALATED，交人工处理 |
| 查询失败、输出不合法、步数耗尽 | 不生成通过记录 | Temporal 按既有重试规则处理；仍失败则 ESCALATED |

置信度调整是宿主的确定性启发规则，不是统计校准或生产执行授权。
未找到反证只表示查询范围内暂未支持替代原因。Reviewer 不取代主 Agent 给出根因，
也不声称任务已修复。原结论 Evidence 保持只追加、不可修改；调整后的结论和反证
报告写入新的 `reviewer.verdict`，绑定原结论 ID 与 RCA 状态版本。
Workflow 查询中的 `conclusion_json` / `conclusion_evidence_id` 保持原结论与原证据
一一对应；`review_json` / `review_evidence_id` 对应复核结果，调整后的置信度位于
`review_json.conclusion.confidence`，两份快照均可按各自 Evidence ID 精确读回。

最多执行两轮复核：首次反证重新调查，第二轮仍有反证则再次回到 INVESTIGATING
后转 ESCALATED。该编排由 Temporal 管理。`AGENT_CONFIG.reviewer_max_steps`
缺省为 16，范围 1–100，LLM 和每次 Tool 调用分别占一步。

## PLANNING 门禁

`tasks` 服务在任何迁移到 PLANNING 的路径检查复核，而非只在 Workflow 检查。
当前任务没有严重程度字段，因此保守覆盖所有 Alert、Release、Git/CI/ArgoCD/
配置中心/云变更事件，以及所有有真实主 Agent 结论的任务。

只有当前 RCA 的唯一原结论、唯一复核通过记录、最终模型检查点、独立观察清单
及成功调用审计均一致才放行。重新调查后旧复核失效。直接跳转、跨任务、旧结论、
反证或覆盖不足均不能进入 PLANNING，失败不追加状态历史或状态迁移审计。
Step 17 的无真实结论、非关键本地 Human 占位测试仍可检查基础状态表；占位 Worker
沿用 local/test + Fake 与回环地址门禁。

Step 27 交付时复核通过后暂停到 WAITING_INFORMATION。Step 28 接入后，当前版本
进入 PLANNING，生成 Action Plan 并按 Policy 结果暂停；默认回滚方案为
WAITING_APPROVAL。当前行为和验收见 [Step 28 文档](action-plans.md)。

## 持久化与恢复

每个 LLM 响应和 Dispatcher 观察按任务、RCA 版本、步序、规范化请求哈希存入 Ledger。
任务行锁串行化并发尝试。Tool 证据、调用审计与观察检查点同事务，写入失败整组回滚。
复核最终结果按同一版本去重，重试复用已提交的查询和模型响应。
对话中的 JSON 使用固定键排序，避免 PostgreSQL JSONB 键顺序造成重试冲突。
Workflow 使用 `reviewer-agent-v1` patch 保留旧历史回放路径。

没有新增依赖或数据库迁移，head 保持 `0010_runbook_engine`。
Fake 复核使用显式网络/缓存/第三方 Span；反证样例注入真实的 Fake 网络超时和
正常数据库 Span，不靠预先指定模型结论演示。

## 自行验收

先启动 Docker Desktop 的 Linux 引擎和已有本地依赖。无需提供公司凭证，
无需手动启动 API 或 Worker。在项目根目录同一个 PowerShell 窗口运行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-reviewer.ps1
if ($LASTEXITCODE -ne 0) { throw 'Reviewer 专项失败' }
.\demo-reviewer.ps1
if ($LASTEXITCODE -ne 0) { throw 'Reviewer 演示失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库回归失败' }
```

专项须显示“Step 27 Reviewer Agent 验收全部通过”。演示须显示：

- 关键任务跳过 Reviewer → PLANNING 被拒绝。
- 无反证：置信度 0.7 → 0.8，状态 WAITING_INFORMATION。
- 网络超时反证：置信度 0.7 → 0.5，RCA → INVESTIGATING。
- 两轮仍有反证 → ESCALATED。
- 四类检查、实际 Evidence ID 和 Workflow ID。
- “Step 27 Reviewer Fake 演示全部通过”，随后临时测试库已清理。

专项和演示自动启动独立 Worker、使用专用临时库/队列，并清理运行中的演示任务。
真实运维数据与 LLM 全部使用 Fake。无反证场景等待 Step 28 是本步预期行为。
前端仍按 Step 47 预留，本步没有可点击的 Reviewer 页面。

依赖未启动时，在已配置本地容器的情况下运行：

```powershell
.\use-local-deps.ps1
docker compose -f .\deploy\docker-compose.yml up -d --wait
if ($LASTEXITCODE -ne 0) { throw '本地依赖启动失败' }
.\check-deps.ps1
```

首次创建依赖请遵循 deploy/README.md；不要填写生产系统地址。

## 自检记录（2026-10-07）

- 统一检查：ruff、格式、mypy（263 个源文件）、Connector 边界、Git 环境文件全部通过；
  pytest `1549 passed, 296 skipped`，依赖测试由专项执行。
- Reviewer 专项：`40 passed`，含 24 项禁止真实网络的离线、13 项 PostgreSQL、
  3 项本机 Temporal 测试，所有本步验收条件均通过。
- 全量数据库/Temporal 回归：`283 passed, 3 skipped`；3 项为既有定时驱动时间跳跃测试。
- 既有 Workflow 专项：`36 passed`；新增 PowerShell 脚本语法检查通过。
- `demo-reviewer.ps1` 两场景与门禁、真实 ID 读回、数据库/Workflow 状态一致、
  Temporal Replay 实际通过；旧主 Agent 演示通过；临时库和演示运行任务清理完成。
- 自检修复：JSONB 键顺序引起的重试冲突；事件半开窗口增加一微秒时的整秒回溯
  越界；原结论与复核结果各自的 JSON/ID 配对；旧状态表与 Runbook 审计验收兼容。

plans.md 仅将 Step 27 标为完成，Step 28 保持“还没做”。
