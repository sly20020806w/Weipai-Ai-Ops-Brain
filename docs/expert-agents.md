# Step 26：按需专家 Agent

本步只实现开发计划 Step 26。六类专家提供调查意见，主 Agent 负责综合证据并给出
最终结论；复杂问题可调用专家，简单问题继续原有调查。专家不会设置任务状态，
不会生成或执行生产动作。Reviewer、Action Plan 等仍属于后续步骤。

## 依据与边界

已完整读取 AGENTS.md、SPEC.md 与 plans.md。实际计划文件名为 plans.md。
它们引用的《Weipai AI Ops Brain 最终设计方案 V1.0.md》仍未出现在仓库中，本步依据
现有 SPEC 的“一个主 Agent + 按需专家”及 Step 26 的明确字段和验收要求实现，
没有补写或猜测缺失的设计章节。

## 调用与证据

主 Agent 可调用 L0 `consult_expert`，参数包括 expert、question、服务、UTC 窗口与
可选的证据 ID。咨询只能使用主调查相同的服务与时间窗。Tool 声明、Policy、执行、
Evidence 与调用审计全部复用唯一 Dispatcher；模型不能提供风险、环境或审批权限。

专家使用自研有限循环，通过受限 I/O 查询已注册高级 Tool。模型可见 Tool 先按
白名单过滤；即使模型伪造其他名称，Dispatcher 仍在执行前拒绝并追加审计，
错误码为 `expert_tool_not_allowed`（未注册名称为 `tool_not_found`）。
禁止递归咨询和写操作，宿主定义白名单，模型不能扩权。

| 专家 | 固定 Tool 白名单 |
| --- | --- |
| Kubernetes | get_service_context、get_dependencies、get_k8s_status、get_service_runtime、query_events、query_metrics、query_logs |
| Database | get_service_context、get_dependencies、get_cloud_resources、query_metrics、query_logs、query_traces |
| Network | get_service_context、get_dependencies、get_cloud_resources、query_metrics、query_traces |
| Release | get_service_context、get_recent_changes、get_recent_deployments、compare_versions、get_service_runtime、query_metrics、query_logs |
| Security | get_service_context、get_dependencies、get_recent_changes、get_k8s_status、query_events、query_logs |
| Cost | get_service_context、get_cloud_resources、query_metrics |
| HolmesGPT | 无；只分析已有证据快照 |

所有成功专家查询各自生成事实 Evidence 和 `expert:<角色>` 调用审计。意见结构为
assessment、findings、confidence、uncertainties，每项判断必须引用本次实际成功
查询的 Evidence ID。拒绝、失败、不存在的 ID 不能成为意见依据。

成功咨询再生成一条 `consult_expert` 意见 Evidence 和主 Agent 调用审计。主 Agent
观察这条意见，并引用其 ID 形成自己的结论；专家意见的内部事实引用可沿 Ledger
逐层查询。它不会自动变成最终根因或已验证结果。

## 预算、重试与 Replay

环境变量 `AGENT_CONFIG` 增加 `experts_enabled`（默认 true）和 `expert_max_steps`
（默认 8，可设 1–30）。`enabled` 仍控制事件是否启用主 Agent，原默认不变。

```powershell
$env:AGENT_CONFIG = '{"enabled":true,"max_steps":20,"experts_enabled":true,"expert_max_steps":8}'
```

专家每次模型响应和每个 Tool 都计一步；实际允许预算取宿主上限、请求上限和
主任务剩余预算的最小值。专家步数计入主任务总预算。专家无有效意见、被 Policy
拒绝或超预算时，调查转交人工，不能继续形成无证据结论。

专家咨询在既有 `agent.observe` 事务内执行，任务行锁、步序和规范化请求哈希
保护并发与重试。已提交咨询由检查点复用，不再次调用专家、模型或 Connector。
观察保存失败时，专家事实、意见和相关审计一起回滚；事务提交前的崩溃可能重做
只读查询。Temporal 负责 Activity 重试与 Worker 恢复，没有新调度器或队列。

Dispatcher Replay 直接返回原意见快照和 Evidence ID，不重新调查或联系 Holmes。

## HolmesGPT 适配

提供 Connector、Fake、原生 HTTP 协议适配与同一个咨询 Tool。Fake 接收已有事实
快照并返回带实际引用的 RCA 意见；输入只接受同任务、有成功 Tool 审计的事实证据，
其他任务、检查点、缺失 ID 与其他专家意见均被拒绝。

