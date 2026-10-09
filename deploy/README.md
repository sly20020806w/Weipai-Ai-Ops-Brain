# Step 2：本地依赖环境

Step 56 的 Dockerfile 在 `deploy/images/`，实际构建、启动与浏览验收见 [镜像与 CI](../docs/images-ci.md)。

本目录只提供本地开发依赖。使用本机 Docker Engine 的 Linux 容器模式和 Docker Compose v2+（支持 `up --wait`），不连接公司运维系统。

## 服务与数据

| 服务 | 镜像 | 本机地址 | 成功状态 |
| --- | --- | --- | --- |
| PostgreSQL + pgvector | `pgvector/pgvector:0.8.6-pg16-bookworm` | `127.0.0.1:5432` | `healthy` |
| Temporal schema 初始化 | `temporalio/admin-tools:1.31.0` | 不发布端口 | `Exited (0)` |
| Temporal Server | `temporalio/server:1.31.0` | `127.0.0.1:7233` | `healthy` |
| Temporal CLI / 命名空间初始化 | `temporalio/admin-tools:1.31.0` | 不发布端口 | `healthy` |
| Temporal UI | `temporalio/ui:2.49.1` | `http://127.0.0.1:8080` | `healthy` |

Compose 使用独立的 `weipai-ai-ops-brain-local` 项目网络与 `postgres-data` 命名卷。所有宿主机端口只绑定回环地址。

同一个 PostgreSQL 实例维护 `weipai`、`temporal` 和 `temporal_visibility` 三个独立数据库。应用库首次初始化时安装 `vector` 扩展；Temporal 自身表结构通过官方 SQL schema 工具创建和升级。Step 3 再引入应用的 SQLAlchemy/Alembic 及业务模型。

启动依赖顺序为 PostgreSQL 就绪 → Temporal schema 初始化成功 → Temporal 端口就绪 → 集群 `SERVING` 与 `default` 命名空间就绪 → UI。`temporal-schema` 是运行完即退出的一次性初始化容器，因此正常状态为退出码 0；其余四个容器必须全部 `healthy`。`temporal` 自身的容器健康检查检测端口，`temporal-admin` 和验收脚本进一步检查 gRPC 集群健康状态。

官方 `server:1.31.0` 镜像不含旧的默认 `docker.yaml` 动态配置文件；本地通过环境变量 `DYNAMIC_CONFIG_FILE_PATH=/dev/null` 使用默认动态配置，不额外引入文件中的配置值。

## 环境变量

密码只从环境变量传入，不提供固定默认密码，不写入源文件或应用数据库。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | 无，必填 | 本地开发数据库密码 |
| `POSTGRES_USER` | `weipai` | 本地初始化用户 |
| `POSTGRES_PORT` | `5432` | PostgreSQL 本机端口 |
| `TEMPORAL_PORT` | `7233` | Temporal gRPC 本机端口 |
| `TEMPORAL_UI_PORT` | `8080` | Temporal UI 本机端口 |

数据卷初始化后，再次启动必须使用同一个数据库用户和密码；修改环境变量不会自动修改已有数据库角色。不要把生产凭证用于本地开发，也不要输出完整 `docker compose config`（其中会包含传入的密码）；静态检查使用 `config --quiet`。

## 启动与自动验收

本机的依赖环境已启动并完成验收。本次启动使用随机本地密码，仅通过容器环境变量传入。现在可以在新 PowerShell 窗口加载现有配置并复检：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\use-local-deps.ps1
.\check-deps.ps1
.\check.ps1
```

`use-local-deps.ps1` 通过固定项目和服务标签定位本项目 PostgreSQL 容器，将其环境变量中的用户和密码加载到当前进程，不显示密码、不写文件。它也适用于容器已停止但尚未被 `down` 删除的情况；若容器不存在，脚本明确报错，按以下首次创建流程处理。使用自定义端口时，在当前窗口设置相同的端口环境变量。

以下命令从仓库根目录运行。先启动 Docker Desktop，检查本机 Linux 引擎：

```powershell
docker context show
docker info
```

本机 Windows 的预期 context 为 `desktop-linux`，`docker info` 应显示 Server 且 `OSType` 为 `linux`。首次运行需联网拉取四个官方镜像。

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
$env:POSTGRES_PASSWORD = [System.Net.NetworkCredential]::new('', (Read-Host '本地数据库密码（请记住，后续启动使用同一个）' -AsSecureString)).Password
docker compose -f deploy/docker-compose.yml config --quiet
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw '依赖启动失败，请查看下面的排查命令' }
.\check-deps.ps1
.\check.ps1
```

`check-deps.ps1` 是单独的集成验收入口，需要 Docker 引擎及上述容器已运行。它检查四个常驻服务的健康状态、schema 初始化退出码、`CREATE EXTENSION`、向量类型、UTC 时区、Temporal 集群健康、默认命名空间以及 UI 首页和命名空间 API。任何一项失败都返回非零退出状态。已有 `check.ps1` 仍执行后端 ruff/mypy/pytest，不要求 Docker；前端工程按 Step 47 建立后纳入统一检查。

浏览器打开 <http://127.0.0.1:8080>，选择 `default` 命名空间。现在没有 Worker 和 AI Task，工作流列表为空是正常的。

端口被占用时，在启动前修改对应环境变量，例如：

