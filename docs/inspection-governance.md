# Step 40：巡检与治理

本步依据 AGENTS.md、SPEC.md、plans.md 和权威长版设计第 26/29 节实现。
计划文件实际名为 `plans.md`。本次仅完成 Step 40。

## 自行运行

先启动 Docker Desktop，并确认本项目 PostgreSQL 和 Temporal 容器已运行。
在 Windows PowerShell 中执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
powershell -NoProfile -ExecutionPolicy Bypass -File .\check-inspections.ps1
if ($LASTEXITCODE -ne 0) { throw '巡检与治理专项失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-inspections.ps1
if ($LASTEXITCODE -ne 0) { throw '巡检与治理演示失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

演示自动创建临时数据库和隔离 Worker，实际执行已有的周期触发 Workflow。
专项检查应显示 `64 passed`。
应显示：

- `异常环境风险条目：4`，分别为缺 PDB、缺 HPA、证书六天后过期、闲置 ECS。
- 巡检覆盖类别为 17，治理分类包含稳定性、容量、安全、成本。
- 重复扫描新增风险、重复扫描新增通知、全部健康新增通知均为 0。
- 各个巡检任务最终 `CLOSED`，打印真实报告 Evidence ID 与 `ai-task-...` Workflow ID。
- `实际运维动作数：0`、`Temporal 历史回放：通过`。
- `Step 40 巡检与治理 Fake 演示全部通过`、`临时测试库已清理`。

不需要启动 API、不需要公司凭证。默认三个周期 Schedule 的暂停状态保持原状，
演示直接执行其 Workflow，不用等到日历触发时间。

完整数据库/Temporal 回归可在同一窗口执行：

```powershell
. .\use-local-temporal.ps1
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库与 Temporal 回归失败' }
```

## 统一闭环

沿用 Step 22 的开工巡检、每小时容量检查、每日治理三个 Temporal Schedule：

```text
PeriodicTriggerWorkflow → OpsEvent/source=Schedule → AITaskWorkflow
→ 优先 search_runbooks → InspectionWorkflow 子流程
→ query_inspection_facts/L0 → Policy → Evidence 与 Tool 审计
→ 风险与报告原子落库 → 仅向本人通知异常/待补充信息
→ 独立 Verifier 核对事实、报告与风险记录 → RESOLVED → LEARNING → CLOSED
```

任务状态由 `tasks/` 唯一服务迁移，没有引入状态枚举或自建调度器。
巡检任务的 RESOLVED 表示本次扫描与风险记录已独立验证，不表示风险已修复。
风险仍打开，只有同一资源的较新可信健康观测才能清除；恢复后再次异常会重新通知。
本步没有运维写操作或隐式整改授权。整改须走既有主 Agent、Reviewer、Action Plan、
Policy、审批、Executor、Verifier 链路。

Runbook 检索先于事实查询；适用与排除条件按现有引擎核对并留证。
目录检查由固定规则执行，Runbook 的处理步骤不在扫描阶段执行。
巡检/治理模式覆盖全目录，小时容量模式只执行容量类检查。

## 覆盖范围和默认规则

17 类巡检覆盖服务、K8s、云资源、数据库、Redis、MQ、磁盘、网络、监控、告警、
日志、Trace、证书、DNS、容量、成本、安全。

| 治理分类 | 检查 |
| --- | --- |
| 稳定性 | 单点、PDB、HPA、副本数、Runbook、监控，以及服务和各依赖健康 |
| 容量 | 磁盘/资源使用率、增长趋势、预计耗尽时间 |
| 安全 | 安全基线、证书、过大权限、凭证风险、公网暴露、安全组风险 |
| 成本 | 日成本增长、闲置 ECS、低利用率、过度配置、临时资源未回收 |

布尔值直接表示对应可核验事实。数值检查的单位是：使用率和增长率为 0–1 的比例；
证书有效期与预计容量耗尽为天；副本数为数量。默认使用率达到 85%、增长达到 20%、
有效期/预计耗尽不超过 7 天、副本数不超过 1 时异常。增长/耗尽由源系统提供带采样时间
和引用的计算结果，不能把单次利用率当作增长预测。完整固定目录见
`backend/app/tasks/inspection/catalog.py`。

## 配置与数据边界

所有配置只从环境变量读取。例如：

```powershell
$env:INSPECTION_CONFIG = '{"services":["payment-service"],"max_age_seconds":900,"thresholds":{"certificate_days":7}}'
```

默认启用，范围为 `payment-service`。服务列表不重复，最多 20 个；只允许已声明数值规则
调整阈值。数据库保存规则指纹，不保存配置对象、Reader 凭证或 Secret。

`connectors/inspection/` 定义共同只读接口、可注入 Fake 和公司事实 GET/JSON 协议适配器。
只暴露 `query_inspection_facts` L0 Tool，所有调用通过唯一 Dispatcher，支持原快照 Replay。
真实分支要求 `INSPECTION_ENDPOINT` 和独立的 inspection Reader 身份，HTTPS 地址和相对
路径严格校验。响应只接受 `service_name`、`complete`、有限测量及无凭证/查询参数的来源 URI。
没有通用 HTTP Tool，没有写方法，不复制原始指标、日志或 Trace。

公司完整巡检事实接口、字段语义、授权和覆盖范围尚未提供，当前真实协议只经 HTTP mock
验证，未生产联调。正式接入须以公司真实接口说明适配，不把 Fake 数据作为线上事实。
本次全部验收使用 Fake、本机 PostgreSQL 与本机 Temporal，未向真实飞书发送消息。

来源未完成、事实缺失、时间过期/未来、值类型错误、Policy 拒绝或查询失败均为 unknown。
需要补充信息的项会成为风险，任务进入 `WAITING_INFORMATION`；收到当前版本恢复信号后
重新采集，不能靠确认信号直接判健康。等待与超时由 Temporal 保存。超时/通知失败转人工。

## 持久化、并发与独立核验

新迁移 `0014_inspection_risks` 添加风险摘要表，按服务/检查/资源唯一。
每条风险有 UTC 观察时间、状态、通知周期和只追加 Evidence 引用。
unknown 使用本次核验时间，来源的未来时间戳不能污染风险时间或阻止后续恢复。
报告、风险变化、查询 Evidence 与审计在同一事务；任务锁复用已提交扫描结果，
服务范围锁处理不同扫描任务的并发。持续异常更新观察时间，不重复打开风险。
不完整和乱序数据不清除已有风险；资源不再出现在响应里，也不能推断其已修复。

通知独立提交，通过风险身份和通知周期生成固定 notification_id；发送后事务失败的
重试使用同一消息身份。健康项不发送通知，未发送的风险可在后续扫描重试。
实际飞书服务的幂等保证仍以 Step 16 Connector 的服务端协议为准。

独立 Verifier 重新计算已留证事实的规则结果，核对覆盖、成功 live Tool 审计、
Runbook 检索、报告/任务版本与异常风险记录，才持内部权限设置 RESOLVED。
actor 字符串、伪造引用、旧版本、规则变化和不完整报告均不能放行。
已有风险时迁移拒绝有损降级，空库可以完整升降级。

API 和前端页面按后续计划实现；前端目录目前只有 `.gitkeep`，本步未创建前端工程。

## 自检记录

2026-10-08 最终自检：

- 巡检专项 `64 passed`：51 项禁止真实网络的单测和 13 项本机 PostgreSQL/Temporal 测试。
- 统一检查 `1923 passed, 493 skipped`，ruff、格式、mypy（398 个源文件）、Connector
  导入边界和 Git 环境文件检查通过；依赖测试由独立入口执行。
- 完整数据库/Temporal 回归 `480 passed, 3 skipped`。3 项既有官方时间跳跃场景由
  定时专项 `20 passed` 覆盖；既有 Workflow `36 passed`、人工问答 `44 passed`。
- Windows PowerShell 交付命令实际复跑，四项异常恰好四条风险，重复扫描新增风险/通知 0，
  健康新增通知 0，实际运维动作 0，父子 Temporal 历史回放通过。
- 空库完整升降级、应用库 head/metadata、脚本语法和 UTF-8 BOM 检查通过。
  本机应用库为 `0014_inspection_risks (head)`；四个本机依赖 healthy。
- 临时数据库、本步运行中的隔离 Workflow 均为 0。

自检修复了缺整项检查后占位风险的恢复、未来时间戳污染、并发验证检查点及旧测试的
迁移 head 断言。巡检任务完成不会清除未修复风险，全部验收未访问生产或真实飞书。
Step 40 已在 plans.md 标为完成，Step 41 保持未开始。
