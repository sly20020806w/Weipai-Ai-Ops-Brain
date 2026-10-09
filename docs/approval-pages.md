# Step 49：审批中心页面验收

本步实现审批、人工判断、补充信息和人工接管页面，复用 Step 44 的接口、Cookie 会话、CSRF 和 Temporal 信号。审批、判断、信息分别按 `WAITING_APPROVAL`、`NEED_HUMAN_JUDGMENT`、`WAITING_INFORMATION` 查询独立列表，分页与分类保存在 URL。

## 一次跑完

前提：本项目已有 uv/Python、Node.js 22.18+、pnpm 11、Edge、Docker Desktop，以及运行中的本机 PostgreSQL/Temporal 依赖。

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-approval-pages.ps1
if ($LASTEXITCODE -ne 0) { throw '审批页面联调失败' }
```

第一条包含后端 ruff/格式/mypy/pytest、Connector 边界、Git 检查和前端 OpenAPI 生成一致性/lint/typecheck/test/build。前端单独检查使用 `check-frontend.ps1`。既有数据库/Temporal 专项仍使用各自检查入口，统一检查中的跳过不表示这些专项已运行。

第二条创建本机临时库、应用既有迁移、运行隔离 Fake Worker、启动真实 API/preview，使用 Edge 验证人工操作。它同时回归既有登录外壳和任务页面。验收结束自动关闭服务、终止样例等待 Workflow 并清理临时库，不改应用数据库与个人账户。

验收预期：浏览器 **3 passed**，打印“真实 Workflow 验证通过”和“临时测试库已清理”。批准请求携带原审批 ID、等待版本和完整动作哈希，后台收到信号后进入真实 `EXECUTING`；拒绝与接管为 `ESCALATED`，两类回答继续到 `CLOSED`。接管 Workflow 实际取消，所有操作留有本人审计与 UTC 时间。

浏览器还会故意篡改动作哈希并确认返回 409；Temporal 日志中的该次 `ConsoleConflict` 是预期拒绝，以末尾测试结果和清理结果检查是否通过。

## 自己在浏览器跑一遍

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-approval-pages.ps1 -Interactive
```

1. 输入 12–256 字符临时密码，输入隐藏。脚本先跑自动检查，再额外创建一组供你亲自处理的等待任务。
2. 打开打印的“浏览器地址”。账户为 `local-browser-owner`，密码为刚设置的值；保持 PowerShell 窗口打开。
3. 在“待审批”列表只应看到等待授权的任务；“待人工判断”应显示业务取舍问题，“待补充信息”显示信息缺失任务。切换后刷新页面，分类仍保留。
4. 打开脚本打印的“批准样例”链接。检查目标 `payment-service`、回滚版本 `v2.3.7 → v2.3.6`、L3、Policy、执行前提、回滚方案、独立验证；点击 Evidence ID 可读回原证据。展开“查看审批绑定信息”能看到审批 ID、等待版本、动作哈希与计划引用。
5. 点击“批准动作”，在核对区域确认任务与绑定信息，再点击“确认批准动作”。应看到操作证据回执，点击“刷新状态”后任务为“执行中”。这一步使用既有 Workflow 的授权交接分支，**演示没有启用 Executor，运维执行次数为 0**；不把执行中或 HTTP 202 显示为任务成功。
6. 打开“拒绝样例”，点击“拒绝动作”并确认。刷新后应为“已转人工”，没有运维执行。
7. 打开“人工判断”样例，用自己的话回答业务优先级，点击“提交判断”并核对确认。回答使用判断接口，生成证据和 Knowledge 草稿，恢复原阶段。这里没有批准按钮；刷新后可以查看新状态及任务证据链。
8. 打开“补充信息”样例，填入实际信息并确认“提交信息”。空白内容不能提交，恢复信号与审批分离。
9. 打开“接管”样例，输入原因后点击“接管任务”并确认。应看到接管证据回执与“已转人工”，后台取消该 Workflow 并持久化阻止后续自动化。
10. 缩窄到手机宽度，列表在自身区域滚动，详情、输入框、确认区域与证据抽屉不应撑宽页面。退出后访问详情链接应要求登录；登录后回到原链接。
11. 检查完成后返回 PowerShell 按 Enter，确认末尾打印“临时测试库已清理”。也支持 Ctrl+C 退出清理。

