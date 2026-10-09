# 后端工程

Step 24「Codex Main Agent」在 `app/agent/` 实现 Think→Plan→Tool→Observe→Reason，Tool 只走 Dispatcher，最终结论逐条引用本次真实 Evidence ID；Temporal 在 INVESTIGATING/RCA 接入调查与证据校验。调查完成暂停到 WAITING_INFORMATION；超限或无效结论转交人工，不进入处置阶段。按任务版本保存每轮检查点，重试复用已提交结果。验收用 `check-agent.ps1`、`demo-agent.ps1`、`check.ps1`，详见 [Step 24 说明](../docs/main-agent.md)。默认保持早期占位模式；新事件启用调查须设置 `AGENT_CONFIG={"enabled":true,"max_steps":20}`。没有新增迁移、依赖或生产写操作，head 保持 `0009_state_prediction`。

Step 23「状态与预测驱动」新增 `app/triggers/detection/`：环境变量规则、三值状态比对、四类趋势预测、异常去重游标，以及 `StatePredictionWorkflow` 和周期 Schedule。迁移 head 为 `0009_state_prediction`。采集/入库分为两个 Activity，使用同一快照重试；检测任务带 Evidence 并沿用统一任务生命周期。验收用 `check-detection.ps1`、`demo-detection.ps1`、`check.ps1`、`check-db.ps1`，详见 [Step 23 验收说明](../docs/state-prediction-triggers.md)。只触发调查，主 Agent 尚未实现。

Step 21「事件接入」新增 `app/triggers/` 的 OpsEvent、归一化/验签去重、Temporal 接入与 K8s Watch；迁移 head 为 `0007_ops_events`。Worker 注册 AI Task、Discovery、Change Timeline、事件接入、K8s Watch 五种 Workflow。验收使用 `check-events.ps1`、`check.ps1`、`check-db.ps1` 和 `demo-events.ps1`，详见 [Step 21 验收说明](../docs/event-ingestion.md)。事件任务使用现有占位阶段，暂停到 WAITING_INFORMATION，没有真实运维处置。

Step 20「Knowledge Brain」提供 `app/knowledge/` 知识 CRUD、UTC 有效期与 pgvector 语义检索；验收使用 `check-knowledge.ps1`、`demo-knowledge.ps1`，详见 [Step 20 验收说明](../docs/knowledge-brain.md)。

Step 17：`app/tasks/workflow.py` 提供 AI Task Temporal Workflow，`app/tasks/worker.py` 为同一后端包的 worker 入口。阶段逻辑为占位 Activity，通过唯一任务服务原子写状态、历史与审计；支持三个独立人工等待状态、信号恢复、持久化超时与有限重试。独立的 `app/verifier/placeholder.py` 负责本地占位 VERIFYING → RESOLVED。该步没有新增迁移或实际运维写操作。

Step 17 自检（2026-10-06）：36 项专项（20 项离线、6 项 PostgreSQL、10 项本地 Temporal）通过；统一检查为 `1275 passed, 178 skipped`，ruff、格式、mypy（149 个源文件）、边界与 Git 检查通过；独立 PostgreSQL 回归 `168 passed`，临时库已清理。自检配置别名与历史预期问题已修复，Worker、三个演示及 UI 可查询均实际验证。验收命令为 `check-workflow.ps1`、`check.ps1`、`check-db.ps1`；完整说明见 [Step 17 文档](../docs/ai-task-workflow.md)。

使用 uv 管理 Python 3.12 环境及锁定依赖。`api` 是 FastAPI 入口，`worker` 是 Temporal Worker 入口，均来自同一后端包；环境变量与验收命令见仓库根目录 `README.md`。占位 Worker 拒绝 staging/production。

