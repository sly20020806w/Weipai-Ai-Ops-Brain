# Step 32：Executor

本步只实现计划第 32 项：重启、扩缩容、回滚的授权执行、动作专属短时凭证、
执行 Evidence／审计，以及向 `VERIFYING` 的交接。没有实现 Step 33 的六条件熔断器，
也没有新增学习、事故复盘、API 或前端页面。

目录中的开发计划实际为 `plans.md`。执行前已完整读取根目录 `AGENTS.md`、
`SPEC.md` 和 `plans.md`；长版《最终设计方案 V1.0》仍未提供，沿用既有步骤记录的
来源限制，依据本次指定的 SPEC 和 Step 32 明确要求实现。

## 自行验收

启动 Docker Desktop，确认本项目 PostgreSQL、Temporal、Temporal UI、Temporal Admin
四个常驻容器运行。在项目根目录的 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-executor.ps1
if ($LASTEXITCODE -ne 0) { throw 'Executor 专项失败' }
.\demo-executor.ps1 -Interactive
if ($LASTEXITCODE -ne 0) { throw 'Executor 演示失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
. .\use-local-temporal.ps1
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库与 Temporal 回归失败' }
```

专项包含禁止真实网络的单测与独立本机数据库／Temporal 测试。脚本自动创建并迁移
`weipai_db_test_*` 临时库、运行隔离 Worker、清理临时库；不需要公司凭证或手动启动 API。
自动演示可省略 `-Interactive`。交互演示只对第一个 Fake 场景接收你输入的「批准」或
「拒绝」，后面固定演示拒绝和超时；所有操作均为 Fake。

输入「批准」时应看到：

- `payment-service` 镜像 `v2.3.7 → v2.3.6`，Fake 执行次数 1。
- 执行 Evidence ID 和 `VERIFYING`；数据库、Workflow 状态历史一致。
- 相同请求重投后执行次数仍为 1。
- 同一凭证操作其他服务被拒绝；到期时被拒绝。
- 关闭写客户端后 Dispatcher Replay 返回原快照，执行次数不增加。
- 拒绝／超时进入 `ESCALATED`，签发次数与执行次数都是 0。
- 最后显示「Step 32 Executor Fake 演示全部通过」「临时测试库已清理」。

输入「拒绝」时第一个场景也为 `ESCALATED` 且执行次数 0。脚本会打印 Workflow ID，
可以在 [本地 Temporal UI](http://127.0.0.1:8080) 的 `default` 命名空间查询该 ID，
查看审批 Signal、`executor.execute_action` Activity 和状态历史。

## 授权与执行

`ExecutionStore` 只加载 Ledger 中同任务当前版本的真实 Action Plan，重查 Reviewer
门禁和有效 Policy。默认首批三类动作至少 L3，需要审批；显式的测试环境 Policy allow
路径无需审批，也必须满足全部计划、目标和参数校验。

需审批时重新核对 Step 30 的任务／等待版本／计划证据／完整动作哈希／批准决定和
操作人审计。拒绝、超时、缺审批、跨任务、旧版本、修改 Policy 均在签发凭证前拒绝。
模型或调用者传入 `actor=executor`、`approved=true` 都不能建立执行权限。

执行前以 L0 `get_execution_target` 经 Dispatcher 读取 UID、资源版本、镜像和副本数。
宿主绑定服务、集群、命名空间、Deployment、容器及允许版本。参数和资源现状不符时
拒绝；回滚需要原镜像一致，扩缩容需要原副本数一致，动作端还会原子核对 UID／资源版本。

| 动作 | 精确参数 | 修改范围 |
| --- | --- | --- |
| `restart_service` | `{"strategy":"rolling"}` | 当前 Deployment 的一次滚动重启，镜像／副本不变 |
| `scale_service` | `{"from_replicas":3,"to_replicas":5}` | 仅改变副本，处于宿主最小／最大副本范围内 |
| `rollback_prod` | `{"from_version":"v2.3.7","to_version":"v2.3.6"}` | 仅改变绑定容器镜像，版本需在宿主已核验白名单内 |

回滚版本白名单是宿主对镜像可用性、配置与数据库兼容性的前提声明，不能由模型补充。
本地缺省绑定只存在于 Fake 支付样例；真实环境没有自动推断目标或默认生产权限。
未知动作、低于 L3 的首批动作、额外参数、缩容至 0 和超出副本上限均拒绝。

`execute_action` 声明缺省 L5，普通 Dispatcher 调用仍被拒绝。仅 `ExecutionStore`
在验收真实计划和审批后，建立绑定任务、精确命令、数据库会话的内部执行范围。
Dispatcher 重新定级到实际动作的宿主等级并核对 Policy，再调用 Tool。
写客户端不进入主 Agent／专家的常规只读注册表。

## 动作凭证和真实通道边界

独立的 `ReaderCredentials` 与 `ExecutorCredentials` 类型不能互换或复用 token。
动作凭证有效期默认 60 秒，最大 300 秒，绑定 `execution_id` 与完整命令 SHA-256：
任务、计划、服务、资源 UID／版本、动作、镜像与副本前后值都在范围内。
Fake 签发端保存不可伪造的随机 token，执行端重新核对精确范围和半开有效期，
同一凭证不能换目标、参数或动作。凭证不进入 Evidence、审计、Temporal 入参或结果。

原生 Kubernetes RBAC 的授权属性是资源、名称和操作，字段级控制由准入机制承担；
普通 ServiceAccount token 不能天然绑定一次批准的参数。
依据：[Kubernetes 授权文档](https://kubernetes.io/docs/reference/access-authn-authz/authorization/)、
[RBAC 文档](https://kubernetes.io/docs/reference/access-authn-authz/rbac/)。
本步没有把普通 token 包装成所谓精确动作凭证，也没有部署新中间件。

`HTTPKubernetesWriteConnector` 实现供已有运维平台动作端适配的显式协议：
`POST execution/inspect` 使用 Reader，`POST execution/credentials` 使用独立签发身份，
`POST execution/actions` 只使用动作 token。动作端必须在服务器侧限制完整命令哈希、
目标与有效期，并持久化幂等回执。HTTPS、超时、禁止重定向、签发有效期与身份不复用、
响应 scope 和固定错误脱敏均经 HTTP mock 验证。

公司真实动作端协议及其服务器限制尚未提供，当前 HTTP 写客户端只接受 `MockTransport`，
在真实联网前拒绝构造；Worker 继续仅允许 local/test + Fake。**生产写入没有开放**。
后续真实接入须核对公司原生协议及服务器授权／幂等能力，不能删除此门禁后直接
把长期 Kubernetes token 交给 Agent。安全策略部署与真实逐级开放仍属于 Step 55／57／58。

## 重试、审计与独立验证

外部副作用和 PostgreSQL 事务不是一个原子事务。先提交只追加的 `execution.intent`
与准备审计，再执行动作。幂等 ID 由真实计划证据、完整计划哈希与动作 ID 确定，
重试复用原 UID／资源版本和精确命令，绝不改查到新资源后盲目重复动作。

成功执行经 Dispatcher 恰好追加一条 `execute_action` Evidence 和一条 Tool 审计，
再追加对应的执行审计。全部计划动作成功才写 `execution.complete` 并由 tasks 服务
迁移至 `VERIFYING`。并发、最终提交后丢响应复用原结果；动作已提交但审计失败时
保留已提交意图，通过相同幂等键向动作端核对，不再施加一次重启／扩缩容／回滚。
不确认结果的失败由 Temporal 有限重试，耗尽后沿用既有 `ESCALATED` 异常出口。

Fake 远端状态和回执在注入的 Connector 对象中维护，Worker 重启测试复用同一 Fake
远端对象；真正进程退出会重置整个 Fake 世界。真实动作端必须跨进程持久化幂等回执，
这项服务端能力尚未生产验收。没有实现自己的队列、重试器或调度器。

Executor 只能证明变更已提交。它不写 `RESOLVED`；独立 Verifier 继承最后一项真实
执行回执的服务、资源目标、动作完成时间、预期镜像和副本数，改写验证目标被拒绝。
专项验证了后续八项独立恢复检查：恢复才为 `RESOLVED`，未恢复为 `INVESTIGATING`。
本步 Workflow 在执行交接的 `VERIFYING` 返回，未自动伪造恢复指标或提前进行事故关闭；
可由宿主调用已有 `verifier.verify_action` Activity 完成后续独立验证。

## 配置与历史兼容

默认 `EXECUTION_CONFIG.enabled=false`。启用本地 Fake 执行：

```powershell
$env:AGENT_CONFIG = '{"enabled":true,"max_steps":20}'
$env:EXECUTION_CONFIG = '{"enabled":true,"credential_ttl_seconds":60}'
.\run-worker.ps1
```

新 OpsEvent 由宿主将配置写入 Workflow 的 `execution_enabled`；直接使用 Workflow
输入时也须显式启用该布尔字段，Activity 同时要求宿主 enabled。占位流程、既有 Step 30
演示默认沿用不执行动作的行为。`executor-v1` Temporal patch 保持历史回放兼容。
鉴权、API、前端仍按后续计划实现；当前 frontend 只有预留目录，没有 pnpm 工程可检查。

本步没有新增依赖或迁移，head 仍为 `0011_human_interaction`。环境配置对象和密钥
不入库；Ledger 只保存实际操作目标、事实快照、哈希和引用。

## 自检记录（2026-10-07）

- Step 32 专项：49 项通过，包括 26 项禁真实网络单测、23 项本机 PostgreSQL／Temporal。
- 统一检查：ruff／格式／mypy（310 个源文件）、Connector 边界和 Git 检查通过；
  1690 项单元测试通过，387 项依赖测试按独立入口执行。
- 既有 Workflow 专项：36 项通过，历史占位流程和 Replay 保持兼容。
- 最终完整 PostgreSQL／Temporal 回归：374 项通过，3 项既有时间跳跃测试沿用独立入口跳过。
- Fake 自动与真实终端中文交互演示：批准回滚一次到 VERIFYING，拒绝／超时零执行，凭证越界／过期拒绝，
  重投与 Dispatcher／Temporal Replay 通过，临时库已清理。
- 自检修复了类型／格式、严格 JSON 证据解析、宿主配置入库、内部执行范围绑定、
  并发完成重试、宿主目标绑定变化、底层方法启用开关、UTC 规范化、验证目标与真实回执
  不一致及未执行计划混用只读验证入口的问题。
- 最终全量回归与专项的临时库均清理完成；两个新增 PowerShell 入口语法检查通过。
