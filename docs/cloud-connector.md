# Step 15：阿里云只读 Connector 验收说明

本步依据 AGENTS.md、SPEC.md 与实际计划文件 plans.md 的 Step 15，提供阿里云资源和系统事件的共享只读接口、真实 HTTP 适配器、可注入 Fake，以及 L0 Tool `get_cloud_resources`。本地和测试环境只使用 Fake；真实协议通过 HTTP mock 验证，未连接实际阿里云账号。

## 你可以直接跑的验收

在项目根目录的 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-cloud.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 15 专项验收失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

两个命令均无需云凭证或数据库。本机已有锁定的 uv/Python 环境；新机器先按根 README 执行 `uv sync --directory backend --frozen --python 3.12`。专项命令使用 `--offline --frozen`，不会下载依赖。

专项预期 `95 passed`，随后显示 `mode=fake`、`get_cloud_resources` 和 payment-service 的 JSON：

- 8 类关联资源：ECS、RDS、Redis、RocketMQ、SLB、VPC、DNS、CDN。
- RDS 活跃连接 `480`、总连接 `520`、最大连接 `600`，采样时间为 `2026-10-01T01:08:00Z`。
- 查询时间窗为 `[2026-10-01T01:00:00Z, 2026-10-01T01:10:00Z)`，含 1 条关联 RDS 的 WARN 云事件。
- 最后显示「Step 15 Fake 样例验收通过（Dispatcher Evidence/审计见专项测试，未连接真实系统）」。

专项中的 `test_step15_acceptance_exact_evidence_audit_and_replay` 真正调用统一 Dispatcher：成功调用恰好产生 1 条 Evidence 和 1 条 Tool 审计，二者引用相同 Evidence ID；关闭 Connector 后回放返回原结果和 ID，只追加回放审计。JSON 演示单独读取 Fake Connector，不替代上述 Dispatcher 验证。

统一检查预期 `1193 passed, 162 skipped`，导入边界、ruff、格式、mypy 与 Git 环境文件检查均通过，最后显示「统一检查全部通过」。162 项数据库集成测试在此入口按既有规则跳过，可用以下命令实际执行：

```powershell
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库集成验收失败' }
```

需要 Docker Desktop 和本项目本地 PostgreSQL 容器运行。预期 `162 passed`，最后显示临时测试库已清理和数据库验收全部通过。新增 `test_cloud_evidence_commit_and_replay` 验证跨会话的真实 PostgreSQL Evidence、参数、审计、UTC 采集时间与原 ID 回放。只写随机命名的临时库，不写现有应用库或 Temporal 数据库。

## 接口与数据含义

`CloudConnector.get_cloud_resources(CloudQuery)` 是 Fake 与真实分支的同一只读接口。`register_cloud_tools(registry, connector)` 向宿主注册一个 L0 Tool，Agent 的 Tool 调用继续走现有 Dispatcher：定级 → Policy → 执行 → Evidence/审计。Policy 拒绝或要求审批时不会读云端。Replay 直接复用原证据，不连接云端。

Tool 输入只接受 `service_name`、可选的 `start` / `end`；时间必须带时区并成对提供，范围最多 24 小时。不提供时真实分支查询最近 15 分钟，Fake 使用确定性的样例时钟。任何端点、云动作、资源 ID、地域、身份或审批参数都会被拒绝。

资源状态是本次读取时的状态；`start/end` 仅限定 RDS 性能采样和云事件，不能用本接口获取过去某时刻的完整云状态。输出 `scope=configured_service_bindings` 明确表示只查询配置关联的资源，不能把未绑定的资源当成不存在。

RDS MySQL/MariaDB 查询 `MySQL_Sessions`，依据 `ValueFormat` 的字段名解析活跃和总连接数，返回窗口内最新采样；保留浮点数以兼容源系统平均采样。无采样为 `availability=no_data`，数值和采样时间为 null；其他引擎为 `unsupported_engine`，`metric_key=null`。最大连接数来自实例详情。当前实现不据此推断健康或替代 Verifier。