`app/api/` 仅负责 HTTP 适配。`app/tasks/` 已实现 Step 4 的任务模型/状态服务与 Step 17 的 Temporal 编排，`app/ledger/` 已实现 Step 5 的证据与审计，`app/graph/` 已实现 Step 6 的关系存储与查询；`app/agent/` 提供 Step 7 的公司 AI 网关客户端与 Fake LLM，`app/policy/` 提供 Step 8 的风险与权限判定。其他业务模块按 `plans.md` 后续步骤实现。

## Step 3：数据库基础层

使用 SQLAlchemy 2.0、asyncpg 和 Alembic，版本锁定在 `uv.lock`。连接地址只由 `DATABASE_URL` 环境变量提供，协议必须为 `postgresql+asyncpg`；`APP_ENV=local/test` 时主机只允许 `127.0.0.1`、`localhost`、`::1`。配置展示隐藏密码，不加载 `.env`。

`app/db/base.py` 的抽象 `Base` 供后续业务模型继承：

- `id`：PostgreSQL UUID 主键，SQLAlchemy 写入时生成 UUID v4。
- `created_at`、`updated_at`：非空 `timestamp with time zone`，默认带时区的 UTC，数据库默认值为 `now()`。
- `updated_at`：经 SQLAlchemy 更新记录时刷新，保留原 `created_at`。当前未提供数据库触发器，直接执行原始 SQL 更新时需显式设置更新时间。
- `UTCDateTime`：拒绝无时区时间，写入和读取都规范为 UTC；会话连接同时设置 PostgreSQL 时区为 UTC。

`app/db/session.py` 的 `Database` 拥有异步引擎和会话工厂。每次 `session()` 创建独立会话，`expire_on_commit=False`；退出时释放会话，未提交事务回滚。事务由业务服务显式使用 `async with session.begin()` 或 `await session.commit()`，HTTP 依赖不自动提交。`app/api/dependencies.py` 的 `get_session` 仅适配请求生命周期。API lifespan 创建引擎并在结束时销毁连接池；worker 等后续入口也可复用 `Database`。

迁移配置为 `alembic.ini` 与 `alembic/env.py`。运行命令时设置 `APP_ENV` 和 `DATABASE_URL`，从 `backend/` 执行 `alembic upgrade head` / `alembic downgrade base`。项目根目录的完整 PowerShell 操作见 `README.md`。连接 URL 直接传入引擎，不写入 INI，避免密码中 `%` 的配置插值问题。`app/db/migrations.py` 将自定义 UTC 类型生成成原生 `sa.DateTime(timezone=True)`，避免生成的迁移引用未导入的应用模块。初始 revision 为 `0001_database_foundation`，只建立版本基线。后续步骤新增业务模型时，在 `alembic/env.py` 导入相应模型，再生成并审核迁移；API 启动不自动建表或执行迁移。

## 验收

在项目根目录执行 `check.ps1`，检查格式、ruff、mypy（包含迁移和验收脚本）、pytest 和 Git 忽略规则。无 Docker 时仍可通过单元测试；数据库集成测试明确跳过。

执行 `check-db.ps1` 时，会加载已有本地容器的配置并新建独立的 `weipai_db_test_<随机 UUID>` 测试库，再运行数据库基础层、任务、Ledger、Context Graph 和 Tool Dispatcher 的五个集成测试文件。测试覆盖真实 PostgreSQL 写入和跨会话读回、UUID、UTC/timestamptz、更新时间、带偏移时间转换、无时区时间拒绝、事务回滚，以及当前迁移 head 的空库升降级、metadata、AI Task 状态历史、证据引用、只追加保护、审计原子性、图约束、并发 upsert、N 跳邻居查询和统一 Tool 调用/历史回放。完成或失败后均尝试删除本次创建的临时库。集成测试还校验连接主机和测试库前缀；不会使用宿主机已有的 `DATABASE_URL`。

## Step 6：Context Graph 存储

