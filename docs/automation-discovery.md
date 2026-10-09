# Step 37：Automation Discovery

依据原设计第 30 节和 plans.md Step 37，实现重复工单、故障、Runbook、发布检查、人工操作的统计与自动化建议任务。本步复用现有 PostgreSQL 表、Evidence Ledger、OpsEvent/AI Task 服务和 Temporal；没有新增迁移、依赖或外部访问。

## 统计口径

默认回看最近 30 天，累计 **至少 5 次** 同类劳动时生成建议，每小时由 Temporal Schedule 扫描一次。按服务和明确内容分组，只统一空白与大小写；不会猜测不同工单是否语义等价。

| 类型 | 事实来源与分组依据 | 建议方向 |
| --- | --- | --- |
| 重复工单 | source=Ticket 的 OpsEvent，同服务、同标题；统计已接纳的工单需求 | Workflow 化 |
| 重复故障 | 完整 Postmortem 的根因章节，同服务、同根因；未确认/待核实根因不分组 | 自动 Runbook |
| 重复 Runbook | 有同任务成功 live 检索审计的 runbook.match，同服务、同 Runbook ID/内容版本 | 评估 Self-Healing |
| 重复发布检查 | Step 22 生成的 Schedule/release-verification 事件，同服务；统计上线检查需求，不把普通发布事件当作完成检查 | Workflow 化 |
| 重复人工操作 | manual.operation 证据及匹配的操作人审计，同服务、同操作名称 | 脚本化 |
| 重复人工回答 | 既有 human.answer 及匹配操作人审计，同服务、同问题；不复制回答正文 | 收集/流转的 Workflow 化，保留人工判断 |

一条 Runbook 匹配或故障在同一任务内重复留证只算一次相同劳动。不同内容版本独立统计。人工操作按原记录计数，同一证据重复审计不重复计数。回放、失败审计、来源不一致和 origin=learning 的改进/建议任务不计入；建议不会产生自己的统计样本。

统计窗口是带时区 UTC 的 `[start, end)`。事实的发生/采集时间和本库入库时间都必须早于截止点，关联与成功检索审计也必须当时已经存在。扫描完全读取本库引用与证据，不查询原始日志、指标或外部系统。

## 建议、幂等和安全边界

每组生成一个 `origin=learning, source=AI` 的 OpsEvent，再由既有 EventService 创建 AI Task、NEW 状态历史和任务创建审计。`automation.suggestion` Evidence 与事件/任务同事务保存，包含建议、分组、每条原始记录的表名/ID、来源任务、UTC 时间和可用 Evidence ID。因此建议中的计数可逐条核对；人工操作的建议引用相应审计及其证据。

身份固定为 `automation:<分组 SHA-256>`。同一服务/类型/内容再次达到阈值、并发扫描、提交后丢响应、后续新增记录或滑动窗口移动都复用同一任务和首次建议证据。当前规则为**一个稳定分组只提出一次建议**，首次记录快照不刷新；建议处理后的再次推荐/更新交互由后续运营功能决定。本步不会重复创建同类待办。

`AutomationDiscoveryWorkflow` 负责单轮 Activity 重试和已提交建议的任务派发；重复结果也重试派发，以修复提交后尚未派发的情况。既有 `event.start_task` 用固定 Workflow ID 保证唯一生命周期。`weipai-automation-discovery` Schedule 为 UTC、SKIP 不重叠、失败暂停；重复注册保留已有配置及暂停状态。

所有建议都是评估任务，不授予权限、不改变 Policy 或 Runbook 成熟度、不执行运维动作。由于使用 learning 来源，建议任务进入统一 AITaskWorkflow 后停在 WAITING_INFORMATION，不套用支付事故 Fake RCA。本步的建议事实可从 Evidence Ledger 读回；任务 API/页面按后续 Step 44/45/51 实现。前端工程仍按 Step 47 建立。

