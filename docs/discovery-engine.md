# Step 18：Discovery Engine

本步实现五类来源的发现、Context Graph 刷新、Temporal 定时调度，以及两个 L0 查询 Tool。
没有进入 Step 19 的 Change Timeline、事件接入或前端开发。原始 V1.0 设计文件仍未提供，
依据已完整阅读的 AGENTS.md、SPEC.md 和 plans.md 实现。

## 数据与关系

`graph/discovery/` 从运维平台读取服务树、应用、负责人；从 Kubernetes 自动枚举命名空间，
按服务标签读取 Deployment、Pod、容器镜像；GitLab/GitHub 的仓库 GET 返回准确仓库标识；
阿里云读取已配置绑定的云资源与 RocketMQ 4.x Topic；ARMS 在 UTC 半开窗口中读取 Trace，
只有实际父子 Span 才构成 `calls` 边。服务清单来自运维平台，不要求人工录入。

图只保存节点标识、名称及关系，不保存原始日志、指标序列、Trace、配置内容或凭证。
当前云 Connector 的可见范围仍是环境变量中的资源绑定，Git 同样按服务绑定读取；未绑定的
服务会在 Workflow 结果 `missing_bindings` 中列出。绑定范围不是全账号自动资源扫描。

| 关系 | 来源 | 置信度与时间 |
| --- | --- | --- |
| 业务树、业务归属、负责人 | ops_platform | 1.0；本轮 UTC 观察时间 |
| 服务→集群/Deployment/Pod/版本、Pod→镜像 | kubernetes | 1.0；本轮 UTC 观察时间 |
| 服务→仓库 | gitlab/github | 1.0；本轮 UTC 观察时间 |
| 服务→关联云资源、MQ 实例→Topic | alibaba_cloud | 1.0；本轮 UTC 观察时间 |
| 上游服务→下游服务 | arms | 0.9；实际子 Span 的 UTC 时间 |

置信度 1.0 表示源系统明确声明关系，不代表源系统绝不会出错。ARMS 是采样到的调用，未采样到
不意味着不存在。未再次观察的边保留历史 last_seen，freshness 随时间增长；旧 Trace 不刷新为新事实。
同来源同边 upsert 只更新 last_seen，first_seen 和 confidence 保持，乱序观察不能使时间倒退。

版本来自镜像明确携带的 tag/digest，不能将它冒充 Git commit。ARMS 的 `payment-db` 服务标识与
云 RDS 实例 `rm-payment` 分别保留，没有事实映射时不能合并。共享 MQ 实例的 Topic 只记录
实例归属，不推断某服务必然发布/订阅。配置与运行状态存储仍沿用原系统。

