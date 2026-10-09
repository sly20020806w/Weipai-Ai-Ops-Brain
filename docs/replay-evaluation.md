# Step 36：Replay 与 AI 评价

依据 AGENTS.md、SPEC.md、权威原设计第 31、32 节和 plans.md，仅实现历史回放与能力评价。没有进入 Step 37。

## 回放边界

`ReplayEvaluationWorkflow` 注册在现有单体 Worker 中。Temporal 负责 Activity 重试和恢复，原事故始终保持 CLOSED。回放请求绑定 run_id、事故任务、基线结论 Evidence ID、UTC 截止点、历史调查规格和候选版本说明。同一 run_id 更改任何输入均拒绝。

调查规格必须与历史 Runbook 匹配检查点的哈希一致。截止点不能晚于基线 RCA 入库时间。Tool 结果必须同时满足：同一任务、同名 Tool、规范化参数完全相同、采集与入库时间均不晚于截止点、有当时已入库的 live 成功审计。重复参数取截止点前最后一份可用快照；返回当时的 Evidence ID 和结果，freshness 不按今天重算。

注册表只提供既有只读 schema，没有 Connector 实例、写 Tool 或可执行的 live 实现。所有观察仍经过唯一 Dispatcher 的 REPLAY 入口，遵守当前 Policy 与 schema。缺历史查询、Policy 拒绝、schema 不兼容、引用伪造或预算超限均生成 incomplete 报告，不补查真实系统，不假装根因已命中。

复用主 Agent 的 Think→Plan→Tool→Observe→Reason 引擎。宿主将成功回放观察映射为调查引擎可用的 succeeded；持久化观察及审计保留 replayed。专家咨询也只能读取已存在的咨询快照，不重新运行专家或 Holmes Connector。

每轮模型响应、观察、回放起点和最终报告只追加到 Evidence Ledger。检查点绑定 run_id、步号、完整输入指纹与本轮请求指纹。并发、提交后丢响应和 Worker 恢复复用检查点，不重复已提交调用。候选每次调用的结果必须引用本次确实观察到的历史 Evidence ID，原任务状态和既有证据不改变。

基线 RCA、后续 Reviewer/执行/Verifier/复盘以及人工基准不作为候选调查结果提供。仅历史阶段已可用的 Runbook、人工补充和紧邻上一轮 Reviewer 反证可以作为背景。根因评分只在调查结束后读取人工标注；明确的等价根因表述进行空白规范化与大小写无关匹配，不使用被评模型自行评分。没有标注或回放未完成时，命中/误判为 null。

## 10 项指标的口径

`EvaluationService.report` 从本库任务、状态历史、证据和审计计算，不访问源系统。窗口采用任务创建时间 `[start, end)`，只纳入在 end 前已结束的任务（CLOSED、FAILED、ESCALATED、AUTOMATION_ABORTED）。end 后的证据/审计不参与，复盘创建的 learning 改进任务不作为第二次事故样本。回放审计和检查点不进入运行指标。

| 指标 | 分子 | 分母/口径 |
| --- | --- | --- |
| RCA 命中率 | 与人工根因基准一致的最新 RCA 数 | 有 RCA 且有根因标注的任务数 |
| Runbook 命中率 | 至少一次检索后验证适用且未被阻断的任务数 | 有 Runbook 匹配检查点的任务数 |
| 自动处理成功率 | Executor 已执行、独立验证恢复且没有接管的任务数 | 已实际尝试执行的任务数，含 Tool 执行失败；Policy 拒绝不算执行 |
| 审批拒绝率 | rejected 的有效审批决定数 | approved/rejected 的有效决定数；按决定 Evidence ID 去重，超时不视作拒绝 |
| 人工接管率 | 曾到 ESCALATED 或 AUTOMATION_ABORTED 的任务数 | 已结束任务数；补充信息/业务判断不直接视为接管 |
| 误报率 | 人工标注 false_alert=true 的 Alert 数 | 有误报标注的 Alert 数 |
| 平均 MTTR | 从事件发生（无事件则 NEW 时间）到首次独立恢复的秒数之和 | 曾 RESOLVED 的任务数；未恢复不按零秒纳入 |
| 平均 Tool Call | 已结束任务的 live Tool 调用数之和 | 已结束任务数；含查询、专家、Reviewer、执行及验证，成功/失败/拒绝均是一次调用 |
| 验证失败率 | 未通过的独立 Verifier 阶段数 | 独立 Verifier 阶段数；按验证状态版本去重，排除 Agent 的只读验证报告 |
| 自动化覆盖率 | 独立恢复且无业务判断、补充信息或接管的任务数 | 已结束任务数；只需批准后由平台执行验证的任务仍属于审批自动化 |

