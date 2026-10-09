# Step 20：Knowledge Brain 验收说明

本次范围为 `plans.md` 的 Step 20。依据完整阅读的 `AGENTS.md`、`SPEC.md` 与实际计划文件 `plans.md` 实施。原长版设计文件未在仓库中提供，沿用现有规格中明确的知识范围；Step 21 及以后保持未实现。

## 交付内容

`app/knowledge/` 提供知识条目、严格输入模型、CRUD 和语义检索服务。新迁移 `0006_knowledge_brain` 建立 `knowledge_entries`，并在空库启用既有 PostgreSQL 的 vector 扩展。降级删除本步的表与索引，保留可能被其他模块共用的扩展。

| 字段 | 含义 |
| --- | --- |
| id、created_at、updated_at | 公共 UUID 主键与 UTC/timestamptz 时间 |
| kind | business_rule、standard、sop、experience、constraint、team_convention、business_priority，对应 SPEC 中的业务规则、规范、SOP、历史经验、特殊限制、团队约定、业务优先级 |
| content、source | 知识正文与来源；不可为空白 |
| valid_from、expires_at | UTC 生效时间与可选到期时间，范围为 `[valid_from, expires_at)`；空结束时间表示长期有效 |
| embedding | 实际 pgvector `vector` 列，由既有 LLMClient.embeddings 生成，调用方不能直接提交向量 |
| embedding_model、embedding_dimensions | 向量模型与维度，隔离不可比较的向量空间 |

知识只承载机器无法自动获得的规则和经验。机器事实仍由 Connector/Context Graph/Change Timeline 管理，配置和凭证仍只来自环境变量或 Secret。

`KnowledgeService` 的 `create/get/list/update/delete/search` 只做业务逻辑，不提交事务；调用方使用 `session.begin()`。更新采用一条 UPDATE 同时替换内容、来源、有效期、向量与模型信息；并发更新以最后成功写入为准，但内容与向量始终对应。网关超时或无效向量发生在写入之前，旧条目保持不变；事务回滚同时恢复正文和向量。

检索在 PostgreSQL 内使用 `<=>` 精确余弦距离，按相关度降序返回，相似度为 `1 - distance`，同分按 UUID 排序。检索只返回已生效、未过期、与查询的模型标识及维度一致的条目，可以限制知识类型及数量（1–100）。`get/list` 可查看过期条目供后续管理页面使用。模型或维度改变后，旧空间的知识不会混入结果，需要通过更新重新生成相应模型的向量。

向量按 pgvector float32 精度校验并单位化，拒绝非有限值、零向量、溢出、下溢为零及不符合 1–16000 维范围的结果。当前使用精确检索和模型/维度普通索引，未添加近似检索索引。依赖使用官方 `pgvector` Python 适配器，版本锁定在 `uv.lock`；实现依据 [pgvector Python 的 SQLAlchemy 文档](https://github.com/pgvector/pgvector-python#sqlalchemy) 和 [pgvector 距离及向量说明](https://github.com/pgvector/pgvector)。

没有新增 Agent Tool、Temporal Workflow、HTTP API 或前端页面。Knowledge API 在 Step 45，Knowledge 页面在 Step 50；前端工程仍按 Step 47 建立。现有唯一 Dispatcher 与生产操作权限边界保持原有契约。

## 自己跑一遍

需要本机 Docker Desktop 启动，且本项目 PostgreSQL 容器运行。不需要 Worker、Temporal 服务、公司网关或生产凭证。

在 PowerShell 中执行，每次失败会立即抛错：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-knowledge.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 20 专项验收失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库回归失败' }
```

`check-knowledge.ps1` 应显示 `50 passed`，并实际演示：

1. 写入 3 条业务规则；查询「支付高峰期是否允许停机？」的首条为「支付高峰期禁止停机」，相似度 `1.000`。
2. 将首条改为「数据库连接异常时先检查连接池」，跨会话读到新向量 `[0, 1, 0]`，原查询相似度变为 `0.000`。
3. 删除该条目后，无法按 ID 读回或在语义结果中找到，剩余 2 条规则。
4. 显示「Step 20 Fake 知识演示通过」「临时测试库已清理」「Step 20 Knowledge Brain 验收全部通过」。

Fake 的向量由脚本固定，验收的是 pgvector 存储、排序、筛选和 CRUD 的行为，不代表真实模型的中文语义质量。本次没有访问公司 AI 网关。

只想查看上面的可运行演示时，执行：

```powershell
.\demo-knowledge.ps1
if ($LASTEXITCODE -ne 0) { throw '知识演示失败' }
```

所有专项、演示和数据库回归都自动创建并清理本项目的本地临时库，不向应用库写入样例。运行途中不要关闭终端；正常成功或异常退出时脚本都会执行清理。

已有依赖容器但尚未启动时，可恢复现有配置：

```powershell
. .\use-local-deps.ps1
docker compose -f .\deploy\docker-compose.yml up -d
if ($LASTEXITCODE -ne 0) { throw '本地依赖启动失败' }
```

首次在其他机器运行时，按 `deploy/README.md` 初始化本地依赖，执行 `uv sync --directory backend --frozen --python 3.12` 安装锁定依赖。

## 服务调用方式

后续 HTTP 或 Activity 适配器应复用该服务，不在适配器内实现业务逻辑；本地仅使用 FakeLLM，生产 embeddings 通过已有公司网关客户端和环境变量配置。

```python
from app.knowledge.schemas import KnowledgeDraft, KnowledgeSearch, KnowledgeType
from app.knowledge.service import KnowledgeService

async with database.session() as session, session.begin():
    service = KnowledgeService(session, llm)
    entry = await service.create(KnowledgeDraft(
        kind=KnowledgeType.BUSINESS_RULE,
        content="支付高峰期禁止停机",
        source="业务负责人说明",
    ))
    matches = await service.search(KnowledgeSearch(query="支付高峰期是否允许停机？"))
```

`llm` 和 `database` 的创建、关闭由宿主管理。读操作也必须使用有效会话；编辑是完整替换知识字段，保留 ID/created_at 并刷新 updated_at。

## 自检记录（2026-10-06）

专项已通过 50 项（35 项离线 + 15 项本地 pgvector），覆盖输入/有效期、模型复制后重校验、Fake LLM、极值向量、相关度、更新删除、模型/维度/类型隔离、跨会话读写、并发一致性、数据库约束与事务回滚。结果集转换和首轮格式/类型问题已修复。

- `check.ps1`：导入边界、ruff、格式、mypy（180 个源文件）、Git 环境检查通过，`1362 passed, 209 skipped`。209 项依赖测试由既有各专项入口运行，本步相关 15 项已在专项和数据库回归中运行。
- `check-knowledge.ps1`：`50 passed`，三条业务规则的实际 pgvector CRUD/检索演示通过，临时库清理完成。
- `check-db.ps1`：`197 passed, 2 skipped`；空库迁移、降级到 Step 19 及更早版本/base、重新升级、metadata 一致性全部通过。两个既有 Temporal 测试沿用各自专项入口，本步未修改 Workflow。
- `demo-knowledge.ps1`：独立入口实际运行成功，新 PowerShell 脚本语法检查通过。
- 本地应用库升级到 `0006_knowledge_brain (head)`，`alembic check` 返回 `No new upgrade operations detected`，知识表中 0 条样例，剩余临时测试库数量为 0。
- 前端仅预留目录，按 Step 47 建立后再执行 lint/typecheck/test；本次未访问真实系统或公司网关。
