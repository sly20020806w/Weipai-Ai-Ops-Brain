# Step 29：人工判断与补充信息

本步实现两个独立等待状态：`NEED_HUMAN_JUDGMENT` 用于业务取舍，
`WAITING_INFORMATION` 用于补充机器无法获取的信息。Temporal 发送问题通知、
持久化等待回答信号、计时、重试和恢复；所有状态迁移仍经过 tasks 服务。

## 自行验收

先启动 Docker Desktop，确保本项目 PostgreSQL 和 Temporal 依赖运行。在根目录
PowerShell 执行，每个脚本执行后立即检查退出码：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-human.ps1
if ($LASTEXITCODE -ne 0) { throw '人工问答专项失败' }
.\demo-human.ps1 -Interactive
if ($LASTEXITCODE -ne 0) { throw '人工问答演示失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
. .\use-local-temporal.ps1
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库回归失败' }
```

专项输出 `44 passed` 和“Step 29 人工判断与补充信息验收全部通过”。
交互演示先展示业务判断问题，再展示补充信息问题；各输入一段非空回答。
你应看到：

1. 对应的独立等待状态和 Fake 飞书问题卡片。
2. 你输入的回答原文、回答 Evidence ID、引用当前任务的 Knowledge 草稿 ID。
3. 回答后任务恢复，继续主 Agent 调查、Reviewer 复核与 Action Plan，最终停在
   `WAITING_APPROVAL`；随后输出“Step 29 人工判断与补充信息 Fake 演示全部通过”。

不传 `-Interactive` 时用固定 Fake 回答自动演示两个场景。脚本自动启动隔离 Worker，
使用本机独立临时库，结束后清理库和演示运行任务；无需手动启动 API、Worker 或提供公司凭证。
演示 ID 供运行当时核对，临时库清理后不可再查询这些草稿。

若本地容器没有启动，可按现有部署说明加载本地环境并启动：

```powershell
. .\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d --wait
.\check-deps.ps1
```

依赖恢复的完整说明见 [本地部署说明](../deploy/README.md)。

## 问答契约

`WorkflowInput.human_questions` 是由任务宿主提供的问题列表。当前每种状态最多一次：
补充信息在 CONTEXT_BUILDING 提问，业务判断在 INVESTIGATING 提问；回答后恢复原阶段。
实际问题正文由宿主提供，不由缺失的信息自行猜测。两类问题可以同一任务顺序出现。
原 `waits` / `human_response` 保留历史占位验收兼容，不能与正式问答混用。

问题 ID 由任务 ID、等待状态和状态版本确定。卡片携带同一身份，回答只能通过
`AITaskWorkflow.answer_question` 的 `HumanAnswer` 信号提交，包含：

| 字段 | 含义 |
| --- | --- |
| question_id | 当前问题的规范 UUID |
| wait_status / wait_version | 当前独立等待状态和版本 |
| answer | 1–8000 字、非空回答原文 |
| respondent | 1–200 字、非空操作人标识 |

错误身份、旧版本、空白、重复信号不恢复任务；同一问题只接受首个有效回答。
布尔信号不能恢复正式问答，问答信号也不能批准动作。超时转 ESCALATED；通知或回答
Activity 重试耗尽同样转人工，不执行处置动作。计时和重试均由 Temporal 完成。

## 证据、审计与知识

`human.question` 保存问题、卡片、发送确认与恢复阶段；`human.answer` 保存问题和回答
原文、操作人及任务引用。两者都进入既有只追加 Ledger，并追加独立
`human_interaction` 审计，不冒充审批记录。

回答 Evidence、`human_knowledge_drafts` 中的 `draft` 条目及回答审计同事务提交。
草稿保存 task_id、等待身份、问题、回答、操作人和 answer_evidence_id；复合外键保证
回答证据来自同一任务。草稿独立于正式 `knowledge_entries`，不进入正式知识语义检索，
不需要调用 embedding 网关。后续审核与 API/页面按计划实现。

恢复后的主 Agent 从同任务的已提交回答证据读取背景，重试使用同一上下文。
回答仅补充调查背景，不能授予生产权限，结论仍要引用成功 Tool 查询产生的真实证据。

任务行锁、等待版本和唯一约束防并发重复。提交后丢响应复用既有 Evidence 和草稿；
问题内容或回答冲突会被拒绝。通知使用固定 notification_id，复用飞书 Connector 的
幂等发送协议。发送后事务失败时以同 ID 重试，Fake 验证只收到一张卡片。

## 迁移与运行边界

新 head 是 `0011_human_interaction`，新增 Knowledge 草稿表，并扩展审计类型及其字段长度。
本机应用库升级命令：

```powershell
. .\use-local-db.ps1
. .\scripts\project.ps1
$projectUv = Get-ProjectUv
& $projectUv run --frozen --directory backend alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw '迁移失败' }
& $projectUv run --frozen --directory backend alembic check
if ($LASTEXITCODE -ne 0) { throw '模型与迁移不一致' }
```

空库支持完整升降级；已产生人工问答审计时，降级到旧版会明确拒绝，以保留只追加历史。
不能靠删除审计完成降级。

本步沿用 local/test + Fake Worker，没有访问生产系统或真实飞书，也没有新增 Agent
飞书 Tool、审批流、Executor 或前端工程。真实飞书回答回调的鉴权 HTTP 入口留给计划
Step 43/44；当前可验收入口是 Temporal 信号和本机交互演示。原长版设计文件仍缺失，
依据已完整读取的 AGENTS.md、SPEC.md、plans.md 中本步明确要求实现。

## 自检记录

44 项专项包括 28 项禁真实网络单测、12 项 PostgreSQL 和 4 项本机 Temporal。
覆盖身份校验、并发去重、卡片快照、回答上下文、事务回滚、发送后重试、提交后丢响应、
Worker 停止期间回答、重启恢复、超时、数据库/Workflow 状态历史一致与历史回放。
两个实际 Fake 主 Agent 演示和自输入中文回答的交互入口通过；本机应用库已升级，metadata 一致。
暂停前适用 Runbook 的恢复场景通过，快照仅允许绑定紧邻本次回答的精确阶段版本。
最终统一检查 `1607 passed, 328 skipped`，ruff、格式、mypy（279 个源文件）、
Connector 边界与 Git 检查通过。完整数据库/Temporal 回归 `314 passed, 3 skipped`，
新增 Runbook 问答恢复场景在最终 44 项专项中通过；旧 Workflow 36 项兼容检查通过。
跳过的依赖测试由专项入口执行；3 项既有定时驱动时间跳跃测试沿用独立入口。
PowerShell 语法检查通过。自检修复了审计约束命名/字段长度、旧类别断言、共享测试库的
知识计数断言、交互标准输入、Runbook 阶段绑定及类型/格式问题。
没有新增依赖，前端工程仍按 Step 47 预留，本步不执行前端 lint/typecheck/test。
