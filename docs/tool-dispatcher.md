# Step 9：Tool 注册表与统一 Dispatcher

依据已完整阅读的 `AGENTS.md`、`SPEC.md` 和实际计划文件 `plans.md` 实施。本步骤实现高级 Tool 声明、注册、统一调用和单次历史回放。目录未提供两份规格引用的最终设计原文，沿用现有 SPEC 的明确要求及已记录的限制。

## 自行运行验收

在仓库根目录打开 PowerShell，依次运行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-tools.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 9 专项验收失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw 'Tool 数据库验收失败' }
```

前两个命令无需 Docker、数据库或公司网关凭证。第三个命令需要 Docker Desktop 和本项目 PostgreSQL 容器运行。

| 命令 | 预期结果 | 实际验证内容 |
| --- | --- | --- |
| `check-tools.ps1` | `45 passed`，退出码 0 | 一次 L0 调用、审批与禁止门禁、风险缺省 L5、严格入出参、重复注册、Replay 不执行实现与历史结果一致 |
| `check.ps1` | ruff、格式、mypy、Git 环境文件检查全通过；`594 passed, 153 skipped`；「统一检查全部通过」 | 新旧后端单元测试回归；数据库测试留给独立入口 |
| `check-db.ps1` | `153 passed`；「临时测试库已清理」和「…Tool Dispatcher 验收全部通过」 | 全部既有数据库测试及新增 7 项 Tool 集成测试，使用真实 PostgreSQL 验证落库、跨会话回放和事务回滚 |

专项命令以详细模式显示每个测试名。可以重点查看：

- `test_l0_exactly_one_evidence_one_audit_and_snapshot_isolation`：Fake 服务返回 `payment-service`，实现调用一次，生成恰好 1 条 Evidence 和 1 条 Tool 审计；返回结果修改不会改变保存的证据。
- `test_unapproved_never_executes`：L1–L5 及未声明风险，没有审批记录时返回 `rejected / approval_required`；实现调用数和证据数为 0，拒绝审计数为 1。
- `test_replay_same_result_original_evidence_and_no_invocation`：先调用一次再回放，两个结果和证据 ID 相同；实现总调用数仍为 1，证据总数仍为 1，新增一条 `replayed` 审计。
- `test_declarations_schema_risk_duplicate_and_copy`：入出参 schema 含必填字段和禁止额外字段，同名注册抛出 `DuplicateTool`；外部修改 schema 副本不影响注册表。

数据库验收只创建和清理 `weipai_db_test_<随机 UUID>` 临时库，自动加载本项目现有容器的本地凭证，不打印密码。现有应用库和 Temporal 库不参与本组读写测试。本步骤没有表或迁移变更，当前 head 仍为 `0004_context_graph`。

如果项目容器已停止，在同一 PowerShell 窗口恢复后重跑：

```powershell
.\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw '本地依赖启动失败' }
.\check-db.ps1
```

若没有创建过本地依赖，按 [本地依赖说明](../deploy/README.md) 先完成本地启动。前端在 Step 47 建立，本步骤通过命令验收；Tool HTTP API 在后续计划实现。

## 声明与调用契约

`app/tools/models.py` 定义严格、冻结的 `ToolModel`、`ToolDeclaration` 和 `DispatchResult`。Tool 入出参模型继承 `ToolModel`，注册表由模型生成 JSON Schema；风险直接复用 Policy 的 `RiskLevel`，未声明或传入 null 按 L5。Tool 名称为最多 64 字符的英文标识符，同名注册立即报错。实现必须为异步函数或异步 callable 对象。

`ToolRegistry.register()` 绑定名称、中文说明、入参模型、出参模型、风险与实现。`declarations()` 返回按名称排序的独立声明副本；注册表没有公开执行入口。调用统一经过 `ToolDispatcher.dispatch()`，入参只包括任务 ID、Tool 名、参数、操作人以及宿主选择的模式和历史回放位置。风险来自注册声明、环境来自宿主 PolicyEngine；不存在由 Agent 提供的 `approved`、风险或环境覆盖项。

正常顺序为：

1. 要求显式外层事务、非空操作人和存在的 AI Task。
2. 从注册表定级，再调用现有 PolicyEngine。
3. Policy 未放行则返回拒绝并追加调用审计，不执行实现、不制造观察证据。
4. 校验 JSON 参数和 schema，将默认参数规范化后调用实现；验证返回值，再深复制为快照。
5. 在同一 SAVEPOINT 中追加 1 条 Evidence 和 1 条 Tool 审计，返回结果和两个 ID；由调用方提交外层事务。

任务创建本身已有一条状态审计，所以计数时应按 `AuditEventType.TOOL_CALL` 筛选。Dispatcher 不更改任务状态，不拥有生命周期、调度或重试；后续 Temporal Workflow 负责这些行为。

审批记录在 Step 30 建立；本步骤所有 `need_approval` 均拒绝执行。Verifier 与 Executor 在 Step 31–32 建立；本步骤即使收到显式允许 L1+ 的规则，实际执行模式仍返回 `write_execution_not_ready`。测试只有内存 Fake Tool，没有外部系统 SDK 或真实运维请求。

## Replay 契约

Replay 复用同一个 `dispatch()`，使用 `mode=DispatchMode.REPLAY`，必须提供 `replay_evidence_id` 和带时区的 `replay_before`。当前 Policy 仍参与判定。Dispatcher 校验历史证据属于同一任务、同一 Tool、同一规范化参数，采集时间不晚于截止点，存在引用该证据的成功调用审计，且结果快照符合当前出参 schema。

通过后返回历史快照原值与原 Evidence ID，仅追加一条回放审计，不生成新的采集证据、不调用实现函数。新版本 schema 的默认值不会补写进历史快照。任何缺失、错配、未来数据或仅有源系统引用的证据都拒绝回放，不转为实际查询。这里交付单次 Tool 回放；事故级 Replay、评价指标与数据集在 Step 36 实现。

## 拒绝、失败与事务

| 错误码 | 含义 |
| --- | --- |
| `tool_not_found` | Tool 未注册；按 L5 判定并记录拒绝 |
| `approval_required` / `policy_denied` | 当前 Policy 需审批或禁止 |
| `write_execution_not_ready` | 当前阶段禁止 L1+ 实际执行 |
| `invalid_parameters` | 非 JSON 对象、额外字段、类型错误或非有限数值 |
| `tool_failed` | 实现报错或出参校验失败，返回 `failed`，只记录审计 |
| `invalid_replay` | 回放 ID／截止时间不完整、时间无时区或实际模式携带回放位置 |
| `replay_evidence_not_found` / `replay_mismatch` | 无历史证据或任务／Tool／参数／截止时间错配 |
| `replay_missing_success_audit` / `replay_invalid_snapshot` | 无成功调用审计，或快照缺失／schema 不符 |

没有事务、无效调用模式、无效操作人、不存在的任务会在执行前抛错。实现异常只留下固定错误码，异常原文不进入返回值或审计，避免记录源系统凭证。任务 ID 已存在且请求满足宿主基本契约时，正常成功、业务拒绝或实现失败各写一次调用审计。

存储失败会向调用方抛错；证据与成功审计的 SAVEPOINT 一起回滚，不返回成功。调用方回滚外层事务时，两条记录也一起撤销。不要将一次成功返回理解为已经提交，必须让外层事务正常提交。并发任务应使用独立会话。