```powershell
$env:POSTGRES_PORT = '15432'
$env:TEMPORAL_PORT = '17233'
$env:TEMPORAL_UI_PORT = '18080'
```

此时 UI 改为 <http://127.0.0.1:18080>，验收脚本使用相同环境变量。

## 手动检查

在同一个已设置密码的 PowerShell 窗口运行：

```powershell
docker compose -f deploy/docker-compose.yml ps --all
$postgresUser = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { 'weipai' }
docker compose -f deploy/docker-compose.yml exec -T postgres psql -v ON_ERROR_STOP=1 -U $postgresUser -d weipai -c 'CREATE EXTENSION IF NOT EXISTS vector;' -c "SELECT extversion FROM pg_extension WHERE extname = 'vector';" -c 'SHOW timezone;'
docker compose -f deploy/docker-compose.yml exec -T temporal-admin temporal operator cluster health --address temporal:7233
docker compose -f deploy/docker-compose.yml exec -T temporal-admin temporal operator namespace describe --address temporal:7233 --namespace default
(Invoke-WebRequest -Uri 'http://127.0.0.1:8080' -UseBasicParsing).StatusCode
```

分别预期常驻容器 `healthy`、初始化容器 `Exited (0)`，扩展版本 `0.8.6` 与时区 `UTC`，集群 `SERVING`，命名空间 `default`，HTTP `200`。

## 停止、重启与排查

停止并保留数据：

```powershell
docker compose -f deploy/docker-compose.yml down
```

重新设置原用户名和密码后，用相同的 `up -d --wait --wait-timeout 180` 命令启动，再执行 `check-deps.ps1`。初始化脚本可重复运行，已有 schema 继续使用官方工具检查升级，不清空数据库；命名空间已存在时不会重复创建。`down` 保留命名卷；不要添加 `--volumes` 或 `-v`，除非明确要丢弃整个本地环境的数据。

若使用本次随机密码，请在 `down` 前先执行 `use-local-deps.ps1`，并在同一 PowerShell 窗口中完成停止和重新启动，当前进程中的环境变量会继续保留。若需要跨窗口保留密码，请保存到自己的密码管理器。也可以用 `docker compose -f deploy/docker-compose.yml stop` 暂停容器；容器保留时，新窗口仍可通过 `use-local-deps.ps1` 加载配置。

启动失败时先检查：

```powershell
docker compose -f deploy/docker-compose.yml ps --all
docker compose -f deploy/docker-compose.yml logs --tail 100 postgres temporal-schema temporal temporal-admin temporal-ui
```

数据库密码错误应恢复首次初始化时的环境变量。schema 或命名空间初始化失败会阻止后续依赖启动，不会忽略错误继续启动。

## 本次自检记录（2026-10-06）

- 三份约定和计划已完整阅读；实际计划文件名是 `plans.md`。首个未完成项是 Step 2。
- Compose 配置已通过 `docker compose config --quiet`；缺少 `POSTGRES_PASSWORD` 时会明确拒绝解析配置。端口覆盖、回环地址绑定及 UI origin 随端口更新的检查均通过。
- 四个固定版本的镜像标签均已通过 `docker manifest inspect` 核验存在。
- PowerShell 验收脚本语法与缺少密码拒绝检查通过；两个初始化脚本通过 Linux `sh -n` 检查，容器挂载文件均为 LF。后端统一检查通过，pytest 8 个测试通过。
- Docker Desktop 4.85.0 的启动故障已恢复：在完全停止时同步保留并重建 `Docker/run` 与 `docker-secrets-engine` 两个临时端点目录，然后启动成功。原目录以 `.recovery-20261006-step2-combined` 后缀保留，较早失败尝试的端点目录以 `.failed-20261006-step2` 后缀保留。未删除镜像、容器数据或虚拟磁盘；原有本地容器正常运行。
- 实际启动发现并修复 Temporal 缺少默认动态配置文件的问题，以及验收脚本将 Compose 的逐行 JSON 误当成单个 JSON 文档的问题。
- `docker compose up -d --wait` 成功；四个常驻容器全部 `healthy`，一次性 schema 容器 `Exited (0)`。第二次启动重新运行 schema 工具也成功，未清空已有数据库。
- `CREATE EXTENSION IF NOT EXISTS vector` 成功；扩展版本为 `0.8.6`；向量类型返回 `[1,2,3]`；数据库时区为 UTC。
- `temporal operator cluster health` 返回 `SERVING`；默认命名空间已注册；UI 首页与 `/api/v1/namespaces` 均 HTTP 200，UI 能读到 `default` 命名空间。
- 通过新的进程加载现有数据库环境，再执行 `check-deps.ps1` 和 `check.ps1`，两者全部通过；ruff/mypy/pytest 通过，pytest 8 个测试通过。
- Step 2 已标记完成。本次未进入 Step 3，前端工程尚未建立，按 Step 47 再纳入前端检查。

## 官方参考

镜像版本与 SQL 初始化方式参考 [Temporal 官方 Compose 示例](https://github.com/temporalio/samples-server/tree/main/compose)。使用当前受维护的 `server` / `admin-tools` 镜像；[旧 `auto-setup` 镜像已弃用](https://hub.docker.com/r/temporalio/auto-setup)。pgvector 镜像版本与扩展用法参考 [pgvector 官方说明](https://github.com/pgvector/pgvector)。
