# Step 56：镜像与 CI

依据 AGENTS.md、SPEC.md、plans.md 和权威原设计实现。本步交付一个后端镜像的 `api` / `worker` 两个入口、前端镜像及 CI。Helm、ACK、真实 Connector 和生产 Worker 开放留给 Step 57–58。

## 自己跑一遍

启动 Docker Desktop，保持本项目四个常驻依赖容器 healthy。在 Windows PowerShell 中执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\check-images.ps1
if ($LASTEXITCODE -ne 0) { throw '镜像验收失败' }
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

首次构建需要下载官方基础镜像及锁定依赖。两个镜像标记为 `weipai-backend:step56`、`weipai-frontend:step56`。成功时分别显示 **“镜像验收全部通过”** 和 **“统一检查全部通过”**；失败返回非零退出码。

镜像验收核对：同一后端镜像启动 API 后 `/health` 为 HTTP 200 且 `status=ok`；镜像内 Alembic 升级临时库到现有 head；同一镜像 Worker 实际连接本机 Temporal 并轮询独立队列；前端 healthy、SPA 深链接刷新、不存在的静态资源 404；同源代理下匿名 401、登录 200、缺 CSRF 403、退出 204、旧会话 401；两个镜像非 root，后端无 pytest/ruff；缺 APP_ENV、无效入口、无效/多行 API_UPSTREAM 均拒绝启动。

统一检查继续包含 Connector 边界、ruff/格式/mypy/pytest、Git 环境规则、密钥/依赖扫描、九项本机 Fake E2E，以及前端 OpenAPI 一致性/lint/typecheck/test/build。既有数据库/Temporal 专项继续使用原独立入口。镜像构建和统一检查按上述顺序执行，避免同时下载依赖、构建和运行 E2E 争用本机资源。

## 浏览器手动验收

