# Step 14：变更链路 Connector 验收说明

本步按 AGENTS.md、SPEC.md 和 plans.md 的 Step 14 实现四类只读来源：GitLab/GitHub、Jenkins/GitLab CI、ArgoCD 和配置中心。默认使用可注入快照的 Fake。真实分支只发送固定 GET 请求，协议通过 HTTP mock 验证；本次未连接真实系统。

## 自行验收

在项目根目录的 PowerShell 执行，无需 Docker、数据库或公司凭证：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-changes.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 14 专项验收失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

专项预期 `76 passed`，随后打印 Fake JSON：

- 服务为 `payment-service`，代码比较为 `v2.3.6 → v2.3.7`，patch 有 `-db.pool.max_connections: 50` 和 `+db.pool.max_connections: 500`。
- `configuration.changes` 恰好包含连接池 `before: "50"`、`after: "500"`；未变化的超时配置不出现。
- 发布记录为 `37 / sha237 / 2026-10-01T01:20:00Z`、`36 / sha236 / 2026-10-01T00:20:00Z`，按时间倒序；构建记录也倒序。
- 时间窗为 UTC `[2026-10-01T00:00:00Z, 2026-10-01T02:00:00Z)`，结束边界的 `38` 记录不返回。
- 最后显示「Step 14 Fake 样例验收通过（未连接真实系统）」。

JSON 演示直接展示 Connector 返回值；同一脚本中的 Tool 专项测试实际通过 Dispatcher 验证 Evidence 和审计，并非演示程序绕过 Dispatcher 替 Agent 调用。

统一检查预期 `1098 passed, 161 skipped`，导入边界、ruff、格式、mypy 与 Git 环境文件检查通过，最后显示「统一检查全部通过」。前端目前只有预留目录，按 Step 47 建立并纳入 lint/typecheck/test；本步不提前实现前端。

本机 Docker Desktop 与项目 PostgreSQL 容器运行时，可进一步执行：

```powershell
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '变更 Tool 数据库验收失败' }
```

预期 `161 passed`，最后显示临时测试库已清理和数据库验收全部通过。新增两项测试跨会话核对两个 Tool 的真实 PostgreSQL Evidence/审计、精确 Evidence ID 引用，以及 Connector 关闭后的 Replay；测试库随机命名，现有应用库和 Temporal 数据库不参与测试写入。

若本地容器已停止，在同一个 PowerShell 窗口恢复：

```powershell
.\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw '本地依赖启动失败' }
.\check-db.ps1
```

## Tool 契约

`register_change_tools(registry, git, ci, argocd, config_center)` 只注册两个 L0 Tool。Agent 的调用入口仍是 `ToolDispatcher.dispatch()`。

| Tool | 必填入参 | 返回快照 |
| --- | --- | --- |
| `compare_versions` | `service_name`、`from_version`、`to_version` | `code` 的文件 patch、比较语义与源引用；`configuration` 的逐键前后差异与源引用 |
| `get_recent_deployments` | `service_name`、`start`、`end` | `deployments` 与 `builds` 两个倒序列表；`history_scope=source_retained_history` |

时间必须带时区，统一转换到 UTC；时间窗须 `start < end`，最多 30 天，按半开区间筛选。版本支持 tag、branch 和 SHA，包括合法的 `release/v2.3.7`，拒绝路径跳转与查询字符串。Tool 不接受端点、源项目、凭证、风险覆盖或审批字段。

每次成功调用恰好生成 1 条 Evidence 和 1 条 Tool 审计；两个来源的快照一同留在该次 Tool 的 Evidence 中。Policy 拒绝或等待审批时不读取任何来源；任一来源失败时不生成成功 Evidence。Replay 返回既有快照和原 Evidence ID，只增加回放审计，且不调用已关闭的 Connector。

构建与发布各保留真实 `revision`。不会因为时间相近就推断构建已经被部署，Jenkins 没有 Git 插件信息时 `revision=null`。发布历史表示 ArgoCD 保留的已发布记录，不能据此判断当前业务健康或代替独立 Verifier。

## 配置与真实协议

宿主从环境变量加载配置，默认 `CONNECTOR_MODE=fake`，`local/test` 禁止真实模式。真实身份由 `CONNECTOR_READER_TOKENS` 中的 `git`、`ci`、`argocd`、`config_center` 四个独立 Reader token 提供，未提供 Executor 身份或写入口。不要把凭证写入版本库或数据库。

以下是**端点配置结构样例**，使用保留的 `.invalid` 域名；不是生产连接指令：

```json
{
  "GIT_CONFIG": {
    "provider": "gitlab",
    "base_url": "https://git.invalid/api/v4/",
    "services": {"payment-service": "weipai/payment-service"}
  },
  "CI_CONFIG": {
    "provider": "gitlab_ci",
    "base_url": "https://git.invalid/api/v4/",
    "services": {"payment-service": "weipai/payment-service"}
  },
  "ARGOCD_CONFIG": {
    "base_url": "https://argo.invalid/",
    "services": {"payment-service": "payment"}
  },
  "CONFIG_CENTER_CONFIG": {
    "base_url": "https://config.invalid/read-api/",
    "services": {"payment-service": "payment-service"},
    "allowed_keys": ["db.pool.max_connections", "db.pool.timeout_seconds"]
  }
}
```

