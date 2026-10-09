# Step 8：Policy Engine 与验收

依据 `AGENTS.md`、`SPEC.md` 和 `plans.md` 的 Step 8 实现。当前目录没有它们引用的《Weipai AI Ops Brain 最终设计方案 V1.0.md》，本步骤按现有文件明确的风险与权限要求实施。

## 默认判定

| 风险 | 含义 | 测试环境 | 生产环境 |
| --- | --- | --- | --- |
| L0 | 只读 | allow | allow |
| L1 | 低风险 | need_approval | need_approval |
| L2 | 有限变更 | need_approval | need_approval |
| L3 | 生产重要变更 | need_approval | need_approval |
| L4 | 高风险生产操作 | need_approval | need_approval |
| L5 | 破坏性或不可逆 | need_approval | need_approval |

`local`、`staging` 使用相同缺省规则。动作缺少 `risk_level` 或 JSON 中声明为 `null` 时，规范为 L5；与显式 L5 的结果完全相同，包括风险、原因和命中规则。无效风险（如 L6、整数、布尔值）直接拒绝。

`allow` 表示策略允许，`need_approval` 表示还需要审批，`deny` 表示禁止。Policy 只进行判定，没有 IO、状态迁移或执行能力；判定结果不充当审批记录或执行凭证。Dispatcher、审批与 Executor 分别在 Step 9、30、32 接入。

## 环境变量规则

配置只从进程环境变量读取，不加载文件、不写数据库：

- `APP_ENV`：沿用项目已有的 `local`、`test`、`staging`、`production`。
- `POLICY_CONFIG`：可选 JSON 对象，默认 `{"rules":[]}`，使用上面的缺省判定。

示例为生产 SQL 增加禁止规则：

```powershell
$env:APP_ENV = 'production'
$env:POLICY_CONFIG = @'
{
  "rules": [
    {
      "id": "deny-production-sql",
      "risk_levels": ["L4", "L5"],
      "environments": ["production"],
      "action_names": ["execute_prod_sql"],
      "decision": "deny",
      "reason": "当前禁止生产 SQL 操作"
    }
  ]
}
'@
```

每条规则的 `id`、`risk_levels`、`decision`、`reason` 必填。`environments` 省略时匹配所有环境；`action_names` 省略或为空列表时匹配所有动作，否则仅精确匹配名称。风险列表和环境列表不能为空，同一列表不允许重复值；规则 ID 不可重复；未知字段、无效枚举、空原因、畸形 JSON 均拒绝，在 `Settings` 初始化/API 创建时失败，不会忽略错误后放行。

一条规则必须同时匹配动作名称、有效风险等级和引擎环境。命中显式规则时按规则判定，未命中时使用缺省值。多条规则冲突固定取 `deny > need_approval > allow`，与配置顺序无关。结果列出全部命中规则 ID，并列出最终判定对应规则的原因，均按 ID 排序，方便后续审计复现。

动作仅包含名称与风险，不接收环境、审批或判定字段。`create_policy_engine(Settings())` 将环境固定为进程的 `APP_ENV`，供后续 Dispatcher 使用。配置、动作和结果均为冻结 Pydantic 模型，规则匹配列表为不可变 tuple。规则配置应由部署环境管理，不由 Agent 提供。

## 自行验收

从根目录执行，不需要 Docker、数据库或网关凭证：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-policy.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 8 专项验收失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

预期：专项测试 `103 passed`；统一检查中 ruff、格式、mypy、pytest 与 Git 环境文件检查全部通过，pytest 为 `549 passed, 146 skipped`，最后输出「统一检查全部通过」。146 项 PostgreSQL 集成测试沿用 `check-db.ps1` 的独立临时库验收入口；本步骤没有数据库或迁移变更。前端工程在 Step 47 建立，当前没有前端检查命令。

专项测试实际覆盖：L0–L5 × 四种环境的缺省矩阵、测试/生产的配置矩阵、未声明及 null 风险与 L5 完全相同、规则范围与名称精确匹配、所有冲突顺序、确定性原因、环境变量加载、非法配置启动拒绝与冻结模型。测试阻止真实 HTTP、DNS 和 socket 连接，不接触外部运维系统。

## 手动查看判定

下面的命令只在本机计算并打印结果，不执行任何动作：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
. .\scripts\project.ps1
$uvPath = Get-ProjectUv
$env:APP_ENV = 'test'
Remove-Item Env:POLICY_CONFIG -ErrorAction SilentlyContinue
@'
from app.config import Settings
from app.policy.engine import create_policy_engine
from app.policy.models import PolicyAction, RiskLevel

engine = create_policy_engine(Settings())
for action in (
    PolicyAction(name="query_logs", risk_level=RiskLevel.L0),
    PolicyAction(name="rollback_prod", risk_level=RiskLevel.L3),
    PolicyAction(name="undeclared_action"),
):
    print(engine.evaluate(action).model_dump_json())
'@ | & $uvPath run --offline --frozen --directory backend python -
if ($LASTEXITCODE -ne 0) { throw 'Policy 演示失败' }
```

预期三行 JSON：`query_logs` 为 L0/allow，`rollback_prod` 为 L3/need_approval，`undeclared_action` 为 L5/need_approval。如设置上面的禁止 SQL 规则，再运行以下片段，应显示 L4/deny 且命中 `deny-production-sql`：

```powershell
@'
from app.config import Settings
from app.policy.engine import create_policy_engine
from app.policy.models import PolicyAction, RiskLevel

result = create_policy_engine(Settings()).evaluate(
    PolicyAction(name="execute_prod_sql", risk_level=RiskLevel.L4)
)
assert result.decision.value == "deny"
assert result.matched_rule_ids == ("deny-production-sql",)
print(result.model_dump_json())
'@ | & $uvPath run --offline --frozen --directory backend python -
if ($LASTEXITCODE -ne 0) { throw '配置规则演示失败' }
```

演示后可恢复本地默认配置：

```powershell
Remove-Item Env:POLICY_CONFIG -ErrorAction SilentlyContinue
$env:APP_ENV = 'local'
```

## 本次自检记录（2026-10-06）

- `check-policy.ps1`：103 项测试通过，脚本 PowerShell 语法检查通过。
- `check.ps1`：ruff、格式（58 个文件）、mypy（57 个源文件）、pytest（549 passed、146 skipped）和 Git 环境文件检查全部通过。冻结字段破坏性测试的类型/静态检查问题已修复。
- 文档手动示例实际运行：L0/allow、L3/need_approval、未声明 L5/need_approval；配置的生产 SQL 返回 L4/deny，命中 `deny-production-sql`。
- 本次仅完成并更新 `plans.md` 的 Step 8，Step 9 保持未完成。
