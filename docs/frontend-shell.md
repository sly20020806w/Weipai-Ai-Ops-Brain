# Step 47：前端外壳验收

本步建立前端工程、生成 API 客户端、登录保护、17 个中文入口和统一检查。任务、证据、审批、认知、运营、指标、审计、AI 对话的具体业务页面仍按 Step 48–53 实现。页面明确显示业务内容待开放，没有虚构任务、指标或生产状态。

## 自动检查

需要 Node.js 22.18+（推荐 24 LTS）、pnpm 11、uv 和项目已有 Python 环境。首次安装：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
pnpm --dir frontend install --frozen-lockfile
.\check-frontend.ps1
.\check.ps1
```

前端专项依次执行 `api:check`、`lint`、`typecheck`、`test`、`build`。根统一检查同时执行后端 ruff、格式、mypy、pytest、Connector 边界和 Git 环境文件检查。前端测试经 MSW 拦截本机 Fake HTTP，所有未声明请求报错；不触达真实运维系统。

确认自动生成可以重复执行：

```powershell
pnpm --dir frontend api:generate
pnpm --dir frontend api:check
git diff -- frontend/openapi.json frontend/src/api/generated
```

预期 `api:check` 打印 OpenAPI 契约和 16 个客户端文件逐字节一致，最后的 diff 为空。当前仓库已有文件均为未跟踪状态，因此 Git 空 diff 本身不能证明一致；`api:check` 会独立比较生成前后字节和文件集合，无需先提交或暂存。

## 在自己的浏览器中运行

先启动 Docker Desktop 与项目本机依赖，保持 API、前端两个窗口运行。本步只使用 API 与本机 PostgreSQL，不需要 Worker。

**窗口一：设置自己选择的账户与密码，启动 API。**

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
. .\use-local-db.ps1
. .\use-local-auth.ps1 -Username 'owner' -Origin 'http://127.0.0.1:5173'
. .\scripts\project.ps1
$projectUv = Get-ProjectUv
& $projectUv run --frozen --directory backend python -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw '数据库迁移失败' }
$env:API_HOST = '127.0.0.1'
$env:API_PORT = '8000'
.\run-api.ps1
```

设置脚本通过隐藏输入接收 12–256 字符密码，只写当前进程环境。关闭窗口后需重新设置；重跑会轮换配置，使之前会话失效。`Origin` 是浏览器前端地址，必须包含 `5173`。地址请统一使用 `127.0.0.1`，不要改用 `localhost`。

**窗口二：启动前端。**

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
pnpm --dir frontend install --frozen-lockfile
.\run-frontend.ps1
```

浏览器打开 [本机审批入口](http://127.0.0.1:5173/approvals)，按以下顺序检查：

1. 未登录应跳到 `/login`，显示“欢迎回来”，不显示控制台导航。
2. 输入错误密码应看到中文错误提示，密码框清空；输入 `owner` 和刚设置的正确密码，返回 `/approvals`。
3. 左侧应有下表的 17 个入口，逐个点击应更新 URL、标题和选中项；待开放页面属于本步的预期结果。
4. 按 F5 刷新，应保留登录和当前入口。
5. 缩窄浏览器至手机宽度，点击顶部“打开导航”，仍可访问全部入口，无横向滚动。
6. 点击右上“退出”，应返回登录；刷新或浏览器后退仍需登录。

| 入口 | 路由 |
| --- | --- |
| 总览 | `/dashboard` |
| AI 任务中心 | `/tasks` |
| 事故中心 | `/incidents` |
| 服务与上下文图 | `/services` |
| 事件中心 | `/events` |
| 发布中心 | `/releases` |
| 巡检中心 | `/inspections` |
| 工单中心 | `/tickets` |
| 运行手册中心 | `/runbooks` |
| 风险中心 | `/risks` |
| 重大保障 | `/war-room` |
| 架构评审 | `/architecture` |
| 自动化中心 | `/automation` |
| 审批中心 | `/approvals` |
| 知识中心 | `/knowledge` |
| 审计中心 | `/audit` |
| AI 对话 | `/chat` |

## 自动真实浏览器验收

安装 Edge，启动项目本机 PostgreSQL 后，在根目录执行：

```powershell
.\check-frontend.ps1
.\check-frontend-browser.ps1
```

脚本创建临时本机数据库并应用既有迁移，以随机端口运行真实 API 和生产构建 preview，在隔离的无界面 Edge 会话中登录、检查 Cookie、逐一访问 17 个入口、刷新、切换手机导航、退出和后退。临时密码只通过进程环境传递，没有请求体、Cookie 或令牌跟踪文件；HTTP 只允许 `127.0.0.1`。结束自动关闭临时服务并删除测试库，不修改你的账户或应用库。

预期显示 `1 passed` 和“Step 47 真实浏览器通过”。布局截图保存在 `.cache/frontend-smoke/`；截图不含密码或令牌。该验收仅验证本步的外壳和账户流程，完整运维闭环 E2E 仍按 Step 54 实现。

## 常见问题

- 登录 403：检查 `use-local-auth.ps1 -Origin` 是否确实为 `http://127.0.0.1:5173`，以及是否使用了相同浏览器地址。
- 无法验证会话或登录服务不可用：确认 API 和 PostgreSQL 已运行，API 继承了 `AUTH_CONFIG` 与 `DATABASE_URL`，端口为 8000。修复后点击“重新连接”。
- 端口占用：关闭占用 5173/8000 的本机服务，或显式配置配套 Origin、API_PORT 与 WEIPAI_API_TARGET；脚本不会默默改成另一个前端端口。
- 类型或契约过期：先运行 `pnpm --dir frontend api:generate`，再运行 `check-frontend.ps1`；不要手工编辑生成文件。
- 首次环境未安装：按根 README 同步后端依赖，再安装上述锁定前端依赖。运行真实浏览器验收需要本机 Edge，普通组件检查不需要 Docker 或浏览器。

## 2026-10-08 自检记录

- 根统一检查通过：后端 ruff、格式、mypy（466 个源文件）、Connector 边界、Git 环境检查及 pytest **2166 passed / 590 skipped**。590 项为统一入口原有的数据库/Temporal 集成测试，仍通过 `check-db.ps1` 等独立入口执行，本步未重跑全部集成回归。
- 前端 **30 passed**，覆盖生成 SDK 的登录/CSRF/同源约束、会话刷新/退出、失效与错误分支、17 个入口和安全返回路径；lint、strict typecheck、build 全通过，构建分块均小于 500 kB。
- 实际重跑 `api:generate`、`api:check` 后，契约与全部 16 个客户端文件逐字节一致；生成文件 Git diff 为空。无手写 API 类型。
- 真实 Edge 浏览器 **1 passed**：独立 PostgreSQL 应用既有全部迁移，生产 preview 经同源代理连接真实 API，验证登录 200、HttpOnly/SameSite Cookie、17 个入口、刷新、手机导航、退出 204、后退和身份查询 401。只调用账户接口，无运维动作或生产请求。
- 桌面、手机和登录截图目视核对，无遮挡、裁切或横向溢出；临时数据库、API 与 preview 已清理。新增中文 PowerShell 脚本 UTF-8 BOM 和语法通过。
- 自检修复了生成器版本兼容、pnpm 11 构建配置、网络失败分支、退出后旧缓存、登录按钮的可访问名称，以及过大的单一构建包。

本次只完成 Step 47；后续步骤状态保持“还没做”。
