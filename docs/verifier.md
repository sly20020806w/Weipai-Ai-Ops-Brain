# Step 31：独立 Verifier

本次只实现 `plans.md` 的 Step 31。`verify_action` 为 L0 Tool，独立验证引擎不用 LLM，
也不采信主 Agent 的“已恢复”判断。查询、Policy、Evidence 和审计均走唯一 Dispatcher。
Executor 仍按 Step 32 实现；本步没有运维写客户端、写凭证或实际回滚。

## 恢复判定

宿主提供不可变的 `VerificationSpec`：任务 ID、VERIFYING 版本、动作 ID、动作完成时间、
UTC 窗口、服务、集群、命名空间、Deployment、容器、目标镜像、副本数与必须验证的资源。
窗口采用 `[start, end)`，必须在动作完成之后。事件任务的服务必须与原 OpsEvent 一致。
目标与阈值由宿主提供，不把 Agent 的自然语言验证方案当作授权或成功证明。

八项检查全部通过才能恢复：

| 检查 | 成功条件 |
| --- | --- |
| Deployment | 对应集群/命名空间/名称，控制器已观测最新 generation；期望、实际、更新、Ready、可用副本数一致，Available 为 True |
| Pod | 副本数正确，全部 Running、Ready，所有容器状态完整且 Ready，目标容器镜像准确匹配 |
| 5xx | 各序列的整个窗口均不超过 1% |
| P99 | 各序列的整个窗口均不超过 500 ms |
| 成功率 | 各序列的整个窗口均不少于 99% |
| 日志 | 成功完成查询且没有 ERROR/FATAL/CRITICAL 或未知级别；零条日志允许通过 |
| Trace | 至少一条对应业务请求；Span 完整返回范围内结果均成功，未知结果按失败处理，返回 Trace 的耗时不超过阈值 |
| 资源 | 每个指定资源存在且状态匹配；RDS 最新连接采样在窗口内、距窗口结束最多 120 秒、总连接数不超过上限的 80%；指定资源没有 WARN/CRITICAL 云事件 |

指标默认每 60 秒查询，至少 3 个不重复采样；覆盖窗口首尾，最大相邻间隔 120 秒。
缺失序列、过期/稀疏样本、错误服务/窗口、查询失败或 Policy 拦截均不能被当作恢复。
标准是保守的初始配置，业务 SLO 调整通过环境变量 `VERIFICATION_CONFIG`，例如：

```powershell
$env:VERIFICATION_CONFIG = '{"max_5xx_ratio":0.01,"max_p99_ms":300.0,"min_success_ratio":0.99}'
```

该配置只保存在进程环境，不入数据库。报告只保存当次判定配置的 SHA-256 指纹，
用于检查重试时规则是否改变，不保存配置对象或环境变量值。
自带恢复样例使用 P99=120 ms，修改阈值后验收应按新标准判定。

## 证据与状态权限

每次完整验证生成 8 条事实 Evidence 和 1 条 `verify_action` 聚合 Evidence；
每条事实以及聚合调用均有 Tool 审计。报告逐项引用真实 Evidence ID，迁移原因引用聚合 ID。
成功：`VERIFYING → RESOLVED`；任一未通过：`VERIFYING → INVESTIGATING`。
整体验证 Activity 无法完成则由 Temporal 编排转人工，绝不会判定恢复。

只传 `actor=verifier` 已不能设置 RESOLVED。TaskService 要求独立验证模块持有与任务、
版本、聚合 Evidence 绑定的内部权限，并校验成功报告、同任务事实和独立成功调用审计。
Tool 自身只读；Agent 或 Replay 调用 Tool 得到报告不能迁移任务。
状态仍只通过 `tasks/` 服务写入，证据、状态历史、审计同事务提交。

任务行锁保证并发去重。提交后丢响应、Worker 重启与 Activity 重试复用已提交报告，
不会再查另一时刻的数据；变更任务、版本、目标、阈值或原因不能复用旧请求。
Dispatcher Replay 返回原快照与原 Evidence ID，不调用引擎/Connector，也不改变状态。
未新增迁移，head 保持 `0011_human_interaction`。