四个环境变量各自取对应的 JSON 对象，不能把上面的整个对象作为一个变量。配置可以附加 `timeout_seconds`（默认 15）、`page_size`（默认 100、最大 100）、`max_pages`（默认 100、最大 1000）。HTTPS 地址不能含身份、查询参数、片段或路径跳转；服务绑定只来自宿主配置，源标识符按路径段编码，不跟随响应中的链接或重定向。分页超限或重复数据时拒绝不完整结果；没有自建重试逻辑。

| 来源 | 固定读取接口与鉴权 | 处理范围 |
| --- | --- | --- |
| GitLab | `projects/{project}/repository/compare?from=…&to=…&straight=true`；`PRIVATE-TOKEN` | 直接比较。比较超时、collapsed 或 too_large 时拒绝，不把截断结果当完整 diff |
| GitHub | `repos/{owner}/{repo}/compare/{base}...{head}`；Bearer + API version `2022-11-28` | `comparison_kind=merge_base`；只读取第一页的 files，不需要收集 commit 分页；文件数达到 300 时保守拒绝。无 patch 的文件标记 `content_available=false` |
| GitLab CI | `projects/{project}/pipelines`；`PRIVATE-TOKEN` | 用 `X-Next-Page` 连续页码读完，保留创建时间、SHA、状态，之后按 UTC 时间窗筛选 |
| Jenkins | `job/{folder}/job/{job}/api/json?tree=builds[…]`；Reader username + API token 的 Basic Auth | 用 tree 范围分片读取保留的构建，毫秒时间戳转 UTC；可选从 Git 插件 `actions.lastBuiltRevision.SHA1` 提取 revision |
| ArgoCD | `api/v1/applications/{application}`；Bearer | 验证应用名，从 `status.history` 读取 `id/revision/deployedAt`，只支持单源 Application，不触发 refresh/sync |
| 配置中心 | `services/{binding}/versions/{version}`；Bearer | 明确的版本化 GET/JSON 适配协议，只保留非敏感白名单键并计算变化 |

GitHub 模式将 `GIT_CONFIG.provider` 改为 `github`，base_url 设为公司的 GitHub API 根地址（公共 GitHub 的 API 根为 `https://api.github.com/`），绑定为 `owner/repository`。Jenkins 模式将 `CI_CONFIG.provider` 改为 `jenkins`，配置 `username`，绑定是 folder/job 路径，base_url 可以包含 Jenkins 的部署前缀。实际生产身份必须在源系统限制为只读；HTTP 客户端的 GET 限制不替代源系统权限配置。

公司配置中心的产品与 API 说明尚未提供，因此没有假定 Nacos/Apollo 端点。当前适配协议响应如下：

```json
{
  "service_name": "payment-service",
  "version": "v2.3.6",
  "values": {
    "db.pool.max_connections": "50",
    "db.pool.timeout_seconds": "5"
  }
}
```

服务名与版本必须精确匹配，`values` 为字符串值的对象；增加键用 `before=null`，删除键用 `after=null`，不变的键省略。白名单拒绝常见 password、secret、token、credential、API/Access/private key 字段，返回中未在白名单内的配置不会进入快照。白名单应只列非敏感业务配置，配置值本身也不得是凭证。

协议依据：[GitLab Repositories API](https://docs.gitlab.com/api/repositories/#compare-branches-tags-or-commits)、[GitLab Pipelines API](https://docs.gitlab.com/api/pipelines/)、[GitHub Compare API](https://docs.github.com/en/rest/commits/commits#compare-two-commits)、[Jenkins Remote Access API](https://www.jenkins.io/doc/book/using/remote-access-api/)、[ArgoCD API](https://argo-cd.readthedocs.io/en/stable/developer-guide/api-docs/) 和 [ArgoCD Application 类型](https://github.com/argoproj/argo-cd/blob/master/pkg/apis/application/v1alpha1/types.go)。公司版本、代理前缀、鉴权权限、Jenkins 插件和配置中心字段需要取得实际说明后核对；HTTP mock 通过不等于实际环境联调完成。

## 实现位置与自检范围

- `backend/app/connectors/changes/`：共享只读接口、配置、严格快照、四类 HTTP 客户端、Fake、配置工厂和离线演示。
- `backend/app/tools/changes.py`：两个 Tool 的严格入出参、L0 声明和注册；输出验证服务/版本一致及时间范围、去重、倒序。
- `backend/tests/test_changes.py`：六种协议路径、鉴权、分页、UTC、diff 完整性、错误脱敏、配置白名单、配置分支与凭证隔离，全程禁止真实 HTTP/DNS/socket 请求。
- `backend/tests/test_changes_tools.py`：Fake 样例、每次调用的一对证据/审计、Policy 拦截、参数限制、失败与关闭源后的 Replay。
- `backend/tests/test_tools_integration.py`：新增两个 PostgreSQL 跨会话 Tool 验收，复用现有独立临时库入口。

本步没有新增依赖、数据库迁移、L1+ 操作、Workflow 或 Change Timeline 表。采集调度、Discovery 和统一变更事件落库分别按后续 Step 17–19 执行。现有本地应用库仍为 `0004_context_graph`。
