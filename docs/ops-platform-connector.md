# Step 11：运维平台 / CMDB 只读 Connector

本次完整阅读 `AGENTS.md`、`SPEC.md` 和实际计划文件 `plans.md`，只实现第一个未完成项 Step 11。两份规格引用的最终设计原文未在目录提供，沿用现有 SPEC 的明确约束。服务树、应用、负责人和工单全部从 Connector 读取，不新增 CMDB 表或同步任务。

公司接口说明尚未提供。HTTP 实现采用下面明确列出的 GET/JSON/Bearer 适配协议，地址和四个路径没有默认值，必须由宿主显式配置；文中的路径只是 mock 样例，不是已确认的公司 API。Fake 与 HTTP mock 已验收，真实公司的字段、鉴权和分页协议需要取得接口说明后核对。此项不代表已经与生产联调或上线。

## 在本机自行验收

在 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-ops-platform.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 11 专项验收失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

不需要 Docker、数据库、公司地址或凭证。专项脚本用 `uv --offline --frozen` 复用已有依赖，先执行 104 项测试，再运行 Fake 样例。预期看到：

- `104 passed`，专项退出码 0。
- JSON 中 `mode` 是 `fake`，`service` 是 `payment-service`，`business` 是 `支付业务（样例）`。
- `owners` 包含 `owner-payment` / `支付负责人（样例）`；`tickets` 包含 `TICKET-1001`，状态 `open`，UTC 时间为 `2026-10-01T01:00:00Z` 与 `2026-10-01T01:05:00Z`。
- `Step 11 Fake 样例验收通过（未连接真实系统）`。
- 统一入口中 Connector 导入边界、ruff、格式、mypy（78 个源文件）和 Git 环境文件检查全部通过；pytest 为 `767 passed, 153 skipped`，最后显示「统一检查全部通过」。

153 项 PostgreSQL 集成测试沿用既有 `check-db.ps1` 独立入口，本次没有数据库变更，未重跑这些测试。前端工程在 Step 47 建立，本次无前端 lint/typecheck/test 可运行。

只想查看样例时，可单独运行：

```powershell
. .\scripts\project.ps1
$uvPath = Get-ProjectUv
& $uvPath run --offline --frozen --directory backend python -m app.connectors.ops_platform
if ($LASTEXITCODE -ne 0) { throw 'Fake 样例验收失败' }
```

样例模块显式选择 local/Fake，并覆盖该 Connector 的凭证和配置，并隔离无关的数据库/LLM 模式，不会因为宿主设置了 real 而连接公司系统。样例不是 HTTP API；当前页面和接口仍按后续计划实现。

## 共享接口

`OpsPlatformConnector` 继承 Step 10 的 `ReadOnlyConnector`，Fake 与 HTTP 实现继承同一个抽象接口，并通过 mypy 严格检查：

| 方法 | 读取内容 | 未找到时 |
| --- | --- | --- |
| `list_service_tree()` | 服务树节点、业务父子关系 | 空 tuple |
| `list_applications(business_id=...)` | 全部应用或指定业务应用 | 空 tuple |
| `get_application(service_name)` | 指定服务的唯一应用、业务 ID 与负责人 ID | `OpsPlatformNotFound` |
| `list_owners(service_name)` | 指定服务的负责人及团队 | 空 tuple |
| `list_tickets(service_name=..., status=...)` | 工单列表，两个条件可组合 | 空 tuple |
| `get_ticket(ticket_id)` | 指定工单详情 | `OpsPlatformNotFound` |

源系统状态保留为字符串，不与 AI Task 状态枚举混用。工单必须有带时区的创建/更新时间，读取后转成 UTC，拒绝无时区时间与更新时间早于创建时间。记录冻结、列表为 tuple；保留源系统 ID 和所需快照，忽略响应中未声明的字段，不在数据库中复制全量 CMDB。

Fake 内置两个样例业务、两个应用、两个负责人和两张工单。通过 `FakeOpsPlatformConnector(snapshot)` 可注入 `OpsPlatformSnapshot`；入口重新验证快照，拒绝重复 ID/服务名、不存在的业务/负责人/服务引用、服务树环路。样例名称均标注“样例”，不代表微派事实。

两种实现都支持 `async with` 与幂等 `aclose()`，关闭后读取失败。只读对象不暴露写入、关闭工单、应用更新或任意 HTTP 请求方法。

## 宿主配置与真实适配协议

宿主通过 `create_ops_platform_connector(settings)` 创建实例，复用 Step 10 的配置工厂。默认 `CONNECTOR_MODE=fake`，local/test 拒绝 real；复制或修改 Settings 也必须重新校验。HTTP 客户端构造时不发送请求。

| 环境变量 | 用途 |
| --- | --- |
| `CONNECTOR_MODE` | Fake / real 选型 |
| `CONNECTOR_READER_TOKENS` | JSON 中 `ops_platform` 项提供该系统 Reader token，使用 `SecretStr` 脱敏 |
| `OPS_PLATFORM_CONFIG` | 以下 JSON 对象；只在创建真实适配器时必需 |

