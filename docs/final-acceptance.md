# Step 58：本地影子演练与平台最终验收

## 本次范围

依据 AGENTS.md、SPEC.md、权威长版设计和 plans.md。按本人本次明确要求直接进入 Step 58；尚未入职，没有真实 ACK、公司 API、Reader 身份和公司 AI 网关，本次完成平台的本地全 Fake 验收。Step 57 保留“本地准备完成，真实上线待验收”。Step 58 的真实告警影子期、真实多场景基准和首个真实 L1 动作留至入职后，不能由本地结果替代。

本次没有新增业务状态、数据库迁移、应用依赖或中间件。调查、审批、执行、验证、学习和 Replay 都使用已有单体服务、唯一 Dispatcher 和 Temporal 引擎。没有开启真实运维写客户端，也没有部署 ACK、发送真实飞书消息或推送远端 CI。

## 影子演练和基准

`check-shadow.ps1` 通过签名 Prometheus 告警进入既有 OpsEvent 和 AI Task 链路，在真实本机 PostgreSQL/Temporal 上运行 Fake Discovery、Change Timeline、Runbook、主 Agent、Reviewer 和 Action Plan。执行配置关闭，Policy 对 L1–L5 以及未声明风险的动作拒绝；仍交付 RCA、Reviewer、候选计划及 Evidence。

候选写计划被 Policy 拒绝后，使用既有 ESCALATED 状态交由人处理。它不会被标为故障已恢复；没有 EXECUTING、VERIFYING、RESOLVED 或 CLOSED 的成功记录。三个告警样例分别检查零凭证签发、零执行、查询/状态/人工标注审计、重投去重和 Temporal 历史 Replay。签名错误为 401。

Fake 样例标签由独立固定验收材料提供，经 `EvaluationService.label` 保存到只追加 Ledger，带操作人和同任务 Evidence 引用；没有用候选模型输出生成评分标签。真实标注仍须由本人依据源系统和现实信息完成。十项指标继续展示分子、分母和单位，无恢复样本时 MTTR 为 null。

Replay 基准包含三个不同事故 ID，复用已有已关闭 Fake 历史事故准备能力，再使用真实 Temporal Replay Workflow 重新调查。三个事故使用相同支付场景；这个小基准只证明回放、评分和门槛机制，不能证明跨场景准确率。候选只看到 RCA 截止时间前的历史结果；人工标签仅评分时读取。所有 live handler 被失败替身覆盖，实际调用必须为零。

`AcceptanceCriteria` 默认本地门槛是 RCA 命中率 1.0、最少 3 个事故。可由本人指定其他门槛：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\check-shadow.ps1 -RcaThreshold 0.95 -MinimumSamples 3
```

门槛必须在 `(0, 1]`，最少样本必须是正整数。当前固定小基准只有 3 个事故；传入 `-MinimumSamples 4` 会失败。未标注、未完成、样本不足、命中率不足均失败；失败回放仍在总样本分母里。同一事故重复回放或混合候选版本被拒绝。通过门槛不会修改 Policy、晋级 Runbook 或启用生产权限。

15 项专项包括 11 项门槛单元场景、3 个只读告警演练和 1 个三事故 Replay 基准。HTTP 只使用 ASGITransport，网络门禁仅允许两个精确本机 PostgreSQL/Temporal 端口，Temporal 原生连接入口另有限域。业务中的任何真实运维/外网尝试均使专项失败。工具准备、依赖漏洞扫描及镜像构建可能需要互联网，它们属于构建验收过程。

## 亲自运行最终验收

先启动 Docker Desktop 的 Linux 引擎，保留本项目现有数据库卷。完整安全验收使用已有本机 kind 节点 `weipai-sim-control-plane`；可用 `-LocalKindNode` 指定另一个受检查的本机 kind control-plane。所有步骤使用现有工具定位、锁定依赖和本机隔离临时库。

在 PowerShell 执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
. .\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw '本机依赖启动失败' }
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\check-final.ps1
if ($LASTEXITCODE -ne 0) { throw '最终验收失败，请查看报告中的失败日志' }
Get-Content .\.cache\acceptance\latest.json -Raw -Encoding UTF8
```

完整验收有 15 个阶段：

