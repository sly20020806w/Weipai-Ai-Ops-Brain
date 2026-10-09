# Step 50：认知页面与自行验收

本步实现服务与上下文图、运行手册中心、知识中心。复用 Step 45 的已有 API、后端 OpenAPI 生成客户端、Cookie 会话及 CSRF 校验。

服务列表按源系统标识打开关系图，显示名称可以是中文。完整上下文包含业务、仓库、版本、集群、Pod、数据库等已发现关系；上游/下游视图只沿后端确认的实际调用边查询，不通过共同资源推断服务依赖。支持 1–4 跳、方向、刷新及 URL 保存查询条件。关系可悬停、键盘聚焦或点击，显示来源、置信度、首次/最近观察时间及新鲜度；节点可查看完整名称、类型与源系统标识。新鲜度由后端相对于本次读取时间计算，时间以 UTC 展示。

运行手册支持成熟度筛选、分页、详情及完整内容的新建/编辑/删除：适用与排除条件、L0 诊断及 JSON 参数、处理步骤与风险、回滚方案、验证方式。统计、可信度、成熟度、自动化等级与内容版本只读，内容变更的版本更新与退回草稿由后端控制。知识支持类型筛选、分页、详情、新建/编辑/删除、来源及 UTC 有效期。保存成功重新读取详情与列表，删除前显示条目确认，成功后返回刷新后的列表；失败保留输入且不自动重试，结果未确认时先返回列表核对。

## 自动检查

从项目根目录的 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-cognition-pages.ps1
if ($LASTEXITCODE -ne 0) { throw '认知页面浏览器验收失败' }
```

统一检查涵盖 Connector 边界、后端 ruff/格式/mypy/pytest、Git 环境文件检查、OpenAPI 客户端逐字节一致性以及前端 lint/typecheck/test/build。数据库与 Temporal 集成仍使用项目原有独立验收入口，不将跳过项计为通过。

浏览器演示要求本机 Docker Desktop Linux 引擎和项目既有 PostgreSQL/Temporal 容器可用；脚本读取既有本地容器配置，创建独立临时数据库，不输出密码。若依赖已停止，可在同一个窗口恢复：

```powershell
docker desktop start
.\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw '依赖启动失败' }
```

首次创建依赖见 [本地依赖说明](../deploy/README.md)。不使用生产账号或生产数据。

## 亲自操作一遍

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-cognition-pages.ps1 -Interactive
```

1. 按提示设置 12–256 字符的临时密码；输入不显示、不保存。脚本先完成自动浏览器验收，再打印本机浏览器地址与服务图链接。
2. 打开打印的地址，用 `local-browser-owner` 和刚才设置的密码登录。进入 `payment-service`，悬停一条关系，确认来源、置信度与新鲜度可见；点击后确认详情有首次/最近观察 UTC 时间。选择跳数与上下游，刷新浏览器后筛选仍保持。
3. 打开“运行手册中心”，查看 `payment-cognition-demo`。点击新建，用英文标识、中文说明/来源填写适用条件；诊断 Tool 可填 `get_service_context`，参数填 `{"service_name":"payment-service"}`，诊断风险固定 L0。填写处理步骤、风险、回滚方案及验证方式后保存，应进入详情并显示“草稿”。回列表后新条目可见，刷新浏览器仍存在。
4. 编辑刚新建的手册说明，保存后列表展示新说明，详情版本增加；总体风险低于处理步骤或诊断参数不是 JSON 对象时应阻止保存。删除自己的验收条目并确认，返回列表后该条目消失。
5. 打开“知识中心”，新建一条自己的业务规则并注明来源。生效/失效时间按字段标签填写 UTC，结束必须晚于开始；失效时间可留空。保存后刷新详情应保留内容。编辑内容后返回列表查看新内容，再删除并确认，列表中应消失。
6. 可把浏览器缩到手机宽度，确认图在自己的区域内滚动、页面整体无横向溢出，表单仍可填写。全部检查后回终端按 Enter，脚本关闭 API/前端并清理临时数据库；再次运行会准备新的样例。

自动浏览器验收会核对 API 返回的图节点/边、关系信息、筛选刷新、两类目录的新建 201/更新 200/删除 204/删除后读回 404、内容版本以及手机布局。后续跨数据库会话检查六条本人编辑审计与最终删除结果。浏览器访问只允许回环地址，运维写请求被限制为本步的知识/手册管理接口。截图位于 `.cache/frontend-smoke/`，不记录 Cookie、密码或请求跟踪。

本步没有新增后端业务接口、迁移、依赖或中间件。全部联调使用 Fake 数据；本机演示的数据只在临时库，本步不进入 Step 51。

## 自检记录（2026-10-08）

- 后端 Connector 边界、ruff、格式、mypy、Git 环境文件检查通过；统一检查后端 **2166 passed / 590 skipped**。590 项既有数据库/Temporal 专项沿用独立入口，本步未全量复跑。
- OpenAPI 与 16 个生成客户端文件逐字节一致。最终前端 lint/typecheck、**79 项组件测试**与生产构建通过，包含本步新增 15 项认知页面场景。
- 真实本机 PostgreSQL、API、Temporal 与 Edge 验收 **3 passed**：服务图 25 节点/31 关系、悬停/键盘信息、筛选刷新、两类内容完整增删改、内容版本及手机布局通过。跨会话核对六条本人目录编辑审计与最终删除状态。
- Windows PowerShell 5.1 交互命令实际运行通过，隐藏密码输入、浏览器地址、等待 Enter 与清理通过；脚本 UTF-8 BOM/语法和桌面/手机截图已检查。
- 自检修复风险枚举、可选时间、服务显示名与标识差异、完整上下文与调用依赖查询差异、Tooltip 焦点、表单标签精确关联和手机图默认居中。Fake 关系来自固定历史时间窗，新鲜度显示数天前是原观察时间的真实投影。
- Docker 启动遇到已有临时 Unix socket 失效，已在停止状态下保留两个临时端点目录副本后恢复；四个项目依赖 healthy，临时测试库为 0，演示 preview 端口关闭。没有修改容器卷数据或虚拟磁盘。
- `plans.md` 仅将 Step 50 更新为完成；Step 51 及之后保持未完成。
