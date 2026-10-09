# Step 13：可观测性 Connector 验收

依据 `AGENTS.md`、`SPEC.md` 和实际计划文件 `plans.md` 的 Step 13，实现 Prometheus、SLS、ARMS 共享只读接口、Fake、原生 HTTP 适配器与三个 L0 高级 Tool。原始 V1.0 设计文件未提供，本步执行现有规格中明确的接入与证据要求。

## 自己运行

在项目根目录的 PowerShell 执行，无需公司凭证、Docker 或数据库：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-observability.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 13 专项验收失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

专项预期 `126 passed`，随后输出 Fake JSON 和「Step 13 Fake 样例验收通过（未连接真实系统）」。`payment-service` 在 `2026-10-01T01:00:00Z` 到 `01:10:00Z` 内，返回 1 条 `http_5xx_ratio` 序列（2 个点，值 0.02、0.12）、2 条连接池超时日志、2 条 Trace 与 2 条 `payment-service → payment-db` 调用关系。其他服务及窗外记录不会返回。

专项中 `test_exact_evidence_audit_and_only_in_window` 分别验证三个 Tool 经真实 Dispatcher 调用后，恰好生成 1 条 Evidence 和 1 条 Tool 审计，Evidence ID 与调用结果一致。JSON 演示直接展示 Connector 的源查询结果；证据与审计由前面的测试验证。没有伪造业务证据 ID。

统一检查预期 Connector 导入边界、ruff、格式、mypy、pytest 与 Git 文件卫生均通过，pytest 为 `1022 passed, 159 skipped`，最后显示「统一检查全部通过」。159 项 PostgreSQL 集成测试使用独立入口。前端工程按 Step 47 建立，目前没有前端 lint/typecheck/test 入口。

本机 Docker Desktop 与本项目 PostgreSQL 容器运行时，执行：

```powershell
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库验收失败' }
```

预期 `159 passed`，随后显示临时测试库已清理。新增 3 项 `test_observability_evidence_commit_and_replay` 实际验证三个 Tool 在独立本地 PostgreSQL 库跨会话保存证据、精确引用、配对审计，以及关闭全部 Connector 后 Replay 成功。回放保留原 Evidence ID，只增加回放审计。脚本自动创建并清理临时库，现有应用库与 Temporal 库不参与读写测试。

## Tool 与时间契约

| Tool | 必填参数 | 可选参数 | 输出 |
| --- | --- | --- | --- |
| `query_metrics` | `service_name`、`start`、`end` | `metric_name` 默认 `http_5xx_ratio`；`step_seconds` 默认 60 | 来源、UTC 时间窗、序列标签与采样点 |
| `query_logs` | 同上 | 无 | 来源、UTC 时间窗、服务、日志时间/级别/消息、源引用 |
| `query_traces` | 同上 | 无 | 来源、UTC 时间窗、Trace/Span 最小快照、源引用与 Span 父子调用拓扑 |

时间必须带时区，接受 UTC `Z` 或明确偏移并转换为 UTC。统一使用半开窗 **`start <= timestamp < end`**；开始时刻包含，结束时刻排除。要求 `start < end`，最长 24 小时。Prometheus 原生结束时间包含端点，SLS 使用整数秒，ARMS 搜索使用整数毫秒；适配器先覆盖查询范围，再按精确 UTC 边界裁剪，窗外 Span 同样不返回。采样步长为 1–3600 秒，每条序列最多 11000 个网格点。

查询只允许服务精确限定的指标选择器或日志搜索，不允许 Agent 传入端点、token、身份、风险、审批布尔值、任意 PromQL/SLS SQL。复杂聚合可以由源 Prometheus recording rule 暴露为指标名称。Fake 根据已注入观测点筛选采样网格，不合成历史观测或插值。

宿主分别通过 `create_prometheus_connector(settings)`、`create_sls_connector(settings)`、`create_arms_connector(settings)` 创建客户端，再调用 `register_observability_tools(registry, prometheus, sls, arms)`。所有高级调用继续使用 `ToolDispatcher.dispatch()`：定级 → Policy → 执行 → Evidence 与审计。拒绝、读取失败或协议不完整不生成成功证据。Replay 不调用 Connector，不联网补查。

ARMS 下游 Span 可属于 `payment-db` 等其他服务；Trace 按搜索服务筛选，Span 按 Trace ID 与时间筛选。拓扑仅从当前返回的父/子 Span ID 建立，缺失父 Span 时不猜测关系。这是查询窗口内的采样拓扑；完整服务图的发现、来源与置信度管理按 Step 18 实现。

## 配置与原生协议

默认 `CONNECTOR_MODE=fake`，`local/test` 禁止真实模式。三个 Fake 可注入类型化快照，输入和返回值深复制。没有生产端点默认值，不读取本机云凭证、代理或 `.env`。

真实模式只用于后续上线联调；需要宿主环境变量/K8s Secret：

