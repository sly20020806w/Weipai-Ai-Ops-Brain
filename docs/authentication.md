# Step 43：单用户登录与会话鉴权

本步只实现登录、身份查询、退出、整个 `/api` 命名空间的会话门禁，以及人工身份与既有审计契约的衔接。任务查询、审批 HTTP 操作、判断回答、补充信息和接管 HTTP 操作仍按 Step 44 实现；本步不增加这些业务接口，也不改变 Temporal 生命周期、Policy 或生产执行权限。

## 行为

| 请求 | 条件 | 结果 |
| --- | --- | --- |
| `POST /api/auth/login` | 正确账户密码、`X-Ops-Login: 1`、允许的来源 | 200，设置会话 Cookie，返回操作人、UTC 到期时间和 CSRF token |
| `GET /api/auth/me` | 有效会话 | 200，同上；可以在刷新页面后重新取得 CSRF token |
| `POST /api/auth/logout` | 有效会话、`X-CSRF-Token`、允许的来源 | 204，数据库撤销会话并清除 Cookie |
| 任意 `/api` 或 `/api/...` | 未登录、伪造、撤销或过期 | 401；不存在的业务路径同样先经过鉴权 |
| 任意 API 写请求 | 已登录但 CSRF 缺失或来源不符 | 403 |
| `POST /webhooks/{origin}` | 既有签名校验 | 保持原行为；登录 Cookie 不能替代 Webhook 签名 |
| `GET /health` | 无需登录 | 200，用于健康检查 |

会话默认有效 1 小时，服务器以 UTC 时间判断到期；API 重启、多个 API 实例共用 PostgreSQL 后仍可使用。退出后，即使重新发送先前保存的 Cookie 也返回 401。账户、密码摘要、签名密钥或公开 Origin 轮换使旧 Cookie 立即失效。

数据库 `auth_sessions` 只保存公开会话 UUID、操作人、创建/更新时间、到期时间、撤销时间；仅凭库内 UUID 无法生成可用 Cookie。`auth_login_guard` 保存失败次数、时间窗和锁定期限，默认一个 60 秒窗口内连续 5 次错误后锁定 60 秒；错误用户名和错误密码返回同样的 401，锁定时返回 429 和 Retry-After。行锁保证跨实例并发不绕过限速，没有内存队列或自建调度器。

Cookie 采用 HttpOnly、SameSite=Strict、Path=/，HTTPS 时使用 `__Host-ops_session`、Secure 且不设置 Domain；本机 HTTP 使用 `ops_session`。写请求额外校验会话绑定的 CSRF token，登录使用自定义请求头与来源校验阻止跨站表单登录。响应为 `Cache-Control: no-store`，登录 422 错误删除原始输入，防止错误响应带回密码。

密码使用 Python 标准库 PBKDF2-HMAC-SHA256、随机盐与 600000 次迭代；会话和 CSRF 使用独立用途的 HMAC-SHA256。依据：[OWASP 密码存储](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)、[会话管理](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)、[CSRF 防护](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)。没有新增依赖或中间件。

## 配置

`AUTH_CONFIG` 是单个 JSON 环境变量，字段为：

| 字段 | 要求 |
| --- | --- |
| `username` | 唯一账户名，同时作为审计操作人；1–200 字符，无首尾空白或控制字符 |
| `password_hash` | 由本项目辅助脚本生成的 PBKDF2 摘要，无明文密码 |
| `session_secret` | 至少 32 字符的独立随机密钥，推荐随机 32 字节 |
| `public_origin` | 浏览器实际访问的 HTTPS Origin；本地允许回环 HTTP，无路径 |
| `session_ttl_seconds` | 可选，默认 3600，范围 60–86400 |
| `max_login_failures` | 可选，默认 5，范围 1–10 |
| `login_lock_seconds` | 可选，默认 60，范围 10–900 |

配置仅从环境变量或后续 K8s Secret 注入，不写配置文件、不入库、不提供默认密码。`staging/production` API 缺少 AUTH_CONFIG、DATABASE_URL 或使用 HTTP Origin 时拒绝启动。Worker 和 Alembic 不需要登录配置。本地未配置账户时 `/health` 和既有签名 Webhook 可继续使用，`/api` 保持关闭，登录返回 503；没有关闭鉴权的开关。

生产 HTTPS 和可信代理配置由后续部署步骤完成；此步不部署、不连接真实系统。

## 一键复验

