# Step 48：任务类页面验收

本步实现 AI 任务中心、事故中心、事件中心和各自详情。查询复用 Step 44 已有 API，所有响应类型与调用都来自 OpenAPI 生成客户端。任务状态只展示后端结果；时间明确显示 UTC。审批、判断和接管的交互页面继续按 Step 49 实现。

## 一次跑完自动检查

需要项目已有 uv/Python 环境、Node.js 22.18+、pnpm 11。真实浏览器验收还需要 Edge、Docker Desktop，以及本项目已有 PostgreSQL 和 Temporal 容器运行。

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-task-pages.ps1
if ($LASTEXITCODE -ne 0) { throw '任务页面浏览器验收失败' }
```

`check.ps1` 执行后端 ruff/格式/mypy/pytest、Connector 边界、Git 环境检查，并执行前端生成一致性/lint/typecheck/test/build。单独检查前端可运行 `check-frontend.ps1`。

第二条命令先构建前端，再创建临时本机数据库、应用既有迁移、复用已实现的 Fake 事故 Workflow 准备数据。真实 API 和 preview 使用随机本机端口，Edge 实际查询这些持久化结果。组件测试的未声明网络请求直接报错；浏览器仅允许 `127.0.0.1`。测试过程不访问公司系统或网关。

## 自己在浏览器检查

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-task-pages.ps1 -Interactive
```

1. 按提示设置 12–256 字符临时密码，输入隐藏。脚本先跑自动浏览器检查。
2. 看到“浏览器地址”后打开打印的 URL，账户为 `local-browser-owner`，使用刚设置的密码登录。
3. AI 任务中心选择 **已关闭 + 告警**，应恰好看到“支付 5xx 发布故障复盘”。选择 **等待补充信息 + AI** 应看到四个改进任务；列表按 API 返回分页，不在当前页做假筛选。
4. 打开事故任务，应看到后端的 CLOSED 状态、状态时间线、Tool 调用、最近一次结论和证据链。点击结论中的 Evidence ID，抽屉应显示该 ID、所属任务、来源 Tool、UTC 采集/入库时间、参数、结果快照和源系统引用。点另一个引用应切换到对应证据。
5. 关闭证据抽屉，点击“刷新任务”，各部分应重新查询。复制带 `?evidence=…` 的详情地址并刷新，证据仍应打开；浏览器后退可回到此前页面。
6. 进入事故中心，按 `payment-service` 查询，打开复盘。应有设计第 22 节十三章、事故时间线、改进建议和任务链接；Evidence ID 均可查看。
7. 进入事件中心，选择告警来源并查询 `payment-service`，应有该事故的 Prometheus 事件。事件详情显示发生时间、外部 ID、去重指纹，关联任务链接进入同一事故任务。
8. 缩窄至手机宽度，应能打开导航和证据抽屉。表格可在自身区域内横向滚动，页面没有横向溢出。退出后打开详情链接应重新要求登录。
9. 完成后回到 PowerShell 按 Enter。脚本关闭 API/preview、删除临时数据库，显示“临时测试库已清理”。也支持 Ctrl+C 清理退出。

演示账户、密码、会话和数据都是临时的。密码只通过当前进程环境传给隔离测试，不保存到文件，不打印密码；不修改你的应用库或个人账户。运行中的演示窗口需保持打开。

## 页面与数据关系

| 页面 | 路由 | 查询/关系 |
| --- | --- | --- |
| AI 任务中心 | `/tasks` | status/source 联合筛选，每页 20 条 |
| 任务详情 | `/tasks/{task_id}` | 状态历史、Tool 调用、全部 Evidence、主 Agent 结论 |
| 事件中心 | `/events` | source/service_name 筛选 |
| 事件详情 | `/events/{event_id}` | OpsEvent 原有字段与关联任务 |
| 事故中心 | `/incidents` | service_name 筛选 |
| 事故复盘 | `/incidents/{incident_id}` | Incident ID 为 postmortem 的 Evidence ID |
| 证据抽屉 | `?evidence={evidence_id}` | `/api/evidence/{id}` 精确读回 |

