# Step 7：AI 网关客户端

本步骤依据完整阅读的 `AGENTS.md`、`SPEC.md` 和 `plans.md` 实现，只交付公司 AI 网关适配与离线 Fake LLM。根目录实际计划文件名为 `plans.md`；现有文件引用的原始《最终设计方案 V1.0》未提供，按现有规格明确要求实施。

## 配置与连接

配置只来自进程环境变量或部署环境的 Secret，不读取 `.env`，不写入数据库。没有公共模型服务的默认 URL 或模型名。

| 环境变量 | 默认值 / 要求 |
| --- | --- |
| `APP_ENV` | 原有必填项：`local/test/staging/production` |
| `LLM_MODE` | 默认 `fake`；使用公司网关时设为 `gateway` |
| `AI_GATEWAY_BASE_URL` | gateway 模式必填，包含公司网关实际协议前缀，例如 `https://gateway.example.invalid/company/v1`；客户端追加 `chat/completions` 或 `embeddings`，不自行追加 `/v1` |
| `AI_GATEWAY_API_KEY` | gateway 模式必填，非空、不含空白，以 `Authorization: Bearer ...` 发送；配置展示隐藏密钥 |
| `AI_GATEWAY_CHAT_MODEL` | gateway 模式必填，使用公司网关提供的模型名 |
| `AI_GATEWAY_EMBEDDING_MODEL` | gateway 模式必填，使用公司网关提供的向量模型名 |
| `AI_GATEWAY_TIMEOUT_SECONDS` | 默认 30，每次尝试的连接/读取/写入/连接池超时，范围 `(0, 300]` 秒 |
| `AI_GATEWAY_MAX_RETRIES` | 默认 2，范围 0–5；是额外重试次数，总尝试次数不超过 `1 + max_retries` |
| `AI_GATEWAY_RETRY_DELAY_SECONDS` | 默认 0.5，范围 0–30 秒；超时后的退避为该值乘以 `2^attempt`，每次最多 30 秒 |

`local/test` 环境只允许 Fake 或显式注入 `httpx2.MockTransport` 的协议测试，即使配置真实地址也不会建立真实网关连接。`staging/production` 的实际网关客户端也必须显式选择 gateway 模式并提供全部四项配置。本次验收全程只用 mock/Fake。

关闭 HTTP 重定向和隐式环境代理，保留 TLS 证书验证。错误只报告超时、连接失败、HTTP 状态码或协议问题，不回显请求、密钥或远端错误正文。

## 服务契约

- `app/agent/models.py`：chat 消息、Tool 定义/调用、chat 请求/响应、embeddings 请求/响应和 Token 用量。文本消息支持 system、developer、user、assistant、tool。
- `app/agent/client.py`：异步 `GatewayClient` 与公共 `LLMClient` 协议，提供 `chat()`、`embeddings()`、`aclose()`；网关客户端也支持 `async with`。
- `app/agent/fake.py`：`FakeLLM`、`ChatStep`、`EmbeddingStep` 与配置工厂 `create_llm_client()`。脚本按全局顺序校验完整请求，可返回正常响应或 `GatewayError`，不匹配/耗尽/关闭后明确失败；匹配成功的调用可从 `calls` 读取。

chat 使用非流式 `POST chat/completions`。Tool 调用保留调用 ID、函数名和原始 JSON 参数字符串，`function.parsed_arguments` 提供解析后的 JSON 对象；无效 JSON、非对象参数、非有限数值和重复调用 ID 会被拒绝。Tool 结果以 `role=tool`、`tool_call_id` 回传。客户端只解析模型建议，具体执行由后续计划中的 Dispatcher 负责。

embeddings 使用 `POST embeddings`，显式指定 `encoding_format=float`，接受一批非空文本。响应按 `index` 还原输入顺序，拒绝缺失、重复、越界下标，以及空向量、不同维度、非有限数值。返回向量与输入一一对应。

