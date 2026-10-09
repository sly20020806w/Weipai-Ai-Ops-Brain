# Step 55：安全审查与加固

本步依据 AGENTS.md、SPEC.md、实际计划 plans.md 与权威原设计第 16–21 节，完成权限策略、密钥/依赖扫描和接口鉴权检查。镜像/CI、Helm/ACK 部署和影子运行仍由 Step 56–58 实施。

## 自己运行

启动 Docker Desktop，保持本项目 PostgreSQL/Temporal 本地容器运行。在 Windows PowerShell 中执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\check-security.ps1
if ($LASTEXITCODE -ne 0) { throw '安全专项失败' }
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

专项依次检查全部注册 API、Webhook、CSRF、文档入口、静态权限/扫描门禁，执行真实扫描和本机隔离 K8s RBAC，再运行既有 PostgreSQL 会话/审计测试。末尾应显示 **“Step 55 安全验收全部通过”**。统一检查含 Connector 边界、ruff/格式/mypy/pytest、Git 文件规则、安全扫描、Step 54 九项本机 E2E 与前端 OpenAPI 一致性/lint/typecheck/test/build，末尾应显示 **“统一检查全部通过”**。

扫描需访问官方 GitHub 发布和 OSV 公共漏洞库，只提交锁定包名与版本，不上传源码、凭证或环境变量。固定 Gitleaks 8.30.1、OSV-Scanner 2.6.0 下载到 `.tools/security/`，每次验证官方发布 SHA256。工具/网络失败、报告缺失、漏包或版本不符均失败，不使用旧报告替代当前结果。脱敏报告在 `.cache/security/<随机run-id>/`，含 UTC 时间及锁文件 SHA256。

## 本机 RBAC

默认使用现有 kind 容器 `weipai-sim-control-plane`；先确认 Docker 为本机 npipe/unix socket及容器 kind control-plane 标签，不使用宿主 kubeconfig或默认 Kubernetes 上下文。创建两个随机 `security-<id>-platform/target` 命名空间和同前缀只读 ClusterRole/Binding；结束后只删除这些测试对象。

使用短时 Reader token 经 stdin 实际认证，`auth whoami` 必须返回 `ai-reader`，创建 ConfigMap 的 server dry-run 写请求必须返回 **Forbidden**。真实 SubjectAccessReview 检查两身份的 create/update/patch/delete/deletecollection 与敏感/提权入口；还检查正常读取、命名空间越界和无授权 Executor。token 只在内存中，不进入参数、文件或报告。

