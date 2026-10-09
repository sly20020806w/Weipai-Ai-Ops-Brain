# Step 25：Runbook Engine 与自行验收

本次完整阅读 `AGENTS.md`、`SPEC.md` 和实际开发计划 `plans.md`，只实施首个未完成项 Step 25。
目录没有 `plan.md`，也没有被引用的长版 V1.0 原设计；沿用既有交付记录，以用户指定的
`SPEC.md` 中明确的 Runbook 要求实施。原设计第 13 节尚无法逐字核对，这一来源限制保留。

## 实现与边界

`app/runbooks/` 实现严格内容模型、PostgreSQL 表、pgvector 检索和 Temporal 匹配 Activity。
`app/tools/runbooks.py` 注册 L0 的 `search_runbooks`，调用、Policy、证据、审计和历史回放
均复用现有唯一 Dispatcher。向量只通过既有公司 AI 网关客户端生成；本地验收使用
显式 Fake embedding，三个关键词方向为支付、网络、容量，仅用于演示与测试。

Runbook 的 15 个内容字段全部必须显式提供，没有缺省内容：

| 字段 | 内容与约束 |
| --- | --- |
| `name` | 英文标识，唯一 |
| `description` | 非空诊断说明 |
| `source` | 非空知识来源 |
| `applicability_conditions` | 至少一条适用条件，必须全部满足 |
| `exclusion_conditions` | 显式提供，可为空；任一条件命中即退出 |
| `diagnostic_steps` | 至少一个有描述、Tool 名、参数和 L0 声明的诊断步骤 |
| `handling_steps` | 至少一个有描述和风险等级的处理方案步骤 |
| `risk_level` | L0–L5，不得低于任一处理步骤 |
| `rollback_plan` | 非空回滚方案 |
| `verification_steps` | 至少一项独立验证要求 |
| `success_count` / `failure_count` | 两个独立的非负整数 |
| `confidence` | 有限数值，范围 0–1 |
| `automation_level` | manual / semi_automated / approval_automated / self_healing |
| `maturity` | draft → reviewed → verified → semi_automated → approval_automated → self_healing |

UUID、UTC `created_at/updated_at`、向量、模型标识和维度由服务生成；保存完整内容与向量
使用同一事务。修改条件或步骤也会重算向量，生成失败不修改旧记录。检索在 PostgreSQL
内计算精确余弦相似度，只比较相同模型和维度的向量；相似度相同时按可信度和 UUID 排序。
公开 CRUD 服务为后续 API 预留，当前没有 Runbook HTTP API 或页面。

条件只接受明确的 `service_name`、`title`、`task_source` 事实字段，以及区分大小写的
`equals` 和 `contains` 运算。任务来源从数据库读取；服务和标题来自已经校验的调查输入。
未知字段或运算保存时拒绝，不解释自然语言、不执行表达式。先检查排除条件，再检查全部
适用条件；Draft/Reviewed 可检索但不作为已验证诊断流程执行。成熟度和计数的自动更新、
晋降级与 Policy 联动留给 Step 35。

启用 Step 24 主 Agent 的任务，在 `RUNBOOK_MATCHING` 先经 Dispatcher 检索，然后判定
条件；匹配成功将完整 Runbook 内容保存为只追加的 `runbook.match` Evidence 快照。
进入 `INVESTIGATING` 后按其 L0 诊断顺序查询，再由主 Agent 基于实际 Evidence 形成结论。
不适用、排除命中、未验证或没有候选时进入 `INVESTIGATING` 自主调查。检索被 Policy
拒绝或失败则保留审计并进入 `ESCALATED`，不能绕过该拒绝继续调查。

诊断参数仅支持顶层固定占位值 `$service_name`、`$start`、`$end`、`$lookback_seconds`。
实际 Tool 风险仍以注册声明和 Dispatcher 判定为准。每个诊断查询和 LLM 轮次共用主
Agent 最大步数预算；诊断被拒、失败或预算耗尽转人工。结论仍逐条验证同任务真实证据
与成功调用审计，处理方案不能成为已执行、已修复的证据。

