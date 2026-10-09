# Step 22：定时驱动与发布后上线验证

本步骤只实现触发：Temporal Schedules / Timer → OpsEvent → AI Task → 既有 AITaskWorkflow。实际巡检、容量分析、治理和上线验证业务逻辑按后续步骤实现；当前任务沿用占位阶段并进入 WAITING_INFORMATION。测试、演示与 Worker 只允许 local/test + Fake，不连接生产系统。

## 最快自行验收

Docker Desktop 和本项目 PostgreSQL、Temporal 容器应已运行。从项目根目录的 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-schedules.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 22 专项验收失败' }
.\demo-schedules.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 22 演示失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库回归失败' }
```

预期专项 `20 passed`；演示输出三个 Schedule ID，随后打印五组 OpsEvent / Task / Workflow ID：开工巡检、每小时容量检查、每日资源治理（三个来源都是 Schedule），payment-service 发布（Release），payment-service 发布后上线验证（Schedule）。最后显示「Step 22 Fake 演示通过」和「临时测试库已清理」。

演示使用独立临时库、隔离队列和三个临时 Schedule，只在演示配置中把发布延迟设为 **2 秒**。专项采用默认 **600 秒**，通过官方时间跳跃服务器检查到期前没有验证任务，到期后恰好产生一个，并检查 Worker 停止/重启、重复事件、重复派发和历史 Replay。日历使用本地 Temporal 的原生 backfill 跳至北京时间工作日 09:00、某小时整点和 18:00，各产生一个任务；星期日 09:00 不触发开工巡检。官方时间跳跃服务器没有 Schedule API，故日历和 Timer 分别使用对应的真实 Temporal 能力验证。

时间跳跃测试禁用 Workflow 缓存，强制重启后从历史恢复，避开测试服务器的 sticky queue 限制。首次专项会在 `.cache/temporal-test` 缓存与已安装 SDK 对应的官方测试服务器；pytest 使用已缓存二进制，无在线下载或生产系统访问。临时库、测试 Schedule、测试服务器和演示中的运行任务会清理；本地 Temporal 保留演示 Workflow 的历史，便于在 UI 查看。

## 查看三个默认 Schedule

先应用新迁移，然后启动 Worker。在窗口 A 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
. .\use-local-db.ps1
. .\scripts\project.ps1
$uvPath = Get-ProjectUv
& $uvPath run --frozen --directory backend alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw '迁移失败' }
.\run-worker.ps1
```

窗口 B 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
. .\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml exec -T temporal-admin temporal schedule list --namespace default
```

列表会包含本步骤的三个默认 ID：

| Schedule ID | 默认日历（Asia/Shanghai） |
| --- | --- |
| weipai-ops-workday-inspection | 周一至周五 09:00 |
| weipai-ops-hourly-capacity | 每小时整点 |
| weipai-ops-daily-governance | 每日 18:00 |

Step 18 的 `weipai-discovery` 也可能列出，属于已有 Schedule。本步骤只增加三个，保留已有 Schedule。列表的可见性索引有短暂延迟，刚启动时可稍后再查。可打开 [本地 Temporal UI](http://127.0.0.1:8080)，选择 default 的 Schedules / Workflows 查看日历与历史。周期任务的时间保存为带时区 UTC；Schedule 使用北京时间解释日历。

重复启动 Worker 不新增 Schedule，不重置已有的暂停状态、日历、队列或计数。配置修改只用于新建的 Schedule；已有 Schedule 需显式通过 Temporal 更新。`enabled=false` 只跳过自动注册，已经存在的 Schedule 仍由 Temporal 管理。周期任务不重叠（SKIP），默认补发窗口一小时，Workflow 失败会暂停 Schedule；基础设施暂时不可用时事件 Activity 持续重试。

验收后默认三个 Schedule 已暂停。需要正常周期运行时，保持窗口 A 的 Worker 运行，在窗口 B 恢复：

```powershell
$scheduleIds = @('weipai-ops-workday-inspection', 'weipai-ops-hourly-capacity', 'weipai-ops-daily-governance')
foreach ($scheduleId in $scheduleIds) {
    docker compose -f deploy/docker-compose.yml exec -T temporal-admin temporal schedule toggle --namespace default --schedule-id $scheduleId --unpause --reason '恢复本地 Fake 定时触发'
    if ($LASTEXITCODE -ne 0) { throw '恢复 Schedule 失败' }
}
```

只想立刻看一个周期任务时，无需等待整点，也无需解除暂停：

```powershell
docker compose -f deploy/docker-compose.yml exec -T temporal-admin temporal schedule trigger --namespace default --schedule-id weipai-ops-hourly-capacity
```

结束体验后，可以把上面循环中的 `--unpause` 改成 `--pause`。停止 Worker 不会删除 Schedule 或运行中的 Workflow；计时由 Temporal 负责，需暂停 Schedule 才会停止未来的周期触发。

## 配置与去重契约

配置来自 `SCHEDULING_CONFIG` 环境变量，没有配置文件或数据库密钥。例如：

```powershell
$env:SCHEDULING_CONFIG = '{"schedule_prefix":"weipai-ops","time_zone":"Asia/Shanghai","service_name":"weipai-platform","inspection_hour":9,"inspection_minute":0,"inspection_weekdays":[1,2,3,4,5],"capacity_minute":0,"governance_hour":18,"governance_minute":0,"release_delay_seconds":600,"catchup_window_seconds":3600}'
```

周期任务以日历执行的 Workflow ID 作为稳定事件身份，保存 TemporalScheduledStartTime；活动提交后丢响应或重试不会新建第二个 OpsEvent 或 Task。`weipai-platform` 表示整个平台的检查范围；发布验证沿用具体发布服务。

任何已归一化为 source=Release 的事件经接入链路派发后，同时启动 `release-verification-<OpsEvent ID>`。到期时间固定为原发布 `occurred_at + 600 秒`，不是重投时重新等待十分钟。迟到超过十分钟的发布事件立即补触发。重复发布和派发后丢响应通过固定 Workflow ID、REJECT_DUPLICATE 和既有数据库指纹约束去重；到期后生成 source=Schedule、external_id=`release-verification:<原发布事件 ID>` 的新事件，再经同一链路启动验证任务。Schedule 来源不会再派生发布 Timer。

新迁移 `0008_scheduled_events` 只扩展 OpsEvent 来源约束，允许 `origin=schedule`；没有新调度表、队列或中间件，事件、任务、NEW 历史与审计仍同事务写入。空库完整升降级和 metadata 一致性由 `check-db.ps1` 验证；已有定时事件时，降至 0007 会因旧约束不兼容被数据库拒绝，以保留事件与审计记录。

前端仍为 Step 47 预留目录，无前端构建或检查命令；本步骤不创建前端工程。Step 23 及后续步骤未实施。
