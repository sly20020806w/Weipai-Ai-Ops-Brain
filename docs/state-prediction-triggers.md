# Step 23：状态与预测驱动

本次只完成 `plans.md` 的 Step 23，沿用 PostgreSQL、既有只读 Connector、OpsEvent、AI Task、Evidence Ledger 和同一个 Temporal Worker。只触发调查，任务使用已有占位阶段并暂停到 `WAITING_INFORMATION`；主 Agent 在 Step 24、前端在 Step 47 实现。

本次自检：统一检查 `1439 passed, 227 skipped`，专项 `35 passed`，数据库回归 `206 passed, 11 skipped`。统一入口跳过依赖测试，Step 23 的 PostgreSQL/Temporal 项已在专项全部通过；既有其他 Temporal 专项沿用各自入口。迁移已应用到本机应用库，`alembic current` 为 `0009_state_prediction (head)`，`alembic check` 无待生成变更，新 PowerShell 脚本语法通过。

## 自己跑一遍

在项目根目录打开 PowerShell。本机已有的 PostgreSQL 和 Temporal 容器需要运行，首次准备依赖按 [部署说明](../deploy/README.md) 操作。

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
./check.ps1
./check-detection.ps1
./demo-detection.ps1
./check-db.ps1
```

1. `check.ps1`：后端 import 边界、ruff、格式、mypy、全部离线单测和 Git 环境文件检查通过。
2. `check-detection.ps1`：全 Fake 专项，包含三值副本比对、四类趋势、手算耗尽时间、正常静默、并发去重、恢复后再次触发、原子回滚，以及实际 Temporal Schedule、Worker 重启、提交后丢响应和历史回放。末尾显示“Step 23 状态与预测驱动验收全部通过”。
3. `demo-detection.ps1`：通过实际隔离 Schedule 注入异常。应看到 **1 个 State + 4 个 Prediction**，磁盘任务有 UTC 预计耗尽时间，每个任务有 OpsEvent、Task、Evidence ID 和 `WAITING_INFORMATION`。持续异常复测新增任务 **0**、ID 不变；健康复测新增事件/任务 **0**。末尾显示“Step 23 Fake 演示通过”。
4. `check-db.ps1`：另一临时库中的全部数据库回归，含空库升级、逐级降级再升级及 metadata 检查。

专项和演示自动读取本项目本机容器配置，不显示密码、不写 `.env`；使用独立临时数据库和隔离 Temporal 队列。退出时删除临时数据库、测试/演示 Schedule，终止任务 Workflow。输出的 Evidence ID 属于本轮临时库，不会留在应用库；Temporal 历史作为本机验收记录保留。测试不访问真实运维系统或公司 AI 网关。前端仍为占位目录，此步没有浏览器业务页面可验收。

## 检测行为

| 类型 | 规则与证据 |
|---|---|
| 状态 | 按集群/命名空间/服务聚合 Deployment 的 readyReplicas 为 Current；Baseline 来自环境变量；Desired 优先用配置，否则取 spec.replicas 合计。任一缺口严格大于 allowed_deficit 时触发 State。证据保存三值、阈值与 Deployment UID。缺失 Deployment 按 Current=0；未观测最新 generation 返回未知。 |
| 容量耗尽 | 按实际时间戳做最小二乘拟合。正斜率、拟合质量达标且到达 limit 的时间处于预测窗口内时触发；已达到 limit 也触发。预计耗尽时间为最后采样时间 + (limit-current)/slope；已经耗尽取最后采样时间。标题和证据 predicted_exhaustion_at 均含 UTC 时间。 |
| 流量增长 | 当前值或可靠正增长预测值严格超过 baseline × (1+growth_fraction) 时触发。 |
| 成本异常 | 同单位成本指标使用相同基线/增长阈值比较，不推断不存在的账单。 |
| 资源瓶颈 | 最后 min_points 个点持续达到 limit，或可靠增长预测在窗口内达到 limit 时触发。样例使用 CPU 使用率。 |

各趋势按完整序列标签分别计算，多个磁盘/资源不混合回归。只消费服务范围和 UTC 半开窗口 `[start,end)` 内的数据。样本/跨度不足、最新点过期或低拟合质量的未超限序列返回未知；缺失数据不会被合成，也不清除已有异常。恒定或下降的健康序列不触发。

新异常的 OpsEvent、任务、初始历史/审计、Evidence 和游标同事务写入。证据只存必要结果摘要、阈值、拟合参数、样本数量/时间范围和源系统引用，原始指标序列仍在 Prometheus。检测键由规则内容和资源目标计算：持续异常复用同一事件/任务，不追加检测证据；可靠健康观测清除活动异常引用，后续再次异常可以新建任务。检测器不会把上一任务设置为 RESOLVED。

旧观测不修改游标。此前已接受的旧异常批次重试仍返回原事件，以完成派发；未接受的过期异常不创建任务。相同采样时间的相反结果拒绝写入。配置改变会产生新的检测键，旧证据保留。

## Temporal 与环境变量

`StatePredictionWorkflow` 每轮先执行 `detection.collect`，再执行 `detection.persist`，最后复用 `event.start_task`。采集结果保存在 Temporal 历史，入库重试使用同一快照，避免提交后丢响应时重新采到变化中的数据。采集/入库有限重试，派发沿用既有固定任务 Workflow ID 和幂等重试。周期由 Temporal Schedules 托管，没有自造调度器或轮询状态机。

默认注册 `weipai-state-prediction` Schedule，每 300 秒运行，UTC、SKIP 重叠、失败自动暂停。重复启动 Worker 不重置已有 Schedule 的暂停状态、队列或周期。默认状态目标为 `payment/payment-service`，Baseline=3、缺口阈值=0。

默认趋势指标为 `disk_used_ratio`、`http_requests_rate`、`daily_cost`、`cpu_usage_ratio`。这是示例约定，需来源提供同名、同单位数据。默认普通 Fake Prometheus 没有这些指标，普通 Worker 会进行副本检测，但不合成预测数据；四类趋势样例仅由专项/演示显式注入。

全部检测范围和阈值只通过 `DETECTION_CONFIG` 环境变量读取。示例只启用状态与磁盘检测：

```powershell
$env:DETECTION_CONFIG = @{
    enabled = $true
    schedule_id = 'weipai-state-prediction'
    interval_seconds = 300
    lookback_seconds = 900
    step_seconds = 60
    state_rules = @(@{
        rule_id = 'replicas'
        namespace = 'payment'
        service_name = 'payment-service'
        baseline_replicas = 3
        desired_replicas = 3
        allowed_deficit = 0
    })
    trend_rules = @(@{
        rule_id = 'disk'
        kind = 'capacity'
        metric_name = 'disk_used_ratio'
        service_name = 'payment-service'
        limit = 1.0
        horizon_seconds = 3600
        min_points = 3
        min_span_seconds = 120
        min_r_squared = 0.9
        max_age_seconds = 120
    })
} | ConvertTo-Json -Depth 6 -Compress
```

两组规则都为空或 enabled=false 时不创建 Schedule。规则 ID 在两组间唯一，最多 100 条。无效阈值、NaN、无效周期和过大采样数量在配置读取时拒绝。比例样例为 0–1，流量/成本默认基线 100、增长阈值 50%；这些不是微派生产标准。计划缩容时应同时调整 Baseline 和 Desired，单改 Desired 不会消除基线偏差。

## 应用库与 Worker

迁移 head 为 `0009_state_prediction`，新增 `detection_cursors`，OpsEvent origin 新增 state/prediction。降级不删除或重写历史检测事件；存在新来源数据时数据库会拒绝不兼容降级。

```powershell
. ./scripts/project.ps1
. ./use-local-db.ps1
$uvPath = Get-ProjectUv
& $uvPath run --frozen --directory backend alembic upgrade head
& $uvPath run --frozen --directory backend alembic check
./run-worker.ps1
```

本机 [Temporal UI](http://127.0.0.1:8080) 的 Schedules 可查看 `weipai-state-prediction`，Workflow 类型为 `StatePredictionWorkflow`，可在 UI 暂停/恢复 Schedule。Worker 仍只允许 local/test + Fake，没有生产写动作。专项/演示只操作隔离 Schedule，不改变默认 Schedule 暂停状态。
