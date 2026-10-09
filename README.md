# Weipai AI Ops Brain

Step 56 镜像验收入口为 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\check-images.ps1`。
同一后端镜像提供 api/worker 两入口，另有前端镜像和 CI；手动浏览加 `-SkipBuild -Interactive`。
操作命令、预期结果与远端 CI 验收条件见 [Step 56 镜像与 CI](docs/images-ci.md)。

Step 55 安全验收入口为 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\check-security.ps1`。
覆盖全部接口鉴权、源码密钥、锁定依赖漏洞、本机隔离 Reader/Executor RBAC 与既有会话审计；
权限策略及运行条件见 [Step 55 安全验收说明](docs/security-hardening.md)。

Step 54 端到端闭环测试已接入统一检查：签名支付 5xx 告警经真实本机 Temporal、
RCA/Reviewer、L3 审批 API、Fake 回滚、独立验证、十三章复盘与 Draft Runbook 到 CLOSED。
拒绝审批和验证未恢复的场景也会检查，真实运维系统与外网请求为 0。
先启动 Docker Desktop 和本项目本机依赖，再运行 `check.ps1`；只验收本步运行
`powershell -NoProfile -ExecutionPolicy Bypass -File .\check-e2e.ps1`。
完整命令和预期结果见 [Step 54 端到端闭环验收](docs/e2e-closed-loop.md)。
前端亲自浏览方式保留在各页面验收文档；固定端口 API/前端启动见 [前端外壳验收](docs/frontend-shell.md)。

本仓库依据 `AGENTS.md`、`SPEC.md`、长版《Weipai AI Ops Brain 最终设计方案 V1.0》和 `plans.md` 开发。
当前进度以 `plans.md` 为准；本次范围仅为 Step 56，目标仓库为 [sly20020806w/Weipai-Ai-Ops-Brain](https://github.com/sly20020806w/Weipai-Ai-Ops-Brain)，分支为 `main`。

## 当前目录

```text
backend/         Python 3.12+ 模块化单体后端及测试
  app/api/       FastAPI HTTP 适配和 api 入口
  app/config.py  环境变量配置
  app/db/        SQLAlchemy 异步会话及 UUID/UTC 公共模型基类
  app/tasks/     AI Task、状态历史、唯一状态迁移服务、Temporal Workflow 与 worker 入口
  app/verifier/  独立验证、恢复证据门禁及工单/发布验证
  app/ledger/    只追加的证据、审计与查询服务
  app/graph/     Context Graph 存储、查询与 Temporal 定时 Discovery
  app/agent/     公司 AI 网关、Fake LLM、主 Agent 调查循环与结论证据校验
  app/knowledge/ 知识 CRUD、UTC 有效期与 pgvector 语义检索
  app/triggers/  OpsEvent、多来源归一化、验签去重与 Temporal 事件接入/K8s Watch
  app/learning/  十三章事故复盘、改进 OpsEvent/AI Task、Runbook Draft 与事故检索
  app/policy/    L0–L5 风险分级、环境变量规则与允许/审批/禁止判定
  app/tools/     Tool 声明与注册、统一调用、证据/审计和历史回放
  app/connectors/ 只读框架、凭证隔离、导入边界及运维平台、K8s、可观测性、变更链路、阿里云适配器与 Fake
  alembic/       应用数据库迁移（当前 head 为 0016_catalog_audit）
  app/           设计规定的其他业务模块包（当前为空骨架）
frontend/        React + TypeScript strict + Vite + Ant Design + TanStack Query 前端外壳
  src/api/generated/ 后端 OpenAPI 生成的类型、SDK 与 Fetch 客户端
deploy/          本地 PostgreSQL（pgvector）、Temporal、Temporal UI 配置
docs/adr/        架构决策预留目录
scripts/         统一检查与 PowerShell 工具定位
```

## 环境与安装

需要 Git、uv、Python 3.12+、Node.js 22.18+（推荐 24 LTS）与 pnpm 11。项目固定使用 Python 3.12，通过 `backend/.python-version` 声明；后端依赖锁定在 `backend/uv.lock`，前端依赖锁定在 `frontend/pnpm-lock.yaml`。

本机已在 `backend/.venv` 创建 Python 3.12 环境，`check.ps1` 和 `run-api.ps1` 会优先使用 PATH 中的 uv，否则使用项目内的 `.tools/uv/bin/uv.exe`。本地工具、缓存和虚拟环境均已被 Git 忽略。

其他机器安装 uv 后，在仓库根目录执行：

```powershell
uv sync --directory backend --frozen --python 3.12
pnpm --dir frontend install --frozen-lockfile
```

## 配置

只从进程环境变量读取配置，不自动加载 `.env`，不连接真实运维系统。

| 变量 | 是否必填 | 值 |
| --- | --- | --- |
| `APP_ENV` | 是 | `local`、`test`、`staging`、`production` |
| `API_HOST` | 否 | 默认 `127.0.0.1` |
| `API_PORT` | 否 | 默认 `8000`，范围 1–65535 |
| `DATABASE_URL` | 数据库功能必填 | `postgresql+asyncpg://用户:密码@主机:端口/数据库`，通过环境变量传入 |
| `TEMPORAL_CONFIG` | 否 | JSON：回环 address、namespace、task_queue、人工/Activity 超时与重试次数，见 Step 17 说明 |
| `DISCOVERY_CONFIG` | 否 | JSON：发现周期、窗口、Activity 超时/重试，默认 300 秒一次，见 Step 18 说明 |

未设置 `DATABASE_URL` 时仍可启动进程健康检查；使用数据库会话、Worker 或执行 Alembic 时必须设置。本地与测试环境只允许数据库回环地址。`/health` 只检查 API 进程；数据库引擎在实际查询时连接。Temporal 配置与占位 Worker 范围见 [Step 17 说明](docs/ai-task-workflow.md)。Step 7 的 `LLM_MODE` 默认 `fake`；公司网关模式的 URL、密钥、模型与超时配置见 [AI 网关说明](docs/ai-gateway.md)。Step 8 的可选 `POLICY_CONFIG` 环境变量配置权限规则，默认 L0 放行、L1–L5 需审批；格式见 [Policy 说明](docs/policy-engine.md)。

## 自行验收 Step 1

下面的命令都从仓库根目录运行。

### 1. 统一检查

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check.ps1
```

预期：ruff 检查与格式检查、mypy 严格类型检查、pytest、Git 环境文件检查均通过，最后输出「统一检查全部通过」，退出码为 0。测试覆盖健康接口、环境变量读取、必填变量缺失、无效配置及 API 入口参数；不请求外部运维系统。

有 uv 的其他平台可用同一检查入口：

```sh
uv run --frozen --directory backend python ../scripts/check.py
```

前端工程尚未创建；前端 lint/typecheck/test 在 Step 47 纳入此入口。

### 2. 启动 API

在第一个 PowerShell 窗口执行：

```powershell
$env:APP_ENV = 'local'
$env:API_HOST = '127.0.0.1'
$env:API_PORT = '8000'
.\run-api.ps1
```

在第二个 PowerShell 窗口执行：

```powershell
$response = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health'
$response.StatusCode
$response.Content
```

预期为 `200` 和 `{"status":"ok"}`。也可打开 `http://127.0.0.1:8000/docs` 查看自动生成的接口文档。验收后在第一个窗口按 Ctrl+C 停止。

Linux/macOS 或直接使用 uv 时的入口为：

```sh
APP_ENV=local uv run --frozen --directory backend api
```

### 3. 验证缺少必填变量会启动失败

停止已运行的 API，在 PowerShell 执行：

```powershell
Remove-Item Env:APP_ENV -ErrorAction SilentlyContinue
.\run-api.ps1
$LASTEXITCODE
```

预期：输出 `APP_ENV` 的 `Field required` 配置错误，退出码非 0，API 不启动。检查完可重新设置 `$env:APP_ENV = 'local'`。

### 4. 验证 Git 忽略规则

```powershell
git check-ignore .env backend/.env backend/.venv/pyvenv.cfg
git ls-files -- '.env' '.env.*' '*/.env' '*/.env.*' '.venv/*' '*/.venv/*'
```

第一条应输出这三个路径，第二条应无输出。统一检查也会验证忽略规则，并拒绝已跟踪的 `.env` 或 `.venv` 文件。

Step 1 不需要启动 PostgreSQL、Temporal、Docker 或前端，`/health` 只表示 API 进程正常。

## 自行验收 Step 2

本机的依赖容器已经启动，可直接在 PowerShell 复检：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\use-local-deps.ps1
.\check-deps.ps1
.\check.ps1
```

`use-local-deps.ps1` 从本项目已有 PostgreSQL 容器的环境变量加载当前数据库用户和密码到当前进程，不显示密码，也不写配置文件。本次首次启动使用随机本地密码；后续复检应加载现有配置，避免另设密码与已有数据卷不一致。

其他机器首次创建环境时，先启动本机 Docker Desktop 的 Linux 引擎，确认 `docker info` 能显示 Server 信息，再在仓库根目录的同一个 PowerShell 窗口执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
$env:POSTGRES_PASSWORD = [System.Net.NetworkCredential]::new('', (Read-Host '本地数据库密码（请记住，后续启动使用同一个）' -AsSecureString)).Password
docker compose -f deploy/docker-compose.yml config --quiet
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
.\check-deps.ps1
.\check.ps1
```

预期：四个常驻容器均 `healthy`，一次性 `temporal-schema` 初始化容器为 `Exited (0)`；`vector` 扩展创建成功并返回版本及 `[1,2,3]`；数据库时区为 UTC；Temporal 集群健康状态为 `SERVING`；UI 首页及命名空间 API 均 HTTP 200；两个检查脚本最后输出「全部通过」。浏览器打开 <http://127.0.0.1:8080>，可选择 `default` 命名空间。当前还没有 Worker 或 AI Task，工作流列表为空是正常的。

更多配置、手动检查、保留数据的停止/重启和本机故障说明见 [deploy/README.md](deploy/README.md)。Step 2 未接入后端数据库会话或实现业务表。

## 自行验收 Step 3

本机的 Step 2 依赖容器仍在运行时，在 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库验收失败' }
```

预期：统一检查中 Connector 导入边界、ruff、格式、mypy、pytest 与 Git 环境文件检查全部通过，最后输出「统一检查全部通过」。当前普通 pytest 为 1022 项通过、159 项 PostgreSQL 集成测试提示执行 `check-db.ps1` 并跳过；前端工程按 Step 47 再建立。

`check-db.ps1` 自动从本项目已有容器读取本地凭证与实际端口，新建随机名称的临时测试库，实际运行数据库基础层、AI Task、Ledger、Context Graph 和 Tool Dispatcher 的全部 159 项集成测试，最后清理该临时库并输出「数据库基础层、AI Task、Evidence Ledger、审计、Context Graph 与 Tool Dispatcher 验收全部通过」。它保留 Step 3 的 UUID、UTC、跨会话读写、时间校验和事务回滚检查，并验证当前 head 的空库升降级、metadata、Step 4 的状态历史与并发、Step 5 的证据与审计、Step 6 的图约束/upsert/邻居查询及 Step 9 的调用/回放与原子回滚。现有 `weipai` 与 Temporal 数据库不参与这组读写测试。

如果本地容器已停止，在同一个窗口恢复依赖后复检：

```powershell
.\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw '本地依赖启动失败' }
.\check-db.ps1
```

需要把迁移基线应用到本地应用库时：

```powershell
.\use-local-db.ps1
. .\scripts\project.ps1
$uvPath = Get-ProjectUv
& $uvPath run --frozen --directory backend alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw '迁移失败' }
& $uvPath run --frozen --directory backend alembic current
```

当前预期版本为 `0006_knowledge_brain (head)`；Step 3 原基线为 `0001_database_foundation`。`use-local-db.ps1` 设置当前进程的 `APP_ENV=local` 与 `DATABASE_URL`，自动转义密码中的特殊字符，不显示凭证、不写文件。之后可以用 `run-api.ps1` 启动配置好数据库会话的 API，健康接口仍返回 `200` 和 `{"status":"ok"}`。

Step 3 的公共基类为抽象类，因此初始迁移只建立 `alembic_version` 基线；AI Task 等业务表按后续步骤建立。测试记录模型只存在于测试元数据中。基类和会话的使用说明见 [backend/README.md](backend/README.md)。

## 自行验收 Step 4

从仓库根目录执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw 'AI Task 数据库验收失败' }
```