已有镜像时执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\check-images.ps1 -SkipBuild -Interactive
```

按提示自行设置本次临时密码，输入隐藏。脚本自动验证后打印随机本机地址，登录名是 `image-check`。打开该地址，登录，访问控制台入口，刷新页面并退出。临时库没有预先导入业务样例，列表为空是预期结果；业务样例仍使用此前各页面演示命令。

终端按 Enter 后自动停止本次容器、删除本次唯一随机 Discovery Schedule 和临时库。脚本不移除个人依赖容器/卷或修改默认 Schedule。密码、AUTH_CONFIG、DATABASE_URL 只保留在进程/容器环境，不写 `.env`、镜像或报告。脱敏报告在 `.cache/images/<随机ID>/report.json`，包含 UTC 时间、镜像 ID 和检查结果。

## 构建与运行

```powershell
docker build -f deploy/images/backend.Dockerfile -t weipai-backend:step56 .
docker build -f deploy/images/frontend.Dockerfile -t weipai-frontend:step56 .
```

两个 Dockerfile 使用仓库根目录作为上下文。`.dockerignore` 采用允许清单，排除 `.env`、`.venv`、node_modules、缓存、Git 和测试产物。后端用 `uv sync --locked --no-dev --no-editable` 安装应用；运行层仅复制已安装环境和迁移。前端用 pnpm 11.25.0 的 frozen lockfile 构建，复制静态产物到 nginx。四个基础镜像均通过不可变 digest 固定。

后端默认 `CMD ["api"]`；传入 `worker` 复用同一镜像，入口通过 exec 保留停止信号。APP_ENV 必填，API 容器默认监听 0.0.0.0:8000；业务配置继续由环境注入，不提供默认账户。迁移由部署者显式执行，API/Worker 启动时不自动迁移。

Worker 保留现有 `local/test + Fake + localhost` 门禁。验收通过同一后端镜像启动临时 Python TCP 转发容器，把经过本机 Docker 标签/网络核对的 PostgreSQL/Temporal 映射到容器回环地址；API、Worker、前端共享该网络空间，前端仅发布宿主 127.0.0.1 随机端口。转发器是测试设施，不引入生产中间件。没有放宽生产 Worker 限制或接入真实系统。

前端以 UID 101 在 8080 端口运行，API_UPSTREAM 默认 `api:8000`，由部署环境设置内部 API 的单行 host:port。模板只替换这一变量；/api 原路径、Host、Origin 和 Cookie 传给后端。SSE 关闭代理缓冲/缓存，读取超时 650 秒；SPA 回退到 index.html，缺静态资源返回 404。前端 /health 检查静态服务，API /health 由 API 提供。

## CI

使用 GitHub Actions 文件 `.github/workflows/ci.yml`，目标仓库为 [sly20020806w/Weipai-Ai-Ops-Brain](https://github.com/sly20020806w/Weipai-Ai-Ops-Brain)，分支为 `main`。push、pull_request、手动触发均运行；token 仅有 contents:read，Action 固定提交 SHA，Checkout 不持久化凭证，不推送镜像或部署生产。

Ubuntu 24.04 runner 准备 Python 3.12、uv 0.12.23、Node 24、pnpm 11.25.0，按两个锁文件安装依赖，然后执行：

```bash
uv run --locked --directory backend python ../scripts/ci.py
```

ci.py 只允许本机 Docker，在任何创建前拒绝已有本项目容器/卷/网络；生成一次性数据库密码到进程环境，启动现有 compose 依赖，依次执行 scripts/check.py 和 scripts/check_images.py，最后清理本次 runner 依赖及卷。个人电脑已有项目环境时不要用这个 CI 专用入口，使用前述 PowerShell 命令。

Linux CI 使用固定官方 Gitleaks 8.30.1 / OSV-Scanner 2.6.0，下载和缓存均验证 SHA256；Gitleaks 只读取指定普通二进制文件，避免压缩包路径写入；损坏缓存或未验证平台直接失败。Windows 保留现有安装器。公共漏洞扫描只发送锁文件包名/版本，不上传源码或应用凭证。

远端验收进入仓库的 [Actions 页面](https://github.com/sly20020806w/Weipai-Ai-Ops-Brain/actions)，找到本次 `main` 提交对应的 `Checks and images`。确认 `check-and-build` 结果为成功，且 `Unified checks and image smoke checks` 日志末尾包含“统一检查全部通过”“镜像验收全部通过”“CI 统一检查与镜像验收全部通过”。该日志同时包含九项零跳过 Fake E2E、前端检查和实际镜像构建/启动检查；不能只依据 workflow 文件存在或依赖安装成功判定完成。

参考：[uv Docker](https://docs.astral.sh/uv/guides/integration/docker/)、[pnpm Docker](https://pnpm.io/docker)、[GitHub Actions 工作流语法](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)。

## 2026-10-09 本地验收记录

- 两个 Dockerfile 实际构建成功；同一后端镜像 api/worker、迁移、前端健康/SPA/鉴权/CSRF/退出、四项负向启动检查全部通过。脱敏报告：`.cache/images/e8287347436c435d83238bd8e38e9be0/report.json`。
- Windows PowerShell 5.1 的交互命令实际复跑，临时密码隐藏输入；Edge 直接访问前端镜像，登录 200、17 个入口、刷新、手机导航和无溢出、退出 204、后续身份 401、无浏览器错误/外部请求通过。按 Enter 后本次容器、Schedule 和临时库已全部清理，报告 `.cache/images/e6295c5568a54b4b8a5d3b9b25b6d4d5/report.json`；四个既有项目依赖仍 healthy。
- 最终 `check.ps1` 全通过：Connector 边界、ruff/格式/mypy（477 个检查源文件）、Git 规则、后端 2295 passed/590 skipped、强制 E2E 9 passed/零跳过、前端 OpenAPI/16 个文件一致、lint/typecheck、132 项测试/build。590 项既有数据库/Temporal 专项没有全量复跑。
- 123 项安全/安装器专项通过；Windows 扫描 0 密钥/0 漏洞，报告 `.cache/security/73b11485e1464d269804e256680464eb/`。Linux 官方工具实际校验和安装成功，对完整 741 文件源码快照与两个锁文件复扫：0 密钥、46 Python + 420 npm 包、0 漏洞，且没有读取错误。Linux 报告在 `.cache/linux-security/*-complete.json`。
- Actionlint 1.7.12 通过。CI 专用入口在已有个人项目容器时实际拒绝执行，compose 显式禁用 `.env` 自动读取的配置验证通过；没有运行远端 CI。
- 初次构建/扫描/E2E 同时运行出现本机内存不足，导致文件读取和基础设施超时；限定本次构建资源、分开执行后完整复跑通过。失败 E2E 临时库及两个精确识别的隔离 Workflow 已清理，没有改动其他项目容器。额外修复 Cookie 头大小写、未创建 Schedule 的清理判定；扫描器退出 0 但存在文件读取错误时现在强制失败。
- 本步没有新增应用依赖、迁移、生产请求或运维动作，应用库仍为 0016_catalog_audit。Step 57–58 未启动。
- plans.md 将 Step 56 记为进行中：**本地实现与自检完成，远端 CI 待验收**。目标仓库/CI 平台未提供前，不声明整个步骤已完成。
