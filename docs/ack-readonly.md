# Step 57：ACK 只读上线准备与验收

本步的本地开发与验收已完成，按用户确认，**Step 57 标记为完成，真实上线待验证**。用户尚未入职，没有真实 ACK、公司系统或 Reader 身份；ACK 部署、真实 Discovery 服务图及云端权限验收待入职接入后补齐，未启用真实生产动作。

## 现在可以亲自检查

启动 Docker Desktop，在仓库根目录的 Windows PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
powershell.exe -NoProfile -File .\check-ack.ps1
powershell.exe -NoProfile -File .\check.ps1
```

`check-ack.ps1` 首次准备经过固定 SHA256 校验的官方 Helm 3.19.0，并按 Chart.lock 获取固定 Temporal Chart。它只渲染资源，不读取 kubeconfig、不部署 Kubernetes，不接入真实公司系统。已有本机 PostgreSQL 与 Temporal 必须启动；命令自动载入已有本地容器配置，不打印密码。

预期结果：`helm lint` 成功、只读资源检查通过、8 项无效参数被拒绝、39 项只读专项全部通过且无跳过。专项在临时数据库中实际启动新的只读 Worker 入口，由 Temporal Schedule 触发 Fake Discovery，读回 **25 个节点、31 条关系**，完成 Workflow 历史 Replay，退出时清除 Ready 文件、隔离 Schedule 与临时数据库；真实运维和外网请求数为 0。`check.ps1` 另运行后端静态检查、全部默认单测、既有 Fake E2E、安全扫描和前端检查。

若想在浏览器中亲自查看已有 Fake 服务图，可复用上一阶段的本机演示：

```powershell
powershell.exe -NoProfile -File .\demo-cognition-pages.ps1 -Interactive
```

按照终端打印的地址和登录提示进入“服务 / Context Graph”，选择 payment-service，查看节点关系及边的来源、置信度、新鲜度。结束后回终端按 Enter 自动清理。此演示采用已完成的本地 Fake 模式，不代表 ACK 部署完成。

## 已实现的部署与运行边界

Chart 位于 `deploy/helm/weipai-ops/`。api 与 worker 使用同一后端镜像的原有两入口；frontend 使用既有同源代理镜像。应用镜像必须引用 SHA256 digest。容器使用非 root、只读根目录、有限 `/tmp`、资源限制和健康探针。应用与 Temporal 使用不同的 selector，避免同名组件交叉选中。

Temporal 复用官方 `temporalio/helm-charts` **1.7.0**，server/admin-tools 固定为项目已有 **1.31.0**；只连接 PostgreSQL，未引入其他存储中间件。SQL schema 使用 pre-install/pre-upgrade Job；Temporal namespace 使用普通 Job，避免 `--wait` 与 post-install hook 互锁。应用 Alembic 迁移为独立 pre-install/pre-upgrade Job，没有新增数据库迁移。官方 Chart 的标签可能仍显示其默认 appVersion 1.32.0，实际镜像由 values 显式固定为 1.31.0。

`OPS_READ_ONLY=true` 强制禁止 Executor 配置、执行目标和 Agent 启用，并在 Policy 中追加 L1–L5 的 deny；环境规则的 allow 不能覆盖它。`WORKER_MODE=readonly_discovery` 只注册既有 `DiscoveryWorkflow` 与 `discovery.refresh` Activity，周期仍由 Temporal Schedule 管理。原来的完整 Fake Worker 保留 local/test、Fake 与 localhost 门禁。生产 AI 调查、事件消费和影子评估仍待 Step 58，不用占位或 Fake 闭环冒充真实生产能力。

首次模式保留登录、退出及查询；登录后的修改、聊天、审批等请求返回 403，避免接纳没有消费者的任务。未登录业务 API 仍返回 401；Webhook 先验签，无签名返回 401，通过签名后仍拒绝任务接入。平台自身写入图、会话和数据库迁移属于平台存储，不是对运维目标的生产写操作。

Worker 独占 Reader ServiceAccount 和短时投射 token，token 最长 3600 秒，每次创建 K8s Connector 都重新读取 token 和 CA，支持 kubelet 轮换。K8s Connector 的命名空间白名单与 Chart RBAC 保持一致。Reader 只能读取 namespaces、白名单命名空间中的 deployments/pods/events，events 另允许 watch；不能读取 Secret、exec Pod 或修改工作负载。API/前端使用无绑定的 console 身份，不挂载 Reader token；API 独占认证 Secret，Worker 只加载五类 Discovery 来源的必要配置和只读身份。

独立 Executor ServiceAccount 仅保留名称，不绑定 Role，不运行 Pod，不生成长期 token Secret，不引用任何写凭证。真实云端 RAM 只读效果仍需在接入时核对；不能根据凭证名称推定权限正确。

## 入职后需要的实际参数

不要把密钥发到聊天或写进 values。先由公司授权的环境准备：

- 目标 ACK 的 kubeconfig 路径、准确上下文、独立平台命名空间、允许观察的业务命名空间；确认平台命名空间 default ServiceAccount 没有业务系统权限，Temporal schema Job 使用它但不配置 Reader/Executor 的 RBAC。
- 推送本次后端/前端镜像后的不可变 digest、拉取 Secret 名称；控制台 HTTPS 域名、IngressClass 和 TLS Secret。
- PostgreSQL 应用库以及 `temporal`、`temporal_visibility` 两个数据库；为应用库预先安装 pgvector，数据库账户只授权各自平台数据库。生产数据库的备份、TLS/CA 和可用性应按公司的部署规范配置。
- 运维平台、K8s、ARMS、Git 和阿里云的只读连接信息。当前 Discovery 使用这五类来源；公司运维平台协议、字段和分页必须与既有适配器逐项核对，不能直接假设示例接口适用于公司。
- 按 `deploy/security/ram-reader.json` 的实际资源 ARN 配置 Reader RAM 身份，保留清单外动作的显式拒绝；不要创建、绑定或提供 `ram-executor.json` 的写身份。

Chart 只引用已有 Secret，没有 Secret 的 data/stringData。默认引用如下（可通过无密钥的个人 `*.local.yaml` 改名，Git 已忽略该后缀）：

| Secret | 必填键 | 使用方 |
| --- | --- | --- |
| weipai-platform-database | DATABASE_URL，值为 PostgreSQL asyncpg URL | api、worker、迁移 Job |
| weipai-console-auth | AUTH_CONFIG，使用现有单用户配置且 public_origin 为实际 HTTPS Origin | api |
| weipai-reader-runtime | OPS_PLATFORM_CONFIG、ARMS_CONFIG、GIT_CONFIG、CLOUD_CONFIG、CONNECTOR_READER_TOKENS | worker |
| weipai-temporal-database | password | 官方 Temporal SQL schema Job/server |
| weipai-console-tls | tls.crt、tls.key | Ingress |

`CONNECTOR_READER_TOKENS` 在投射 K8s token 模式下必须恰好包含 `ops_platform`、`arms`、`git`、`cloud` 四个键。ARMS/cloud 的值使用既有 `AlibabaReaderKey` JSON 协议；不能加入 `kubernetes` 的静态 token、Executor 或通知身份。配置格式见现有 `docs/ops-platform-connector.md`、`docs/observability-connectors.md`、`docs/change-connectors.md`、`docs/cloud-connector.md`。Git/云资源服务绑定不足时会报告 missing_bindings，不能虚构关系。

本机自检还对全部 Helm 资源执行了已知 kind 仿真集群的服务端 dry-run，验证 Kubernetes schema/admission，临时命名空间已清理。它未启动 Chart 工作负载，不能替代 ACK 中 Pod Ready、首次真实 Discovery 或真实 RAM 授权验收。CI 已加入本机只读 Worker 专项与 Helm 渲染步骤，本次没有推送触发远端运行。

## 真实上线与最终验收（当前未执行）

先在目标上下文核对部署命名空间和权限、准备上述已有 Secret 与数据库，然后把 Chart 默认的 `registry.invalid`/`.invalid` 域名、镜像 digest、数据库地址/用户、集群名和业务命名空间改为实际值。内置 Temporal 的 `temporalAddress` 必须与其 fullnameOverride 和 Service 端口一致，temporalNamespace 必须由 namespace Job 创建；也可关闭 `temporal.enabled` 引用公司已有 Temporal。

在 Helm 可执行文件加入 PATH 后，显式指定 kubeconfig 和上下文，不改变当前默认上下文：

```powershell
helm dependency build .\deploy\helm\weipai-ops
helm lint .\deploy\helm\weipai-ops --strict -f .\ack.local.yaml
uv run --frozen --directory backend python ../scripts/check_helm.py --values "$PWD\ack.local.yaml"
helm upgrade --install weipai .\deploy\helm\weipai-ops --kubeconfig $env:ACK_KUBECONFIG --kube-context $env:ACK_CONTEXT --namespace weipai-ops -f .\ack.local.yaml --wait --timeout 15m
kubectl --kubeconfig $env:ACK_KUBECONFIG --context $env:ACK_CONTEXT -n weipai-ops get pods
```

上面安装命令只可在实际目标和全部参数核对后执行，不要直接使用默认占位 values。迁移 Job 只升级平台库，不修改公司的业务数据库。暂不提供自动一键生产部署，以免环境不明时选错目标。

最终必须实际确认：全部常驻 Pod Ready；HTTPS 控制台可登录；API `/health` HTTP 200 且 `status=ok`；首次 **真实** Discovery 完成，服务图中的 source/confidence/UTC 观察时间和缺失绑定报告与源系统一致。Reader 的任意写操作被拒；Executor 没有 RoleBinding/ClusterRoleBinding、工作负载、token Secret 或任何云端写凭证，且其他既有授权不能间接给它写权限。

例如下面是只查询授权的命令，不会实际创建或删除业务资源。完整命名空间范围以及全部写动词仍需按安全验收清单检查：

```powershell
kubectl --kubeconfig $env:ACK_KUBECONFIG --context $env:ACK_CONTEXT auth can-i patch deployments --namespace <业务命名空间> --as system:serviceaccount:weipai-ops:weipai-ops-reader
kubectl --kubeconfig $env:ACK_KUBECONFIG --context $env:ACK_CONTEXT auth can-i create pods --namespace <业务命名空间> --as system:serviceaccount:weipai-ops:weipai-ops-executor
```

两项预期均为 `no`；需要操作者有授权查询或 impersonation 权限。这些云端结果目前没有证据，属于 Step 57 保留的“真实上线待验证”事项，等入职接入后补齐。

实现参考：[Helm Chart 与 values.schema.json](https://helm.sh/docs/topics/charts/)、[Temporal 官方 PostgreSQL/Secret 配置](https://github.com/temporalio/helm-charts/blob/main/README.md)、[Kubernetes ServiceAccount 投射 token](https://kubernetes.io/docs/concepts/security/service-accounts/)。