预期统一检查全部通过，当前 pytest 为 `1022 passed, 159 skipped`；数据库验收为 `159 passed`，随后显示临时测试库已清理和验收全部通过。测试保留 8 个来源、17 个状态的全部 289 个组合（74 条合法边、215 个非法组合），并实际验证任务表、逐次历史、UTC、并发与回滚；Step 5–6 在同一入口增加证据、审计与图验收。

Step 4 迁移为 `0002_ai_tasks`，创建 `ai_tasks`、`ai_task_status_history`；当前应用库 head 已升级为 `0006_knowledge_brain`。完整迁移表、服务调用契约、依赖恢复方法和查看实际业务表的命令见 [Step 4 验收说明](docs/task-state-machine.md)。

## 自行验收 Step 5

本机 Docker Desktop 和项目依赖容器运行时，在 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '证据与审计数据库验收失败' }
```

预期：统一检查全部通过，pytest 为 `1022 passed, 159 skipped`；数据库验收为 `159 passed`，最后显示临时测试库已清理与验收全部通过。

Step 5 迁移 `0003_evidence_ledger` 新增 `evidence_ledger` 和 `audit_log` 两张只追加表，当前本地应用库 head 为 `0006_knowledge_brain`。验收实际检查证据 ID 查询、任务时间序查询、ORM 和原始 SQL 更新/删除拒绝、TRUNCATE 拒绝、任务状态与审计的原子性。数据库测试只操作自动创建和清理的独立临时库。

本步骤交付后端存储与服务，证据 API、页面和真实 Tool 调度按后续步骤实现；前端仍为 Step 47 预留目录。详细契约、服务示例、依赖恢复与查看表的命令见 [Step 5 验收说明](docs/evidence-ledger.md)。

## 自行验收 Step 6

本机 Docker Desktop 和项目依赖容器运行时，在 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw 'Context Graph 数据库验收失败' }
```

预期：统一检查中 ruff、格式、mypy 全通过，pytest 为 `1022 passed, 159 skipped`；数据库验收为 `159 passed`，最后显示临时测试库已清理与 Context Graph 验收全部通过。

Step 6 迁移 `0004_context_graph` 新增 `context_graph_nodes` 与 `context_graph_edges`，当前应用库 head 为 `0006_knowledge_brain`。数据库验收包含 Step 6 的 23 项测试，实际检查缺来源/置信度拒绝、UTC、重复与并发 upsert 仅刷新 last_seen、动态 freshness、样例图两跳结果、方向、环路去重和整体事务回滚；当前模型与迁移一致。图样例只在独立临时测试库创建，不写入应用库。

本步骤交付后端图存储与服务，图 API、Discovery 与页面按后续步骤实现。完整字段、服务示例、样例图预期与依赖恢复命令见 [Step 6 验收说明](docs/context-graph.md)。

## 自行验收 Step 7

从仓库根目录执行，不需要网关凭证、Docker 或数据库：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-llm.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 7 专项验收失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

预期专项验收为 `63 passed`；统一检查中 ruff、格式、mypy 全通过，pytest 为 `1022 passed, 159 skipped`，最后输出「统一检查全部通过」。159 项数据库集成测试可按原有 `check-db.ps1` 单独运行。

专项验收检查配置地址/模型、chat 与多个 Tool 调用的解析及结果往返、embeddings 输入顺序、超时重试、错误处理和 Fake 脚本；测试在事件循环初始化后阻止实际 HTTP、DNS 与 socket 请求。统一检查同时回归全部既有单元测试。

