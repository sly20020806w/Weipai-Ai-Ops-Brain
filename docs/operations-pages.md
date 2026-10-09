# Step 51：运营页面与自行验收

本步实现发布中心、工单中心、巡检中心、风险中心、重大保障、架构评审、自动化中心七个页面。复用 Step 45 的只读接口、后端 OpenAPI 生成客户端、Cookie 会话与现有审批中心。

六类运营任务支持列表、状态与服务联合筛选、分页、刷新和详情。详情展示任务状态、来源事件、源系统标识、UTC 时间、各阶段报告与 Evidence。报告保留原始观察时间和所有历史记录，不将旧报告当作当前观察；异常与待核实项分别展示。Evidence 引用可点击读回原始快照，关联任务链接可查看状态时间线、Tool 调用和完整调查链。审批、判断、补充信息、接管入口跳转至已实现的审批中心。

风险中心按服务、稳定性/容量/安全/成本类别及恢复状态筛选。详情保留首次/最近观察、异常次数、恢复时间和发现/观察/通知证据。巡检详情与服务风险互相链接；巡检任务 CLOSED 表示扫描闭环，未恢复风险仍保留，不因任务关闭自动消失。风险是否恢复以后台重新检查为准，页面不修改风险状态。

筛选、页码和打开的 Evidence 保存在 URL 中，详情返回列表保留筛选。未登录详情在登录后回到原链接；401 清除身份与业务缓存，404/422/503 提供可恢复错误和重试。源文本按纯文本显示。手机布局在表格内滚动，长标识与证据内容换行。

## 自动检查

在项目根目录 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-operations-pages.ps1
if ($LASTEXITCODE -ne 0) { throw '运营页面浏览器验收失败' }
```

统一检查包含后端 Connector 边界、ruff/格式/mypy/pytest、Git 环境文件检查、OpenAPI 生成一致性和前端 lint/typecheck/test/build。本步没有修改后端业务逻辑、API 或数据库迁移；既有数据库/Temporal 专项仍使用原有独立入口，跳过项不计为通过。

浏览器验收需要本机 Docker Desktop Linux 引擎和项目既有 PostgreSQL/Temporal 容器。若依赖已停止，在同一窗口执行：

```powershell
docker desktop start
.\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw '本机依赖启动失败' }
```

首次建立依赖见 [本地依赖说明](../deploy/README.md)。演示创建独立临时数据库，配置和临时密码只在进程中；不输出或保存凭证。准备样例复用已经验收的 Fake 事故、发布、工单、保障、评审和自动化 Workflow，报告来自它们真实落库的证据；模拟动作遵循既有 Policy/审批/Executor/Verifier，只调用 Fake。巡检通过真实本机 Temporal 产生四条风险。浏览器阶段只发起登录和只读查询，拒绝外部请求与业务写请求。

## 亲自操作一遍

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-operations-pages.ps1 -Interactive
```

1. 设置 12–256 字符的临时密码，输入不显示。脚本准备样例并先完成浏览器自动验收，随后打印可打开的本机地址。用 `local-browser-owner` 和刚才设置的密码登录。
2. 进入“巡检中心”，选择 `payment-service` 的巡检。任务应显示“已关闭”，报告包含服务健康与治理检查。点击 Evidence ID，应打开证据抽屉并显示匹配 ID、来源 Tool、UTC 时间与原快照。
3. 点击“查看关联服务风险”，应出现四条：缺少 PDB、缺少 HPA、证书有效期、闲置 ECS，恢复状态均为“未恢复”。选择“稳定性”和“未恢复”应剩两条；刷新浏览器后筛选保持。打开一条风险，检查观察时间和证据，再返回列表确认筛选未丢失。
4. 打开“发布中心”，查看正常发布和异常暂停/回滚的记录与报告；高风险 SQL 的样例转人工。打开“工单中心”，查看权限工单的上下文、处理证据、验证和回填记录。动作均为准备样例时的 Fake 历史。
5. 打开“重大保障”，查看容量评估、准备与回收和十一章报告；打开“架构评审”，检查全部十二个维度，稳定性/高可用明确指出单点数据库，其余材料不足处保留待核实。
6. 打开“自动化中心”，展开建议查看五条原始人工操作记录及其 Evidence 引用。建议本身不授予自动执行权限。可点击关联任务或审批中心入口查看已有状态。
7. 将浏览器缩到手机宽度，检查列表、报告、风险详情与证据抽屉。完成后回终端按 Enter，脚本关闭 API/前端并清理临时数据库；再次运行会建立新的样例。

自动验收还核对六类列表与详情的真实接口结果、登录详情返回、精确证据读回、风险筛选/刷新/返回、手机无整体横向溢出，并回归外壳与原任务页面。截图保存在 `.cache/frontend-smoke/`，不录制密码、Cookie 或请求跟踪。

本步只实现 Step 51；Dashboard/Audit、AI Chat 页面及后续步骤保持原计划。

## 自检记录（2026-10-08）

- 统一后端 Connector 边界、ruff/格式/mypy、Git 环境文件检查通过；pytest **2166 passed / 590 skipped**。590 项既有数据库/Temporal 专项沿用独立入口，本步未全量复跑。最终修改的两份 Python 辅助脚本另经 ruff/格式/mypy 检查通过。
- OpenAPI 与 16 个客户端文件逐字节一致。最终前端 lint/typecheck、**95 项组件测试**与生产构建通过，其中本步新增 16 项，覆盖七类列表/详情、引用、筛选、错误、会话、场景切换与纯文本安全展示。
- 最终 Windows PowerShell 5.1 交付命令成功，真实本机 PostgreSQL/API/Temporal/Edge **3 passed**：六类既有 Fake 闭环报告、四条持久化巡检风险、联合筛选剩两条、精确 Evidence、登录详情返回、刷新/返回及手机布局通过，外壳与任务页面回归通过。浏览器业务写请求和外部请求均为 0。
- 桌面风险列表、十二维评审报告与手机风险详情截图已目视核对；手机页面和抽屉无整体横向溢出。脚本 UTF-8 BOM 与 PowerShell 语法检查通过，交互式输入和结束提示复用既有演示流程，用户可按上面的命令亲自操作。
- 自检修复风险检查标识、报告常量导出、工单/复盘默认展开、严格类型、中文按钮定位，以及折叠 Evidence 多处引用、异步页面导航和首个会话响应的浏览器验收时序。配置指纹与内部阶段版本从阅读视图收起，完整原快照仍可在证据抽屉查看。
- 本机 Docker 两处失效临时 socket 目录在停机后改名保留副本并恢复，未修改容器卷或虚拟磁盘。验收结束四个依赖 healthy，临时测试库数量为 0，API/preview 由脚本退出清理。
- `plans.md` 仅将 Step 51 更新为完成，Step 52 及以后仍未完成。没有新增后端业务接口、迁移、依赖或中间件；所有场景运行均为 Fake，无生产请求或真实飞书消息。