## 操作与失败处理

| 页面操作 | 已有接口 | 身份绑定 |
| --- | --- | --- |
| 查询待处理详情 | `GET /api/tasks/{id}/interaction` | 当前任务、状态与版本 |
| 批准/拒绝 | `POST /api/tasks/{id}/approval` | 审批 ID、wait_version、action_hash、decision |
| 判断回答 | `POST /api/tasks/{id}/judgment` | question_id、wait_version、answer |
| 补充信息 | `POST /api/tasks/{id}/information` | question_id、wait_version、answer |
| 接管 | `POST /api/tasks/{id}/takeover` | expected_version、reason |

操作人由后端已验证会话派生，页面不传 actor、权限或风险配置。输入与操作只存当前内存，不写 localStorage/sessionStorage。新问题身份到达时不沿用旧问题未提交的回答。

页面不乐观修改任务状态，不新增轮询器、状态机或调度器。操作接纳后刷新任务、待处理投影、历史和证据缓存；若信号后的状态尚未提交，可手动“刷新状态”。已成功提交的旧等待版本不能立即改投相反决定。

409 表示等待版本、动作哈希或决定冲突，须“刷新并重新核对”；404/422/403 显示中文错误。503 或断网表示结果未确认，不自动重新发送，也不清空原操作；“以相同内容重试”固定原内容，后端持久化去重。提交期间按钮加锁，连续点击只发送一次。401 隐藏控制台并清空业务缓存。

状态与审批单提交之间可能暂时没有待处理记录。记录尚未生成、查询失败、身份或状态版本不一致时不显示授权按钮；可刷新等待已有后端流程提交。旧占位等待恢复只使用后端提供的 recovery 与 question_id。

## 范围

只完成 Step 49。没有新后端业务接口、迁移、依赖、中间件、生产请求或真实飞书发送；Step 50 及后续保持未完成。原始 API 类型和客户端仍从后端 OpenAPI 生成。浏览器操作只针对本机临时库中的 Fake 任务，截图位于 `.cache/frontend-smoke/`，不包含密码、Cookie 或令牌。

## 2026-10-08 自检记录

- `check.ps1` 全部通过：Connector 边界、ruff/格式、mypy（467 个源文件）、Git 检查，后端 **2166 passed / 590 skipped**。590 项为既有数据库/Temporal 专项，本步未全量重跑。
- 前端 **64 passed**，其中新增本步组件场景 **20** 项；OpenAPI 契约与 16 个生成客户端文件逐字节一致，lint、strict typecheck、生产 build 通过。
- Edge **3 passed**，实际使用本机临时 PostgreSQL、API、Temporal 与 Fake；验证精确动作哈希、状态分列表、本人证据、重投同回执、篡改 409、两类回答与接管。后台另验证五类最终状态、审计唯一性、会话操作人、时间与 Workflow 取消；新样例运维执行 0、浏览器外部请求 0。
- Windows PowerShell 5.1 的 `demo-approval-pages.ps1 -Interactive` 实际复跑：隐藏密码、自动检查、打印地址与五个新样例、等待 Enter；等待期间再次用真实浏览器回答判断，202 后 Worker 继续到 CLOSED。
- 退出码 0，临时测试库数量 0，本次隔离任务队列运行中的 Workflow 为 0，preview 端口关闭。新增 PowerShell UTF-8 BOM/语法与 Git 空白检查通过；桌面详情、手机页面、判断列表和接管截图已目视检查。

自检修复了生成 SDK 必需 CSRF 请求头、联合模型类型、中文按钮可访问名称、重复点击提交、旧问题草稿串用、成功后旧版本反向决定，以及浏览器刷新与真实 Workflow 状态提交的检查时序。产品只展示实际查询结果，保留手动刷新；交互等待放到线程中以保证同一事件循环中的 Worker 可继续处理信号。
