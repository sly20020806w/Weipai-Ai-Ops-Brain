# Step 35：Runbook 成熟度

本步依据 `AGENTS.md`、`SPEC.md`、`plans.md` Step 35，以及长版设计第 13、16、18、20、21 节。
只实现 Runbook 的人工审核、验证统计、可信度、晋降级和 Policy 输入。

## 默认规则

成熟度按 Draft → Reviewed → Verified → Semi-Automated → Approval-Automated → Self-Healing
逐级演进，每条新验证结果至多晋一级。

| 目标阶段 | 条件 | 自动化等级 |
| --- | --- | --- |
| Draft | 新复盘草稿、内容修改、人工拒绝 | manual |
| Reviewed | 当前内容版本已由本人审核 | manual |
| Verified | 至少 3 次独立验证成功，可信度 ≥ 0.8 | manual |
| Semi-Automated | 至少 5 次成功，可信度 ≥ 0.8 | semi_automated |
| Approval-Automated | 至少 10 次成功，可信度 ≥ 0.8 | approval_automated |
| Self-Healing | 至少 20 次成功，可信度 ≥ 0.95 | self_healing |

可信度采用带保守先验的成功比例：`(成功数 + 1) / (成功数 + 失败数 + 2)`。
初始值为 0.5，3 次成功为 0.8，20 次成功为 21/22，约 0.9545。
达到次数只是条件之一，必须同时具有当前版本的人工审核和足够可信度。
审核本身只把 Draft 提升为 Reviewed，不会伪造成功或越级放行。

首次失败立即把 Self-Healing 降到 Approval-Automated；可信度低于普通门槛时退回 Reviewed。
连续 2 次失败退回 Reviewed 并撤销人工审核，需要再次审核才可晋级。
成功会清零连续失败数，历史成功／失败总数继续保留。既有任务熔断器继续独立生效。

Reviewed 可匹配并试用已有 L0 诊断步骤，用于积累实际验证结果。Draft 仍不匹配；
Reviewed 的处理方案仍经 Reviewer、Action Plan、Policy、审批与 Executor，不能直接执行。
所有写动作未达到可信 Self-Healing 时需要审批；达到后还必须命中明确的 Policy allow。
L3–L5 即使来自 Self-Healing 仍需审批，deny 和 need_approval 规则不能被成熟度覆盖。
本项目首批 Executor 动作至少为 L3，因此成熟度不会开放无审批的生产回滚。

## 证据和版本

审核入口为内部 `RunbookLifecycle.review`，操作人由宿主调用方传入，使用 source=Human 的
任务承载，记录 `runbook.review`、`runbook.lifecycle` Evidence 和人工交互审计。
它不注册为 Agent Tool。鉴权、审核 API 和页面仍按后续开发步骤实现。

成功统计只接受独立 Verifier 当前调用范围内的 `verify_action` 结果，校验八项同任务事实与
live 调用审计。失败包含完整事实证明未恢复，以及本轮 Runbook L0 诊断实际执行失败；
诊断失败绑定 `agent.observe` 和同任务的 failed/live 调用审计。缺数据、模型错误或查询
被 Policy 拒绝不算 Runbook 失败。
匹配必须属于本轮调查，且选中快照须确实出现在同任务的原 `search_runbooks` 成功结果中。
反证或验证失败后的自主调查不把结果归功于旧 Runbook。

同一任务、同一 Runbook 内容版本只累计首次有效试用结果一次。
重复观测、并发、提交后丢响应、Activity 重试、Worker 重启、Dispatcher Replay
和 Temporal 历史回放都不会重复累计。不同任务在同一 Runbook 行锁下串行更新统计。
验证事实、成熟度 Evidence、统计、任务状态、状态历史和审计在同一事务中提交；
成熟度审计失败时整次验证回滚。

新迁移 `0013_runbook_maturity` 为 Runbook 增加正整数 `content_version`。
内容、条件、诊断／处理／验证／回滚方案变动时递增版本，清零当前版本计数，回到
manual/Draft，可信度为 0.5。把内容改回原文也会产生新版本，不能复用旧审核。
旧记录留在只追加 Ledger，不存环境配置对象或密钥，只存判据指纹。
仅修改计数、可信度、成熟度、自动化等级的普通 CRUD 更新被拒绝。
已有成熟度记录时迁移拒绝丢失内容版本的降级；空库仍可完整升降级。