| 阶段 | 检查内容 |
| --- | --- |
| unified | Connector 边界、ruff/格式、双平台 mypy、后端单测、Git 环境规则、源码/历史密钥和锁定依赖漏洞扫描、强制 Fake E2E、OpenAPI 生成一致性、前端 lint/typecheck/test/build |
| database | 全部既有数据库/Temporal 场景、迁移升降级及 metadata；加载官方时间跳跃服务器，JUnit 拒绝任何跳过 |
| workflow / schedules / replay | Workflow 历史兼容和恢复、日历/时间跳跃、Replay 缺历史/伪造引用/晚到证据及十项手算指标 |
| security | Reader/Executor 本机 kind 权限、全部接口鉴权与扫描；临时 RBAC 资源清理 |
| readonly | Helm lint/渲染/参数拒绝、本机只读 Discovery；不部署 ACK 工作负载 |
| shadow | 15 项影子与门槛专项，Evidence 和 Replay 报告 |
| images | 重建后端/前端镜像，实际启动 api 和 worker、登录/CSRF/SSE 代理与 SPA 验收 |
| 六组 browser | 17 入口和任务证据、审批/判断/接管、图/Runbook/知识 CRUD、七类运营场景、总览/审计、SSE/追问/Human 任务及手机布局 |

每个阶段输出自己的日志路径；结束后显示“Step 58 本地最终验收全部通过”和退出码 0。报告在 `.cache/acceptance/latest.json`，每次独立运行也保留自己的 `report.json` 和阶段日志。报告包含 UTC 起止时间、源码 SHA-256、门槛、阶段退出码、影子证据和待真实验收项。运行中/失败会覆盖最新状态，不能把上次通过误认为本次通过；验收期间源码变化会使最后一步失败。`.cache` 不入 Git，也不保存凭证。

默认命令从头运行全部阶段。若只修复本次验收驱动或 `frontend/smoke/` 浏览器脚本，可传 `-ResumeFrom` 指向本项目某次独立运行的 `report.json`。续跑必须有可核验的逐文件源码快照、原始日志和日志 SHA-256，且门槛、本机 kind 节点和 Fake 范围一致。业务代码、依赖或其他测试改动均拒绝复用，必须执行默认完整命令；统一检查和全部浏览器阶段始终重新运行。复用阶段在报告中保留原检查日志、`reused_from` 和原源码指纹，不声称重新执行。

统一普通 pytest 中的集成跳过属于拆分入口；最终验收随后实际运行完整数据库回归和各专项，不依赖那些跳过项宣称成功。影子和 E2E 均核对 JUnit 实际数量与零跳过。完整数据库回归要求至少覆盖既有 580 个场景且零跳过。

只复检本步核心能力时：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\check-shadow.ps1
if ($LASTEXITCODE -ne 0) { throw '影子专项失败' }
Get-Content .\.cache\shadow\latest.json -Raw -Encoding UTF8
```

## 亲自浏览并操作

最终验收自动使用 Edge 检查全部页面。本人可再运行下面的交互演示，设置一个临时密码，按脚本打印的本机地址登录；全部数据在独立临时库，结束时按 Enter 清理：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\demo-cognition-pages.ps1 -Interactive
```

这个入口可以逐一打开 17 个导航，查看服务图、边的来源/置信度/新鲜度，并新建、编辑、删除知识或 Runbook。验收通过条件是保存后列表与详情更新、删除后记录消失。

要实际体验审批、人工判断和接管，退出上一个演示后运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\demo-approval-pages.ps1 -Interactive
```

使用脚本打印的待审批样例，核对动作/目标/风险/回滚/验证与哈希，批准后任务进入 EXECUTING；这个专用页面演示检查授权交接，未启用 Executor。完整 Fake 回滚→独立验证→复盘→CLOSED 由最终验收中的 E2E 检查。业务判断和补充信息显示在独立列表，不能替代批准。

要体验流式问答与证据：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\demo-chat-pages.ps1 -Interactive
```

在 AI Chat 的支付样例中发起只读问答、点开 Evidence、继续追问，并在 AI Task Center 查到 Human 任务。处置回滚请求必须停在 WAITING_APPROVAL，审批前零执行。所有演示只支持预设 Fake 能力，不能用它们评价真实公司网关的开放问题推理效果。

## 入职后恢复真实验收

1. 按 `docs/ack-readonly.md` 补公司协议、Reader 身份、命名空间/服务绑定、Secret 和 ACK 参数；完成 Step 57 的真实 Pod/health/Discovery 和无 Executor 写凭证检查。
2. 适配并核对真实 webhook/签名、指标/日志/Trace 时间和字段语义、公司网关行为，再设计并验收真实任务影子 Worker。现有云端 `readonly_discovery` 只运行 Discovery，仍禁止 Agent 任务；本次没有开放完整真实任务 Worker。
3. 以 L0 查询调查真实告警，由本人标根因/误报并引用真实 Evidence，构建覆盖多种业务故障的历史基准，设定有代表性的样本量与命中率阈值。未完成/未标注不得排除后计算通过。
4. 分别验收公司动作端的短时动作凭证签发、参数限制、资源范围和服务器幂等协议。真实写适配器当前仍只允许 HTTP mock；不能用普通 Reader token 替代动作授权。
5. 每级写能力单独授权和验收。首个真实 L1 须经过 Policy→Executor→独立 Verifier，核对完整审计、拒绝/失败/熔断与回滚效果后才扩大范围；Replay 门槛达标不会自动放权。

