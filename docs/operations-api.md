# Step 45：认知与运营 API

本步依据 AGENTS.md、SPEC.md、plans.md 和《Weipai AI Ops Brain 最终设计方案 V1.0》实施。
HTTP 只适配输入输出；查询位于 tasks/operations_queries.py，编辑位于 tasks/catalog_service.py，
复用既有 Graph、Knowledge、Runbook、Evaluation 和 Ledger 服务。

## 自己跑一遍

在项目根目录打开 PowerShell，确保 Docker Desktop 的 Linux 引擎运行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
# 原有依赖容器已存在时，加载原配置并启动，不清空数据。
. .\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
.\check-operations.ps1
.\demo-operations.ps1 -Interactive
.\check.ps1
```

专项会自动新建并迁移独立本机测试库，执行离线与 PostgreSQL 测试，再清理临时库。
预期所有测试通过，最后显示「Step 45 认知与运营 API 验收全部通过」。
统一检查预期 ruff、格式、mypy、pytest、Connector 导入边界和 Git 环境文件检查全部通过。
前端仍按 Step 47 预留，本步没有前端可运行工程。

演示自动启动隔离的 Fake 巡检 Worker 和临时 API，以真实回环 HTTP 登录并检查：

1. 未登录查询服务返回 401；登录后读取 payment-service 的图，关系包含 source、confidence、UTC 时间与 freshness_seconds。
2. 真正的 Fake 巡检经既有 Temporal 工作流产生恰好 4 条活动风险；HTTP 详情读回完整报告和原 Evidence ID。
3. 输入一条自己的业务规则；直接回车也可使用样例。Knowledge 新建 201、更新 200、删除 204、随后查询 404。
4. Runbook 新建为 Draft；编辑后内容版本增加且仍为 Draft；随后删除并确认 404。
5. 审计中恰好出现本次本人 6 条编辑记录，十项能力指标完整返回；其他运营列表和 OpenAPI 均可查询。

预期末尾显示「Step 45 演示通过」和「临时测试库已清理」。演示会关闭临时 API；本机依赖容器保留。
演示和专项不修改应用库中的资料；不请求生产系统，飞书仅使用 Fake。
不希望输入时直接运行 `demo-operations.ps1`。

需要完整数据库/Temporal 回归时：

```powershell
. .\use-local-temporal.ps1
.\check-db.ps1
```

## 启动自己的 API，用浏览器逐项检查

在第一个 PowerShell 窗口执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
. .\use-local-db.ps1
. .\scripts\project.ps1
$projectUv = Get-ProjectUv
& $projectUv run --frozen --directory backend python -m alembic upgrade head
. .\use-local-auth.ps1 -Username owner -Origin 'http://127.0.0.1:8000'
$env:CONNECTOR_MODE = 'fake'
$env:LLM_MODE = 'fake'
.\run-api.ps1
```

账户配置只保存在当前进程环境，辅助脚本会提示设置密码。
迁移 head 是 `0016_catalog_audit`；已有应用数据保留，新增目录编辑的只追加审计表。

打开 <http://127.0.0.1:8000/docs>，依次操作：

1. POST `/api/auth/login`：`X-Ops-Login` 填 `1`，输入账户 `owner` 和刚设置的密码。
2. 登录响应为 200；浏览器自动保存 HttpOnly 会话 Cookie。记下响应中的 `csrf_token`。
3. GET `/api/services`、`/api/knowledge`、`/api/runbooks`、`/api/risks` 和 `/api/metrics`，确认返回 200。未采集的数据列表为空是正常的。
4. POST `/api/knowledge` 的 `X-CSRF-Token` 填该令牌，提交下面的 JSON，确认 201；复制返回的 `id`。

```json
{
  "kind": "business_rule",
  "content": "支付核心链路优先保障；回滚必须独立验证。",
  "source": "本人验收输入"
}
```

5. GET `/api/knowledge/{entry_id}`，确认内容一致；PUT 同一路径提交完整修改后的正文，确认 200。
6. GET `/api/audits?actor=owner&event_type=catalog_edit`，确认新建/更新记录含当前操作人与 UTC 时间。
7. DELETE `/api/knowledge/{entry_id}`，携带同一 CSRF，确认 204；再次 GET 确认 404。
8. 使用已记录的 `id` 查询 Audit 详情，确认删除知识后审计仍保留。

缺少 CSRF 的编辑返回 403；UUID 无效、缺必填字段、空白内容、无时区或倒置时间窗返回 422。
Runbook 名称重复返回 409，失败请求不新增编辑审计。
结束后在第一个窗口按 Ctrl+C。自己的 API 操作会保存到应用库，与自动隔离演示不同。

## 接口范围

所有路径都有 `/api` 前缀，复用单用户 Cookie 鉴权。每个编辑请求要求 `X-CSRF-Token`；
操作人只来自已验证会话，请求体不能指定操作人。

