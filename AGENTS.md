# AGENTS.md — Weipai AI Ops Brain 工程契约

唯一权威设计：`Weipai AI Ops Brain 最终设计方案 V1.0.md`（下称"设计"）。实现与设计冲突以设计为准；要偏离先改设计或写 `docs/adr/`。

## 技术栈与目录
- 单仓：`backend/`、`frontend/`、`deploy/`（Dockerfile / Helm / docker-compose）、`docs/`。后端是模块化单体，同一镜像两个入口：`api`（FastAPI）与 `worker`（Temporal Worker），不拆微服务。
- 后端：Python 3.12+、FastAPI、Pydantic v2、SQLAlchemy 2 + Alembic；uv 管依赖，ruff + mypy + pytest。
- `backend/app/` 按设计模块分包：`connectors` `tools` `graph` `triggers` `tasks` `agent` `runbooks` `knowledge` `ledger` `policy` `executor` `verifier` `learning`；`api` 只做 HTTP 适配，不写业务逻辑。
- AI Task 生命周期、定时任务、审批暂停/恢复、重试一律用 Temporal，不自造队列、调度器或轮询状态机。
- 存储只用 PostgreSQL（+ pgvector 做知识/Runbook 检索），Context Graph 用节点/边表实现；引入 Neo4j、Redis、Kafka 等新中间件须先写 ADR。
- LLM 只经公司 AI 网关（OpenAI 兼容协议）调用，模型名走配置；Agent 循环（Think→Plan→Tool→Observe→Reason）自研，不引入 LangChain/LangGraph 类框架；HolmesGPT 等开源能力以适配器接入，不 fork。
- 前端：React + TypeScript(strict) + Vite + Ant Design + TanStack Query，pnpm；API 类型由后端 OpenAPI 生成，禁止手写。
- 部署到阿里云 ACK（Helm）；配置与密钥只来自环境变量 / K8s Secret，绝不入库。

## 不可违反
1. 只有 `connectors/` 能接触外部运维系统的 SDK/API；Agent 只能通过 `tools/` 的高级 Tool 访问世界。新接入系统 = Connector + Fake + Tool。
2. 所有 Tool 调用走唯一 dispatcher：定级 → Policy → 执行 → 写 Evidence Ledger 与审计；Replay 复用同一入口回放历史结果。
3. 每个 Tool/Action 必须声明风险等级 L0–L5，未声明按 L5；Executor 只执行带 Policy 放行或审批记录的 Action，写操作用短时、最小权限、动作级凭证，与只读身份分离。
4. 告警、工单、巡检、发布、人工请求都先归一为 OpsEvent 再成为 AI Task，共用一个引擎；状态只用设计第 10 节枚举，只能由 `tasks/` 迁移；NEED_HUMAN_JUDGMENT 与 WAITING_APPROVAL 永不合并。
5. AI 结论必须引用 Evidence ID，关键故障/发布/重大变更须经 Reviewer 反证后才能进入 Action Plan；RESOLVED 只能由独立 Verifier 设置；连续失败、指标恶化、证据冲突、超 Action 上限即熔断转人工。
6. 原始指标/日志/Trace 留在源系统按需查询，本库只存关系、引用与证据快照；图中每条边必带 source/confidence/first_seen/last_seen；时间一律存带时区的 UTC。
7. 测试与本地开发绝不触达真实生产系统（用 Fake）；Policy、Verifier、审计未就绪前，不实现任何 L1+ 写操作。
8. 标识符与提交信息用英文，UI 文案与文档用中文；交付前须通过后端 ruff/mypy/pytest 与前端 lint/typecheck/test。