`AutomationService.record_manual()` 仅供宿主记录已经发生的人工劳动，不是 Agent Tool，也不会执行该操作。宿主需提供已有任务、稳定 record_key、服务、操作人、操作名称、源系统引用与 UTC 发生时间。相同任务/record_key 重投复用证据；改内容、未来时间、跨服务和建议任务反馈均被拒。真实来源已有记录应由后续场景适配宿主调用，不应让用户为可机器读取的事实重复录入。

## 配置

仅从 `AUTOMATION_CONFIG` 环境变量读取，例如：

```powershell
$env:AUTOMATION_CONFIG = '{"enabled":true,"threshold":5,"lookback_seconds":2592000,"interval_seconds":3600}'
```

还可配置 `schedule_id`、`activity_timeout_seconds` 和 `activity_max_attempts`。阈值至少 2；无效类型、未知字段、空窗口和未来截止点被拒。运行配置不入库；数据库只保存发生的事实、分组身份和建议证据。设置 `enabled=false` 不再注册新 Schedule且扫描不生成建议；已有 Schedule 的暂停状态通过 Temporal 控制，不能靠重复注册重置。

## 你可以自行验收

先启动 Docker Desktop 的 Linux 引擎，确认本项目已有 PostgreSQL、Temporal 依赖正在运行。首次启动依赖的方法见 [deploy/README.md](../deploy/README.md)。在 PowerShell 中执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
powershell -NoProfile -ExecutionPolicy Bypass -File .\check-automation.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 37 专项检查失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-automation.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 37 演示失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

专项检查覆盖五类统计来源、阈值、引用、并发、事务回滚、原始操作重投、窗外/迟到数据、禁止自反馈，以及本机 Temporal 的提交后丢响应、Worker 重启、历史回放和实际 Schedule 周期触发。离线测试禁止实际 HTTP/DNS/socket 连接；数据库/Temporal 测试只使用本机依赖和 Fake。

演示预期输出：

- 四条记录时建议任务数：0。
- 五条记录时建议任务数：1。
- 原始记录引用：恰好五条，每条含 record_id、task_id、evidence_id。
- 一个建议 Evidence ID 和 `ai-task-...` Workflow ID，任务停在 WAITING_INFORMATION。
- 重复扫描新增任务数：0；Temporal 历史回放：通过；实际运维动作数：0。
- 最后显示「Step 37 Automation Discovery Fake 演示全部通过」「临时测试库已清理」。

两个入口自动创建并清理独立临时数据库；演示结束终止等待中的隔离任务，不修改应用库或遗留常驻 Schedule。演示输出的 UUID 随每次运行变化，清理后引用只用于本次输出核对，不能继续在应用库查询。

若要查看后台注册的默认 Schedule，在另一个 PowerShell 运行 `run-worker.ps1`；启动前应按项目说明加载本地数据库和 Temporal 环境。Worker 会注册 `weipai-automation-discovery`，可在 Temporal UI 查看。专项测试使用自己的 Schedule，并在结束时删除它。

## 2026-10-07 自检结果

- 专项：`36 passed`，其中 25 项禁止真实网络的单测、11 项本机数据库/Temporal 验收。
- 统一检查：ruff、格式、mypy（355 个源文件）、Connector 导入边界、Git 环境检查通过；pytest 为 `1828 passed, 458 skipped`。依赖测试通过专项及数据库入口执行。
- 完整数据库/Temporal 回归：`445 passed, 3 skipped`；跳过项是既有时间跳跃测试，仍保留独立入口。本步全部新增集成项在专项和完整回归中通过。
- 使用 Windows 自带 PowerShell 5.1 复跑上面的专项和演示命令成功；修复相关辅助脚本 UTF-8 无 BOM 导致的中文解析问题，五个涉及的脚本语法/编码检查通过。
- 演示实际通过阈值、五条引用、任务派发、重复去重和历史回放；临时测试库 0、测试 Schedule 0、本步运行 Workflow 0，四个本机依赖 healthy。
- 只完成 Step 37；Step 38 及后续步骤仍未开始。