## Temporal 接入及本步边界

同一 Worker 注册 `verifier.verify_action` Activity。现有 `AITaskWorkflow` 增加
`verification_json` 入口，接管已在 VERIFYING 的验证阶段；该入口拒绝混用调查与人工等待。
Step 32 接入 Executor 后可调用相同 Activity，不另建队列、调度器或任务引擎。
失败回到 INVESTIGATING 后，本步演示结束；持续重新调查/执行的闭环由后续步骤接入。

Step 17 的 `verifier.placeholder` 名称保留以兼容已有历史，现在执行显式的恢复 Fake 快照
和相同八项检查及权限门禁。此恢复快照只在 local/test + Fake 下构造，没有改变已有故障样例。
正常调查/审批仍按此前步骤停在 EXECUTING 等待 Executor；批准不会触发本步演示。

原长版《最终设计方案 V1.0》仍未提供，本步依据现有 AGENTS.md、SPEC.md 和 plans.md
中对独立验证的明确要求执行。前端仍按 Step 47 建立，当前没有可运行的前端检查工程。

## 自行验收

确保 Docker Desktop 的 Linux 引擎和本项目 PostgreSQL、Temporal 容器正在运行。
已有容器暂停时，在根目录运行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
. .\use-local-deps.ps1
docker compose -f .\deploy\docker-compose.yml up -d
if ($LASTEXITCODE -ne 0) { throw '本地依赖启动失败' }
```

然后在同一个 PowerShell 窗口运行：

```powershell
.\check-verifier.ps1
if ($LASTEXITCODE -ne 0) { throw 'Verifier 专项失败' }
.\demo-verifier.ps1
if ($LASTEXITCODE -ne 0) { throw 'Verifier 演示失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
. .\use-local-temporal.ps1
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库及 Temporal 回归失败' }
.\check-workflow.ps1
if ($LASTEXITCODE -ne 0) { throw '既有 Workflow 回归失败' }
```

演示必须打印两次“伪造 actor=verifier 设置 RESOLVED：已拒绝”，恢复场景为 RESOLVED，
未恢复场景为 INVESTIGATING；每场景打印 8 个事实 ID、1 个聚合 ID、Workflow ID。
最终显示“实际运维动作执行次数：0”“Step 31 Verifier Fake 演示全部通过”“临时测试库已清理”。
脚本自动准备隔离 Worker 和临时库；不用启动 API、手动运行 Worker或提供公司凭证。
临时库清理后 Evidence 不再保留；Temporal UI 可按打印的 Workflow ID 查看 Activity 和历史。
演示复用固定历史 UTC 快照，代表动作后恢复条件，不代表执行过真实回滚。

## 自检记录（2026-10-07）

| 验收入口 | 实际结果 |
| --- | --- |
| `check.ps1` | Connector 边界、ruff、格式、mypy（300 个源文件）、Git 检查全部通过；1664 passed、364 skipped |
| `check-verifier.ps1` | 53 passed：34 项禁真实网络的离线、16 项 PostgreSQL、3 项 Temporal |
| `demo-verifier.ps1` | 两个预期状态、伪造身份拒绝、9 条 Evidence、实际运维动作次数 0，全部通过 |
| `check-db.ps1`（先加载本机 Temporal） | 351 passed、3 skipped；全量数据库和 Temporal 回归通过 |
| `check-workflow.ps1` | 36 passed；既有完整 Workflow、人工信号、超时、重启与 Replay 通过 |
| 两个新增 PowerShell 脚本 | 语法检查通过 |

统一检查跳过依赖测试，由本机专项和数据库入口运行；数据库入口沿用跳过的 3 项
为 Step 22 官方时间跳跃测试。测试与演示临时库均已清理。本次没有前端工程变化，
`frontend/` 仍只有占位文件，lint/typecheck/test 按 Step 47 接入。

自检修复了只读报告与已提交迁移检查点混用、环境阈值配置入库、报告检查项重复、
UTC 归一化、事实作用域、旧验收审计计数，以及类型和格式问题。修复后专项、
统一检查、演示和既有 Workflow 均已重新通过。计划只把 Step 31 改为完成。
