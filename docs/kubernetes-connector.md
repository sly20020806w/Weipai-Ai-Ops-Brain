# Step 12：Kubernetes 只读 Connector 验收

本步依据 `AGENTS.md`、`SPEC.md` 与 `plans.md` 的 Step 12 实现 ACK/Kubernetes 只读 Connector、Fake 以及三个 L0 高级 Tool。没有新增数据库表或迁移。Watcher、Discovery、Verifier、Executor、API 和前端按后续步骤实现。

## 自己运行

在项目根目录的 PowerShell 执行，不需要集群凭证、Docker 或数据库：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-kubernetes.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 12 专项验收失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

专项预期 `129 passed`，之后输出 `ack-fake` 集群的 JSON：`payment` 命名空间中 `payment-service` 有 1 个 Deployment（期望副本 3、Ready 副本 2）、3 个 Pod（第三个容器重启 4 次）和 1 个 `Warning / BackOff` Event。最后显示「Step 12 Fake 样例验收通过（未连接真实集群）」。

统一检查预期 Connector 导入边界、ruff、格式、mypy、pytest 和 Git 环境检查全部通过；pytest 为 `896 passed, 156 skipped`，最后显示「统一检查全部通过」。前端目录当前只有 `.gitkeep`，前端 lint/typecheck/test 在 Step 47 建立工程时纳入。

如果本机 Docker Desktop 和本项目 PostgreSQL 容器已运行，可额外检查真实证据持久化：

```powershell
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库验收失败' }
```

预期 `156 passed`，最后显示临时测试库已清理与验收全部通过。脚本只创建并清理随机名称的独立本地测试库。新增 3 项测试分别验证三个 Kubernetes Tool 成功后，跨会话读取恰好 1 条证据和 1 条 Tool 审计；关闭 Fake Connector 后回放仍成功，证据不重复、另追加 1 条回放审计。任务创建审计独立计数。

## Tool 契约

| Tool | 参数 | 输出 |
| --- | --- | --- |
| `get_k8s_status` | 必填 `namespace`，可选 `service_name` | 集群名、命名空间、Deployment 的期望/当前/Ready/可用/更新副本、generation 与 conditions |
| `get_service_runtime` | 必填 `namespace` 和 `service_name` | 集群名、服务、Pod 阶段、Ready condition、节点名、容器镜像、Ready 与重启次数 |
| `query_events` | 必填 `namespace`，可选 `service_name` | 集群名、命名空间、Event 类型、原因、消息、次数、关联对象和 UTC 时间 |

三个 Tool 使用严格参数 schema，拒绝 URL、token、任意标签表达式、身份或风险参数。服务按宿主配置的单个标签键做精确等值筛选。所有查询都明确指定命名空间；查询不到时返回空集合。`get_k8s_status` 和 `query_events` 不传服务时读取该命名空间的完整相关列表。容器环境变量、Secret 引用、注解和其他无关字段不进入本步快照。

`query_events` 按服务查询时，先获取带服务标签的现存 Deployment/Pod，再用 UID、kind、namespace、name 四项匹配 Event 的 `involvedObject`。Event 自身通常没有服务标签。同名旧 Pod、其他服务和其他命名空间事件不会混入。该筛选只涵盖现存 Deployment/Pod，已删除对象、ReplicaSet、Node 等事件可通过不传 `service_name` 的命名空间查询读取；本步不猜测其服务归属。没有状态的刚创建 Pod 返回 `phase=null`，没有观测 generation 的 Deployment 返回 `observedGeneration=null`。嵌套快照 JSON 保留 Kubernetes 的 `apiVersion`、`readyReplicas` 等字段别名，与声明的 schema 一致；Python 属性采用 snake_case。

宿主创建 Connector 后调用 `register_kubernetes_tools(registry, connector)`，由既有 `ToolDispatcher.dispatch()` 调用。入参模型、输出模型及 handler 注册位于 `backend/app/tools/kubernetes.py`。调用顺序沿用「风险定级 → Policy → 执行 → Evidence 与审计」。成功调用含可精确引用的 `evidence_id`。拒绝或读取失败不生成成功证据；错误只留下固定错误码。Replay 返回原快照与原 Evidence ID，不读取 Fake 或真实集群。