每项返回 name、中文名称、分子、分母、值和单位；比率使用 0–1，MTTR 使用秒。零分母返回 null。样本附 Task ID、Evidence ID 与状态历史 ID，便于核查。未标注根因/误报不自动当作正确或正常，分母清楚反映已标注样本覆盖。

人工基准通过 `EvaluationService.label` 留证，必须带操作人并引用同任务 Evidence，与人工交互审计原子提交。再次标注会追加新版本；报表按截止时间取最新已审计版本，已保存回放报告保留原评分基准，不会被后续标注改写。应用规则和网关密钥仍只从环境变量读取，不存数据库。

## 自己运行验收

在 PowerShell 中启动 Docker Desktop，确认使用 Linux 引擎。现有容器配置和密钥由项目脚本加载，命令不显示密码：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
. .\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw '本机依赖启动失败' }
.\check-replay.ps1
if ($LASTEXITCODE -ne 0) { throw 'Replay 专项失败' }
.\demo-replay.ps1
if ($LASTEXITCODE -ne 0) { throw 'Replay 演示失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

专项验收创建并最终删除 `weipai_db_test_*` 独立数据库，全源系统与 LLM 为 Fake；自动覆盖十项手算指标、已关闭事故回放、live 实现调用为零、截止点/跨任务/规格拒绝、缺历史/伪造引用/预算超限、并发及检查点恢复、原状态不变、运行指标不受回放影响、Temporal 历史回放。

演示应显示：原事故 CLOSED；Replay Workflow ID；基线和候选均命中、误判 false；基线与候选各 4 次调查 Tool；4 个历史 Evidence ID；回放期间 Connector 调用 0；10 项指标及分子/分母；末尾“Step 36 Replay 与 AI 评价 Fake 演示全部通过”和“临时测试库已清理”。单个演示事故没有拒绝/失败样本，对应比率为 0；它用于确认链路，十项非零/混合手算场景由专项测试验证。

统一检查执行后端 ruff、格式、mypy、pytest、Connector 导入边界和 Git 环境文件检查。frontend 当前只有 `.gitkeep`，前端工程与 lint/typecheck/test 按计划 Step 47 建立，本步没有提前创建。

完整数据库回归：

```powershell
. .\use-local-temporal.ps1
.\check-db.ps1
```

## 使用限制

历史没有采集过的事实无法回放。升级 Tool schema 若不兼容旧快照，本次报告明确失败，不能以今天的事实替代。当前 registry 对应已实现的高级调查 Tool schema；早期 Step 11 的同名运维平台 get_service_context 旧 schema 不会伪装为当前图查询 schema。

候选版本名称是操作者提供的版本说明；实际模型仍来自公司网关环境配置或 Fake，不能把版本名称当作已验证的真实模型身份。比较报告包含根因、命中、误判、Tool 次数和耗时，不自动改变 Policy 或晋级 Runbook。

基线耗时从该 RCA 阶段首个调查检查点入库到 RCA 入库；候选耗时从回放起点到报告生成，含恢复等待。两者运行环境不同，缓存/数据库/网络耗时也不同，不能单凭这两个数认定模型性能变好。MTTR 是业务事件恢复时间，与回放耗时分开计算。API/前端指标展示按后续步骤实现。

Fake 事故使用固定的历史事件时间，因此演示 MTTR 可能较大；它表示从该历史事件到本次 Fake 恢复的时间，回放本身的耗时单独显示。

## 2026-10-07 自检记录

- `check-replay.ps1`：47 passed，含 24 项禁真实网络单测、14 项 PostgreSQL/Temporal 回放场景和 9 项既有专家/Reviewer/Executor/成熟度回放兼容。
- `check.ps1`：1803 passed、447 skipped；集成测试由独立入口执行，ruff、格式、mypy（345 个源文件）、Connector 边界和 Git 检查全部通过。
- 加载本机 Temporal 后 `check-db.ps1`：434 passed、3 skipped。3 项是既有定时驱动的官方时间跳跃测试，需要原有 `check-schedules.ps1` 独立入口，本步没有改动调度实现。
- `demo-replay.ps1` 实际运行成功：CLOSED 事故、基线/候选根因均命中、误判 false、各 4 次调查查询、4 个历史 Evidence ID、回放 Connector 调用 0、完整 10 项指标和临时库清理。
- 两个新增 PowerShell 入口语法通过。本机四个依赖 healthy、Temporal SERVING、UI/命名空间 API HTTP 200，应用库 `0013_runbook_maturity (head)`、Alembic metadata 检查无待迁移变更。

修复了自检发现的测试 handler 计数替身、恢复测试对 Runbook 的假设，以及采集时间早于入库/审计时间的旧回放截止点。Executor 回执回读只调整截止点到完整留证之后。没有增加生产执行能力。Docker 启动时的两处不可访问 runtime socket 目录保留为恢复副本后正常启动，WSL 与数据库卷数据保留。