DNS 域名详情没有统一运行状态，`status=null`，保留源系统的 `in_black_hole` / `in_clean` 标志；不虚构 healthy。MQ 首批采用 RocketMQ 4.x 的实例接口，状态保留源系统数值的字符串，例如 `5`。SLB 首批采用 CLB 的实例详情接口。

每条资源带 `source_ref`，每条事件带准确关联的产品、资源、地域及 UTC 时间。资源按产品/地域/ID 排序，事件按发生时间倒序。仅保留定义的状态字段与最新采样快照；不透传任意 Tags、Description、灾备配置或事件 Content，不存储原始指标序列。

## 真实分支配置

配置与密钥只从 Settings 环境变量进入：

| 变量 | 用途 |
| --- | --- |
| `APP_ENV` | local/test 强制 Fake；真实分支只允许 staging/production |
| `CONNECTOR_MODE` | 缺省 fake，真实为 real |
| `CLOUD_CONFIG` | JSON：产品端点、服务到资源的准确绑定、超时和分页上限 |
| `CONNECTOR_READER_TOKENS` | JSON：`cloud` 值为序列化的 AccessKey/可选 STS 凭证 JSON，使用 Reader 身份 |

`CLOUD_CONFIG` 示例（这里只展示结构，不运行真实请求）：

```json
{
  "endpoints": {
    "ecs": "https://ecs.aliyuncs.com",
    "rds": "https://rds.aliyuncs.com",
    "redis": "https://r-kvstore.aliyuncs.com",
    "mq": "https://ons.cn-hangzhou.aliyuncs.com",
    "slb": "https://slb.aliyuncs.com",
    "vpc": "https://vpc.aliyuncs.com",
    "dns": "https://alidns.aliyuncs.com",
    "cdn": "https://cdn.aliyuncs.com",
    "cms": "https://metrics.aliyuncs.com"
  },
  "services": {
    "payment-service": [
      {"product": "ecs", "resource_id": "i-payment", "region_id": "cn-hangzhou"},
      {"product": "rds", "resource_id": "rm-payment", "region_id": "cn-hangzhou"},
      {"product": "redis", "resource_id": "r-payment", "region_id": "cn-hangzhou"},
      {"product": "mq", "resource_id": "MQ_INST_payment", "region_id": "cn-hangzhou"},
      {"product": "slb", "resource_id": "lb-payment", "region_id": "cn-hangzhou"},
      {"product": "vpc", "resource_id": "vpc-payment", "region_id": "cn-hangzhou"},
      {"product": "dns", "resource_id": "payment.example.com", "region_id": "cn-hangzhou"},
      {"product": "cdn", "resource_id": "static.payment.example.com", "region_id": "cn-hangzhou"}
    ]
  },
  "timeout_seconds": 15,
  "page_size": 100,
  "max_pages": 100
}
```

示例 ID 为虚构值；生产配置须按实际账号、地域和产品端点填入。无需配置未绑定产品的端点，cms 必填。每个服务最多 100 个绑定。CMS 按源系统返回的 `RegionId + ResourceId` 准确关联：若源系统使用 ARN，绑定中补 `event_resource_id` 为完整的原始 ARN；不会用名称包含或后缀猜测归属。重复或有歧义的绑定会拒绝。

`cloud` Reader 凭证的内部 JSON 形状如下；外层 `CONNECTOR_READER_TOKENS.cloud` 存放该 JSON 的序列化字符串：

```json
{"access_key_id":"<Reader AccessKey ID>","access_key_secret":"<Reader AccessKey Secret>","security_token":"<可选 STS Token>"}
```

无 STS 时省略 `security_token`。这些字段以 SecretStr 保存，不写数据库，不向 Tool 暴露。凭证 JSON 结构复用已有阿里云可观测性 Connector 的类型；不复用它的身份。宿主须给 cloud 配置独立的只读 RAM 身份。只读客户端拒绝 ExecutorCredentials 和其他 Connector 的 Reader 凭证。

