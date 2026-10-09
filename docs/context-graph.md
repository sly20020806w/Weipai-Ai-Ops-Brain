# Step 6：Context Graph 存储与验收

本步骤只实现 PostgreSQL 节点、边存储及 N 跳邻居查询，依据已完整阅读的 `AGENTS.md`、`SPEC.md` 和实际计划文件 `plans.md`。仓库未提供引用的《Weipai AI Ops Brain 最终设计方案 V1.0》，因此仅落实现有规格明确的图存储要求。Discovery、Connector、Tool、图 API 与前端按后续步骤实现。

## 存储契约

`backend/app/graph/models.py` 定义两张表，迁移为 `0004_context_graph`，前驱为 `0003_evidence_ledger`。

| 表 | 字段与约束 |
| --- | --- |
| `context_graph_nodes` | UUID 主键，`kind`、`external_id`、`name` 非空；`kind + external_id` 唯一 |
| `context_graph_edges` | UUID 主键，`from_node_id`、`to_node_id` 为节点外键；`relation`、`source` 非空；`confidence` 必填且在 0–1 之间；`first_seen`、`last_seen` 必填且 `last_seen >= first_seen` |

两表继承公共 UUID、UTC `created_at/updated_at`。观察时间为 PostgreSQL `timestamptz`，服务拒绝无时区时间，接受带偏移时间并规范为 UTC。节点只存类型、稳定标识与名称，边只存关系和观察信息，不复制原始指标、日志或 Trace。`external_id` 应使用带环境、系统和资源范围的稳定标识，避免不同集群同名资源冲突。

节点 upsert 以 `kind + external_id` 去重，保留 ID 和创建时间，刷新名称与更新时间。边 upsert 以 `from_node_id + to_node_id + relation + source` 去重：首次写入时两个观察时间均为 `observed_at`（省略时取当前 UTC）；重复写入**只刷新 `last_seen`**，保留原 ID、置信度、`first_seen` 与公共时间。同一边的不同来源各自保留，另一关系也为独立记录。

`last_seen` 使用旧值和本次观察时间的较大值，迟到或并发观察不会让时间倒退。`first_seen` 表示首次入库的观察时间，迟到记录不改写它。数据库唯一约束和原子 `ON CONFLICT` 防止并发重复行。节点不存在时外键拒绝写边；数据库也拒绝绕过服务写入缺失来源、缺失置信度、越界、NaN/Infinity 或观察时间倒序的边。

`freshness` 不落库，含义为“距最近一次观察经过的时间”：`max(当前 UTC - last_seen, 0)`，返回 `timedelta`。需要固定评估时间时调用 `edge.freshness_at(now)`，未来观察按零时长计算。由调用方决定何种时长算过期，本步骤不增加过期阈值或调度逻辑。

## 服务用法

`GraphService` 写方法要求显式事务，不自行提交。一个事务内可以写入多个节点与关系，任一步失败由外层事务整体回滚。

```python
from app.graph.service import GraphService

# database 为既有 app.db.session.Database 实例。
async with database.session() as session, session.begin():
    graph = GraphService(session)
    payment = await graph.upsert_node(
        kind="service", external_id="fake://local/payment-service", name="payment-service"
    )
    db = await graph.upsert_node(
        kind="database", external_id="fake://local/payment-db", name="payment-db"
    )
    edge = await graph.upsert_edge(
        from_node_id=payment.id, to_node_id=db.id,
        relation="uses", source="fake.cmdb", confidence=0.95,
    )
    payment_id = payment.id
    print(edge.id, edge.freshness.total_seconds())

async with database.session() as session:
    neighbors = await GraphService(session).neighbors(payment_id, hops=2)
    print([node.name for node in neighbors])  # ["payment-db"]
```

`neighbors(node_id, hops=N, direction=...)` 返回 **1 至 N 跳**内的唯一节点，排除起点，默认 `outgoing` 沿有向边向外，`incoming` 沿反向，`both` 双向。`hops=0` 返回空列表，负数、布尔值和非整数被拒绝；不存在的起点抛出 `GraphNodeNotFound`。返回按 `kind/external_id/id` 排序。递归 SQL 按节点和深度去重，并受跳数上限约束，自环、环路、多来源和菱形路径不会产生重复结果或无限递归。

## 你可以自己运行的验收

在 PowerShell 从仓库根目录执行，需 Docker Desktop 与已有本地 PostgreSQL 容器运行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw 'Context Graph 数据库验收失败' }
```

预期统一检查末尾为「统一检查全部通过」，pytest 为 `383 passed, 146 skipped`。普通 pytest 不连接数据库，PostgreSQL 测试会提示执行 `check-db.ps1` 并跳过。前端仍为 Step 47 预留目录。

数据库验收新建随机名称的 `weipai_db_test_...` 临时库，在其中运行全部四个数据库测试文件，随后清理该库。预期 `146 passed`（其中 23 项为图测试），最后显示「临时测试库已清理」和「数据库基础层、AI Task、Evidence Ledger、审计与 Context Graph 验收全部通过」。测试实际验证：

- 空库升级到 `0004_context_graph`，metadata 与迁移一致；降至 Step 5 时两张图表移除、旧表保留；再升级、降至 base、重建均成功。
- 不经服务直接向数据库写入缺失 source/confidence 的边失败，外键、非空、置信度范围和时间顺序约束有效。
- 同来源同边重复 upsert 只有一行，只刷新 `last_seen`；其他来源和关系独立保留；五个并发观察仍只有一行，时间取最新值。
- Fake 图中 `payment-service → payment-db/payment-cache → db-host → zone`，一跳返回 DB/cache，两跳返回 DB/cache/host，三跳再包含 zone；两跳排除 zone 和上游 caller。反向、双向、环路、自环、菱形和孤立节点分别通过断言。
- 跨会话读回时间为 UTC，freshness 根据 `last_seen` 计算，整个图写入事务失败后无部分记录。

如果容器已停止，在同一个窗口先恢复已有依赖：

```powershell
.\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw '本地依赖启动失败' }
.\check-db.ps1
```

需要将迁移应用到自己的本地应用库时：

```powershell
.\use-local-db.ps1
. .\scripts\project.ps1
$uvPath = Get-ProjectUv
& $uvPath run --frozen --directory backend alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw '迁移失败' }
& $uvPath run --frozen --directory backend alembic current
```

预期版本 `0004_context_graph (head)`。应用库升级只建立图表，不注入 Fake 样例；图的行为由独立临时测试库验收。此阶段 API 仍只有健康检查，图页面按后续步骤交付。

## 实现参考

原子 upsert 使用 [SQLAlchemy PostgreSQL ON CONFLICT 官方文档](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#insert-on-conflict-upsert)，N 跳查询使用 [SQLAlchemy 递归 CTE 官方文档](https://docs.sqlalchemy.org/en/20/core/selectable.html#sqlalchemy.sql.expression.HasCTE.cte)。

## 本次自检（2026-10-06）

统一检查全部通过：ruff、格式、mypy（49 个源文件）、383 项单元测试，146 项数据库测试在普通入口明确跳过。`check-db.ps1` 的 146 项 PostgreSQL 集成测试全部通过，临时库已清理。首次自检发现的长行与 RETURNING 语句类型推断问题均已修复。

本地应用库已从 Step 5 升级为 `0004_context_graph (head)`，`alembic check` 返回无新迁移操作。Step 6 已标记完成，Step 7 未开始。
