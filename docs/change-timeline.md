# Step 19：Change Timeline 验收说明

本次仅完成 `plans.md` 的 Step 19。原长版设计文件不在仓库，依据现有 `AGENTS.md`、`SPEC.md` 和计划执行，保持这一既有来源限制；未实现 Step 20 的 Knowledge Brain 或后续事件/定时驱动。

## 已实现

- `change_events` 保存服务、来源、类型、源引用、版本引用、UTC 发生时间与首次采集时间，不保存配置值、提交正文、原始日志或凭证。迁移为 `0005_change_timeline`，可升降级并通过 Alembic metadata 检查。
- `(service_name, source, kind, source_ref)` 是唯一键。重复或并发采集使用 PostgreSQL 原子 `INSERT ... ON CONFLICT DO NOTHING`，保持原 ID 和时间。同一源引用的发生时间/版本冲突时报错，整批回滚，禁止覆盖事实。
- `ChangeTimelineWorkflow` 已注册在既有 `worker` 入口。Workflow 确定一次 UTC 时间窗，Activity 执行六类来源采集并在一个事务内提交；重试复用原时间窗。失败有限重试，证据冲突直接失败，Temporal 历史仅含固定错误消息。周期和事件触发接入仍由后续计划实现，没有自建调度器。
- `get_recent_changes` 为 L0 Tool，参数为 `service_name`、`lookback_seconds`（默认 3600，最多 30 天）、可选 `end`。只查询已采集的数据库事实，范围为 `[end - lookback_seconds, end)`，按发生时间升序返回；同时间按来源、类型、源引用排序。空结果表示该时间窗没有已采集记录，不承诺源系统没有变更。
- 查询复用唯一 Dispatcher，成功时恰好追加 1 条 Evidence 和 1 条 Tool 审计。Policy 拒绝不查询；Replay 复用原证据和原快照，不执行数据库实时查询或来源采集。

## 来源语义

| 来源 | 时间与类型 | 当前限制 |
| --- | --- | --- |
| GitLab / GitHub | commit 的 committer 时间；多父提交为 Merge，其余为 Commit | Merge 表示 Git merge commit；squash / rebase 不伪装为独立 MR/PR 合并事件。GitHub 当前读取默认分支可达历史，GitLab `all=true` 读取所有分支。 |
| Jenkins / GitLab CI | 构建创建时间，Build | 不将构建成功推断为镜像生成或发布成功；不落库可变化的构建状态。 |
| ArgoCD | 原 API 保留的部署历史时间，Sync | 仅代表源系统保留的同步记录，不代表工作负载就绪。 |
| 配置中心 | `published_at`，Config | 公司协议未提供，沿用明确的版本化 GET/JSON 适配协议，仅保存 ID 与版本引用。 |
| K8s | Event 首次时间或 eventTime；Pod Pulled 为 Image，Deployment NewReplicaSetAvailable 为 Deploy，其余为 KubernetesEvent | Image 表示镜像拉取/准备的观测，不是镜像构建事件。保留源事件引用，不解析可能含敏感数据的 message。无稳定发生时间的 Event 不纳入；只采集当前接口能按服务归属且仍被源系统保留的事件。 |
| 阿里云 | 云事件时间，CloudEvent | 保留事件事实，不将指标告警当作已执行资源变更；资源/地域与服务范围沿用既有 Connector 校验。 |