## 原生接口与官方依据

真实分支固定使用 HTTPS GET 和 [ACS3-HMAC-SHA256 签名](https://help.aliyun.com/zh/sdk/product-overview/v3-request-structure-and-signature)，可携带 STS Token。签名与实际 URL 采用一致的 RFC3986 编码；不跟随重定向、不读取系统代理、不自建重试或调度器。请求只能使用以下清单：

| 产品 | 只读动作 | API 版本 |
| --- | --- | --- |
| ECS | [DescribeInstances](https://help.aliyun.com/zh/ecs/developer-reference/api-ecs-2014-05-26-describeinstances) | 2014-05-26 |
| RDS | [DescribeDBInstanceAttribute](https://help.aliyun.com/zh/rds/developer-reference/api-rds-2014-08-15-describedbinstanceattribute)、[DescribeDBInstancePerformance](https://help.aliyun.com/zh/rds/developer-reference/api-rds-2014-08-15-describedbinstanceperformance) | 2014-08-15 |
| Redis | [DescribeInstanceAttribute](https://help.aliyun.com/zh/redis/developer-reference/api-r-kvstore-2015-01-01-describeinstanceattribute-redis) | 2015-01-01 |
| RocketMQ | [OnsInstanceBaseInfo](https://help.aliyun.com/zh/apsaramq-for-rocketmq/cloud-message-queue-rocketmq-4-x-series/developer-reference/api-ons-2019-02-14-onsinstancebaseinfo/) | 2019-02-14 |
| SLB/CLB | [DescribeLoadBalancerAttribute](https://help.aliyun.com/zh/slb/classic-load-balancer/developer-reference/api-slb-2014-05-15-describeloadbalancerattribute) | 2014-05-15 |
| VPC | [DescribeVpcAttribute](https://help.aliyun.com/zh/vpc/developer-reference/api-vpc-2016-04-28-describevpcattribute) | 2016-04-28 |
| DNS | [DescribeDomainInfo](https://help.aliyun.com/zh/dns/api-alidns-2015-01-09-describedomaininfo) | 2015-01-09 |
| CDN | [DescribeCdnDomainDetail](https://help.aliyun.com/zh/cdn/developer-reference/api-cdn-2018-05-10-describecdndomaindetail) | 2018-05-10 |
| 云事件 | [DescribeSystemEventAttribute](https://help.aliyun.com/zh/cms/cloudmonitor-1-0/developer-reference/api-cms-2019-01-01-describesystemeventattribute) | 2019-01-01 |

ECS 按唯一 ID 读取并核对总数，其他资源使用单实例/单域名详情。RDS 采样从覆盖请求边界的分钟窗口查询，再精确裁剪为 `[start,end)`。CMS 读取账号在窗口内的事件，分页至不足一页，再做准确资源关联；重复页或达到上限时明确失败，不把部分结果当作完整证据。原生业务错误、超时、协议不符或资源 ID/地域不符也会失败；错误不携带源响应正文或凭证。

## 自检与本步范围

2026-10-06：95 项专项测试与 Fake 演示通过；全量统一检查中 ruff、格式、mypy（129 个源文件）、导入边界与 Git 检查全部通过，1193 项单元测试通过，162 项数据库测试由独立入口实际执行通过并清理临时库。专项阻断真实 HTTP transport、DNS 与 socket 连接，只允许 Fake/MockTransport，覆盖服务与时间边界、签名参考值、STS、错误脱敏、分页完整性、畸形响应、身份分离、环境门禁、宿主配置隔离及 Dispatcher Evidence/审计/Policy/Replay。

自检修复了严格类型、格式、CMS 成功标志的类型判断和不支持引擎的指标声明。本步没有新增依赖、数据库表或迁移、写操作、通知、Workflow、Discovery、HTTP API 或前端。前端仍为预留目录，按 Step 47 建立后接入 lint/typecheck/test；没有进入 Step 16。