| 变量 | 内容 |
| --- | --- |
| `APP_ENV` | `staging` 或 `production` |
| `CONNECTOR_MODE` | `real` |
| `CONNECTOR_READER_TOKENS` | JSON 对象，独立键 `prometheus`、`sls`、`arms` |
| `PROMETHEUS_CONFIG` | Prometheus 地址与服务标签配置 |
| `SLS_CONFIG` | Project 端点、Logstore 与日志字段映射 |
| `ARMS_CONFIG` | ARMS 端点、Region、Span 时间单位 |

配置示例均为不可连接的占位地址：

```json
{
  "base_url": "https://prometheus.example.invalid/",
  "service_label": "service",
  "timeout_seconds": 15,
  "max_series": 1000
}
```

```json
{
  "base_url": "https://example-project.example.invalid/",
  "project": "example-project",
  "logstore": "application-logs",
  "service_field": "service_name",
  "level_field": "level",
  "message_field": "message",
  "page_size": 100,
  "max_pages": 100
}
```

```json
{
  "base_url": "https://arms.example.invalid/",
  "region_id": "cn-hangzhou",
  "span_timestamp_unit": "milliseconds",
  "page_size": 100,
  "max_pages": 100
}
```

`prometheus` Reader token 是 Bearer token。`sls`、`arms` Reader token 是 **JSON 字符串**，内部字段为 `access_key_id`、`access_key_secret`、可选 `security_token`；外层仍为 `CONNECTOR_READER_TOKENS` 的 JSON 字符串值。凭证全程使用 `SecretStr`，不进入查询 schema、证据参数或数据库配置。需要分别授予这些身份目标系统的只读权限；RAM 部署按 Step 55/57 实施。当前没有 Executor 凭证或写方法。

所有端点必须 HTTPS，无 URL 凭证、查询参数和片段，始终启用证书与主机名校验。Prometheus 可配置代理路径前缀；SLS/ARMS 必须是无路径根端点，SLS hostname 以配置的 Project 开头。

- Prometheus：`GET api/v1/query_range`，构造单指标 + 服务标签的选择器，读取 matrix。拒绝错误状态、警告/信息、服务/指标错配、非有限数、重复采样时间及超序列上限。
- SLS：`GET /logstores/{logstore}?type=log`，按 `line/offset` 读取全部搜索页，使用原生 HMAC-SHA1 签名。响应必须 `x-log-progress=Complete`，`x-log-count` 与数组长度一致。只抽取配置字段及 `__time__`，丢弃其他原始日志字段。
- ARMS：API version `2019-08-08`，`SearchTracesByPage` + `GetTrace`，使用 ACS3-HMAC-SHA256 请求签名。搜索页号、总数与 Trace ID 必须一致；详情分页支持平铺及嵌套 `Children`、数组或 `{Span: [...]}` 包装，重复跨页 Span/冲突内容报错。

ARMS 搜索时间为毫秒；详情的 Application Monitoring 与 XTrace 时间单位不同，配置 `span_timestamp_unit` 为 `milliseconds` 或 `microseconds`，不按数字大小猜测。必须据实际使用的产品和 API 响应核对该配置。本次原生协议和签名经 HTTP mock/固定参考值验证，没有真实 Prometheus、SLS 或 ARMS 环境联调。

分页超过上限、返回不完整、HTTP 错误、超时与连接错误明确失败；不跟随重定向、不自建重试或轮询。原始指标/日志/Trace 留在源系统，平台仅通过既有 Ledger 保存此次按需查询的证据快照/引用，没有新增数据表、迁移或后台采集器。

协议依据：[Prometheus HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/)、[SLS GetLogs](https://www.alibabacloud.com/help/en/sls/developer-reference/use-getlogs-to-query-logs)、[SLS 签名](https://www.alibabacloud.com/help/en/sls/developer-reference/request-signatures)、[ARMS SearchTracesByPage](https://www.alibabacloud.com/help/en/arms/application-monitoring/developer-reference/api-arms-2019-08-08-searchtracesbypage-apps)、[ARMS GetTrace](https://www.alibabacloud.com/help/en/arms/application-monitoring/developer-reference/api-arms-2019-08-08-gettrace-apps)、[XTrace GetTrace](https://www.alibabacloud.com/help/en/arms/tracing-analysis/api-xtrace-2019-08-08-gettrace-arms)、[ACS3 签名](https://www.alibabacloud.com/help/en/sdk/product-overview/v3-request-structure-and-signature)。

## 自检范围

`test_observability.py` 对 Fake 与 HTTP mock 执行共享契约，覆盖服务、开始/结束边界、UTC、亚毫秒裁剪、分页完整性、嵌套 Span/微秒单位、来源协议、配置门禁、Reader/Executor 类型隔离、失败脱敏和原生签名参考值。

`test_observability_tools.py` 验证严格 schema、L0、重复注册、成功证据/审计、Policy 拒绝、Replay、非法参数和失败路径；专项禁止真实 HTTP transport、DNS/socket 连接。`test_tools_integration.py` 新增三个数据库用例，验证真实 Ledger 持久化与回放。

本次仅完成 Step 13。Step 14 变更链路 Connector、Watcher、Discovery、Agent 调查循环、Verifier、API 和前端按计划后续实施。
