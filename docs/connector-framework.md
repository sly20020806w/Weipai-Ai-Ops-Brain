# Step 10：Connector 框架

依据完整阅读的 `AGENTS.md`、`SPEC.md` 和实际计划文件 `plans.md`，本次只实现第一个未完成项 Step 10。目录未提供两份规格引用的最终设计原文，沿用现有 SPEC 的明确约束。具体运维平台、Kubernetes、可观测性等适配器在 Step 11–16 实现。

## 自行运行验收

在项目根目录打开 PowerShell：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
.\check-connectors.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 10 专项验收失败' }
.\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

无需 Docker、数据库、公司凭证或外部 SDK。依赖复用已有锁文件和虚拟环境，专项命令使用 uv 的 `--offline --frozen`；没有新增依赖。

| 命令 | 预期 | 实际检查 |
| --- | --- | --- |
| `check-connectors.ps1` | `69 passed`，退出码 0 | 配置选型、生命周期、凭证分离、只读接口和违规导入 |
| `check.ps1` | 导入边界、ruff、格式、mypy、Git 环境检查通过；`663 passed, 153 skipped`；「统一检查全部通过」 | 新旧单元测试与工程门禁 |

153 项数据库集成测试明确跳过，仍由 `check-db.ps1` 在独立临时库执行。本次没有表、迁移、数据库写入、API 或前端变更。前端在 Step 47 建立。

专项命令逐项显示测试名，可以重点看：

- `test_default_fake_has_no_credentials_and_same_reader_interface`：四个环境默认选 Fake，返回离线结果 `fake`；无 Reader/Executor 凭证、无 `write` 或 `execute_action`，正常退出关闭资源。
- `test_real_configuration_selects_real_constructor_only`：staging/production 配置 real，得到真实分支构造替身，Fake 工厂不调用；传入的是指定 Connector 的 Reader 凭证。替身只返回 `real-constructor-stub`，不包含真实系统请求。
- `test_swapped_credential_types_cannot_cross_connector_boundary`：Reader 和 Executor 凭证混用会抛错。`test_read_write_identity_separation_and_masking` 还验证同 token 拒绝和凭证冻结/脱敏。
- `test_local_real_rejected_at_startup`：local/test 选 real，API 构建阶段拒绝配置。复制或修改 Settings 也不能绕过工厂校验。
- `test_unified_check_fails_for_injected_sdk_and_passes_after_removal`：在临时隔离项目的 tools/ 写入 `import kubernetes`，调用同一 `scripts/check.py`，非 0 退出并定位违规文件/行，尚未运行 ruff；移除后边界检查退出码 0。无需安装 Kubernetes SDK。

测试禁止实际 HTTP 客户端构建、HTTP transport、DNS 和 socket 连接。导入测试只解析源码，并调用本地子进程，不执行所注入的 SDK。验收临时目录固定在本项目 Git 忽略的 `.cache/pytest-connectors` 与 `.cache/pytest-check`；pytest 只管理各自的测试临时目录。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CONNECTOR_MODE` | `fake` | `fake` 或 `real`；local/test 只允许 fake |
| `CONNECTOR_READER_TOKENS` | 空对象 | JSON 对象，英文 Connector 名称映射到 Reader token；真实工厂要求对应项存在 |

名称为小写英文标识符，首位字母，后续字母/数字/下划线，最多 64 字符。空白 token、无效名称/模式会被配置校验拒绝。配置只来自进程环境变量/K8s Secret，不加载 `.env`，不写入数据库。Reader token 使用 `SecretStr`，配置 repr 不显示映射，JSON 序列化脱敏。

本地运行只需 `APP_ENV=local`，保持默认 `CONNECTOR_MODE=fake`；验收测试会隔离宿主配置。真实系统 URL、专有鉴权协议和业务方法由后续具体 Connector 定义，当前没有可连接公司系统的实现。

## 基类与工厂契约

`Connector` 是抽象异步生命周期接口，要求实现 `aclose()`，支持 `async with`，发生异常也关闭资源。没有通用 HTTP 请求、数据库查询或变更方法。

`ReadOnlyConnector` 只接受 `ReaderCredentials` 或 Fake 的空凭证，不接收 Executor 凭证；`WriteConnector` 只接受 `ExecutorCredentials`，是独立抽象基类。本阶段不提供任何写方法或写工厂，动作级授权、短时凭证签发、Policy/审批校验在 Step 30–32 接入。

读写凭证模型包含 Connector 名和脱敏 token，禁止额外字段，冻结且在只读/写基类入口重新校验。`validate_credential_separation(reader, executor)` 用于验证同一系统的两个身份不复用同一 token；它不授予执行权限。实际身份权限仍由外部系统的 Reader/Executor 授权策略决定，凭证类型本身不能证明源系统权限。

后续具体只读接口应继承 `ReadOnlyConnector` 并声明业务读取方法，让 Fake 与真实实现实现同一接口。宿主使用 `ConnectorFactory[该接口](name, fake=无参构造函数, real=接收 ReaderCredentials 的构造函数)`，调用 `create(settings)`。需要额外 URL 等配置时由宿主构造闭包绑定；不能由 Agent 传入身份、模式或任意 URL。

Fake 分支不调用真实工厂、不传真实凭证；真实分支找不到对应 Reader token 会明确失败，不回退 Fake。返回值必须是只读实例；Fake 不能携带真实凭证，真实实例必须绑定相应 Reader 凭证，写实例/混合写类型被拒绝。具体 Connector 仍须在构造时避免进行网络请求，并实现明确的只读接口。

高级 Tool 只能调用 Connector 的业务接口；Agent 仍只经 Step 9 的 Dispatcher 调用高级 Tool。框架未新增 Tool、调度、队列或重试，后续生命周期仍由 Temporal 驱动。

## 导入边界

统一检查首先运行 `python -m app.connectors.boundaries --root <项目根目录>`，扫描 `backend/app/**/*.py` 和 `backend/alembic/**/*.py`。用 AST 解析，不需要安装外部 SDK。tests/ 不在业务边界扫描范围，允许导入 mock 与测试工具。

规则如下：

- `app/connectors/` 可以引入外部 SDK/API 客户端；`connectors_extra/` 等相似目录没有例外。
- 其他业务模块只允许标准库和声明的基础框架依赖；Kubernetes、阿里云、Git、Jenkins、Prometheus SDK 与未来未知第三方 SDK 都拒绝。
- 标准库 `socket`、`http.client`、`urllib.request`、`xmlrpc.client` 也禁止在边界外引入。
- 已有公司 AI 网关 `app/agent/client.py`、`app/agent/fake.py` 仅允许 `httpx2`，不扩大到 agent/ 其他文件或其他 SDK。
- 检查函数内、条件分支、普通 import/from import，以及 `__import__`/`importlib.import_module` 常见别名和静态字符串调用；边界外无法静态确定的动态导入被拒绝。语法错误也会使检查失败。

新增基础框架依赖时需显式维护 `FRAMEWORK_MODULES`，外部系统 SDK 留在 Connector。此检查约束源码导入，不是任意 Python 反射的运行时沙箱；具体适配器的读写方法与身份权限仍需按工程契约审查。