`app/graph/models.py` 定义节点和边，`service.py` 提供显式事务中的节点/边 upsert、节点读取及 N 跳邻居查询。迁移 `0004_context_graph` 建立 `context_graph_nodes`、`context_graph_edges`，含来源、置信度、UTC 观察时间、外键与同来源同边唯一约束。

重复观察只刷新边的 `last_seen`，不覆盖置信度或首次观察时间，迟到/并发观察不会使时间倒退；freshness 动态计算为距最近观察的时长，不落库。邻居查询默认有向，支持反向和双向、去重与跳数限制。详细字段、服务示例、样例图预期和可执行验收命令见 [Step 6 说明](../docs/context-graph.md)。

Step 6 自检（2026-10-06）：ruff、格式、mypy（49 个源文件）通过，pytest 为 383 passed、146 skipped；`check-db.ps1` 的 146 项 PostgreSQL 集成测试全部通过（其中 23 项为图测试），临时库已清理。首次检查的长行与 RETURNING 类型推断问题已修复；本地应用库当前为 `0004_context_graph (head)`，metadata 检查通过。当次未进入 Step 7。

## Step 7：AI 网关客户端

`app/agent/models.py` 定义网关和 Fake 共用的 chat、函数调用与 embeddings 类型；`client.py` 实现异步 OpenAI 兼容协议与有上限的超时重试；`fake.py` 提供按请求和顺序校验的脚本化 Fake，以及配置工厂。HTTP 库 `httpx2` 从原开发依赖提升为运行依赖，版本继续由 uv 锁定。

默认 `LLM_MODE=fake`，公司网关的地址、密钥与两个模型名只能来自配置；`local/test` 禁止实际网关网络请求。完整环境变量、错误策略、可运行 Fake 示例和专项验收命令见 [Step 7 说明](../docs/ai-gateway.md)。

Step 7 自检（2026-10-06）：`check-llm.ps1` 的 63 项专项测试通过；ruff、格式、mypy（54 个源文件）与 Git 环境文件检查通过，pytest 为 446 passed、146 skipped；`check-db.ps1` 的 146 项本地 PostgreSQL 集成测试全部通过，临时库已清理。修复了网络拦截对 Windows 事件循环初始化的影响、测试环境覆盖与多 Tool JSON 严格解析问题；文档中的 Fake 示例实际运行成功。Step 7 已完成，未进入 Step 8。

## Step 8：Policy Engine

`app/policy/models.py` 定义 L0–L5 风险、三种判定、规则配置和结果；`engine.py` 进行无 IO 的确定性判定。`Settings` 新增可选环境变量 `POLICY_CONFIG`，默认 L0 放行、L1–L5 需审批。缺失或 null 风险规范为 L5，配置规则按动作名称、风险、环境匹配，冲突固定取 `deny > need_approval > allow`。后续 Dispatcher 可使用 `create_policy_engine(settings)`，环境由进程配置确定。

Step 8 自检（2026-10-06）：103 项专项测试通过，禁止实际 HTTP/DNS/socket 请求；ruff、格式、mypy（57 个源文件）与 Git 环境文件检查通过，pytest 为 549 passed、146 skipped。既有数据库集成测试沿用 `check-db.ps1` 独立入口，本步骤没有数据库或迁移变更；前端在 Step 47 建立。自检发现的冻结模型测试类型/静态检查问题已修复，文档默认与配置示例实际运行成功，验收脚本的 PowerShell 语法检查通过。Step 8 已完成，未进入 Step 9。可复制的验收命令、规则格式与手动判定示例见 [Step 8 说明](../docs/policy-engine.md)。

## Step 9：Tool 注册表与统一 Dispatcher

`app/tools/models.py` 定义严格入出参基类、声明与结果；`registry.py` 根据 Pydantic 模型生成 schema 并拒绝同名注册；`dispatcher.py` 为唯一调用入口，按注册风险交 Policy 判定，L0 执行后在同一 SAVEPOINT 中追加证据与审计。未声明风险按 L5；当前所有需审批调用及 L1+ 实际执行均拒绝。