协议测试使用的非真实配置：

```json
{
  "base_url": "https://ops.example.invalid/company/api/v1",
  "service_tree_path": "cmdb/tree",
  "applications_path": "cmdb/apps",
  "owners_path": "cmdb/owners",
  "tickets_path": "ops/tickets",
  "timeout_seconds": 15,
  "page_size": 100,
  "max_pages": 100
}
```

URL 必须为 HTTPS，不能带内嵌凭证、query、fragment 或路径跳转；四个路径必须为相对路径，不能自带查询参数或换主机。所有配置来自环境变量/K8s Secret，不加载 `.env` 或写入数据库。真实分支只接受名称为 `ops_platform` 的 Reader 凭证，不接收 Executor 类型，不携带 Executor 身份。

HTTP 只执行 GET，附带 `Authorization: Bearer <Reader token>` 和 `Accept: application/json`，禁用环境代理和重定向。查询参数如下：

| 路径配置 | 可用业务查询参数 |
| --- | --- |
| `service_tree_path` | 无 |
| `applications_path` | `business_id` 或 `service_name` |
| `owners_path` | `service_name` |
| `tickets_path` | `ticket_id`，或可组合的 `service_name` / `status` |

每次 GET 都附带 `limit=page_size`，第二页起附带 `cursor`；响应为 `{"items": [...], "next_cursor": "opaque-cursor"}`，最后一页 cursor 为 null 或省略。游标始终作为参数，不解释为下一页 URL。重复游标、重复记录 ID、超出 `max_pages` 都抛错，不能把部分结果当成完整清单。单条查询必须只返回与目标匹配的那一条记录；列表筛选响应必须符合查询条件。

`items` 字段契约如下，源系统多余字段会被丢弃：

| 类型 | 字段 |
| --- | --- |
| `ServiceTreeNode` | `id`、`name`、可选 `parent_id` |
| `Application` | `id`、`service_name`、`name`、`business_id`、非空 `owner_ids` 数组 |
| `Owner` | `id`、`name`、`team` |
| `Ticket` | `id`、`title`、`description`、`service_name`、`status`、`requester_id`、可选 `assignee_id`、`created_at`、`updated_at` |

若公司 API 使用其他 envelope、字段名、分页方式或鉴权方式，应在 `connectors/ops_platform/` 根据实际协议改解析/鉴权并同步 mock。不得将源系统 HTTP 代码移到 Tool 或 Agent。真实连通性在取得公司接口信息及后续只读上线时验证，本次不访问外部系统。

超时、连接异常、HTTP 错误与响应校验错误使用固定错误文本，不输出 token、请求 URL 或源系统错误正文。HTTP 404 转成 `OpsPlatformNotFound`。Connector 不自行重试；后续 Workflow 重试仍由 Temporal 管理。

## L0 Tool 与 Dispatcher

按 AGENTS.md 的“新接入系统 = Connector + Fake + Tool”约定，提供三个宿主显式注册的高级 Tool。未接入 AI 调查循环，也没有启动 Discovery 或工单 Workflow。

| Tool | 输入 | 结果 |
| --- | --- | --- |
| `get_ops_service` | `service_name` | 应用、从根到业务的归属路径、负责人；拒绝关系缺失/环路和负责人引用冲突 |
| `list_ops_services` | 可选 `business_id` | 服务树和应用清单 |
| `query_ops_tickets` | `ticket_id`，或可选服务/状态条件 | 工单列表或单条详情；ID 与筛选条件互斥 |

通过 `register_ops_platform_tools(registry, connector)` 注册到已有 `ToolRegistry`，每项声明风险 `L0` 和严格入出参 schema。Agent 只能经既有 `ToolDispatcher.dispatch()` 调用，不能指定模式、凭证、URL 或伪造审批。一次成功 Tool 调用生成一条 Evidence 和一条审计，即使内部读取多页或多个资源；Policy 拒绝则不读取。Replay 复用历史快照和 Evidence ID，不调用 Connector。

Step 18 的 `get_service_context` / Discovery、Step 38 的工单处置，以及其他系统 Connector 留在各自步骤。本次没有 L1+ 操作、数据库表、迁移、API 或前端变更。

## 自检记录（2026-10-06）

104 项离线专项与 Fake 样例通过。测试禁止实际 HTTP transport、DNS 和 socket 连接，真实实现使用 `.invalid` mock 地址与测试 Reader token。覆盖两种实现同契约、配置选型、生命周期、分页、重定向拒绝、目标错配、UTC、异常脱敏、Policy、单次证据/审计与 Replay。统一检查全部通过，`767 passed, 153 skipped`；PowerShell 验收脚本语法通过。首轮发现的测试 Settings 动态参数类型注解问题已改为重新验证入口并通过 mypy；最后复查发现的宿主数据库/网关配置影响 Fake 演示的问题已隔离并增加回归测试。