匹配版本、规范化输入哈希和行锁保证并发/提交后丢响应只检索一次；之后使用已提交
快照，Runbook 被修改或删除也不会更换在途诊断步骤。调查请求不能省略、篡改匹配结果，
诊断观察复用原有持久化检查点。Temporal `runbook-engine-v1` patch 保持旧历史可回放。

工程契约要求真实 Verifier、审计和 Executor 就绪后才能实现 L1+ 运维操作，本步按顺序
执行只读诊断。处理、回滚和验证方案完整保存，后续由 Reviewer、Action Plan、审批、
Executor 与独立 Verifier 接入。调查结束仍为 `RCA → WAITING_INFORMATION`。
没有进入 Step 26，没有生产访问或新增依赖，前端工程仍按 Step 47 预留。

## 你可以复制执行的检查

先启动 Docker Desktop 的 Linux 引擎。当前项目四个依赖容器已 healthy；若已停止，
从根目录执行以下恢复命令，首次配置见 `deploy/README.md`：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
. .\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d
if ($LASTEXITCODE -ne 0) { throw '本地依赖启动失败' }
```

正常验收无需手动启动 API 或 Worker，也无需公司凭证：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
.\check-runbooks.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 25 专项失败' }
.\demo-runbooks.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 25 演示失败' }
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库回归失败' }
```

- 统一检查：ruff、格式、mypy、Connector 边界与 Git 环境文件检查通过；
  `1498 passed, 254 skipped`，最后为「统一检查全部通过」。跳过的依赖测试由专项入口运行。
- Step 25 专项：`52 passed`，最后为「临时测试库已清理」和
  「Step 25 Runbook Engine 验收全部通过」。其中 36 项离线测试禁止真实网络，
  12 项 PostgreSQL 测试和 4 项本机 Temporal 测试实际运行。
- 演示：15 个内容字段逐一缺失均拒绝；输出 applicable、excluded、empty 三个场景，
  每个场景带 Task/Workflow/Search Evidence/匹配 Evidence/结论 Evidence ID。
  applicable 使用 4 个诊断查询和 1 次 LLM；excluded 和 empty 使用自主调查的
  4 次查询和 5 次 LLM。最终均为 `WAITING_INFORMATION`。
  看到「Step 25 Runbook Fake 演示全部通过」和「临时测试库已清理」即可确认。
- 三场景均先 `search_runbooks`，再查询服务上下文、近期变更、指标、日志；
  结论四个引用逐条读库核对，Tool 原证据回放和 Temporal 历史回放通过。
- 数据库回归：`224 passed, 20 skipped`；跳过的 Temporal 项按原有专项入口运行，
  本步 4 项 Temporal 已在 Runbook 专项实际通过。既有主 Agent `34 passed`、
  占位 Workflow `36 passed`；新迁移全链路升降级和 metadata 检查通过。

专项和演示新建独立临时库与随机队列，退出清理运行任务和临时库，不写入应用库样例。
Temporal 历史按源系统保留规则保存，可打开 http://127.0.0.1:8080，选择 `default`，
搜索演示打印的 `ai-task-...` ID；清理后的执行状态为 Terminated，历史中仍可看到
`runbook.match` 和诊断 Activity、RCA、等待 Timer，数据库临时证据已随测试库删除。

## 本地应用库

迁移 `0010_runbook_engine` 是 Step 25 的新增 head。如在另一机器或恢复旧应用库：

```powershell
. .\use-local-db.ps1
. .\scripts\project.ps1
$projectUv = Get-ProjectUv
& $projectUv run --frozen --directory backend alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw '应用库迁移失败' }
& $projectUv run --frozen --directory backend alembic check
if ($LASTEXITCODE -ne 0) { throw '模型与数据库不一致' }
```

启用新事件的调查仍使用 `AGENT_CONFIG={"enabled":true,"max_steps":20}`。
Worker 继续只允许 local/test、Fake Connector/Fake LLM 和本机 Temporal。
本步修复了静态类型/格式、新测试遗漏 Replay 截止点，以及调查请求省略已匹配快照的缺口。

本机应用库已升级到 `0010_runbook_engine (head)`，`alembic check` 返回无新迁移差异；
应用库 Runbook 表为 0 条，遗留 `weipai_db_test_*` 临时库为 0 个。Step 25 已在计划中标记完成。