Replay 复用同一入口，校验任务、Tool、规范化参数、UTC 截止时间、成功调用审计与快照 schema，返回历史原值与原 Evidence ID，只追加回放审计，不调用实现。新版本 schema 的默认值不会改写历史结果；证据／审计存储失败一起回滚。没有调度或重试逻辑，后续由 Temporal 负责；没有新增数据库迁移、审批流或 Connector。

Step 9 自检（2026-10-06）：45 项离线专项测试通过；ruff、格式、mypy（62 个源文件）与 Git 环境文件检查通过，pytest 为 `594 passed, 153 skipped`；153 项本地 PostgreSQL 集成测试通过，其中 7 项新增 Tool 测试，临时库已清理。修复了自检中的格式／类型问题，并补充历史快照保持、模型重新校验和 JSON 时间字段往返测试。PowerShell 专项脚本语法检查通过。详细验收命令和调用契约见 [Step 9 说明](../docs/tool-dispatcher.md)。当次未进入 Step 10。

## 官方参考

Step 10 的 Connector 框架说明见下一节；下列参考仅对应既有数据库实现。

异步引擎、会话生命周期和 `expire_on_commit` 的用法遵循 [SQLAlchemy 2 官方异步文档](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)；迁移使用 [Alembic 官方 asyncio 方式](https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic)。

## Step 10：Connector 框架

`app/connectors/base.py` 定义异步生命周期与独立的只读/写基类；`models.py` 定义互不替代的 Reader/Executor 凭证类型及同凭证拒绝检查；`factory.py` 根据环境变量选择显式绑定的只读 Fake/真实实现。默认 Fake，local/test 禁止真实模式，工厂创建前重新校验宿主配置。没有写方法、写工厂、动作凭证签发或具体运维业务适配器。

`app/connectors/boundaries.py` 用 AST 扫描应用与迁移目录，外部 SDK、HTTP 客户端及直接联网的标准库只允许在 connectors/ 引入。已有公司 AI 网关的两个文件保留 httpx2 例外；基础框架使用显式允许清单。统一检查在其他检查之前运行边界门禁，违规 SDK 未安装时也可检出。

Step 10 自检（2026-10-06）：69 项离线专项测试通过；ruff、格式、mypy（68 个源文件）、导入边界与 Git 环境文件检查通过，pytest 为 `663 passed, 153 skipped`。隔离项目向 tools/ 注入 kubernetes 后，同一统一检查入口实际失败；移除后边界检查通过。修复了首轮静态类型和 Windows 默认临时目录权限问题，两个验收入口改用项目 .cache 下的独立测试目录；新增 PowerShell 脚本语法通过。153 项既有数据库集成测试沿用独立入口，本次没有数据库变更。真实分支使用测试构造替身，不接触外部系统；未进入 Step 11。验收命令、配置和接口说明见 [Step 10 说明](../docs/connector-framework.md)。

## Step 11：运维平台 / CMDB 只读 Connector

`app/connectors/ops_platform/` 包含共享抽象接口、源系统记录模型、配置、Fake、HTTP 适配器与配置工厂，支持服务树、应用、负责人和工单读取。`python -m app.connectors.ops_platform` 可运行独立的 Fake 样例；根目录 `check-ops-platform.ps1` 会先跑专项测试再展示样例。

`app/tools/ops_platform.py` 声明三个 L0 高级 Tool：`get_ops_service`、`list_ops_services`、`query_ops_tickets`，由宿主显式注册，调用仍走现有 Dispatcher。没有 Discovery Workflow、工单处置、写操作或新迁移。

