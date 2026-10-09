# Step 16：飞书通知通道验收

本步交付平台向本人推送文本消息和交互卡片的 Connector、真实 HTTP 适配器与 Fake。调用来自平台宿主，飞书不注册为 Agent Tool。没有新增数据库迁移、生产运维动作、Workflow、审批或卡片回答处理；后续人工判断和审批分别按 Step 29、30 接入。

当前目录的计划文件实际为 `plans.md`。依据已完整阅读的 `AGENTS.md`、`SPEC.md` 与该计划，本次只完成 Step 16。工程约定引用的原始《Weipai AI Ops Brain 最终设计方案 V1.0.md》仍未提供，沿用既有 SPEC 与接口约定。

## 一键离线验收

在 PowerShell 执行，无需飞书账号、凭证、Docker 或数据库：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-feishu.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 16 飞书专项验收失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

专项预期 `62 passed`，随后显示 Fake 发件箱 JSON：

- `mode` 为 `fake`，`agent_tools` 为空。
- `sent_messages` 有两条：一条文本、一条 `interactive` 卡片；收件人均为 `ou_fake_owner`。
- 卡片标题为「payment-service 需要关注」，按钮为「查看任务」，按钮数据包含 `task_id=sample-task`。
- 每条通知有独立的 `notification_id`、Fake `message_id` 和带 `Z` 的 UTC 确认时间。
- 最后输出「Step 16 Fake 通知验收通过」。

统一检查预期：Connector 导入边界、ruff、格式、mypy、Git 环境文件检查通过，pytest 为 `1255 passed, 162 skipped`，最后输出「统一检查全部通过」。162 项既有 PostgreSQL 集成测试保留独立的 `check-db.ps1` 入口；本步没有数据库变更。前端仍是 Step 47 的预留目录，没有可运行的前端检查项目。

专项测试在事件循环初始化后拦截真实 HTTP transport、DNS 和 socket 连接；真实分支全部经 `MockTransport` 验证，没有向飞书发送消息。

## 单独运行样例

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
. .\scripts\project.ps1
$taskUv = Get-ProjectUv
& $taskUv run --offline --frozen --directory backend python -m app.connectors.feishu
if ($LASTEXITCODE -ne 0) { throw 'Fake 通知样例失败' }
```

入口显式构建 Fake，不读取宿主飞书凭证。卡片发送后会按通知 ID 读回并断言内容完整，再打印结果。卡片按钮在当前阶段仅携带交互数据，点击事件没有接入服务或改变任务状态。

## 接口与边界

代码位于 `backend/app/connectors/feishu/`：

- `FeishuConnector` 提供异步 `send()`、`send_text()`、`send_card()` 与标准关闭生命周期。真实和 Fake 实现同一接口。
- `TextNotification`、`InteractiveCard` 和 `CardButton` 校验声明字段，禁止空内容；卡片支持标题、Markdown 和 1–5 个带回调数据的按钮。文本最多 4096 字符，完整内容按 UTF-8 JSON 限制为 20 KiB。
- `NotificationReceipt` 表示源系统确认受理，包含通知 ID、消息 ID 和 UTC 时间；它不代表本人已读，也不代表任务已解决。
- Fake 的 `sent_messages` 和 `get_sent(notification_id)` 可读回完整发送内容。输入与读回结果均深复制；修改按钮的嵌套数据不改变历史快照。
- Fake 同一通知 ID 与内容重复发送只产生一条记录；同 ID 改内容会拒绝。真实请求携带该 ID 作为 `uuid`，重试时宿主应保留原 ID 与原内容，并遵守飞书的去重有效期。当前没有跨进程持久发件箱。
- 飞书身份使用专门的 `FeishuNotificationCredentials`，不复用 `ReaderCredentials` 或 `ExecutorCredentials`；此通道不能读取或修改生产运维资源。
- 已把全部 12 个既有高级 Tool 注册进测试用注册表，确认没有飞书、发送或通知 Tool；通过 Dispatcher 尝试调用四个通知名称全部返回 `tool_not_found` 并保留拒绝审计，没有发送消息或创建 Evidence。

## 真实适配协议与配置

真实适配器面向飞书企业自建应用机器人，固定使用官方 HTTPS 地址和两条 POST 路径：先以应用凭证请求短时 `tenant_access_token`，再以该 token 向配置中的本人 `open_id` 发送文本或卡片。消息的 `content` 是 JSON 字符串，接收类型固定为 `open_id`；调用参数不接受任意收件人、群聊或 URL。这些路径与请求字段已对照官方生成 SDK 并经 HTTP mock 验证。[消息请求](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/api/im/v1/model/create_message_request.py)、[消息字段](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/api/im/v1/model/create_message_request_body.py)、[鉴权请求](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/api/auth/v3/model/internal_tenant_access_token_request.py)。

`Settings` 只从环境变量读取：

| 环境变量 | 内容 |
| --- | --- |
| `APP_ENV` | `local` / `test` / `staging` / `production` |
| `CONNECTOR_MODE` | 缺省 `fake`；`local` / `test` 强制 `fake` |
| `FEISHU_CONFIG` | JSON：`recipient_open_id` 必填，`timeout_seconds` 缺省 15，范围 `(0, 120]` |
| `FEISHU_NOTIFICATION_CREDENTIALS` | JSON：通知专用自建应用的 `app_id`、`app_secret` |

非敏感目标配置示例：

```json
{"recipient_open_id":"ou_your_open_id","timeout_seconds":15}
```

真实工厂必须同时提供目标与专用通知凭证；即使 `Settings` 被赋值或 `model_copy` 修改，工厂也重新校验环境门禁。Fake 分支忽略真实目标和凭证，固定使用本地样例收件人。凭证用 `SecretStr` 隐藏，配置和密钥不落库或写入 `.env`。

适配器关闭环境代理与重定向，不输出源系统正文、消息或密钥。HTTP、业务错误、畸形鉴权与发送确认分别报出安全错误；没有有效消息 ID 时不会报告成功。每次发送按需获取 token，不自造续期调度或重试循环；超时等情况说明发送结果可能未知，恢复与重试以后交给宿主 Temporal Workflow。

真实账号尚未联调。将来接入前需在企业自建应用中启用机器人、配置发送权限，并确认本人的 open_id 和应用可用范围；文档入口见[飞书发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create)与[自建应用获取 token](https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal)。本次验收只使用 Fake/mock，不要求配置真实应用，也没有发送实际通知。

## 自检记录

已验证 Fake 文本与卡片完整读回、UTC 时间、重复发送去重、冲突拒绝、嵌套快照隔离、非法及过大内容发送前拒绝、环境门禁、凭证类型隔离、真实 HTTP 请求结构、错误脱敏、重定向拒绝、没有自动重试、关闭生命周期和 Tool 排除。自检过程中修复了测试的 Dispatcher 事务/拒绝状态用法、无效模型构造时机、类型标注和格式问题。

本次没有修改 Step 17 或后续步骤。