## 配置与标准 API

默认 `CONNECTOR_MODE=fake`，`local/test` 禁止真实模式。Fake 可注入 `KubernetesSnapshot`，默认样例为离线虚构数据。输入快照与返回值均深复制，外部修改 labels 不影响后续读取。

真实分支需显式设置以下环境变量（实际联调属于后续只读上线步骤）：

| 变量 | 内容 |
| --- | --- |
| `APP_ENV` | `staging` 或 `production` |
| `CONNECTOR_MODE` | `real` |
| `CONNECTOR_READER_TOKENS` | JSON，键 `kubernetes` 对应单独 AI Reader Bearer token |
| `KUBERNETES_CONFIG` | 下方结构的 JSON |

```json
{
  "cluster_name": "ack-staging",
  "base_url": "https://kubernetes-api.example.invalid:6443",
  "service_label_key": "app.kubernetes.io/name",
  "timeout_seconds": 15,
  "page_size": 100,
  "max_pages": 100
}
```

`base_url` 必须是无凭证、无路径、无查询参数的 HTTPS API Server 地址；路径由 Connector 固定生成。`ca_cert_pem` 为可选 PEM CA 文本，经同一环境变量/K8s Secret 传入；默认系统证书校验，始终校验证书与主机名，没有关闭 TLS 校验的配置。`service_label_key` 可设为集群实际采用的合法标签键，例如 `app`。一个实例对应一个配置集群；Agent 无法变更目标 URL 或凭证。

Connector 不读取本机 kubeconfig、默认 ServiceAccount、代理或 `.env`。Reader 与 Executor 凭证类型隔离，本步不提供任何写方法。Reader 在目标集群的最小权限需要包括指定命名空间的 `apps/deployments`、core `pods` 和 core `events` 的读取列表；具体 RBAC 部署按 Step 55/57 执行。

HTTP 实现只调用以下标准 GET 接口，使用已有 `httpx2`，没有新增 SDK 或依赖：

```text
GET /apis/apps/v1/namespaces/{namespace}/deployments
GET /api/v1/namespaces/{namespace}/pods
GET /api/v1/namespaces/{namespace}/events
```

Deployment/Pod 的服务查询使用 `labelSelector`。所有列表按 `limit` 与 `metadata.continue` 分页；空页带游标仍继续，游标作为不透明查询参数传递。重复 UID/对象名、重复游标、分页资源版本变化、超过页数上限、类型或筛选不匹配均报错，不把不完整结果写成成功证据。不跟随重定向，不自建重试或调度。HTTP 错误（包括 410）、超时、连接错误不输出凭证、URL 或源系统错误正文。

API 路径与分页依据 Kubernetes 官方文档：[Deployment](https://kubernetes.io/docs/reference/kubernetes-api/apps/deployment-v1/)、[Pod](https://kubernetes.io/docs/reference/kubernetes-api/core/pod-v1/)、[core/v1 Event](https://kubernetes.io/docs/reference/kubernetes-api/core/event-v1/)、[API 分页概念](https://kubernetes.io/docs/reference/using-api/api-concepts/#retrieving-large-results-sets-in-chunks)。实际 ACK 端点、CA、只读身份与服务标签尚未生产联调。

## 自检覆盖

`test_kubernetes.py` 对 Fake 与 HTTP MockTransport 执行相同只读契约，检查命名空间/服务筛选、UID 关联、自定义标签、合法分页、空分页、UTC、关闭行为、配置门禁、凭证隔离、无效协议、脱敏、错误响应与快照隔离。`test_kubernetes_tools.py` 检查三个 L0 注册、重复注册拒绝、Dispatcher 的证据/审计数量、Policy、Replay、非法输入和失败路径。专项禁止真实 HTTP transport、DNS 与 socket 连接。

`test_tools_integration.py::test_kubernetes_evidence_commit_and_replay` 在独立临时 PostgreSQL 库验证三个 Tool 的真实 Ledger 存储与回放。本步使用既有迁移 head `0004_context_graph`，不改应用库。