## 自检记录

首次完整集成回归为 579 passed、1 failed、零跳过。失败位于重大保障异常演练：测试在启动 Worker 前开始计算十秒活动期限，准备计划时期限已过，生产授权门禁正确拒绝旧计划。已把该测试的活动创建移到 Worker 就绪后，保持原十秒期限、过期拒绝规则和全部断言。重大保障专项复跑 43 passed，临时库已清理；没有放宽任何业务授权或验证保护。

2026-10-09 最终 15 阶段全部通过，报告为 `.cache/acceptance/74eb788c042f4d5188f3f8754a7967ad/report.json`，最新入口为 `.cache/acceptance/latest.json`，状态 `passed`、范围 `local_fake`。Step 58 已在 plans.md 标为“完成（本地 Fake 最终验收）”；Step 57 真实验收仍保留。

最终统一检查后端 2353 passed/595 skipped，双平台 mypy 490 个源文件、ruff/格式/边界/Git 检查通过；595 个拆分集成场景由独立入口实际覆盖。完整数据库/Temporal 580 passed、零跳过；Workflow 36、日历/时间跳跃 20、Replay 47、只读 39、影子 15、强制 E2E 9、前端组件 132 全通过。前端生成 16 文件一致、lint/typecheck/build 通过；六组 Edge 浏览器为 2/3/3/3/3/3 passed，均包含外壳与任务回归。密钥和已知依赖漏洞 0；镜像实际 API/Worker/frontend 启动通过。

最后仅变更验收脚本和说明，未改动业务源码。完整业务回归、权限、只读、影子和镜像结果在逐文件源码及原始日志哈希一致后复用，报告保留检查来源；最终统一检查和六组浏览器均重新执行。临时测试/镜像数据库 0、镜像验收容器 0、四个依赖 healthy；PowerShell 语法和 git diff --check 通过，桌面和手机截图目视核对。完成状态及本文自检注记在测试成功后追加，报告源码指纹对应测试时的文件快照。

修复后的完整集成回归 580 passed、零跳过。随后镜像健康探测遇到一次 Docker exec 超时，原驱动未捕获瞬时超时而提前退出；容器、Schedule 和临时库已清理。已将 TimeoutExpired 纳入既有 90 秒启动等待流程，持续超时仍失败，实际退出错误仍立即失败；HTTP 200/status=ok 的断言保持。新增三个离线回归场景验证瞬时重试、总期限和错误传播。

三个启动探测回归通过，实际镜像专项复跑通过 API 健康、前端 healthy/登录/CSRF/SPA、Worker 连接与轮询、无效配置拒绝和退出清理。已使用当前构建的镜像完成验证，随后重新执行完整最终验收。

第三轮完整运行中，580 项集成零跳过、各专项、影子和镜像都通过；外壳浏览器在首次冷启动时未在五秒内看到登录标题，任务/证据测试通过。将外壳测试的标题等待设为有限十五秒、全场景设为九十秒，保留全部断言与零自动重试；实际两项浏览器复跑通过，覆盖登录、17 入口、刷新、注销、手机、任务和证据。四项续跑保护单测验证验收脚本修复可以续跑、业务变化/日志变化/门槛变化必须拒绝。已核对第三轮原源码指纹并保存逐文件及日志快照，最终汇总重跑统一检查与全部浏览器，未改动的业务场景保留原检查证据。

认知浏览器专项发现“第一条关系边的曲线中点”可能被节点或其他关系覆盖，键盘提示检查正常。鼠标验收改为固定的支付到数据库关系，并通过实际 DOM 命中检查选取可悬停的曲线点，未修改产品图实现或删减来源/置信度/新鲜度断言。复跑三项浏览器全部通过；两类内容的六次 CRUD 和六条本人审计经跨会话数据库读回确认，删除后记录不存在。

继续验收时 Docker Desktop 已停止，启动又遇到已记录的 `Docker/run/dockerInference` 临时 socket 不可访问故障。按 deploy/README.md 的既有恢复方式，在 Docker 完全停止后核对两个临时目录绝对路径，分别保留 `.recovery-step58-ad9065f40e244dae95424b757fcdf688` 副本后重建；没有移动或删除镜像、卷或虚拟磁盘。Linux 引擎恢复，本项目四个依赖 healthy、初始化退出码 0，既有本机 kind 两个节点容器恢复运行。