本步骤交付后端客户端；可直接运行的 Fake chat/向量示例、完整配置和协议范围见 [Step 7 验收说明](docs/ai-gateway.md)。Policy 已在 Step 8 完成；AI 调查循环、Dispatcher、HTTP API 和前端页面按后续计划实现。

## 自行验收 Step 8

从仓库根目录执行，不需要网关凭证、Docker 或数据库：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-policy.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 8 专项验收失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

预期专项验收为 `103 passed`；统一检查中 ruff、格式、mypy 全通过，pytest 为 `1022 passed, 159 skipped`，最后输出「统一检查全部通过」。159 项数据库集成测试沿用 `check-db.ps1` 单独运行，本步骤没有数据库变更。前端工程按 Step 47 建立。

实现了 L0–L5 风险等级、环境变量规则与 `allow / need_approval / deny` 判定。缺省 L0 放行、L1 及以上需审批，未声明按 L5；规则冲突取最严格结果。专项测试覆盖缺省/配置矩阵、缺失风险、精确匹配、规则冲突及无效配置拒绝，并禁止实际网络访问。可复制执行的手动判定示例与完整配置格式见 [Step 8 验收说明](docs/policy-engine.md)。Policy 判定现由 Step 9 的统一 Dispatcher 使用。

## 自行验收 Step 9

从仓库根目录执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-tools.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 9 专项验收失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw 'Tool 数据库验收失败' }
```

预期：专项为 `45 passed`；统一检查中 ruff、格式、mypy、Git 环境文件检查全通过，pytest 为 `1022 passed, 159 skipped`；数据库验收为 `159 passed`，最后显示临时测试库已清理与 Tool Dispatcher 验收全部通过。数据库验收需要 Docker Desktop 和本项目 PostgreSQL 容器运行，前两个命令完全离线。

实现了高级 Tool 入出参 schema、L0–L5 风险声明、重复注册拒绝、唯一调用入口和历史回放。一条成功 L0 调用恰好保存 1 条证据和 1 条 Tool 审计；需审批调用拒绝执行；Replay 返回原证据结果且不调用实现。缺省风险按 L5，当前阶段禁止 L1+ 实际执行。自检覆盖严格参数/结果、失败脱敏、回放错配/未来数据拒绝、历史原值保持，以及 PostgreSQL 跨会话读回与原子回滚。详细行为、测试名和容器恢复命令见 [Step 9 验收说明](docs/tool-dispatcher.md)。

## 自行验收 Step 10

从仓库根目录执行，无需 Docker、数据库或公司凭证：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-connectors.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 10 专项验收失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

预期：专项 `69 passed`；统一检查先显示「Connector 导入边界检查通过」，随后 ruff、格式、mypy 与 Git 环境文件检查全部通过，pytest 为 `1022 passed, 159 skipped`，最后显示「统一检查全部通过」。专项验证 Fake/真实分支选型、只读无写接口、读写凭证分离、local/test 真实模式拒绝及违规 SDK 导入使统一入口失败。临时测试文件位于 Git 忽略的 `.cache/pytest-connectors` 或 `.cache/pytest-check`，避免 Windows 默认临时目录权限问题。

`CONNECTOR_MODE` 默认为 `fake`；真实模式只允许 staging/production，由后续具体 Connector 使用。Step 10 交付通用框架，真实分支测试使用不联网的构造替身，未接入公司系统或实现 Step 11。没有数据库变更，159 项数据库集成测试仍由 `check-db.ps1` 单独运行；前端按 Step 47 建立。完整配置、接口约定和重点测试名见 [Step 10 验收说明](docs/connector-framework.md)。

## 自行验收 Step 11

在项目根目录的 PowerShell 执行，不需要 Docker、数据库或公司凭证：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-ops-platform.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 11 专项验收失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

专项预期为 `104 passed`，随后输出 `payment-service` 的 `支付业务（样例）`、负责人 `owner-payment` 与 `TICKET-1001` 工单 JSON，最后显示「Step 11 Fake 样例验收通过（未连接真实系统）」。统一检查预期为 `1022 passed, 159 skipped`，ruff、格式、mypy、导入边界与 Git 环境文件检查均通过，最后显示「统一检查全部通过」。

Step 11 实现服务树、应用、负责人和工单的同一只读接口，提供可注入快照的 Fake 与配置化 HTTP GET 适配器；三个 L0 高级 Tool 经既有 Dispatcher 执行，证据、审计、Policy 与 Replay 均保留。公司真实 API 说明尚未提供，HTTP 使用明确的 GET/JSON/Bearer 适配协议和 mock 验收，实际字段、鉴权及分页需取得公司说明后核对；本次未生产联调。没有新增数据库表、迁移或写操作，前端按 Step 47 建立。详细配置、协议和验收说明见 [Step 11 验收说明](docs/ops-platform-connector.md)。

## 自行验收 Step 12

从项目根目录的 PowerShell 执行，不需要集群凭证、Docker 或数据库：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-kubernetes.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 12 专项验收失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

专项预期 `129 passed`，之后展示 `payment-service` 的 1 个 Deployment、3 个 Pod 与 1 个 `Warning / BackOff` Event JSON，最后显示「Step 12 Fake 样例验收通过（未连接真实集群）」。统一检查预期 `1022 passed, 159 skipped`，ruff、格式、mypy、导入边界与 Git 检查通过，最后显示「统一检查全部通过」。

本机 PostgreSQL 依赖容器已运行时，可额外执行 `.\check-db.ps1`；预期 `159 passed`，并显示临时测试库已清理。其中 3 项新增测试实际验证 Kubernetes Tool 经 Dispatcher 跨会话保存 Evidence/审计与 Replay。没有新增迁移；前端工程按 Step 47 建立。配置、Tool 契约、事件关联范围与验收说明见 [Step 12 说明](docs/kubernetes-connector.md)。

## 自行验收 Step 13

从项目根目录的 PowerShell 执行，无需 Docker、数据库或公司凭证：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-observability.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 13 专项验收失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

专项预期 `126 passed`，随后展示 `payment-service` 在 UTC 窗口 `[2026-10-01T01:00:00Z, 01:10:00Z)` 内的 1 条指标序列（2 个点）、2 条日志、2 条 Trace 和 2 条 `payment-service → payment-db` 调用关系，最后显示「Step 13 Fake 样例验收通过（未连接真实系统）」。测试验证每次成功 Tool 调用恰好生成 1 条 Evidence 和 1 条 Tool 审计；窗口外数据不返回。

统一检查预期 `1022 passed, 159 skipped`，ruff、格式、mypy、导入边界与 Git 检查通过，最后显示「统一检查全部通过」。本机 Docker Desktop 与项目 PostgreSQL 运行时，执行 `.\check-db.ps1`，预期 `159 passed` 并显示临时库已清理；其中 3 项新增测试验证真实 Ledger 的跨会话证据、审计和关闭 Connector 后的 Replay。

本步提供 Prometheus、SLS、ARMS 的只读接口、可注入 Fake、原生 HTTP 适配器及 L0 的 `query_metrics`、`query_logs`、`query_traces`。真实协议与签名经 HTTP mock 验证，尚未实际环境联调。没有新增依赖、数据库迁移或写操作；前端按 Step 47 建立，本次未进入 Step 14。配置、协议、时间边界和测试范围见 [Step 13 说明](docs/observability-connectors.md)。

## 自行验收 Step 14

从项目根目录的 PowerShell 执行，无需 Docker、数据库或公司凭证：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-changes.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 14 专项验收失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

专项预期 `76 passed`，随后展示 `payment-service` 从 `v2.3.6` 到 `v2.3.7` 的代码 diff 和连接池 `50 → 500` 配置差异；发布与构建记录按时间倒序为 `37`、`36`，UTC 窗口外的 `38` 不返回。最后显示「Step 14 Fake 样例验收通过（未连接真实系统）」。专项测试实际通过既有 Dispatcher 验证每次成功 Tool 调用恰好生成 1 条 Evidence 与 1 条审计，以及 Policy 拦截和关闭来源后的 Replay。

当前统一检查预期 `1098 passed, 161 skipped`，ruff、格式、mypy、导入边界和 Git 检查全部通过。Docker Desktop 与本项目 PostgreSQL 容器运行时，继续执行 `.\check-db.ps1`，预期 `161 passed`，并显示临时测试库已清理；新增 2 项测试验证两个 Tool 的跨会话证据、审计与回放。

本步实现 GitLab/GitHub、Jenkins/GitLab CI、ArgoCD、配置中心共享只读接口、HTTP 适配器、可注入 Fake 和 L0 的 `get_recent_deployments`、`compare_versions`。真实协议经 HTTP mock 验证，尚未真实环境联调；公司配置中心 API 未提供，当前采用明确的版本化 GET/JSON 适配协议。没有新增依赖、数据库迁移或写操作；前端按 Step 47 建立。完整配置、比较语义、源系统保留历史的范围和验收说明见 [Step 14 说明](docs/change-connectors.md)。

## 自行验收 Step 15

从项目根目录的 PowerShell 执行，无需云凭证、Docker 或数据库：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-cloud.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 15 专项验收失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

专项预期 `95 passed`，随后展示 payment-service 的 8 类关联资源和 1 条 WARN 云事件。RDS 样例为活跃连接 `480`、总连接 `520`、最大连接 `600`，采样时间 `2026-10-01T01:08:00Z`；最后显示「Step 15 Fake 样例验收通过」。专项通过既有 Dispatcher 核对每次成功调用恰好追加 1 条 Evidence 与 1 条审计，Policy 拦截不查询云端，Replay 使用原结果与原 Evidence ID。

当前统一检查预期 `1193 passed, 162 skipped`，ruff、格式、mypy、导入边界与 Git 检查全部通过。已有本地 PostgreSQL 容器运行时，再执行 `.\check-db.ps1`，预期 `162 passed` 且临时库清理完成；新增 1 项测试验证云资源 Tool 的跨会话真实证据、审计与关闭 Connector 后的回放。

本步实现 ECS、RDS、Redis、RocketMQ、SLB/CLB、VPC、DNS、CDN 与云事件只读 HTTP 适配器、可注入 Fake、配置工厂和 L0 的 `get_cloud_resources`。真实协议与签名经 HTTP mock 验证，未连接实际阿里云账号；没有新增依赖、迁移或写操作。前端按 Step 47 建立。完整配置、数据含义和验收范围见 [Step 15 说明](docs/cloud-connector.md)；没有进入 Step 16。

## 自行验收 Step 16

从项目根目录的 PowerShell 执行，无需飞书凭证、Docker 或数据库：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-feishu.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 16 飞书专项验收失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

专项预期 `62 passed`，随后展示一条文本和一张发往 `ou_fake_owner` 的交互卡片 JSON。卡片标题为「payment-service 需要关注」，按钮为「查看任务」，完整内容可按通知 ID 从 Fake 发件箱读回。最后显示「Step 16 Fake 通知验收通过」。测试注册全部 12 个既有高级 Tool，确认飞书未注册；Agent 尝试发送时 Dispatcher 返回 `tool_not_found`。

当前统一检查预期 `1255 passed, 162 skipped`，ruff、格式、mypy、导入边界与 Git 检查全部通过。162 项既有 PostgreSQL 集成测试沿用 `check-db.ps1` 单独执行，本步没有数据库变更；前端按 Step 47 建立。

本步实现独立通知身份、固定本人收件人、文本与交互卡片共享接口、Fake、真实 HTTP 适配器与配置工厂。真实协议经 HTTP mock 验证，未向飞书发送实际消息；卡片回答和审批回调按后续步骤实现。完整配置、可运行样例与边界见 [Step 16 说明](docs/feishu-notifications.md)。没有进入 Step 17。

## 自行验收 Step 17

Docker Desktop 与本项目 PostgreSQL/Temporal 容器运行时，从根目录执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-workflow.ps1
if ($LASTEXITCODE -ne 0) { throw 'Workflow 专项验收失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库回归失败' }
```