新增 Connector 读取协议参照官方 [GitLab Projects API](https://docs.gitlab.com/api/projects/)、
[GitHub Repository API](https://docs.github.com/en/rest/repos/repos#get-a-repository) 和
[阿里云 OnsTopicList](https://www.alibabacloud.com/help/en/apsaramq-for-rocketmq/cloud-message-queue-rocketmq-4-x-series/developer-reference/api-ons-2019-02-14-onstopiclist)。
真实适配器只经过 HTTP mock 检查，本步未连接公司或生产系统。

## Temporal 与配置

现有 `worker` 入口同时注册 `AITaskWorkflow` 和 `DiscoveryWorkflow`。Worker 启动时创建
`weipai-discovery` Schedule：默认每 300 秒运行一次；UTC、SKIP 不重叠、只补一个周期内的
遗漏运行，重试耗尽则暂停 Schedule。周期触发、Activity 超时和重试都由 Temporal 执行。
Activity 完整采集后，在单个 PostgreSQL 事务中落图，失败回滚。提交后丢响应的重试利用
稳定节点 ID 和原子 upsert 去重。异常写入 Temporal 时只有固定脱敏消息。

`DISCOVERY_CONFIG` 是可选环境变量 JSON，例如：

```powershell
$env:DISCOVERY_CONFIG = '{"schedule_id":"weipai-discovery","interval_seconds":300,"lookback_seconds":900,"activity_timeout_seconds":300,"activity_max_attempts":3}'
```

重复启动不新增 Schedule，也不覆盖已有的周期或人工暂停状态。修改已存在 Schedule 的配置
应在 Temporal UI 编辑；环境配置用于首次创建。运行中的占位 AI Task Worker 仍遵守 Step 17
的 local/test + Fake 门禁，真实生产部署按后续计划实现。

## 两个查询 Tool

`register_graph_tools(registry, GraphService(session))` 注册 `get_service_context` 和
`get_dependencies`，风险均为 L0。调用必须经已有 Dispatcher、Policy、Evidence Ledger 与审计。
它们只读图，不联网补查。成功调用各追加一条 Evidence 和一条 Tool 审计；Replay 返回历史
原值与 Evidence ID，freshness 不按当前时间重算。

`get_service_context` 参数为 `service_name`、`hops`（默认 2、范围 1–4）。返回根服务、N 跳节点
及范围内的边，每条边含 source/confidence/first_seen/last_seen/freshness_seconds，`as_of` 为
读取 UTC 时间。freshness_seconds 是距最后观察的秒数，越小越新。
`get_dependencies` 参数为 `service_name`、`hops`（默认 1）、`direction`（upstream/downstream/both，
默认 both）；只沿实际 `calls` 边查询，不通过共同业务或资源推断服务依赖。未发现服务返回
Dispatcher 的 `tool_failed` 并保留失败审计，不伪造成功证据。

## 自己跑一遍

先打开 Docker Desktop，本机原有依赖容器应在运行。以下命令均从仓库根目录运行。
如已有容器停止，可先执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
```

自动验收（不需要自己另开 Worker）：

```powershell
.\check.ps1
.\check-discovery.ps1
```

预期分别输出「统一检查全部通过」「Step 18 Discovery 验收全部通过」，退出码 0。
专项创建独立临时库和短周期 Schedule，验证后全部清理，不修改本地应用库。
测试覆盖样例完整关系、来源/UTC、新鲜度、重复与乱序观察、回滚、上下游方向、两条 Tool 的
证据/审计、Policy 拒绝与 Replay，以及真实 Temporal 的提交后丢响应重试、历史回放、重复
Schedule 注册和至少两次周期触发。所有外部运维来源是 Fake/mock。

交互演示：第一个 PowerShell 窗口运行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\run-worker.ps1
```

第二个窗口运行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\demo-discovery.ps1
.\demo-discovery.ps1 resume
.\demo-discovery.ps1 schedule
```

预期输出两个 `discovery-demo-...` Workflow ID，第二次关系 ID 不变、last_seen 刷新；
两个 L0 Tool 各得到 Evidence ID；上下文包含仓库、v2.3.7、ack-fake、3 个 Pod、RDS、Redis、
Topic 和 checkout-service→payment-service→payment-db，且有 freshness_seconds；Replay 检查通过。
演示图和查询证据保留在本地应用库，方便后续查看。checkout-service 缺 Git/云绑定是明确的 Fake 样例。

打开 [本地 Temporal UI](http://127.0.0.1:8080)，在 `default` 的 Workflows 中找到上述 ID，
状态应是 Completed；Schedules 中找到 `weipai-discovery`，默认等待一个 5 分钟周期后能看到
新的 DiscoveryWorkflow。自定义 UI 端口时使用对应端口。

结束周期演示或恢复：

```powershell
.\demo-discovery.ps1 pause
# 预期 paused=True；需要继续定时发现时：
.\demo-discovery.ps1 resume
```

最后在第一个窗口 Ctrl+C 停止 Worker。图查询的 HTTP API 和前端按 Step 45/50 实现，
本步通过脚本与 Temporal UI 验收。
