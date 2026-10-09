# Step 34：事故复盘

本步按《Weipai AI Ops Brain 最终设计方案 V1.0》第 22 节实现全部十三个章节：事件现象、影响范围、Timeline、证据链、根因、处理过程、验证结果、为什么没有提前发现、监控改进、告警改进、架构改进、自动化建议、Runbook变更。没有进入 Step 35。

## 实现与边界

主调查 Workflow 执行成功后，自动从回执生成验证目标，通过 Temporal Timer 等待完整观测窗口，再由独立 Verifier 验证；成功进入 `LEARNING`，生成复盘、改进任务和 Runbook Draft，派发改进任务后进入 `CLOSED`。独立验证入口也衔接相同学习阶段。没有恢复的事故继续调查或交由熔断接管，不生成“已恢复”复盘。Step 17 的历史占位闭环保留；单独验收 Step 31/32 时可用宿主字段 `postmortem_enabled=False` 隔离学习阶段，该字段不由 LLM 控制。

执行后自动验证要求环境配置 `VERIFICATION_CONFIG.resources_by_service` 明确各服务的资源恢复标准，观测窗口由 `window_seconds` 指定，默认 300 秒。缺少标准时转 `ESCALATED`，不能把当时观察到的任意资源状态当成健康目标。镜像、副本、宿主目标和完成时间只能继承已审计的动作回执。

复盘合成器复用已接受的主 Agent RCA、真实执行回执、独立 Verifier 报告及成功 Tool 审计。每条结论包含同事故的真实 Evidence ID。缺少证据时明确写“尚未确认”或“需核对”，不编造受影响人数、金额或未提前发现的原因。UTC Timeline 包含事件发生时间、源系统变更发生时间、状态迁移和证据采集时间，采集时间不会冒充故障发生时间。

复盘以只追加的 `postmortem` Evidence 保存，可按 Evidence ID 精确读取。`search_incidents` 是 L0 高级 Tool，按文字和服务检索，调用经过唯一 Dispatcher、Policy、证据和审计，Replay 必须提供历史截止时间，不重新查询。主 Agent 的 Tool 组合根已注册此入口。查询为 PostgreSQL 文字检索；本步没有引入新中间件。

任务行锁和阶段版本保证并发、重试与提交后丢响应复用同一复盘。上下文快照、报告、Runbook、改进 OpsEvent/Task 及引用在同一事务保存，末尾保存失败也整体回滚。改进任务 source 为 `AI`、origin 为 `learning`，保留父事故和 Evidence 引用，再由 Temporal 派发；当前等待补齐实施方案，不直接执行监控、告警、架构或其他生产变更，也不递归生成改进任务。

Runbook 由实际诊断、处置计划、回滚方案和验证证据生成，保存向量及完整字段，但成熟度固定 `draft`、自动化等级 `manual`、成功/失败次数为 0。审核、计数和晋级留给 Step 35。

`0012_postmortem` 仅扩展 OpsEvent 来源约束。已有 `learning` 事件时拒绝降级，不删除事故或改进记录。配置和密钥仍来自环境；数据库只保存业务事实和证据。

## 自己运行检查

在项目根目录打开 PowerShell。需要项目已有的本机 PostgreSQL、Temporal 容器正常运行；两个专项脚本会自行读取本项目容器配置、创建独立临时库、迁移并启动临时 Worker，无需另开 Worker。

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-postmortem.ps1
.\demo-postmortem.ps1
.\check.ps1
```

- `check-postmortem.ps1`：专项全部通过，最后显示“Step 34 Postmortem 验收全部通过”和“临时测试库已清理”。覆盖十三章节、必需引用、同任务/版本门禁、原子回滚、并发去重、Policy 拦截、Dispatcher Replay、Temporal 提交后丢响应和历史 Replay。
- `demo-postmortem.ps1`：接纳一条 Fake 告警后，在同一 `AITaskWorkflow` 中完成主 Agent 调查、Reviewer、Action Plan、Fake 审批/回滚、独立验证和自动复盘。应看到最终 `CLOSED`、13 个章节及 Evidence ID、4 个改进任务/Workflow、1 个 `draft` Runbook、检索成功、Fake 回滚次数为 1。样例使用固定历史窗口和恢复数据，不需等待真实五分钟。只访问本机依赖，真实运维请求为 0。
- `check.ps1`：Connector 边界、ruff、格式、mypy、离线 pytest、Git 检查全部通过。数据库/Temporal 测试由专项入口执行，此处跳过。

演示和专项会清理临时库、演示中运行的改进 Workflow，重复执行会使用新的隔离 ID。终端打印的 Evidence ID 属于临时库，演示结束后不能再通过应用库查询。报告界面及 Incident API 留给计划中的后续步骤，当前用这些命令验收。

完整数据库和 Temporal 回归：

```powershell
. .\use-local-temporal.ps1
.\check-db.ps1
.\check-workflow.ps1
```

本机应用库更新到本步 head：

```powershell
. .\use-local-db.ps1
. .\scripts\project.ps1
$projectUv = Get-ProjectUv
& $projectUv run --frozen --directory backend python -m alembic upgrade head
```

## 本次自检结果

- Step 34 专项：35 项通过（22 项禁止真实网络的单测、13 项本机 PostgreSQL/Temporal 测试）。
- 统一检查：Connector 边界、ruff、格式、mypy（328 个源文件）、1733 项离线测试和 Git 检查通过；415 项依赖测试由独立入口执行。
- 完整数据库/Temporal 回归：402 项通过，3 项既有时间跳跃测试按原入口规则跳过；Runbook 专项 52 项、既有 Workflow 专项 36 项通过。
- 最终 Fake 主 Workflow 演示通过，任务为 `CLOSED`；本机应用库为 `0012_postmortem`，Alembic metadata 检查无差异，新 PowerShell 脚本语法通过。
- 四个本机常驻依赖 healthy；验收临时库与演示中的运行任务已清理。前端仍为 Step 47 的预留目录，本步没有创建前端工程。