Step 11 自检（2026-10-06）：104 项全离线专项、Fake 样例、PowerShell 语法通过；ruff、格式、mypy（78 个源文件）、导入边界及 Git 检查通过，pytest 为 `767 passed, 153 skipped`。修复了测试动态配置的类型问题，以及 Fake 演示受宿主真实数据库/网关配置影响的问题。153 项既有数据库集成测试沿用独立入口，本次未重跑。公司 API 协议尚未提供，真实 HTTP 实现以显式配置的 GET/JSON/Bearer 协议进行 mock 验证，实际字段/鉴权/分页待公司接口说明核对；未生产联调。配置与完整验收见 [Step 11 说明](../docs/ops-platform-connector.md)；未进入 Step 12。

## Step 12：ACK / Kubernetes 只读 Connector

`app/connectors/kubernetes/` 实现共享只读接口、必要状态快照、环境变量配置、Fake、标准 Kubernetes GET API 适配器与配置工厂。`app/tools/kubernetes.py` 注册 L0 的 `get_k8s_status`、`get_service_runtime`、`query_events`，均沿用既有 Dispatcher、Policy、Evidence 与审计。服务按配置标签精确匹配，事件按现存 Deployment/Pod UID 关联；分页不完整或源响应不符合筛选条件时报错。没有新增依赖、数据库迁移、写操作、Watcher、Discovery、API 或前端。

Step 12 自检（2026-10-06）：129 项离线专项测试及 Fake 样例通过；ruff、格式、mypy（88 个源文件）、导入边界、PowerShell 语法与 Git 检查通过，pytest 为 `896 passed, 156 skipped`。156 项本地 PostgreSQL 集成测试通过，其中 3 项新增 Kubernetes Tool 的跨会话 Evidence/审计与 Replay 测试，临时库已清理。自检中的泛型、测试字典类型、格式及 JSON 字段别名与 schema 一致性问题已修复；补充了集群级 Event、空分页、自定义标签和快照隔离验证。真实接口通过 HTTP mock 验证，未连接实际 ACK/K8s 集群。验收命令为 `check-kubernetes.ps1`、`check.ps1`、`check-db.ps1`，详细说明见 [Step 12 说明](../docs/kubernetes-connector.md)；本次未进入 Step 13。

## Step 13：可观测性只读 Connector

`app/connectors/observability/` 提供 Prometheus、SLS、ARMS 的共享只读接口、原生 HTTP 客户端、可注入 Fake 和环境变量配置工厂。`app/tools/observability.py` 注册 L0 的 `query_metrics`、`query_logs`、`query_traces`，调用继续经既有 Dispatcher、Policy、Ledger 和审计。所有时间规范为 UTC，查询窗口使用 `[start, end)`；ARMS 拓扑基于返回 Span 的父子关系，完整服务发现按 Step 18 实施。

Step 13 自检（2026-10-06）：126 项离线专项测试、Fake JSON 演示和 PowerShell 语法通过；ruff、格式、mypy（102 个源文件）、导入边界及 Git 检查通过，pytest 为 `1022 passed, 159 skipped`。159 项本地 PostgreSQL 集成测试通过，含 3 项新增 Tool 的跨会话 Evidence/审计与关闭 Connector 后 Replay 测试，临时库已清理。格式、类型与分页闭包问题已修复；UTC 边界、亚毫秒裁剪、嵌套 Span/微秒单位、分页完整性、凭证隔离、错误脱敏和原生签名参考值验证通过。真实接口仅经 HTTP mock 验证，未连接实际系统。验收命令为 `check-observability.ps1`、`check.ps1`、`check-db.ps1`，完整配置与协议见 [Step 13 说明](../docs/observability-connectors.md)。没有新增依赖、迁移或写操作，本次未进入 Step 14。

## Step 3 自检历史（2026-10-06）