如果默认容器不存在，传入另一个本机 kind control-plane：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\check-security.ps1 -LocalKindNode '自己的本机kind控制节点容器名'
```

如需准备新测试集群，安装官方 kind 后在本机执行 `kind create cluster --name weipai-security`，再传 `-LocalKindNode 'weipai-security-control-plane'`；自行创建的集群可在结束后用 `kind delete cluster --name weipai-security` 清理。脚本不自动安装集群，不向 ACK 写入。

## K8s 权限与命名空间范围

`deploy/security/kubernetes.json` 是原生 JSON List 模板，可交给 kubectl。占位符为 `PLATFORM_NAMESPACE`、`TARGET_NAMESPACE`、`BINDING_PREFIX`；渲染只替换已解析的 JSON 字符串，缺值失败，不通过原始文本插值修改结构。

- Reader/Executor 为不同 ServiceAccount，均关闭自动 token 挂载，不创建长效 Secret token。
- Reader 只读目标命名空间 Deployment/Pod 的 get/list、Event 的 get/list/watch；Discovery 所需 Namespace 元数据有 cluster get/list。无通配 Allow、Secret 读取、Pod exec、TokenRequest、bind/escalate/impersonate 或任何资源写权限。
- Executor 基线无 RoleBinding/ClusterRoleBinding，没有工作负载权限。真实写 Connector 继续只允许 MockTransport。
- 真实 Reader 配置 `KUBERNETES_CONFIG.namespace_allowlist` 为已授予 RoleBinding 的命名空间，如 `["prod"]`；Discovery/Timeline 返回筛选后的命名空间，查询和 Event Watch 的越界输入在联网前被拒。非法、空或重复白名单被拒；白名单内不存在的 namespace 报错。旧本机 Fake/mock 未设置时保持兼容，ACK 上线必须明确配置范围。多个业务命名空间分别生成 Role/Binding。

RBAC 权限为叠加授权，模板不能抵消其他 Binding；上线需审查身份的全部绑定。原生 RBAC 无法约束回滚镜像参数、replicas、完整动作哈希或幂等键，普通 SA token 不能替代现有动作级凭证。公司动作端仍须实施 Policy/审批、精确目标/参数、有效期与持久化去重。依据：[Kubernetes RBAC](https://kubernetes.io/docs/reference/access-authn-authz/rbac/)、[ServiceAccount](https://kubernetes.io/docs/concepts/security/service-accounts/)。

## 阿里云 RAM

`ram-reader.json` 包含现有 Cloud/SLS/ARMS Connector 的 **14 个精确 RAM Action**，无产品级或 `Describe*`/`Get*` Allow。`Deny + NotAction` 拒绝清单外全部动作，包括写入、AssumeRole、PassRole 与未来 API，其他 Allow 无法覆盖该拒绝。

| 占位符 | 资源 ARN 形态 |
| --- | --- |
| ECS_RESOURCE | `acs:ecs:<region>:<account>:instance/<id>` |
| RDS_RESOURCE | `acs:rds:<region>:<account>:dbinstance/<id>` |
| REDIS_RESOURCE | `acs:kvstore:<region>:<account>:instance/<id>` |
| MQ_RESOURCE | `acs:mq:<region>:<account>:<instance-id>` |
| SLB_RESOURCE | `acs:slb:<region>:<account>:loadbalancer/<id>` |
| VPC_RESOURCE | `acs:vpc:<region>:<account>:vpc/<id>` |
| CDN_RESOURCE | `acs:cdn:*:<account>:domain/<domain>` |
| LOG_RESOURCE | `acs:log:<region>:<account>:project/<project>/logstore/<logstore>` |

DNS DescribeDomainInfo、CMS DescribeSystemEventAttribute 与 ARMS SearchTracesByPage/GetTrace 官方只支持 `Resource: "*"`，仅以精确动作放行；服务/时间窗由 Connector 校验。RocketMQ OpenAPI 名称与 RAM Action 不同：OnsInstanceBaseInfo→mq:QueryInstanceBaseInfo，OnsTopicList→mq:ListTopic。

`ram-executor.json` 在没有真实云写 Action 的当前阶段显式 Deny 全部操作。后续新增云写 Connector 必须定义具体动作和服务器参数限制再更新基线；session policy 不能给被 Deny 的角色扩权。信任策略、账号/OIDC 绑定由真实公司身份决定，本步不虚构账户、委托或角色。

**本步只执行 RAM 结构/精确动作/资源范围/显式拒绝的离线测试，未创建或绑定真实云角色、签发云凭证或试写生产。** 真实 RAM 与全部附加策略在 Step 57 授权环境验收。依据：[RAM 元素](https://www.alibabacloud.com/help/en/ram/policy-elements)、[RDS 权限](https://www.alibabacloud.com/help/en/rds/developer-reference/api-rds-2014-08-15-describedbinstanceperformance)、[MQ 权限映射](https://www.alibabacloud.com/help/en/apsaramq-for-rocketmq/cloud-message-queue-rocketmq-4-x-series/developer-reference/api-ons-2019-02-14-onstopiclist)、[DNS 权限](https://www.alibabacloud.com/help/en/dns/api-alidns-2015-01-09-describedomaininfo)、[CMS 权限](https://www.alibabacloud.com/help/en/cms/cloudmonitor-1-0/developer-reference/api-cms-2019-01-01-describesystemeventattribute)、[ARMS 权限](https://www.alibabacloud.com/help/en/arms/application-monitoring/developer-reference/api-arms-2019-08-08-gettrace-apps)。

## 扫描与修复

Gitleaks 目录扫描覆盖源码、文档、脚本、锁文件与未跟踪文件；有 HEAD 时另扫 Git 历史。当前仓库全部文件未跟踪且无提交，报告明确 `git_history_scanned: false`。只排除本机工具、依赖、缓存及构建产物，不排除测试/文档，不使用基线忽略、指纹忽略或行内放行。stdin 合成随机 token 阳性对照必须检出并完整脱敏，对照不计入源码密钥命中数。

OSV 覆盖两个锁文件全部依赖（包含开发依赖和本项目包），按包名/版本/生态逐一比较覆盖率。任何已知漏洞均使门禁失败，包含低分及未知严重度，不添加漏洞忽略。结论表示检查时公共库中已知的锁定依赖风险，后续新公告需再次扫描。

首次发现 js-yaml 4.2.0 的三项 HIGH：GHSA-52cp-r559-cp3m、GHSA-5p4m-2wfm-xmqj、GHSA-2883-xcg3-v3hh。通过 pnpm 4.x 精确 override 固定至 `4.3.2` 并更新锁文件，保持上层依赖不变，没有新增应用依赖。[修复公告](https://github.com/advisories/GHSA-2883-xcg3-v3hh)。

## 接口鉴权

全部 `/api` 业务读写接口匿名访问先返回 401，早于参数解析和数据库调用。已登录写请求仍需 CSRF/Origin，会话操作人和审计契约不变。Swagger `/docs`、OAuth2 redirect、ReDoc、`/openapi.json` 及尾随斜杠现要求同一会话，响应 no-store；登录后正常访问，离线 OpenAPI 导出及前端生成不受影响。

公开例外为 `/health`、登录入口与验签 Webhook。健康检查 200；登录需公开才能创建会话，保留标记/来源/密码/限速校验；全部 13 个有效 Webhook origin 缺签名为 401，Cookie 不能代替签名；已登录写请求缺 CSRF 为 403。没有关闭鉴权的配置选项。

## 自检修复

修复了模板名称前缀未替换、kubectl 对批量授权检查输出多个 JSON、写入拒绝消息大小写、uv 子进程继承 PowerShell 7 模块路径、扫描覆盖漏计本项目包等问题。初次模板生成的两个只读测试对象已按实际名称核对后清理；后续成功/失败路径均自动清理。额外负向测试确认远端 Docker、非 kind 节点及远端 kubeconfig 在任何集群写请求之前被拒。

前端完整检查首次出现三个默认 5 秒交互超时和一个页面等待超时；相同三个文件单 worker、15 秒测试时限复跑 **45 passed**。将这一资源与时限配置固定在 Vitest，保留全部业务断言、MSW 未声明请求拒绝和测试隔离，随后执行完整统一检查。

## 2026-10-09 最终验收记录

- `check-security.ps1` 在 Windows PowerShell 5.1 实际复跑通过：117 项离线安全测试、56 项既有鉴权测试（含 14 项真实 PostgreSQL）；本机 RBAC 59 项拒绝、3 项正常读取与真实短时 Reader 身份检查通过。
- Gitleaks 源码密钥命中 0、阳性对照检出且脱敏；OSV 对 46 个 Python 包、420 个 npm 包完整扫描，全部已知漏洞 0。最终统一检查扫描报告为 `.cache/security/6cbe57b7d71544ed9e7f8e997ddb1acd/`；含本机 RBAC 的专项报告为 `.cache/security/68e40f1c579943758151a48bece85903/`。
- 最终 `check.ps1` 完整通过：Connector 导入边界、ruff/格式、mypy（473 个检查源文件）、Git 环境文件规则；后端 2289 passed/590 skipped；真实本机 PostgreSQL/Temporal E2E 9 passed、零跳过；前端 OpenAPI 与 16 个生成文件逐字节一致、lint/typecheck、132 项组件测试和生产 build。
- 590 项既有数据库/Temporal 专项继续使用原独立入口，本步未全量复跑。没有新迁移，应用 head 保持 0016_catalog_audit；没有真实生产运维请求或飞书消息。
- 新 PowerShell 脚本 UTF-8 BOM 与 5.1 语法通过，权限 JSON 可解析；本步随机 K8s 命名空间/角色/绑定和临时数据库清理完成，四个项目本机依赖仍 healthy。
- plans.md 仅将 Step 55 改成完成；Step 56–58 保持未完成。真实云端 RAM 效果尚未验收，属于 Step 57 的授权环境范围。