Git 读取使用固定 GET 路径、受限页数和本地连续页码，不追随分页 URL；父提交和时间字段来自 [GitLab Commit API](https://docs.gitlab.com/api/commits/) 与 [GitHub Commit API](https://docs.github.com/en/rest/commits/commits)。真实协议仅 HTTP mock 验证，没有访问真实系统。

配置中心历史适配协议：

```text
GET services/{绑定的源服务}/versions?start=...&end=...&page=1&page_size=100
{
  "service_name": "payment-service",
  "versions": [
    {"id": "publish-237", "version": "v2.3.7", "published_at": "2026-10-01T01:30:00Z"}
  ],
  "next_page": null
}
```

`next_page` 必填，为 `null` 或严格连续的下一页整数。发布 ID 要稳定唯一，同一个版本可有不同发布 ID。配置值和未知字段不进入时间线；正式接入时按公司协议核对。

## 你自己运行

在项目根目录打开 PowerShell。已有本项目 PostgreSQL、Temporal 依赖容器时，先执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check.ps1
.\check-timeline.ps1
.\check-db.ps1
```

预期三个命令退出码均为 0，分别显示「统一检查全部通过」、「Step 19 Change Timeline 验收全部通过」及数据库验收通过。专项包含离线来源协议测试、PostgreSQL 并发去重/回滚/UTC/时间窗、Dispatcher/Policy/Replay 和真实本地 Temporal 的提交后丢响应重试及历史回放。测试只使用自动创建并清理的本地临时库，普通统一检查会跳过这些依赖验收。

如果本项目依赖容器未启动，先按 `deploy/README.md` 启动；恢复已有依赖配置用 `. .\use-local-deps.ps1`，随后 `docker compose -f .\deploy\docker-compose.yml up -d`。

人工演示需要两个 PowerShell 窗口。窗口一保持 Worker 运行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\run-worker.ps1
```

窗口二执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\demo-timeline.ps1
```

演示会将本项目本机应用库升级到最新 head，运行两次采集 Workflow，并创建一条 Human 验收任务与证据/审计记录。样例使用固定历史窗 **2026-10-01 01:00–02:00 UTC（北京时间 09:00–10:00）**，不随当天时钟伪造新变更。

预期输出：

1. 两个 `timeline-demo-...` Workflow ID；每轮采集 8 条变更，第二轮新增 0 条，记录 ID、发生时间和首次采集时间不变。
2. 核心链路 `Commit → Merge → Build → Image → Sync → Deploy`，另外含 Config、CloudEvent；所有时间带 `+00:00`。此样例验证发生时间序列，不声称完整的因果关系。
3. `1 条 Evidence + 1 条 Tool 审计` 和实际 Evidence ID；Replay 返回原快照。
4. 最后显示「Step 19 演示通过」。

在 [本地 Temporal UI](http://127.0.0.1:8080) 的 `default` 命名空间搜索输出的 Workflow ID，应看到 `ChangeTimelineWorkflow` 已 Completed，以及 `timeline.collect` Activity 的输入时间窗与返回计数。实际 UI 端口如已自定义，以本项目 Compose 配置为准。演示结束后窗口一按 Ctrl+C 停止 Worker；未完成执行由 Temporal 保留。

前端工程按 Step 47 预留，本步骤没有新增前端页面、OpenAPI 或前端检查命令。

## 本次自检结果（2026-10-06）

- `check.ps1`：Connector 导入边界、ruff、格式、mypy（173 个源文件）、Git 环境检查通过；pytest `1327 passed, 194 skipped`，依赖型测试使用独立入口验证。
- `check-timeline.ps1`：`36 passed`（29 项离线 + 7 项 PostgreSQL/Temporal），包括真实本地 Workflow 提交后丢响应重试及历史回放；临时库已清理。
- `check-db.ps1`：`182 passed, 2 skipped`，迁移从空库升级、降到旧版本/base、重新升级及 `alembic check` 通过。两项需要 Temporal 的测试由专项入口单独验证，新增 Timeline 项已通过；临时库已清理。
- `demo-timeline.ps1`：已实际启动既有 worker 跑通两轮采集、Evidence/审计和 Replay；本机应用库为 `0005_change_timeline`，已留下 8 条 Fake 历史变更供你复验。Worker 验收后已停止，既有 Discovery Schedule 的暂停状态保持不变。
- 新增 PowerShell 脚本语法通过；本地 Temporal UI HTTP 200；前端目录仅 `.gitkeep`，依计划未提前建前端。