在 Windows PowerShell 中运行，先启动 Docker Desktop 和项目已有本地依赖容器：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-auth.ps1
.\demo-auth.ps1
.\check.ps1
```

`check-auth.ps1` 自动创建专用本机临时数据库，执行离线配置/门禁测试和真实 PostgreSQL 会话、并发锁定、退出撤销、审批/判断审计测试，结束后删除临时库。`demo-auth.ps1` 自动启动临时回环 API、执行真实 HTTP 请求并清理进程和数据库，预期输出：

```text
本机 HTTP：未登录 401 → 登录 200 → 查询身份 200 → 缺 CSRF 403 → 退出 204 → 会话失效 401
Step 43 登录演示通过；无生产调用、无运维动作。
```

完整数据库/Temporal 回归可用：

```powershell
. .\use-local-temporal.ps1
.\check-db.ps1
```

## 用自己的账户手动验证

在第一个 PowerShell 窗口设置当前进程配置、升级本机应用库并启动 API：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
. .\use-local-db.ps1
. .\use-local-auth.ps1 -Username 'owner'
. .\scripts\project.ps1
$projectUv = Get-ProjectUv
& $projectUv run --frozen --directory backend python -m alembic upgrade head
.\run-api.ps1
```

辅助脚本提示设置 12–256 字符密码，生成摘要和独立随机密钥，只捕获到当前进程 AUTH_CONFIG。重跑辅助脚本会轮换配置并使旧会话失效。账户配置不写磁盘；关闭窗口后需重新设置。

在第二个 PowerShell 窗口执行以下命令，登录密码通过隐藏输入取得，不进入命令历史：

```powershell
$baseUrl = 'http://127.0.0.1:8000'
try { Invoke-WebRequest "$baseUrl/api/auth/me" -UseBasicParsing } catch {
    [int]$_.Exception.Response.StatusCode  # 预期 401
}
$loginPassword = Read-Host '输入刚才设置的登录密码' -AsSecureString
$passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($loginPassword)
try {
    $loginBody = @{
        username = 'owner'
        password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
    } | ConvertTo-Json
    $loggedIn = Invoke-RestMethod "$baseUrl/api/auth/login" -Method Post `
        -ContentType 'application/json' -Body $loginBody `
        -Headers @{ 'X-Ops-Login' = '1' } -SessionVariable opsSession
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
    $loginPassword.Dispose()
    Remove-Variable loginBody -ErrorAction SilentlyContinue
}
(Invoke-WebRequest "$baseUrl/api/auth/me" -WebSession $opsSession -UseBasicParsing).StatusCode # 200
$loggedIn.actor # owner
$loggedIn.expires_at # UTC 到期时间
(Invoke-WebRequest "$baseUrl/api/auth/logout" -Method Post -WebSession $opsSession `
    -Headers @{ 'X-CSRF-Token' = $loggedIn.csrf_token } -UseBasicParsing).StatusCode # 204
try { Invoke-WebRequest "$baseUrl/api/auth/me" -WebSession $opsSession -UseBasicParsing } catch {
    [int]$_.Exception.Response.StatusCode  # 401
}
```

## 人工操作审计衔接

API `require_principal` 只读取 middleware 验证的会话身份，不信任请求体、请求头或查询参数提供的 actor。`Principal.approval_response` 和 `Principal.human_answer` 将该身份填入既有 Temporal 数据契约，审批/判断的原服务仍负责动作哈希、版本绑定和只追加审计，审计带真实操作人与 UTC 时间。

专项测试把实际登录身份送入既有 ApprovalStore/HumanInteractionStore，检查落库的审计操作人和时区。Step 44 的接管适配器同样须从 `require_principal.actor` 取操作人并调用既有 tasks/ledger 服务；本步未提前开放接管接口。前端尚待 Step 47 建立，本步没有前端源码或 lint/typecheck/test 命令。

迁移为 `0015_single_user_auth`；降级删除会话和限速表，使全部会话失效，已有运维证据与审计保持在原 Ledger 中。

## 2026-10-08 最终自检记录

- 鉴权专项 **56 passed**：42 项禁止真实网络的配置/密码/门禁/协议测试，14 项本机 PostgreSQL 会话/并发/身份审计测试，无跳过。
- 统一检查 **2023 passed、544 skipped**；ruff、格式、mypy（436 个源文件）、Connector 边界与 Git 环境文件检查全部通过。数据库类测试通过下述独立入口执行。
- 完整数据库/Temporal 回归 **531 passed、3 skipped**；跳过的是既有定时触发时间跳跃专项，本步鉴权没有跳过场景。
- 完整空库迁移升降级与 Alembic metadata 检查通过；本机应用库已升级到 `0015_single_user_auth (head)`。
- Windows PowerShell 交付命令实际复跑，真实 HTTP 演示符合上述六个状态码；账户辅助脚本用进程内随机密码验证配置可读回，没有输出或落盘凭证；新脚本语法及 UTF-8 BOM 通过。
- 临时数据库及 HTTP 演示进程已清理，全部运行验收为本机 Fake，没有生产调用或真实飞书消息。
- 自检发现并修复了迁移 head 断言、测试 Fake 图准备、Windows 事件循环、错误响应脱敏、FastAPI OpenAPI 缓存/类型兼容和 Origin 规范化问题。

本机 Docker 的不可访问 socket 故障按既有项目恢复方式处理：停止故障实例，保留并重建 `Docker/run` 与 `docker-secrets-engine` 两处临时端点目录。副本后缀为 `.recovery-step43-20261008130149`；镜像、容器数据和虚拟磁盘保留，四个项目依赖已恢复 healthy。