历史导入的成熟度标签和计数不构成自动执行资格，Policy 以宿主校验的审核和验证记录为准。
规划把当前成熟度证据绑定进完整动作哈希，审批与 Executor 每次签发动作凭证前重新检查；
内容、审核、成熟度或判据变化后原计划失效，须重新规划和审批。

## 配置

配置只从环境变量 `RUNBOOK_MATURITY_CONFIG` 读取，默认如下：

```powershell
$env:RUNBOOK_MATURITY_CONFIG = '{"verified_successes":3,"semi_automated_successes":5,"approval_automated_successes":10,"self_healing_successes":20,"consecutive_failure_limit":2,"min_confidence":0.8,"self_healing_confidence":0.95}'
```

四个成功阈值须严格递增；连续失败阈值须为正整数，可信度门槛须有效，
Self-Healing 的可信度不能低于普通晋级门槛。无效环境配置在启动时拒绝。
修改规则后旧信任上下文失效，不会因为降低阈值就自动批准历史计划。

## 自行验收

启动 Docker Desktop，并确保本项目 PostgreSQL、Temporal、Temporal UI、admin 容器运行。
若已有容器停止，先按 `deploy/README.md` 恢复；首次安装仍按该文档准备依赖。
在仓库根目录 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-maturity.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 35 专项失败' }
.\demo-maturity.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 35 演示失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
. .\use-local-temporal.ps1
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库和 Temporal 回归失败' }
```

专项覆盖默认阈值、风险矩阵、实际独立验证、原子回滚、修改／回退／删除、审核冲突、
旧审批零签发零执行、回放不计数、Worker 重启及真实主 Workflow 闭环。
演示须显示 Draft、Reviewed，以及成功 3／5／10／20 次后的四级晋升，打印真实 Evidence ID；
最后两次失败退回 reviewed，成功 20／失败 2，提示重新审核。
末尾须显示「Step 35 Runbook 成熟度 Fake 演示全部通过」和「临时测试库已清理」。
演示中 L1 明确 allow 仅在 Self-Healing 时生效；L3 始终 need_approval。
第 20 个试用任务实际由 Temporal 执行并通过历史回放，输出 Workflow ID，可在
[本地 Temporal UI](http://127.0.0.1:8080) 的 default 命名空间查询。
演示试用没有运维写入；专项另有一次经真实 Fake 审批的回滚闭环。
脚本创建并自动清理独立临时库和隔离队列，不需要手动启动 API、Worker 或提供公司凭证。

若启动正常本地 Worker，应用库需先升级：

```powershell
. .\use-local-db.ps1
. .\scripts\project.ps1
$projectUv = Get-ProjectUv
& $projectUv run --frozen --directory backend alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw '应用库升级失败' }
& $projectUv run --frozen --directory backend alembic check
```

完整回归中的空库降级只发生在专用临时库，不对应用库执行降级。
前端目前仅有占位目录，按 Step 47 建工程；本步验收通过 PowerShell 完成。

## 自检记录

2026-10-07 最终自检：

- 成熟度专项：`64 passed`，46 项禁止真实网络的单测、18 项本机 PostgreSQL/Temporal 集成。
- 统一检查：ruff、格式、mypy（335 个源文件）、Connector 导入边界、Git 环境检查全部通过；
  `1779 passed, 433 skipped`。依赖测试通过专项和数据库入口实际执行。
- 完整数据库/Temporal 回归：`420 passed, 3 skipped`。三项跳过为既有定时驱动时间跳跃测试，
  不属于本步新增测试；空库全链路升降级、metadata 检查通过。
- 最终默认阈值演示：3/5/10/20 次成功依次晋级、20/2 成功失败计数与连续失败降级通过；
  第 20 个试用由 Temporal 执行，历史回放通过，L3 保留审批，演示实际运维动作次数为 0。
- 主链路专项实际使用 Reviewed Runbook，经主 Agent、Reviewer、规划、审批、Fake 回滚、
  独立验证、复盘到 CLOSED，原 Runbook 成功计数恰好加 1；回滚恰好执行 1 次。
- 应用库已升级到 `0013_runbook_maturity (head)`，`alembic check` 无差异。
- 新增 PowerShell 脚本语法通过；临时测试库为 0，本步隔离运行任务已清理。

自检修复了类型与格式、诊断配置引用、主闭环终点和停止 Worker 后的清理方式，
并补强跨任务检索归因、自主调查归因、内容回退和环境配置隔离。
没有新增依赖、中间件或真实生产访问。`plans.md` 仅将 Step 35 改为完成，Step 36 保持未开始。
