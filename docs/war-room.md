# Step 42：War Room 重大保障

本步依据 AGENTS.md、SPEC.md、plans.md 及权威长版设计第 28 节实施。实际计划文件名为
`plans.md`。本次只完成 Step 42，鉴权、业务 HTTP API 和前端按后续步骤实现。

## 已实现的闭环

重大活动、新服、迁移、大版本及高峰期均使用 `WarRoomSubmission`：请求 UUID、服务、
名称、类型、预计峰值 RPS、带时区的开始/结束时间。时间统一转 UTC，单次最多一天。
通过 `submit_war_room()` 接纳为 manual/Human OpsEvent 和 AI Task，由既有
`event.start_task` 派发到同一 `AITaskWorkflow`。相同请求重复提交只建一个任务；修改
已提交材料会被拒绝；同一服务的保障时间不能重叠，避免争抢临时资源。

准备阶段先经 `search_runbooks` 查询最多 100 个候选，检查适用/排除条件和成熟度。
再经 L0 `query_war_room_facts`、`get_execution_target` 查询容量、实际资源及 32 项风险。
复用 Step 40 的 17 类巡检及稳定性/容量/安全/成本规则，含监控、告警与回滚准备。
全部查询走唯一 Dispatcher、Policy、Evidence Ledger 和审计。

容量依据来源测量的单副本吞吐量和活动峰值计算，默认保留 25% 余量并向上取整。
示例：1000 RPS / 每副本 250 RPS × 1.25 = 5 副本。若原有 3 副本，则准备动作为 3→5；
现有容量足够时不写资源。缺容量、未就绪、缺适用 Runbook、来源不完整/过期/未来观测
均不能当作健康，进入 WAITING_INFORMATION；回答后重新采集，不能替代审批。
明确风险或容量超过宿主上限进入 NEED_HUMAN_JUDGMENT，保留判断并转人工修订。

独立 Reviewer 重新查询，反证容量假设不足、监控遗漏、资源被修改、回收后容量不足。
当前评估须有成功查询审计和独立复核才能进入 PLANNING。首次准备和结束后回收分别
生成结构化 `scale_service` Action Plan，风险均为 L3，含前置条件、回滚、验证和 Evidence；
默认均需要新的动作哈希审批。执行复用已有 Executor、短时动作凭证、幂等执行 ID、
意图/回执/审计和熔断。Policy deny、拒绝、超时、规则/参数变化或准备授权过期不能执行。

资源准备经独立 Verifier 读回后，Temporal Timer 等待开始并逐窗盯盘。每个已完成窗口
独立采集、评估；异常或未知产生去重的 Alert OpsEvent/处置任务，留存父任务、窗口、
真实事实 Evidence 引用并派发到统一任务引擎。后续写操作继续受子任务的 Reviewer、
Policy、审批和 Verifier 约束。报告不把子任务创建等同于子任务已解决。持续同类异常
不重复建任务，每个窗口仍单独留证。

结束后重新检查负载以及资源 UID/版本/镜像与准备回执。只有本次确实增加且未被其他
操作改变的临时副本才可恢复到原副本数。未结束、负载未下降、风险未恢复、资源被修改
时保留容量转人工。回收后的资源、就绪、业务健康和容量余量由独立 Verifier 重查，通过
后才由 tasks 服务设置 RESOLVED→LEARNING→CLOSED；执行成功而业务未恢复不能置成功。

完成报告包含设计全部 11 个环节的具体结论，每章引用对应 Evidence ID。窗口汇总保留
全部窗口引用，报告列出动作、审批、最终核验和异常处置任务。可用 Ledger 服务按 ID
精确读取。行锁和检查点保证并发、重投、丢响应、Worker 重启不重复已完成的采集、
资源动作或异常任务，Temporal 历史可回放。

## 自己跑一遍