HTTP 协议依据 [HolmesGPT 官方 HTTP API](https://holmesgpt.dev/latest/reference/http-api/)
实现 `/api/chat` 的非流式快照请求及结构化输出，配置模型名、认证、超时，禁重定向，
不自动重试，错误内容脱敏；返回工具执行或审批请求时拒绝接受意见。

**真实 Holmes 通道当前未开放。** 原生 Holmes 服务能自主运行内置工具，仅发送
“不要调用工具”的提示无法保证本平台 Dispatcher 边界。因此 HTTP 实现当前强制
要求注入 MockTransport，无 transport 时在任何网络请求前拒绝构造。生产接入须
先完成可验证的原生工具隔离及公司 AI 网关配置；本步没有部署 Holmes、安装 SDK、
fork 源码或允许外部专家绕过 Dispatcher。

`HOLMES_CONFIG` 为 base_url、model、snapshot_only=true、可选 timeout_seconds；
独立 Reader 凭证来自 `CONNECTOR_READER_TOKENS.holmes`，不会写入 Ledger。
local/test 一律 Fake，真实协议仅 HTTP mock 验收。

## 自行验收

启动 Docker Desktop。若项目已有容器已停止，在根目录 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw '本地依赖启动失败' }
.\check-experts.ps1
if ($LASTEXITCODE -ne 0) { throw '专家专项失败' }
.\demo-experts.ps1
if ($LASTEXITCODE -ne 0) { throw '专家演示失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库回归失败' }
```

专项验证六角色查询、Database 越权拒绝及审计、Holmes 快照意见、引用拒绝、
Policy、预算、幂等、回滚、实际 Worker 重启与 Temporal 历史回放。
演示打印 Database、HolmesGPT 的意见 Evidence ID 和主 Agent 综合结论；复杂场景
暂停到 WAITING_INFORMATION，简单场景专家调用为 0。最后显示
“Step 26 专家 Fake 演示全部通过”。演示和专项均自动清理本机临时库，
演示额外终止隔离 Workflow，不需要手动启动 API 或 Worker。

前端仍为空壳，按 Step 47 建立，不提前引入前端工程。本步没有新依赖或迁移，
Alembic head 保持 0010_runbook_engine。运维系统及 LLM 全部使用 Fake，
本机 PostgreSQL/Temporal 为实际依赖，未访问生产系统。

## 本次自检记录（2026-10-07）

- 专家专项 `53 passed`：27 项禁止真实网络的离线测试、24 项 PostgreSQL 与 2 项
  本机 Temporal。六类专家查询、越权拒绝及审计、递归拒绝、Policy、引用校验、
  预算、并发/重试、观察失败回滚、Worker 重启与两种 Replay 全部通过。
- `demo-experts.ps1` 的复杂/简单场景实际运行成功，意见与内部事实引用逐条读库核验，
  主 Agent 综合结论停在 WAITING_INFORMATION，简单场景专家调用 0 次。演示清理后
  Temporal UI 中对应执行为 Terminated，这是隔离任务清理的预期状态。
- 统一检查：ruff、格式、mypy（252 个源文件）、Connector 导入边界和 Git 检查通过；
  pytest `1525 passed, 280 skipped`。依赖测试按专项入口执行，前端工程尚未建立。
- 在已加载本机 Temporal 环境的同一个窗口执行数据库回归，结果为
  `267 passed, 3 skipped`；3 项跳过为既有定时驱动官方时间跳跃测试。
  另执行完整 Workflow 专项 `36 passed`，覆盖所有等待/信号、状态历史和恢复流程。
- 新增两个 PowerShell 脚本语法通过；四个依赖容器 healthy，Temporal UI HTTP 200；
  验收后临时测试库数量为 0，应用库 head 保持 `0010_runbook_engine`。
- 自检修复了 Fake 脚本响应契约、Kubernetes Fake 命名空间、JSON 快照类型与格式问题。
- Docker 曾因临时 socket 端点不可访问而无法启动。确认进程停止后，按照仓库已有
  恢复方法备份并重建了两个临时通信目录，没有删除配置、镜像、卷或数据库。
  备份保存在 `C:\Users\38149\AppData\Local\Docker\run.recovery-step26-20261007090004`
  和 `C:\Users\38149\AppData\Local\docker-secrets-engine.recovery-step26-20261007090004`。

Step 26 已标记完成，没有进入 Step 27。