筛选和页码保存在 URL，刷新、后退和登录返回详情均保留。17 个状态/8 个来源由生成类型检查中文映射是否完整；人工判断、补充信息、审批分别展示。任务详情按采集时间分批读取全部证据，避免第 101 条以后的结论遗漏；证据链本地分页展示，Tool 调用服务端分页。手动刷新和窗口重新获得焦点时更新查询，不增加业务调度器或状态机。

结论展示来自只追加的 `agent.conclusion` Evidence，历史结论保留采集时间；尚无结论明确显示空态。失败/拒绝的 Tool 调用可能没有 Evidence，页面保留实际审计结果，不补造证据。源系统文字只按普通文本呈现。404/422/503 显示中文错误并允许重试，证据切换时不残留上一条快照；401 使用既有会话机制隐藏控制台并清空业务缓存。

## 常见问题

- 找不到本地数据库：先启动 Docker Desktop 和项目已有依赖；不要为验收连接真实数据库。
- Temporal 无法连接：启动项目本机 Temporal；本步的样例数据复用既有事故 Workflow。默认使用 `127.0.0.1:7233`，辅助脚本会发现本项目容器的映射地址。
- Edge 不可用：安装本机 Edge；普通组件测试可先通过 `check-frontend.ps1` 执行。
- 应用库列表为空：可以正常显示空态。上方临时演示自动准备完整 Fake 数据，不需要向应用库手工写入事故。
- 想使用固定 5173 端口访问应用库：沿用 [前端外壳启动说明](frontend-shell.md) 的 API/前端两个窗口，访问 `/tasks`、`/incidents`、`/events`；没有数据时先用本页隔离演示验收。

布局截图保存在 `.cache/frontend-smoke/`，只含 Fake 运维数据，无密码/Cookie/令牌。此处只验收 Step 48 的页面查询，完整运维闭环 E2E 仍按 Step 54 实现。

## 2026-10-08 最终自检记录

- `check.ps1` 全部通过：Connector 边界、ruff/格式、mypy（466 个源文件）、Git 检查和后端 **2166 passed / 590 skipped**。590 项为既有数据库/Temporal 专项，本步未全量重跑；本步真实浏览器单独使用临时 PostgreSQL 与本机 Temporal。
- 前端 **44 passed**，其中新增任务页面 **14** 项；生成契约与 16 个客户端文件逐字节一致，lint、strict typecheck、build 全通过。
- Edge **2 passed**：外壳回归和任务页面真实 API 验收。使用既有 Fake Workflow 产生的真实 Task/Event/Evidence/Incident 身份，查询与证据读回无手工替换；浏览器运维写请求和外部请求均为 0。
- 实际通过 Windows PowerShell 5.1 的 `demo-task-pages.ps1 -Interactive` 验证隐藏密码输入、自动检查、打印可浏览 URL、等待 Enter 和清理；三个中文 PowerShell 检查/演示脚本 BOM 和语法通过。
- 最终交互按 Enter 后退出码 **0**，打印“临时测试库已清理”；本机 `weipai_db_test_%` 临时库数量 **0**，本次 preview 端口无监听。
- 桌面详情、证据、事故和手机截图已目视检查；手机加载全部数据后页面宽度为 390px，证据抽屉不超出视口，表格在自身区域滚动。构建最大分块约 311 kB。

自检修复了生成十三章 tuple 夹具、共享组件导出、类型、中文可访问标签、长导航测试超时、隐藏测量行计数、分块初始化顺序，以及手机 Grid 的最小列宽和抽屉宽度。Windows 交互退出曾因重定向 stdin 无法接收 Enter 而阻塞，已改用与隐藏密码相同的控制台读键通道；该次阻塞进程和临时库经 PID/父子关系及事故任务 ID 核对后清理，最后重新验证整个交互命令。本次没有修改后端业务、迁移或新增依赖；`plans.md` 仅把 Step 48 改为完成，Step 49 及后续仍是“还没做”。
