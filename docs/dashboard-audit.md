# Step 52：总览与审计页面验收

本步依据 AGENTS.md、SPEC.md、实际计划文件 plans.md 和权威设计第 32/34 节，实现总览 Dashboard 与 Audit Center。页面只使用现有 OpenAPI 生成客户端和登录会话。

## 页面行为

总览将待审批、待人工判断、待补充信息独立显示，三类统计均来自任务接口的 `total`，与审批中心共用状态、查询参数和缓存键。点击卡片进入相应列表。运行中任务包括 NEW、CONTEXT_BUILDING、RUNBOOK_MATCHING、INVESTIGATING、RCA、PLANNING、EXECUTING、VERIFYING、RESOLVED、LEARNING；RESOLVED 后仍有复盘阶段。三种人工等待另列，FAILED、AUTOMATION_ABORTED、ESCALATED、CLOSED 不计为运行中。按每个阶段读取总数和最近十条，再展示全局最近创建的十条；各阶段链接可查询完整分页列表。查询失败显示错误，不以零冒充成功读取。

能力指标覆盖原设计全部十项。默认最近 30 天，可输入明确的 UTC 半开时间窗，结束时刻不包含在内。统计口径沿用后端：窗口内创建且在窗口结束前已结束的任务，缺少标注和零样本为未知。比例显示百分数、MTTR 显示秒、Tool Call 显示次数；展开“查看统计依据”可核对接口原值、分子、分母与单位。页面不重新计算样本或评分。默认窗口随“刷新总览”更新，自定义窗口保持选定范围。

审计中心按 UTC 时间、六类操作类型和操作人精确匹配联合筛选。包含任务审计与独立内容编辑审计，按接口时间顺序分页。查询条件和页码保存在 URL，刷新、登录详情返回和从详情返回列表均保留条件。详情展示 ID、操作人、原始结果、发生时间、关联任务、Evidence 与操作详情；内容编辑没有任务或证据时明确显示。Evidence 可点击按 ID 读取原快照。来源文本按纯文本展示。

未登录返回登录；会话失效清除身份和业务缓存。404/422/503 显示错误与重试，非法时间窗或类型不提交无效查询。手机布局卡片纵向排列，宽表格在自身区域滚动，证据抽屉可在手机宽度阅读。

## 自动检查

在根目录 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-dashboard-pages.ps1
if ($LASTEXITCODE -ne 0) { throw '总览与审计浏览器验收失败' }
```

统一检查包括后端 Connector 边界、ruff/格式/mypy/pytest、Git 环境文件检查，以及 OpenAPI 一致性和前端 lint/typecheck/test/build。既有数据库/Temporal 专项沿用独立入口，跳过项不计为通过。本步浏览器演示单独使用临时 PostgreSQL 与本机 Temporal。

如果本机依赖未启动，先执行：

```powershell
docker desktop start
.\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw '本机依赖启动失败' }
```

本机依赖首次配置见 [依赖说明](../deploy/README.md)。浏览器演示需要 Node.js、pnpm、uv、项目既有本地依赖及 Microsoft Edge。准备样例使用既有 Fake Workflow、Fake LLM 和 Fake Connector，创建真实持久化的事故、等待任务及两条内容编辑审计；不会连接生产系统。浏览器验收只允许登录和只读请求，拒绝外部请求与业务写请求。密码只在进程内，API/preview/运行等待 Workflow 和临时数据库在退出时自动清理。

## 亲自操作

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-dashboard-pages.ps1 -Interactive
```

1. 设置 12–256 字符的临时密码，输入不显示。脚本先自动验收，再打印浏览器地址。使用 `local-browser-owner` 和自己设置的密码登录。
2. 打开总览，记下三个待处理数字。分别点击对应卡片，核对审批中心底部“共 N 条”和所选列表类别，再返回总览。待审批与人工判断应各自独立。
3. 查看运行中任务数及各阶段链接。样例主要为已关闭事故和等待人工任务，运行数可能为零；真实运行数必须以接口状态为准。
4. 检查十项能力指标，展开统计依据。百分比应为原值乘 100；平均 MTTR 用秒，平均 Tool Call 用次数。未知样本不应显示为 0%。设置 UTC 起止时间并应用，刷新浏览器后范围应保留。
5. 打开审计中心，操作人填写 `local-browser-seed`，类型选择“内容编辑”，点击“查询审计”。应有两条准备样例时的目录编辑审计，操作人完全一致。打开详情，再返回列表，筛选应保留。
6. 将类型改为“Tool 调用”、操作人改为 `codex-main-agent`。打开一条审计，点击 Evidence ID，核对抽屉中的 ID、所属任务、来源 Tool、UTC 时间和快照。缩到手机宽度检查卡片、详情及抽屉。
7. 完成后回到终端按 Enter。应显示临时库清理完成；再次执行脚本会创建新的独立样例。

自动浏览器验收同时回归登录外壳和任务页面，截图位于 `.cache/frontend-smoke/`。本步没有新增后端业务接口、迁移、依赖或中间件。Step 53 及以后保持原计划。

## 自检记录（2026-10-09）

- 最终源码统一检查全部通过：Connector 边界、ruff/格式/mypy（467 个检查源文件）与 Git 环境检查；后端 **2166 passed / 590 skipped**。590 项既有数据库/Temporal 专项沿用独立入口，本次未全量复跑。
- OpenAPI 与 16 个客户端文件逐字节一致，前端 lint/typecheck、**113 项组件测试**与生产构建通过。本步新增 18 项，覆盖多阶段任务 total、待处理/审批一致、十项指标、null/零值、联合筛选/分页/详情返回、UTC 表单倒置拒绝与修正提交、错误恢复、登录深链接与 401 清缓存。
- Windows PowerShell 5.1 自动和交互交付命令实际运行，均 **3 项真实浏览器测试通过**：总览/审计专项及外壳/任务页回归。待处理数量与审批中心一致，十项指标原值/分子/分母与接口一致；按操作人和类型筛选准确，UTC 时间窗及条件刷新保持，审计关联 Evidence 精确读回。浏览器业务写请求与外部请求均为 0。
- 桌面总览/审计详情、手机总览/证据抽屉截图已目视检查，手机无整体横向溢出。新脚本 UTF-8 BOM/PowerShell 语法、隐藏密码、打印地址和 Enter 退出清理均通过；退出后临时测试库数量为 0，preview 端口关闭，四个项目依赖 healthy。
- 自检修复组件热更新导出、加载按钮及操作类型下拉框的可访问名称。真实主 Agent 审计操作人为 `codex-main-agent`，验收夹具及手工说明已据代码修正。
- `plans.md` 仅将 Step 52 更新为完成。没有新增业务接口、迁移、依赖或生产访问，数据库 head 保持 `0016_catalog_audit`。