只对 HTTP 超时进行有限重试；HTTP 错误、普通连接错误和协议错误不重试，异步取消正常向上传递。此处是 Step 7 要求的单次 HTTP 调用重试；AI Task、审批、任务重试与调度按后续计划接入 Temporal。本步骤不添加任务流程、Policy、Tool 执行、API 或页面。

## 可运行的离线 Fake 示例

在仓库根目录的 PowerShell 中执行以下完整代码，不需要任何网关配置：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
. .\scripts\project.ps1
$uvPath = Get-ProjectUv
@'
import asyncio
from app.agent.fake import ChatStep, EmbeddingStep, create_llm_client
from app.agent.models import ChatMessage, ChatRequest, ChatResponse, EmbeddingRequest, EmbeddingResponse
from app.config import Settings

async def main():
    question = ChatRequest(messages=(ChatMessage(role="user", content="检查支付服务"),))
    answer = ChatResponse(
        id="fake-chat-1", model="fake-chat", finish_reason="stop",
        message=ChatMessage(role="assistant", content="Fake：支付服务状态正常"),
    )
    embedding = EmbeddingRequest(inputs=("支付业务规则",))
    client = create_llm_client(
        Settings(APP_ENV="local", LLM_MODE="fake"),
        fake_steps=(
            ChatStep(question, answer),
            EmbeddingStep(embedding, EmbeddingResponse(model="fake-embedding", vectors=((0.1, 0.2),))),
        ),
    )
    try:
        print((await client.chat(question)).message.content)
        print((await client.embeddings(embedding)).vectors)
    finally:
        await client.aclose()

asyncio.run(main())
'@ | & $uvPath run --offline --frozen --directory backend python -
if ($LASTEXITCODE -ne 0) { throw 'Fake LLM 示例失败' }
```

预期输出：

```text
Fake：支付服务状态正常
((0.1, 0.2),)
```

## 自行验收

当前机器的虚拟环境已更新，直接执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-llm.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 7 专项验收失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

专项验收逐项显示网关请求地址/模型/鉴权/超时、多个 tool_calls 与 Tool 结果往返、embeddings 排序、超时成功与耗尽、取消、错误脱敏、无效协议拒绝、配置读取及 Fake 脚本执行。测试在异步事件循环初始化之后阻止 DNS、socket 连接和真实 HTTP transport；任何绕过 mock 的网络尝试都会使验收失败。Fake 测试额外阻止构建 HTTP 客户端。

其他机器首次安装时先执行 `uv sync --directory backend --frozen --python 3.12`；安装依赖完成后这两个验收入口不需要公司凭证、Docker、数据库或 Temporal。前端仍按 Step 47 建立工程。本步骤没有数据库变更，数据库回归仍使用原有 `check-db.ps1`。

## 自检记录（2026-10-06）

- `check-llm.ps1`：63 项 Step 7 专项测试通过，真实网关 HTTP/DNS/socket 尝试为 0；PowerShell 语法检查通过。
- `check.ps1`：ruff、格式、mypy（54 个源文件）与 Git 环境文件检查全部通过；pytest 为 `446 passed, 146 skipped`。
- `check-db.ps1`：原有 146 项本地 PostgreSQL 集成测试全部通过，临时测试库已清理，公共配置变更没有破坏任务、Ledger、审计和图存储。
- 本文完整 Fake 示例实际运行成功，输出预期中文回答和 `((0.1, 0.2),)`。
- 修复首轮自检中 Windows 异步事件循环初始化被网络拦截的问题、测试环境覆盖问题，以及严格 JSON 类型解析拒绝多 Tool 数组的问题；修复后全部检查通过。
- 只将 `plans.md` 的 Step 7 标记完成，未进入 Step 8。前端按 Step 47 建立工程，当前无前端检查命令。

## 协议参考

chat 请求与响应字段依据 [OpenAI 官方 Chat Completions 协议](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)，函数调用与结果消息依据 [官方 Function calling 说明](https://developers.openai.com/api/docs/guides/function-calling)，向量下标和数值编码依据 [官方 Embeddings 协议](https://developers.openai.com/api/reference/resources/embeddings/methods/create)。实现仅将这套兼容协议用于显式配置的公司 AI 网关。