- 完整阅读 `AGENTS.md`、`SPEC.md`、实际计划文件 `plans.md`；首个未完成项为 Step 3。目录未提供 AGENTS/SPEC 引用的原始设计文件，本次按现有规格中明确的数据库要求实施。
- `check.ps1` 全部通过：ruff、格式、mypy（31 个源文件）、pytest（19 passed，4 项数据库集成测试在此入口明确跳过）及 Git 环境文件检查。
- `check-db.ps1` 的全部 4 项 PostgreSQL 集成测试通过；空库 upgrade → check → downgrade → upgrade 成功，跨会话读写、UUID、UTC/timestamptz、更新时间及事务回滚符合预期。临时测试库已清理。
- 修复初次自检的严格类型问题，并修复最终复核发现的自定义 UTC 类型迁移生成问题；修复后统一检查和 PostgreSQL 集成验收再次通过。
- `use-local-db.ps1` 实际加载本地应用库环境成功；本地 `weipai` 应用库已执行 `alembic upgrade head`，`alembic current` 返回 `0001_database_foundation (head)`。两个新增 PowerShell 脚本的语法检查通过。
- Step 3 已标记完成。未创建 Step 4 的 AI Task 或状态历史表，未接入真实运维系统。前端目录仍为预留骨架，按 Step 47 建立工程后再运行前端检查。

## Step 4：AI Task 模型与状态机

`app/tasks/states.py` 定义 8 个来源、SPEC 的全部 17 个状态与不可变合法迁移表；`models.py` 定义 `AITask` 和 `TaskStatusHistory`；`service.py` 是状态写入的唯一业务入口。创建任务时写入 NEW 的初始历史；每次成功迁移写入恰好一条带原因、调用角色与 UTC 时间的历史。迁移使用行锁与状态版本检查，状态和历史在同一 SAVEPOINT 中落库，外层事务由调用方提交。

迁移 `0002_ai_tasks` 建立两张表及枚举 CHECK、历史外键和序号唯一约束。公开状态属性只读，内部状态属性赋值及 Session 批量写入受到服务边界约束。`RESOLVED` 仅允许从 `VERIFYING` 以 Verifier 内部角色迁移；真实独立验证在 Step 31 接入。完整迁移表、使用例子、当前范围和自检命令见 [Step 4 说明](../docs/task-state-machine.md)。

Step 4 自检（2026-10-06）：统一检查通过，mypy 检查 39 个源文件；pytest 为 321 passed、90 skipped。`check-db.ps1` 中全部 90 项 PostgreSQL 集成测试通过，包含 74 条合法边真实落库、跨会话读回、状态与历史回滚、两个旧状态会话竞争迁移及 ORM 写入边界。首次自检发现的格式、类型标注、表清单顺序断言和批量写入测试构造问题均已修复；临时测试库已清理。本地应用库当前 head 为 `0002_ai_tasks`。

## Step 5：Evidence Ledger 与审计日志

`app/ledger/models.py` 定义 `Evidence`、`AuditRecord` 与四种审计事件类型；`service.py` 提供追加记录、证据 ID 精确查询和任务时间序查询。迁移 `0003_evidence_ledger` 建立 `evidence_ledger`、`audit_log`、任务/证据外键、JSON 对象与非空约束，以及拒绝 UPDATE/DELETE/TRUNCATE 的 PostgreSQL 触发器。ORM 和 Session 批量更新/删除也受只追加边界保护。

任务创建与迁移现在同步追加审计，状态、历史与审计共用任务服务原有 SAVEPOINT；审计写入失败时全部回滚。证据和审计追加要求显式外层事务；服务不提交，调用方应将关联记录一起提交。所有采集、发生及公共时间为 UTC，审计不能引用另一任务的证据。升级前的历史不做回填。

Step 5 自检（2026-10-06）：ruff、格式、mypy 通过，mypy 检查 44 个源文件；pytest 为 349 passed、123 skipped；`check-db.ps1` 的全部 123 项 PostgreSQL 集成测试通过，临时库已清理。格式和迁移泛型标注问题已修复；本地应用库当前 head 为 `0003_evidence_ledger`，metadata 检查、两张新表和两个保护触发器均通过。详细服务示例与验收方法见 [Step 5 说明](../docs/evidence-ledger.md)。