| 组 | 列表 | 详情/其他查询 |
| --- | --- | --- |
| 服务 | GET `/services` | GET `/services/{service_name}`、`/services/{service_name}/dependencies` |
| Context Graph | GET `/context-graph/nodes`、`/context-graph/edges` | GET `/context-graph/nodes/{node_id}`、`/context-graph/edges/{edge_id}` |
| Change Timeline | GET `/changes` | GET `/changes/{change_id}` |
| Runbook | GET `/runbooks` | GET `/runbooks/{runbook_id}`；POST 列表、PUT/DELETE 详情 |
| Knowledge | GET `/knowledge` | GET `/knowledge/{entry_id}`；POST 列表、PUT/DELETE 详情 |
| 发布 | GET `/releases` | GET `/releases/{task_id}` |
| 工单 | GET `/tickets` | GET `/tickets/{task_id}` |
| 巡检/容量/治理 | GET `/inspections` | GET `/inspections/{task_id}` |
| 风险 | GET `/risks` | GET `/risks/{risk_id}` |
| War Room | GET `/war-rooms` | GET `/war-rooms/{task_id}` |
| 架构评审 | GET `/architecture-reviews` | GET `/architecture-reviews/{task_id}` |
| 自动化建议 | GET `/automations` | GET `/automations/{task_id}` |
| 能力指标 | GET `/metrics` | GET `/metrics/{metric_name}` |
| 审计 | GET `/audits` | GET `/audits/{audit_id}` |

列表返回 `items/total/limit/offset`，`limit` 为 1–100、默认 50，`offset` 默认为 0。
运营列表以 OpsEvent 的真实来源/身份分组，包含尚未产生报告的进行中任务；详情 ID 为 AI Task ID。
详情保留该任务完整 Evidence 快照；未完成阶段不伪造报告，同一任务不能作为另一类场景读出。
架构、保障、建议使用既有事件前缀，巡检仅包含三种既有周期任务，不把发布后验证混入巡检。

筛选：运营列表支持 service_name/status；Knowledge 支持 kind；Runbook 支持 maturity；
图节点支持 kind，图边支持 node_id；风险支持 service_name/active/category；
Change 支持 service_name/kind/source/start/end，按变更发生时间正序显示；
Audit 支持 actor/event_type/task_id/start/end，按时间倒序显示。
带时间窗口的查询默认最近 30 天，窗口为 UTC 半开区间 `[start,end)`；输入可带其他时区偏移。

服务上下文 `hops` 为 1–4；依赖查询只沿实际 calls 边，direction 为 upstream/downstream/both。
图 freshness 在读取时计算；HTTP 查本地关系和证据，不通过源系统补查或伪装成 Agent Tool 调用。
十项指标直接复用 Step 36 的计算与证据口径；零样本保持 value=null，不伪造成功率。

## 编辑与审计保证

- Knowledge 内容和 pgvector embedding 同步更新；向量失败、存储失败或审计失败使整次修改回滚。
- Runbook HTTP 只接受可编辑内容；成功/失败计数、可信度、成熟度和自动化等级由既有审核/验证服务管理。
  无内容变更保留原审核状态；内容变更增加版本、清零计数并退回 Draft/manual。
  编辑本地 Runbook 不产生执行授权，也不执行其处理/回滚步骤。
- 目录编辑审计保存在 ledger 模块独立的 catalog_audit_log，和内容在同一事务提交。
  原任务审计的必填 task_id 和同任务 Evidence 外键保持不变；不为本地资料编辑伪造运维任务。
- ORM、批量 SQL 和原始 SQL 的 UPDATE/DELETE/TRUNCATE 都不能修改目录审计；
  已有目录审计时迁移拒绝有损降级。Audit 查询合并两种只追加来源后统一筛选、计数和分页。
- 返回的知识/Runbook 不包含向量原值；配置、密码、Cookie、网关密钥和连接凭证不进入新增表。

本步不新增运维写动作或新中间件，没有实现 Step 46 的 AI Chat 或 Step 47 的前端。

## 2026-10-08 自检记录

- 本步专项 82 项通过：62 项禁止真实网络的离线测试和 20 项本机 PostgreSQL 集成测试。
- 最终源码统一检查通过：2134 passed/580 skipped，ruff、格式、mypy、导入边界与 Git 检查通过。
- 最终源码完整数据库/Temporal 回归 567 passed/3 skipped；三个独立时间跳跃场景由定时专项
  20 passed 覆盖，人工交互专项 44 passed。
- 真实回环 HTTP 自动与中文输入演示通过：25 个图节点、31 条关系、四条真实 Fake 巡检风险、
  报告 Evidence 原样读回、目录 CRUD 全流程、六条本人编辑审计、十项指标。
- 自检修复了同名 Runbook 的错误映射、夹具名称隔离、分页数据假设和旧 head 断言；
  空库升降级、只追加审计与拒绝有损降级均通过。
- 本机应用库 head 为 0016_catalog_audit，metadata 一致；临时库及演示 API 已清理。
  本机 Docker 临时端点故障已保留原目录后恢复，没有移动容器数据，四个项目依赖 healthy。