本次实际结果为：专项 `36 passed`；统一检查 `1275 passed, 178 skipped`，ruff、格式、mypy（149 个源文件）、导入边界与 Git 检查通过；数据库回归 `168 passed`，临时库均已清理。统一入口跳过的依赖测试由两个专项入口实际运行。前端仍按 Step 47 建立。

在窗口 A 执行 `.\run-worker.ps1` 并保持运行；窗口 B 依次执行 `.\demo-workflow.ps1 -Mode normal`、`.\demo-workflow.ps1 -Mode signal`、`.\demo-workflow.ps1 -Mode timeout`。前两种最终为 `CLOSED`，第三种停在 `ESCALATED`；命令打印 Workflow ID、数据库状态历史和一致性结果。打开 [本地 Temporal UI](http://127.0.0.1:8080)，选择 `default` 并查询对应 ID，可查看 Activity、Timer 与 Signal。本次已实际跑通三个命令，确认 UI HTTP 200 和执行记录可查询。

阶段逻辑为占位，仅允许 local/test 与 Fake。`RESOLVED` 由独立占位 Verifier 设置；信号演示不构成真实动作审批。没有新增数据库迁移或生产运维操作，没有进入 Step 18。完整检查点、容器恢复、迁移准备与配置见 [Step 17 说明](docs/ai-task-workflow.md)。

## 自行验收 Step 22

Docker Desktop 和本项目 PostgreSQL、Temporal 运行时，在根目录执行：

```powershell
.\check-schedules.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 22 专项失败' }
.\demo-schedules.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 22 演示失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库回归失败' }
```

专项预期 `20 passed`，覆盖三个真实日历、默认 600 秒发布 Timer、重投去重、Worker 重启与 Replay。演示输出三个周期任务、一个发布任务和一个上线验证任务的事件/任务/Workflow ID，随后清理临时库和 Schedule；仅演示把延迟设为 2 秒。统一检查为 `1411 passed, 220 skipped`，数据库回归为 `201 passed, 9 skipped`；依赖专项由独立入口执行。

Step 22 交付时迁移 head 为 `0008_scheduled_events`，各步骤中的版本与测试数为历史记录。默认三个周期为北京时间工作日 09:00 开工巡检、每小时整点容量检查、每日 18:00 资源治理。查看默认 Schedule、启动与恢复 Worker、配置和完整验收说明见 [Step 22 说明](docs/scheduled-triggers.md)。

## Step 23：状态与预测驱动

新增副本 Current/Baseline/Desired 比对，以及容量耗尽、流量增长、成本异常和资源瓶颈四类趋势检测。默认 `weipai-state-prediction` Temporal Schedule 每 300 秒采集，只使用已有只读 Connector；异常先成为 OpsEvent，再创建带 Evidence 的 State/Prediction 调查任务并启动任务 Workflow。持续异常去重，确认恢复后可再次触发。主 Agent 在 Step 24 实现，当前任务沿用占位阶段并暂停到 WAITING_INFORMATION；前端仍按 Step 47 预留。

```powershell
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
.\check-detection.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 23 专项失败' }
.\demo-detection.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 23 演示失败' }
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库回归失败' }
```

演示应输出 **1 个 State + 4 个 Prediction**，每个任务带 OpsEvent/Task/Evidence ID；磁盘任务包含 UTC 预计耗尽时间。持续异常和健康复测均新增 **0** 个任务。专项/演示自动清理临时库、Schedule 和运行任务，不访问真实系统。当前迁移 head 为 `0009_state_prediction`。配置阈值、应用库升级、Temporal UI 检查及完整说明见 [Step 23 说明](docs/state-prediction-triggers.md)。未进入 Step 24。

本次统一检查 `1439 passed, 227 skipped`；Step 23 专项 `35 passed`；数据库回归 `206 passed, 11 skipped`。依赖测试分入口执行，Step 23 专项已包含全部本步 PostgreSQL/Temporal 项。本机应用库已升级到新 head，metadata 检查通过。

## Step 24：Codex Main Agent

主 Agent 已实现调查循环，所有查询走 Dispatcher，结构化结论逐条引用本次真实 Evidence ID。Temporal 在 INVESTIGATING/RCA 接入；有效结论暂停到 WAITING_INFORMATION，无效引用和步数超限进入 ESCALATED。后续模块从 Step 25 继续实现。

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
.\check-agent.ps1
if ($LASTEXITCODE -ne 0) { throw '主 Agent 专项失败' }
.\demo-agent.ps1
if ($LASTEXITCODE -ne 0) { throw '主 Agent 演示失败' }
```

专项应显示 `34 passed`。演示打印四个查询的 Evidence ID、结构化结论和三种场景结果，最后显示「Step 24 主 Agent 演示全部通过」。本机 Docker 依赖须运行，无需启动 API 或手动运行 Worker，脚本自动清理临时库和隔离任务。

本次统一检查 `1462 passed, 238 skipped`；数据库回归 212 项、既有 Workflow 36 项和事件接入 40 项通过。没有新增依赖、迁移或生产写操作，前端仍按 Step 47 预留。新事件启用调查须设置 `AGENT_CONFIG={"enabled":true,"max_steps":20}`；完整配置与验收说明见 [Step 24 文档](docs/main-agent.md)。

## Step 25：Runbook Engine

已实现完整 Runbook 内容与 pgvector 检索、L0 `search_runbooks` 和 Temporal
`RUNBOOK_MATCHING` 条件判定。适用时按诊断步骤经 Dispatcher 查询；排除、无匹配或
尚未验证时转自主调查。匹配快照支持重试复用，处理/回滚/验证方案保存供后续闭环接入。

启动 Docker Desktop 与本项目 PostgreSQL/Temporal 依赖后，在根目录执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
.\check-runbooks.ps1
if ($LASTEXITCODE -ne 0) { throw 'Runbook 专项失败' }
.\demo-runbooks.ps1
if ($LASTEXITCODE -ne 0) { throw 'Runbook 演示失败' }
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库回归失败' }
```

统一检查 `1498 passed, 254 skipped`；专项 `52 passed`。演示输出适用、排除、无匹配
三场景和真实 Evidence ID，最后显示「Step 25 Runbook Fake 演示全部通过」，并自动
清理临时库和隔离运行任务。无需手动启动 API/Worker 或提供公司凭证。
新增 head 为 `0010_runbook_engine`。前端仍按 Step 47 预留，本步按工程契约仅执行
L0 诊断，没有进入 Step 26。完整字段、配置、恢复与验收见 [Step 25 文档](docs/runbook-engine.md)。

数据库回归 `224 passed, 20 skipped`，既有主 Agent `34 passed`、Workflow `36 passed`。
本机应用库已升至新 head，metadata 一致；应用库没有演示 Runbook，临时测试库已全部清理。

## Step 26：专家 Agent

主 Agent 可按需调用 Kubernetes、Database、Network、Release、Security、Cost 六类
专家和 HolmesGPT 快照适配器。专家只能经 Dispatcher 查询固定白名单中的 Tool，
意见保存为引用事实的 Evidence；最终结论由主 Agent 给出。专家轮次计入主任务预算，
已提交意见支持重试复用与 Replay。简单场景专家调用为 0。

Docker Desktop 和本项目依赖运行时，在同一个根目录 PowerShell 窗口执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-experts.ps1
if ($LASTEXITCODE -ne 0) { throw '专家专项失败' }
.\demo-experts.ps1
if ($LASTEXITCODE -ne 0) { throw '专家演示失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库回归失败' }
```

专项应显示 `53 passed`。演示打印 Database、HolmesGPT 的意见 Evidence ID 和
主 Agent 综合结论，复杂任务暂停到 WAITING_INFORMATION，简单场景输出“专家调用 0 次”，
最后显示“Step 26 专家 Fake 演示全部通过”。脚本自动启动隔离 Worker 并清理临时库，
无需运行 API 或提供公司凭证。

本次统一检查 `1525 passed, 280 skipped`，ruff、格式、mypy、导入边界和 Git 检查通过；
数据库回归 `267 passed, 3 skipped`，既有完整 Workflow 专项 `36 passed`。
统一入口跳过的依赖测试在专项执行；数据库入口跳过的 3 项为既有定时驱动时间跳跃测试。
本步没有新依赖、迁移或生产访问；前端仍按 Step 47 建立。

Holmes 真实通道须先证明其原生工具隔离并配置公司 AI 网关，当前在联网前拒绝构造，
交付 Fake 和经 HTTP mock 验证的协议适配。配置、权限、依赖恢复和完整验收记录见
[Step 26 文档](docs/expert-agents.md)。本次只完成 Step 26。

## Step 27：Reviewer Agent

RCA 结论后自动启动独立 Reviewer，尝试从网络、Redis、发布、第三方依赖四个方向
证伪。所有事实经固定 L0 Tool 白名单和唯一 Dispatcher 查询、审计、留证。
有反证会降低置信度并返回 INVESTIGATING，无反证会提高置信度；覆盖不足转人工。
tasks 服务拦截关键任务未经有效复核的所有 PLANNING 入口。
原结论与复核分别绑定 Evidence ID，支持并发去重、重试复用、Worker 重启和 Replay。

Docker Desktop 和本项目本地依赖运行时，在根目录同一个 PowerShell 窗口执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-reviewer.ps1
if ($LASTEXITCODE -ne 0) { throw 'Reviewer 专项失败' }
.\demo-reviewer.ps1
if ($LASTEXITCODE -ne 0) { throw 'Reviewer 演示失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库回归失败' }
```

专项应显示 `40 passed`。演示须显示：跳过 Reviewer 进入 PLANNING 被拒绝；
无反证置信度 `0.7 → 0.8`；网络超时反证 `0.7 → 0.5`，任务回到 INVESTIGATING；
两轮仍有反证转 ESCALATED。最后显示“Step 27 Reviewer Fake 演示全部通过”。
脚本自动启动隔离 Worker、清理临时库和运行中的演示任务，无需启动 API 或提供公司凭证。

2026-10-07 自检：统一检查 `1549 passed, 296 skipped`，Reviewer 专项 `40 passed`，
数据库/Temporal 回归 `283 passed, 3 skipped`，既有 Workflow 专项 `36 passed`。
依赖测试在专项执行；数据库入口沿用跳过的 3 项为既有定时驱动时间跳跃测试。
本步没有新增依赖或迁移，head 保持 `0010_runbook_engine`。
复核通过后暂停到 WAITING_INFORMATION，等待 Step 28 Action Plan 接入；
前端仍按 Step 47 预留。完整行为、配置、边界、依赖恢复与自检记录见
[Step 27 文档](docs/reviewer-agent.md)。本次只完成 Step 27。

## Step 28：Action Plan

Reviewer 通过后进入 PLANNING，主 Agent 生成含目标、参数、风险、回滚和验证方式的
结构化动作计划，逐项交 Policy 判定并留证。Fake 支付场景生成
`payment-service v2.3.7 → v2.3.6` 回滚候选动作，等级 L3，判定 need_approval，
任务暂停在 WAITING_APPROVAL。缺回滚或验证方式、引用伪造证据的计划被拒。
当前普通人工信号不能触发执行；审批已按 Step 30 接入，Executor 留给 Step 32。

本机依赖运行时，在根目录 PowerShell 执行：

```powershell
.\check-action-plans.ps1
if ($LASTEXITCODE -ne 0) { throw 'Action Plan 专项失败' }
.\demo-action-plans.ps1
if ($LASTEXITCODE -ne 0) { throw 'Action Plan 演示失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库回归失败' }
```

脚本自动启动隔离 Worker、使用并清理临时库，无需公司凭证或手动运行 API。
本步无新增依赖或迁移；前端按 Step 47 预留。完整行为、预期输出和检查记录见
[Step 28 文档](docs/action-plans.md)。旧主 Agent、专家、Reviewer 和 Runbook 演示的
正常调查场景现在经过 PLANNING 暂停到 WAITING_APPROVAL。

## Step 29：人工判断与补充信息

两个独立人工等待状态已接入问题卡片、回答信号、Evidence/审计和引用任务的 Knowledge
草稿。回答后恢复原阶段继续调查；草稿不进入正式知识检索。

Docker Desktop 和本项目 PostgreSQL/Temporal 运行时，在根目录 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-human.ps1
if ($LASTEXITCODE -ne 0) { throw '人工问答专项失败' }
.\demo-human.ps1 -Interactive
if ($LASTEXITCODE -ne 0) { throw '交互演示失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
. .\use-local-temporal.ps1
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库回归失败' }
```

专项应显示 `44 passed`。交互演示中各回答一个业务判断和补充信息问题，应打印你的回答、
Evidence ID、Knowledge 草稿 ID，随后继续主 Agent/Reviewer/Action Plan 并停到
`WAITING_APPROVAL`，最后显示“Step 29 人工判断与补充信息 Fake 演示全部通过”。
省略 `-Interactive` 使用固定回答自动演示；脚本自动启动 Worker、清理临时库和演示任务。

本步新增 head `0011_human_interaction`，本机应用库已升级且 metadata 一致。
真实飞书回调 HTTP 入口与前端按后续计划实现；审批见 Step 30。
问答信号格式、依赖恢复、知识草稿边界和完整检查说明见
[Step 29 文档](docs/human-interaction.md)。

## Step 30：审批流

WAITING_APPROVAL 已接入完整动作哈希绑定的审批单、Fake 飞书批准／拒绝卡片、
Temporal 信号、操作人审计与超时。篡改参数或目标等计划字段后，原审批失效。
批准进入 EXECUTING 完成本步授权交接，等待 Step 32；拒绝／超时转 ESCALATED。
本步实际执行次数为 0。

在本机依赖运行时，于根目录 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-approval.ps1
if ($LASTEXITCODE -ne 0) { throw '审批专项失败' }
.\demo-approval.ps1 -Interactive
if ($LASTEXITCODE -ne 0) { throw '审批演示失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

专项应显示 `40 passed`。演示可自行输入“批准”或“拒绝”，随后自动演示拒绝和超时，
打印审批 Evidence ID、操作人、参数篡改失效和实际执行次数 0；最后显示
“Step 30 审批流 Fake 演示全部通过”与“临时测试库已清理”。
脚本自动启动隔离 Worker，无需手动启动 API。完整回归在同一窗口加载
`. .\use-local-temporal.ps1` 后运行 `.\check-db.ps1`。

本步无新增依赖或迁移，head 保持 `0011_human_interaction`；真实飞书回调 HTTP 入口
按 Step 43/44 实现，前端按 Step 47 建立。详细行为、信号和恢复说明见
[Step 30 文档](docs/approval-flow.md)。

## Step 31：独立 Verifier

`verify_action` 已实现八项独立恢复检查：Deployment、Pod、5xx、P99、成功率、日志、
Trace、关联资源。全部通过才进入 `RESOLVED`；未恢复、缺数据或事实查询被拒绝回到
`INVESTIGATING`。仅传 `actor=verifier` 无法设置 RESOLVED，每项检查都引用真实 Evidence ID。
查询走既有 Dispatcher，L0 只读；本步没有运维动作执行，Executor 留给 Step 32。

Docker Desktop 和本项目 PostgreSQL、Temporal 容器运行时，在根目录 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-verifier.ps1
if ($LASTEXITCODE -ne 0) { throw 'Verifier 专项失败' }
.\demo-verifier.ps1
if ($LASTEXITCODE -ne 0) { throw 'Verifier 演示失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

专项应显示 `53 passed`。演示显示恢复场景 `RESOLVED`、未恢复场景 `INVESTIGATING`、
两次伪造身份被拒绝，并打印每场景的 8 个事实 ID 和 1 个聚合 Evidence ID。
最后显示“实际运维动作执行次数：0”“Step 31 Verifier Fake 演示全部通过”和
“临时测试库已清理”。脚本自动准备隔离 Worker 和临时库，无需启动 API 或提供公司凭证。

完整数据库/Temporal 回归在同一窗口加载 `. .\use-local-temporal.ps1` 后执行
`.\check-db.ps1`；既有 Workflow 回归执行 `.\check-workflow.ps1`。
本步没有新增依赖或迁移，head 保持 `0011_human_interaction`，前端仍按 Step 47 建立。
恢复标准、阈值配置、历史兼容与完整验收说明见 [Step 31 文档](docs/verifier.md)。

2026-10-07 自检：统一检查 `1664 passed, 364 skipped`；Verifier 专项 `53 passed`；
本机数据库/Temporal 回归 `351 passed, 3 skipped`；既有 Workflow 专项 `36 passed`。
依赖测试由专项执行，数据库入口的 3 项跳过为既有时间跳跃测试。两个 Fake 演示状态、
权限拒绝、Evidence 和 PowerShell 语法均通过，临时库已清理。本次只完成 Step 31。

## Step 32：Executor

已接入重启、扩缩容、回滚的精确授权执行；只有当前 Policy 放行或有效动作审批才能签发
短时凭证。执行、Evidence 和审计经唯一 Dispatcher；并发、重投、审计失败和 Worker
重启按相同幂等键恢复。执行完成进入 `VERIFYING`，独立 Verifier 继承实际回执验证目标。
当前真实动作端尚未联调，生产写入关闭，全部验收只使用 Fake。

Docker Desktop 与本项目 PostgreSQL／Temporal 运行时，在根目录 PowerShell 执行：

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

专项应显示 `49 passed`。交互演示输入「批准」后，应显示支付版本 `v2.3.7 → v2.3.6`、
`VERIFYING`、执行 Evidence ID 与执行次数 1；凭证操作其他服务和到期均被拒绝，
相同请求重投和关闭 Connector 后的回放不增加执行次数。后续拒绝／超时为
`ESCALATED` 且执行次数 0。省略 `-Interactive` 自动演示。
最后显示「Step 32 Executor Fake 演示全部通过」「临时测试库已清理」。

统一检查 `1690 passed, 387 skipped`，ruff／格式／mypy、Connector 边界与 Git 检查通过。
最终数据库／Temporal 回归 `374 passed, 3 skipped`，既有 Workflow 专项 `36 passed`。
3 项跳过为既有时间跳跃测试，依赖测试按专项入口执行；全部临时库已清理。
脚本自动运行隔离 Worker 和临时库，无需启动 API 或提供公司凭证。
本步没有新增依赖或迁移，head 仍为 `0011_human_interaction`；前端按 Step 47 预留。
配置、真实通道限制、幂等与验证交接、完整自检记录见 [Step 32 文档](docs/executor.md)。
本次只实现 Step 32，Step 33 自动熔断仍未开始。

## 自行验收 Step 35：Runbook 成熟度

默认人工审核后按成功 3／5／10／20 次依次晋级，统计只接受独立 Verifier 的有效结果；
连续两次失败降级并撤销审核。内容修改产生新版本，旧审核与统计不再适用。
成熟度参与规划、审批和执行门禁，L3–L5 仍需审批。

Docker Desktop 与本项目本地依赖运行时，在根目录 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-maturity.ps1
if ($LASTEXITCODE -ne 0) { throw '成熟度专项失败' }
.\demo-maturity.ps1
if ($LASTEXITCODE -ne 0) { throw '成熟度演示失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
. .\use-local-temporal.ps1
.\check-db.ps1
if ($LASTEXITCODE -ne 0) { throw '数据库与 Temporal 回归失败' }
```

演示打印六级成熟度、验证 Evidence ID、Temporal Workflow ID，以及两次失败后的降级。
最后应显示「Step 35 Runbook 成熟度 Fake 演示全部通过」「临时测试库已清理」。
脚本自动运行隔离 Worker 和临时库，无需启动 API 或提供公司凭证。
本步新增迁移 `0013_runbook_maturity`；前端按 Step 47 建立。
规则、配置、应用库升级和完整检查点见 [Step 35 说明](docs/runbook-maturity.md)。

2026-10-07 自检：专项 `64 passed`，统一检查 `1779 passed, 433 skipped`，
ruff／格式／mypy（335 个源文件）、Connector 边界与 Git 检查通过；完整数据库／Temporal
回归 `420 passed, 3 skipped`，3 项跳过为既有定时驱动时间跳跃测试。
最终演示、PowerShell 语法、空库迁移与应用库 metadata 检查通过；
本机应用库为 `0013_runbook_maturity (head)`，临时库和本步隔离运行任务已清理。
该记录对应 Step 35 的验收，后续步骤以 plans.md 当前状态为准。

## 自行验收 Step 36：Replay 与 AI 评价

只使用历史截止点前已经入库并成功审计的结果，复用主 Agent 调查循环与统一 Dispatcher。
回放注册表没有 Connector 实例和写 Tool；原事故保持 CLOSED，报告与恢复检查点追加到 Ledger。
10 项能力指标同时返回分子、分母和结果，缺人工标签或零样本时为未知。

在已启动 Docker Desktop 的 PowerShell 中执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
. .\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw '本机依赖启动失败' }
.\check-replay.ps1
if ($LASTEXITCODE -ne 0) { throw 'Replay 专项失败' }
.\demo-replay.ps1
if ($LASTEXITCODE -ne 0) { throw 'Replay 演示失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

演示应显示原事故 CLOSED、Replay Workflow ID、基线与候选根因均命中、误判 false、
调查 Tool Call 各 4 次、历史 Evidence ID、回放期间 Connector 调用 0 和完整 10 项指标。
末尾显示「Step 36 Replay 与 AI 评价 Fake 演示全部通过」「临时测试库已清理」。
脚本自动创建隔离库和 Worker，无需生产凭证、API 或手动操作数据库。
完整边界、指标口径与回归方式见 [Step 36 说明](docs/replay-evaluation.md)。
没有新增数据库迁移或依赖；前端工程仍按 Step 47 建立。

2026-10-07 最终自检：专项 `47 passed`；统一检查 `1803 passed, 447 skipped`，
ruff/格式/mypy（345 个源文件）、导入边界与 Git 检查通过；完整本机数据库/Temporal
回归 `434 passed, 3 skipped`，3 项为既有时间跳跃测试，沿原独立入口执行。
最终演示、PowerShell 语法、依赖健康和应用库 metadata 检查通过；临时库已清理。
Step 36 交付时未进入 Step 37；Step 37 的当前验收说明见下文。

## 自行验收 Step 37：Automation Discovery

统计重复工单、故障、Runbook、发布检查和人工操作，默认同服务同类记录累计 5 次时生成一个带原始记录引用的自动化建议任务。周期扫描、失败重试和派发使用 Temporal；同类建议并发/重投不重复创建。

Docker Desktop 的本项目本机依赖运行后，在根目录 PowerShell 执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\check-automation.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 37 专项检查失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-automation.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 37 演示失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

演示应显示“四条记录时建议任务数 0、五条时 1、五条原始记录引用、重复扫描新增 0、实际运维动作数 0”，并输出建议 Evidence ID/Workflow ID。等待任务和临时数据库自动清理。统计口径、配置、默认 Schedule 和限制见 [Step 37 说明](docs/automation-discovery.md)。

本步只生成评估建议，实施仍走既有 Policy/审批/Executor/Verifier；未进入 Step 38，前端按 Step 47 保留。

2026-10-07 自检：专项 `36 passed`；统一检查 `1828 passed, 458 skipped`，ruff/格式/mypy、导入边界和 Git 检查通过；完整数据库/Temporal 回归 `445 passed, 3 skipped`（既有时间跳跃项沿独立入口）。演示实际通过，Windows 自带 PowerShell 中文脚本编码已修复，临时库/测试 Schedule/本步运行 Workflow 全部清理。

## 自行验收 Step 40：巡检与治理

已有三个定时入口现已接入实际巡检：覆盖设计的 17 类检查，以及稳定性、容量、安全、
成本四类治理。只有异常和待补充信息项生成风险并通知；持续异常去重，可信恢复后清除，
再次异常重新通知。任务状态仍由统一 AI Task 引擎管理，独立 Verifier 核验后结束扫描，
未修复风险保持打开。

启动 Docker Desktop 及本项目本机 PostgreSQL/Temporal 后，在 PowerShell 中执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
powershell -NoProfile -ExecutionPolicy Bypass -File .\check-inspections.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 40 专项失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-inspections.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 40 演示失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

演示应显示：缺 PDB、缺 HPA、证书六天后过期、闲置 ECS 共 **4 条风险**；巡检覆盖类别
**17**；重复扫描新增风险/通知、全部健康新增通知和实际运维动作数均为 **0**。
各任务 `CLOSED`，报告打印真实 Evidence ID 与 Workflow ID，末尾显示
“Step 40 巡检与治理 Fake 演示全部通过”“临时测试库已清理”。脚本自动准备隔离
Worker 和临时库，无需启动 API 或提供公司凭证。

本步新增迁移 `0014_inspection_risks`，本机应用库已升级且 metadata 一致。
真实巡检事实接口尚未提供，当前验收为 Fake/HTTP mock；前端按 Step 47 保留。
检查目录、配置、恢复/去重边界和完整自检记录见 [Step 40 说明](docs/inspection-governance.md)。
本次只实现 Step 40，后续步骤以 plans.md 状态为准。

## 自行验收 Step 41：架构评审

技术方案经 OpsEvent/Human AI Task 和同一 Temporal Workflow 评审，结合 Context Graph、
有效公司规范和历史故障，输出设计第 27 节的全部十二个维度与 Evidence 原文引用。
单点数据库样例在稳定性/高可用维度指出风险；材料不足的维度明确待补充。

启动 Docker Desktop 及本项目本机 PostgreSQL/Temporal 后，在 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
powershell -NoProfile -ExecutionPolicy Bypass -File .\check-architecture.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 41 专项失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-architecture.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 41 演示失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

演示应显示 **12 个维度、稳定性/高可用单点风险、真实 Evidence 引用、10 个待补充维度、
CLOSED、重复提交新增任务 0、实际运维动作 0、Temporal 历史回放通过**。脚本自动准备
临时库与隔离 Worker，并清理临时库；无需启动 API 或提供公司凭证。
本步完成后仍不进入 Step 42；API 与前端按后续计划实现。
使用方式、实现范围和自检记录见 [Step 41 说明](docs/architecture-review.md)。

## 自行验收 Step 42：War Room

重大活动经 OpsEvent/Human 任务接入同一 Temporal Workflow，完成容量评估、32 项准备
检查、独立 Reviewer、L3 准备、逐窗盯盘、异常派发、结束后单独审批回收、独立验证和
全部 11 章保障报告。

启动 Docker Desktop 及本项目本机依赖后，在 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
powershell -NoProfile -ExecutionPolicy Bypass -File .\check-war-room.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 42 专项失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-war-room.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 42 演示失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

预期显示 **3→5 准备、5→3 回收、1 个去重异常任务、2 个分别审批的 Fake 动作、11 章报告、
Evidence ID、CLOSED 和 Temporal 历史回放通过**，末尾显示“临时测试库已清理”。
可加 `demo-war-room.ps1 -Interactive` 亲自输入两次 `approve`，交互演示约 90 秒。
全部为本机 Fake，无需 API、生产凭证或手动启动 Worker。

配置、授权失效、资源归属及回收停止条件见 [Step 42 说明](docs/war-room.md)。无新迁移，
head 保持 `0014_inspection_risks`；真实事实聚合协议仍待接入。本次只实施 Step 42，不进入
Step 43；前端按 Step 47 建立。

2026-10-08 自检：最终保障专项 `43 passed`，统一检查 `1981 passed, 530 skipped`；完整
数据库/Temporal 回归 `516 passed, 3 skipped`，最终新增动作身份场景由专项覆盖，三个时间
跳跃场景由定时专项 `20 passed` 覆盖，旧 Workflow `36 passed`。自动及交互演示通过，
临时库已清理；完整记录见 Step 42 说明。

## Step 43：单用户登录与会话鉴权

已接入环境配置账户、可过期/可撤销的 PostgreSQL 会话、整个 `/api` 命名空间的登录门禁、
CSRF 与来源校验。API 重启后会话继续有效，退出或账户/密钥轮换后旧 Cookie 失效；
密码、签名密钥和完整 Cookie 不入库。既有 Webhook 保持签名校验。

启动 Docker Desktop 和本项目已有本机依赖后，从根目录运行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
powershell -NoProfile -ExecutionPolicy Bypass -File .\check-auth.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 43 专项失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-auth.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 43 HTTP 演示失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

HTTP 演示预期：**未登录 401 → 登录 200 → 查询身份 200 → 缺 CSRF 403 → 退出 204 → 会话失效 401**。
临时 API 进程和数据库自动清理，没有生产请求或真实飞书发送。

自己设置账户并手动调用 API 的完整命令见 [Step 43 鉴权说明](docs/authentication.md)。
新迁移为 `0015_single_user_auth`，先升级本机应用库再启用登录；前端仍待 Step 47 建立，
审批/判断/接管业务 HTTP 接口仍由 Step 44 实现。本次只实施 Step 43。

2026-10-08 自检：最终鉴权专项 **56 passed**，统一检查 **2023 passed、544 skipped**，
完整数据库/Temporal 回归 **531 passed、3 skipped**（既有时间跳跃专项）。真实 HTTP 演示、
自设账户辅助脚本、迁移升降级与 metadata 检查通过，临时库和演示 API 已清理。

## Step 45：认知与运营 API

从根目录运行 `check-operations.ps1`、`demo-operations.ps1 -Interactive`、`check.ps1`。
演示时输入自己的业务规则，确认服务图、四条风险、目录增删改、六条本人编辑审计及十项指标。
接口列表、逐项 Swagger 验收和预期输出见 [Step 45 验收说明](docs/operations-api.md)。

2026-10-08 自检：本步专项 **82 passed**，统一检查 **2134 passed/580 skipped**，
最终源码完整数据库/Temporal 回归 **567 passed/3 skipped**；三个时间跳跃场景由定时专项
**20 passed** 覆盖，人工交互专项 **44 passed**。自动和中文交互真实 HTTP 演示通过。
应用库已升级至 `0016_catalog_audit` 且 metadata 一致，临时数据库和演示 API 已清理。
本次只完成 Step 45，没有进入 AI Chat 或前端步骤。

## 自行验收 Step 46：AI Chat API

已接入登录保护的主 Agent SSE 对话、追问和持久化回答查询。每轮输入先归一为
OpsEvent/manual 和 Human 任务；只读回答经过独立证据核验，动作请求进入已有
Reviewer、Policy、审批和执行链路。重复请求保持同一任务，SSE 断开后任务保留。

启动 Docker Desktop 和本项目本机依赖后，在根目录 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
powershell -NoProfile -ExecutionPolicy Bypass -File .\check-chat.ps1
if ($LASTEXITCODE -ne 0) { throw '聊天专项失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-chat.ps1 -Interactive
if ($LASTEXITCODE -ne 0) { throw 'SSE 演示失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

专项应显示 **42 passed**。演示可自己输入支付场景问题，逐段看到回答和真实 Evidence ID；
首轮与追问 CLOSED，重复请求新增任务 0；回滚请求显示 L3 / need_approval /
WAITING_APPROVAL，审批前实际运维动作 0。末尾显示“Step 46 AI Chat API Fake 演示全部通过”
及“临时测试库已清理”。省略 `-Interactive` 使用默认问题，API/Worker/临时库自动准备与清理。

完整协议、依赖启动、重连方式和自检结果见 [Step 46 验收说明](docs/ai-chat-api.md)。
本步为本机 Fake 验收，真实网关与生产系统尚未联调；没有新增迁移或依赖，
head 保持 0016_catalog_audit。本次只实现 Step 46。

## 自行验收 Step 49：审批中心页面

已实现审批、人工判断、补充信息与人工接管，三类等待任务分开展示。按以下命令检查，并在浏览器亲自处理一组本机 Fake 样例：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-approval-pages.ps1 -Interactive
if ($LASTEXITCODE -ne 0) { throw '审批页面验收失败' }
```

输入隐藏的临时密码后，脚本先自动验收，再打印浏览器地址与五个样例链接。使用 `local-browser-owner` 登录：批准后刷新显示真实 `EXECUTING`；拒绝/接管显示 `ESCALATED`；判断与补充信息使用独立接口恢复任务。审批证据与本人审计可追溯。这个页面演示仅完成批准后的授权交接，没有启用 Executor，运维执行次数为 0。检查结束按 Enter 清理临时库与服务。

逐项操作、预期结果、版本冲突与结果未确认时的重试方法见 [Step 49 验收说明](docs/approval-pages.md)。本次只实现 Step 49，没有进入 Step 50。

## 自行验收 Step 50：认知页面

服务与上下文图支持关系来源、置信度、新鲜度和 UTC 观察时间；运行手册与知识中心支持筛选、分页及完整增删改。

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-cognition-pages.ps1 -Interactive
if ($LASTEXITCODE -ne 0) { throw '认知页面验收失败' }
```

本机依赖启动后，输入隐藏的临时密码。脚本先自动核对真实 API 与落库审计，再打印浏览器地址；使用 `local-browser-owner` 登录，悬停/选择服务关系，并亲自新建、编辑、删除手册和知识。每次保存后刷新页面确认持久化，检查完成回终端按 Enter 清理临时库与服务。

字段填写、预期结果、依赖恢复与检查范围见 [Step 50 验收说明](docs/cognition-pages.md)。本次只完成 Step 50。

## 自行验收 Step 51：运营页面

发布、工单、巡检、风险、重大保障、架构评审、自动化七个入口支持列表与详情；报告 Evidence 可精确读回，巡检与风险相互关联，筛选和详情返回保留查询条件。

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-operations-pages.ps1 -Interactive
if ($LASTEXITCODE -ne 0) { throw '运营页面验收失败' }
```

本机依赖启动后设置隐藏的临时密码。脚本运行既有 Fake Workflow 并自动检查真实 API/Edge，再打印可浏览地址。用 `local-browser-owner` 登录：巡检已关闭，四条风险仍未恢复；逐一查看七类记录与 Evidence，筛选风险、刷新和返回后核对条件。检查完成回终端按 Enter 清理临时库与服务。

详细预期结果与依赖恢复见 [Step 51 验收说明](docs/operations-pages.md)。本次只完成 Step 51。

## 自行验收 Step 52：总览与审计

总览显示运行中任务、三类独立待处理事项和十项能力指标；审计可按 UTC 时间、类型、操作人精确筛选，详情与 Evidence 可读回。

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-dashboard-pages.ps1 -Interactive
if ($LASTEXITCODE -ne 0) { throw '总览与审计验收失败' }
```

本机依赖启动后，设置隐藏的临时密码。脚本完成自动检查后打印地址，用 `local-browser-owner` 登录：核对总览待审批数量与审批中心一致，展开十项指标查看原值、分子和分母；审计筛选 `local-browser-seed` + “内容编辑”应有两条，详情返回保留条件。完成后回终端按 Enter 清理临时库与服务。

逐项验收、时间窗口径与依赖启动见 [Step 52 验收说明](docs/dashboard-audit.md)。本次只实施 Step 52。

## 自行验收 Step 53：AI Chat 页面

已实现主 Agent 流式问答、追问、可点击的 Evidence 引用、回答恢复与处置任务入口。
对话生成真实 Human AI Task，处置请求沿用 Policy/审批门禁。

启动 Docker Desktop 及本项目本机依赖后，在根目录 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-chat-pages.ps1 -Interactive
if ($LASTEXITCODE -ne 0) { throw '聊天页面验收失败' }
```

设置隐藏临时密码，自动验收后打开脚本打印的地址，用 `local-browser-owner` 登录。
保持固定支付样例时间窗，发送问题、点击 Evidence、刷新恢复、追问，并在任务中心筛选“人工”。
处置模式回滚应显示等待审批，动作计划 L3，审批前执行 0。完成后回终端按 Enter 清理。

依赖启动、逐项操作、断流恢复与完整自检结果见 [Step 53 验收说明](docs/chat-page.md)。
本步全部使用本机 Fake；本次只完成 Step 53。