## Step 25：Runbook Engine

`app/runbooks/` 提供全部必填内容、成熟度/自动化等级、PostgreSQL/pgvector 服务与
`runbook.match` Activity；`app/tools/runbooks.py` 提供 L0 `search_runbooks`。
AITaskWorkflow 在主 Agent 调查前判定适用/排除条件，冻结匹配快照后顺序执行只读诊断，
仍经统一 Dispatcher 和证据/审计服务。正文、条件、步骤与向量原子保存；匹配和诊断
重试复用检查点。处理、回滚与验证方案留给后续模块，成熟度自动演进在 Step 35 实现。

2026-10-07 自检：统一检查 `1498 passed, 254 skipped`，ruff、格式、mypy（238 个源文件）、
导入边界和 Git 检查通过；Runbook 专项 `52 passed`，三场景 Fake 演示通过。
迁移 head 为 `0010_runbook_engine`；原长版设计未提供，范围依据现有 SPEC 明确字段。
完整验收与边界见 [Step 25 说明](../docs/runbook-engine.md)，未进入 Step 26。

## Step 27：Reviewer Agent

`app/agent/reviewer/` 实现独立反证循环、结构化报告、宿主置信度调整与
`reviewer.review` Temporal Activity；`app/tasks/review_gate.py` 保护所有 PLANNING
迁移入口。复核绑定任务、当前 RCA 版本、原结论 ID 与独立成功查询审计。
原结论保持不可修改，复核追加新的 Evidence；两份 JSON 与各自 ID 精确对应。
有反证向主 Agent 传回反馈、重新调查，两轮仍冲突转 ESCALATED。

2026-10-07 自检：统一检查 `1549 passed, 296 skipped`，Reviewer 专项 `40 passed`，
数据库/Temporal 回归 `283 passed, 3 skipped`，既有 Workflow 专项 `36 passed`；
ruff、格式、mypy、导入边界与 Git 检查通过。两场景演示和旧主 Agent 演示通过。
没有新依赖或迁移，head 保持 `0010_runbook_engine`；前端预留至 Step 47。
完整验收、Fake 数据和范围见 [Step 27 说明](../docs/reviewer-agent.md)，未进入 Step 28。

## Step 36：Replay 与 AI 评价

`app/learning/evaluation/` 提供只用历史证据的主 Agent 回放、人工基准留证和十项能力指标。
`ReplayEvaluationWorkflow` 与 `learning.replay` 注册在既有 Worker；模型与观察检查点支持重试恢复。
`app/tools/replay.py` 仅声明历史只读 schema，没有 Connector 或 live 实现，调用仍经过 Dispatcher。
运行指标按 UTC 窗口读取任务、证据、审计和状态历史，排除回放及复盘改进任务。
从仓库根目录执行 `check-replay.ps1`、`demo-replay.ps1` 和 `check.ps1`。
完整验收与指标定义见 [Step 36 说明](../docs/replay-evaluation.md)。

## Step 45：认知与运营 API

`app/api/operations.py` 只做 HTTP 适配；`tasks/operations_queries.py` 提供本地认知与运营投影，
`tasks/catalog_service.py` 复用知识/Runbook CRUD 并原子写入本人编辑审计。
服务图查询保留来源、置信度和读取时的新鲜度；运营详情保留真实 Task/Event/Evidence。
能力指标直接复用已有 EvaluationService；审计统一筛选任务审计和独立的目录编辑审计。

从仓库根目录运行 `check-operations.ps1`、`demo-operations.ps1 -Interactive` 和 `check.ps1`。
完整接口、逐项操作及启动自己的 API 的命令见 [Step 45 说明](../docs/operations-api.md)。
应用迁移 head 为 `0016_catalog_audit`；本步没有前端工程或 AI Chat 接口。
