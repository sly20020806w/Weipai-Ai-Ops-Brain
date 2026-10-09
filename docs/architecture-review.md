# Step 41：架构评审

本步完整读取 AGENTS.md、SPEC.md、开发计划实际文件 plans.md，以及它们指向的权威
《Weipai AI Ops Brain 最终设计方案 V1.0》。按设计第 27 节实现架构评审，只做 Step 41。

## 自己运行一遍

启动 Docker Desktop，确认本项目 PostgreSQL 和 Temporal 已运行，然后打开 PowerShell：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
powershell -NoProfile -ExecutionPolicy Bypass -File .\check-architecture.ps1
if ($LASTEXITCODE -ne 0) { throw '架构评审专项失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-architecture.ps1
if ($LASTEXITCODE -ne 0) { throw '架构评审演示失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

脚本从现有本机容器读取数据库配置到进程环境，不显示密码、不写配置文件；自动建立临时
数据库、应用现有迁移并运行隔离 Worker。无需启动 API、提供公司凭证或连接真实系统。
专项应显示 `52 passed`，末尾显示“Step 41 架构评审验收全部通过”和“临时测试库已清理”。

演示提交的方案是：支付服务部署在 ACK，支付数据写入单点数据库 payment-db，尚未设计
备库和故障切换。Fake 公司规范要求消除核心数据库单点，设置跨可用区备库、自动故障
切换和恢复演练。演示应输出：

- 全部 **12** 个维度；稳定性、高可用均为 `risk`，指出单点数据库并引用方案和公司规范
  的真实 Evidence ID、来源 Tool 和原文摘录。
- 其他 **10** 个维度为 `unknown`，列出需要补充的材料。材料不足不会被包装成通过。
- Context Graph、规范和历史事故检索都经过 Dispatcher；演示没有历史事故时保留空检索
  结果，不编造历史。专项另用已闭环的 Fake 事故验证历史经验能够进入报告并被精确引用。
- 重复提交新增任务 **0**，实际运维动作 **0**，任务最终 `CLOSED`。
- 真实 `ai-task-...` Workflow ID、报告 Evidence ID、“Temporal 历史回放：通过”。
- “Step 41 架构评审 Fake 演示全部通过”“临时测试库已清理”。

完整数据库/Temporal 回归可在同一窗口执行：

```powershell
. .\use-local-temporal.ps1
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库与 Temporal 回归失败' }
```

## 统一任务链路

```text
技术方案 → manual/Human OpsEvent → AI Task → AITaskWorkflow
→ CONTEXT_BUILDING → RUNBOOK_MATCHING → INVESTIGATING
→ search_runbooks + 适用/排除核对
→ get_service_context → search_knowledge/公司规范 → search_incidents/历史故障
→ 公司网关或显式 Fake 生成十二维结构化报告 → RCA
→ PLANNING/EXECUTING 仅交付平台内部报告 → VERIFYING
→ 独立 Verifier 核验覆盖和证据 → RESOLVED → LEARNING → CLOSED
```

所有状态只由 tasks 服务迁移，暂停、重试和生命周期由 Temporal 管理。没有新增队列或
状态枚举。报告中的建议不生成生产 Action，评审完成表示报告已交付和留证，不授予发布
权限，也不表示方案风险已经消除。后续整改仍必须走既有 Reviewer、Action Plan、Policy、
审批、Executor 和独立 Verifier。

十二维固定为：稳定性、高可用、容量、Kubernetes、云资源、网络、存储、安全、成本、
运维复杂度、可观测性、发布和回滚。每个维度有 risk/supported/unknown、结论、建议以及
Evidence ID 和原文摘录；缺维度、重复、乱序、空引用、伪造/跨任务引用、错误摘录、截断/
拒绝响应或模型试图调用 Tool 均被拒绝。

## 数据、权限与复用

`tasks/architecture/service.py` 的 `submit_review` 是内部提交入口，必须在事务中调用；
`ReviewSubmission` 包含 UUID 请求身份、已发现的服务名、标题和至多 20000 字的方案。
由 EventService 创建归一化事件与任务，输入保存为只追加的 `architecture.submission`。
同一请求身份重复提交复用原任务，修改方案或服务必须使用新请求身份。

事件派发识别 `architecture-review:` 身份，进入现有统一 Workflow。所需服务尚未进入
Context Graph 时查询失败，任务转 ESCALATED，需要先完成 Discovery；不会用 Fake 补充
未知线上事实。HTTP API 和控制台页面仍按 Step 44/45/47/51 实现。

复用现有图查询（含每条边的 source/confidence/freshness）、KnowledgeService 的 pgvector
检索与 UTC 有效期，以及 search_incidents。新 `search_knowledge` 声明 L0，只查询
`standard` 类型的有效公司规范，入出参为严格 Tool schema，支持原快照 Dispatcher Replay。
查询注册表只包含评审所需的四类 L0 查询，不打开外部运维 Connector 或写客户端。
LLM 复用公司网关客户端，模型和凭证仍由环境变量配置。

方案、知识、图和事故正文均视为数据，不能更改宿主指令。系统提示要求区分计划设计和
当前环境，考虑图的新鲜度与置信度，历史事故仅作经验，不直接证明新方案会复现事故。
独立 Verifier 核验的是报告结构和来源追踪；真实模型评审意见的语义质量仍须后续评价和
实际材料校核。本步全部运行验收为 Fake，不代表真实公司网关或生产环境已联调。

Fake 只对文档中的明确单点样例输出对应风险，其他方案保持 unknown；它用于可重复验收，
不承担通用方案分析。正式分析使用公司网关；现有 Worker 的 local/test + Fake 门禁保持。

## 事务、重投与独立验证

先在任务行锁下查询并提交 Evidence、成功/失败调用审计和 `architecture.context` 检查点，
再在独立事务中生成报告。模型无效或报告保存失败不抹掉已经发生的查询。重试复用同一
查询快照；成功报告在并发、提交后丢响应和 Worker 重启后复用，不重复查询或生成。
Policy 拒绝保存拒绝审计和 `architecture.blocked`，父 Workflow 转人工。

Verifier 在 L0 `verify_architecture` 下核对输入哈希、任务和阶段版本、全部十二维、五份
来源快照、Runbook 条件核对与查询顺序、真实成功 live 审计以及每条引用摘录。报告引用
必须与已提交上下文一致。只有持当前任务/版本/验证 Evidence 的内部权限才能经 tasks
服务设置 RESOLVED，伪造 actor 字符串无法放行。失败调用也保存审计。

没有新增依赖、中间件或数据库迁移，现有 head 保持 `0014_inspection_risks`。配置和密钥
不入库；方案和报告是业务证据。没有真实生产请求、真实飞书发送或运维写操作。

## 自检记录

2026-10-08 最终自检：

- 专项 `52 passed`：36 项禁止真实网络的离线测试，16 项本机 PostgreSQL/Temporal 测试。
- 统一检查 `1959 passed, 509 skipped`；ruff、格式、mypy（410 个源文件）、Connector
  导入边界与 Git 环境文件检查均通过。依赖测试由独立入口运行。
- 完整数据库/Temporal 回归 `496 passed, 3 skipped`；默认跳过的官方时间跳跃场景由
  定时专项 `20 passed` 覆盖，既有 Workflow 回归 `36 passed`。
- Windows PowerShell 交付命令实际运行，十二维和真实引用完整，稳定性/高可用指出单点；
  其余十维保留待补充，任务 CLOSED、重复提交新增任务 0、实际运维动作 0，历史回放通过。
- 历史故障输入、伪造/跨任务引用、查询拒绝、旧版本、原快照 Replay、并发/提交后丢响应、
  Worker 重启，以及无效模型响应后查询审计仍保留并可复用，均验收通过。
- 新 PowerShell 脚本语法与 UTF-8 BOM 检查通过；临时库自动清理。

自检修复了知识 Tool schema 的基类约束、类型与格式问题、无效模型响应导致源查询审计
回滚的事务边界，以及报告引用与查询检查点的绑定。全部实际验收使用 Fake 和本机依赖。
前端仍只有 `.gitkeep`，lint/typecheck/test 随 Step 47 工程建立。本步已在 plans.md 标为
完成，Step 42 保持未开始。