启动 Docker Desktop 的 Linux 引擎，并确保本项目依赖运行。已有依赖可这样启动：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
. .\use-local-deps.ps1
docker compose -f deploy/docker-compose.yml up -d --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw '本机依赖启动失败' }
```

随后运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\check-war-room.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 42 专项失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-war-room.ps1
if ($LASTEXITCODE -ne 0) { throw 'Step 42 演示失败' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\check.ps1
if ($LASTEXITCODE -ne 0) { throw '统一检查失败' }
```

预期显示 32 项准备检查、所需 5 副本、**3→5 准备、5→3 回收、两个连续异常窗口只创建
1 个处置任务、两个分别审批的 Fake 动作、11 章报告、真实 Evidence ID、CLOSED、重复提交
新增任务 0、Temporal 历史回放通过**。末尾显示“Step 42 War Room Fake 演示全部通过”及
“临时测试库已清理”。脚本自动准备临时库和隔离 Worker；无需启动 API、手动迁移、
生产凭证或常驻 Worker。临时库清理后业务记录不再保留，终端输出供本次验收。

若要亲自批准两次资源动作：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\demo-war-room.ps1 -Interactive
```

两张计划分别显示目标、参数、L3 风险、回滚和验证。输入 `approve` 批准、`reject` 拒绝。
交互演示约 90 秒，等待由 Temporal 管理。拒绝准备应看到 ESCALATED、动作数 0；拒绝
回收应看到 ESCALATED、动作数 1，并保留临时副本。退出时清理模拟对象、等待中的
异常 Workflow 和临时库。完整回归先加载 `. .\use-local-temporal.ps1` 再运行
`.\check-db.ps1`；三个既有官方时间跳跃场景沿 `check-schedules.ps1` 独立入口运行。

## 配置与当前范围

`WAR_ROOM_CONFIG` 仅从环境读取，默认 enabled=true、interval_seconds=60、max_windows=1440、
capacity_margin=1.25。总窗口数超限被拒；宿主规则和执行绑定只保存指纹，不保存配置
对象或凭证。`EXECUTION_CONFIG.enabled` 默认 false，身份隔离沿用既有实现。当前 Worker
仍只允许 local/test + Fake 和本机 PostgreSQL/Temporal。

首批支持已有 K8s 扩缩容，恢复活动前副本数；未增加云资源创建/删除能力。事实接入提供
Connector 接口、Fake 和高级 Tool，公司容量、就绪副本、监控窗口聚合协议尚未提供，
当前没有真实联网实现。源系统需保证窗口完整性，不能用实时采样冒充历史全窗；本步
运行验收全部为 Fake，审批通知也是 Fake 飞书。

无新增依赖、中间件或迁移，head 保持 `0014_inspection_risks`。API/鉴权和前端分别由
Step 43–45、47 及后续页面步骤接入，本步不扩展到这些步骤。

## 2026-10-08 自检记录

最终保障专项 **43 passed**（22 项禁止真实网络的单测、21 项本机数据库/Temporal）。
统一检查 **1981 passed, 530 skipped**，ruff、格式、mypy（423 个源文件）、Connector 边界
和 Git 检查通过。依赖集成测试通过独立入口执行：完整数据库/Temporal 回归
**516 passed, 3 skipped**，最终新增的动作身份校验场景由 43 项专项覆盖；三个既有时间
跳跃场景由定时专项 **20 passed** 覆盖，旧 Workflow 回归 **36 passed**。

自动及 Windows PowerShell 两次 `approve` 交互演示实际通过，均输出 CLOSED、两次 Fake
动作、一个去重异常任务、十一章报告、真实 Evidence ID 和历史回放成功；临时库已清理。
修复了严格类型/格式、报告引用、候选截断、资源基线/动作身份校验及短测试期限引起的
正常授权过期问题。活动准备授权过期保护保持生效，新脚本 BOM 和语法检查通过。

最终依赖检查：四个本机服务 healthy，pgvector 0.8.6/UTC 可用，Temporal SERVING，UI
HTTP 200；应用库仍为 `0014_inspection_risks`，metadata 无新增差异。临时测试数据库数
及本机运行中的 AITaskWorkflow 数均为 0。前端仍仅保留 `.gitkeep`，Step 43 仍为还没做。
