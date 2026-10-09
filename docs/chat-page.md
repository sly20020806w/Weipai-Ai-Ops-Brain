# Step 53：AI Chat 页面

本次完整阅读 AGENTS.md、SPEC.md、实际开发计划 plans.md 及权威长版设计，仅实施 Step 53。
页面复用 Step 46 的主 Agent SSE 与持久化回答接口，以及现有证据抽屉、任务中心和审批中心。

## 自己跑一遍

先启动 Docker Desktop；已有本机依赖可从根目录 PowerShell 恢复：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
. .\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw '本机依赖启动失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-chat-pages.ps1 -Interactive
if ($LASTEXITCODE -ne 0) { throw '聊天页面验收失败' }
```

演示会提示设置 **12–256 字符的隐藏临时密码**，不显示或保存密码。
随后自动创建临时 PostgreSQL 数据库、API、生产构建 preview 和隔离 Temporal Worker，
自动运行真实 Edge 验收。成功后打印可浏览地址与账户 `local-browser-owner`。
所有运维来源和模型均为 Fake，无需公司网关或生产凭证。

在浏览器中亲自检查：

1. 打开打印的地址并登录。服务为 `payment-service`，保留地址中的固定 UTC
   时间窗 `2026-10-01T01:00:00Z` 到 `2026-10-01T02:00:00Z`。
2. 在“只读问答”输入“payment-service 为什么出现 5xx？请引用证据。”
   点击“发送问题”，调查期间显示核验状态，之后逐段显示已核验的回答。
3. 点击回答中的任一 Evidence ID，查看来源 Tool、任务归属、UTC 时间、参数与原始快照。
   点击所属任务或“查看任务”，应进入真实 Human 任务详情；任务中心筛选来源“人工”也能找到。
4. 返回对话并刷新，显示“已保存回答”。输入“哪些证据支持这个判断？”，保留“基于上一轮追问”，
   新轮使用不同任务 ID 与本轮重新采集的 Evidence。
5. 选择“发起处置任务”，输入“请调查并回滚 payment-service v2.3.7 到 v2.3.6”。
   显示等待审批与“策略判定：需要审批”；动作计划证据中的回滚风险为 L3。
   可进入人工处理页面查看计划；本步自动验收不批准动作，审批前运维执行为 0。
6. 可在新问题调查期间点击“停止接收”。页面保留已接纳任务，使用“读取已保存回答”手动恢复，
   或“按原内容重试”；原内容重试复用完整输入及原 request_id，不创建第二个任务。
7. 用手机宽度检查表单、回答与证据抽屉没有横向溢出。检查完成回终端按 Enter，
   临时服务、运行中的隔离 Workflow 和测试库自动清理，末尾显示“临时测试库已清理”。

省略 `-Interactive` 只执行自动验收并退出，不等待浏览器操作。
Fake 模型采用固定支付故障脚本，自填问题不代表真实公司网关已经完成开放语义联调。
原有手动常驻 API/前端运行方式可继续使用，见 frontend-shell.md。

## 页面行为与边界

- 请求类型使用后端 OpenAPI 生成的 ChatInput、ChatAnswer、EventReceipt；
  SSE 客户端也复用生成实现，没有手写 API 类型或修改生成文件。
- 只读问答和处置任务独立选择。每轮提交归一为 OpsEvent/manual 和 Human AI Task，
  经既有 Runbook、主 Agent、Dispatcher、Reviewer、Policy、审批与独立 Verifier；
  页面只发起聊天请求和查询，不产生授权信号。
- 模型调查过程中只显示进度。生产结论经服务端证据核验后才按 delta 段发送；
  收到 done 使用完整持久化回答替换片段，不能凭断流或部分回答推断任务成功。
- Evidence 引用按本轮服务端提供的真实引用列表生成链接，原文按 React 文本展示，
  不解释源 HTML，不执行链接或脚本。
- SSE 不自动重发。手动重试清空该轮旧片段，保留原 UUID、消息、模式、服务、
  时间窗和上一轮任务。输入框后续修改不会改变原请求；409/422 提示核对后发新问题。
- 停止接收、离开页面和断网仅停止浏览器连接，任务由 Temporal 继续运行。
  回答恢复只读持久化接口，无前端任务轮询或调度器。
- URL 保留当前 task_id、服务与可选时间窗，支持刷新和登录后返回；完整本地多轮界面只保留
  在当前页面内存。刷新读取当前轮持久化回答，原提问可从任务详情的输入证据查询。
- Cookie 与 CSRF 沿用现有同源认证。生成 SSE 不调用普通响应拦截器，因此包装层明确
  处理 401，撤销内存身份/CSRF并清空业务缓存。
- 时间窗可留空使用接纳时刻前一小时；显式时间必须同时填写、带时区、结束晚于开始，
  最长 24 小时，统一转换为 UTC。
- 没有新的数据库迁移、依赖、中间件或后端业务接口；head 保持 0016_catalog_audit。

## 自检记录

2026-10-09 自检完成：

- 统一检查通过：Connector 边界、ruff/格式、mypy（468 个源文件）、Git 环境文件检查，
  后端 **2166 passed / 590 skipped**。590 项既有数据库/Temporal 专项保留独立入口，
  本次未全量复跑；本步真实数据库/Temporal 场景由浏览器演示独立验收。
- 最终前端 OpenAPI 与 16 个生成文件逐字节一致，lint/typecheck、
  **132 项组件测试**（本步新增 19 项）和生产构建通过。
- 最终 Windows PowerShell 5.1 交互命令实际复跑：隐藏密码、打印可浏览地址、
  等待 Enter 与退出清理通过；真实 PostgreSQL/API/Temporal/Edge **3 passed**，
  包括问答/追问、真实 SSE 多段、Evidence 读回、刷新恢复、Human 列表、
  L3/need_approval/WAITING_APPROVAL、审批前 execute_action 为 0 和手机布局。
- 浏览器业务写请求只有三次聊天提交，没有审批/执行写请求；外部请求 0。
  桌面、证据抽屉与手机截图已目视核对；临时测试库 0、演示 preview 端口关闭、
  本项目四个依赖仍 healthy；UTF-8 BOM 和 PowerShell 语法通过。
- 自检修复表单/下拉框精确可访问名称、测试夹具类型、UTC 窗口 URL 同步与相关缓存刷新；
  浏览器流观察改用页面内真实 Fetch，消除 DevTools SSE 响应体缓存的不稳定性。
  演示 Worker 配置隔离个人规则，保持默认审批门禁。
- 没有新依赖、迁移、后端业务修改、生产请求或真实飞书发送；
  仅完成 Step 53，Step 54 及之后的步骤保持未完成。
