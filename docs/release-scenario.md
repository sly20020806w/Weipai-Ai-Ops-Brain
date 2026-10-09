# Step 39：发布与变更场景验收

本步依据 AGENTS.md、SPEC.md、plans.md 和权威设计第 25 节实现。发布先归一为
OpsEvent/source=Release，再进入既有 AITaskWorkflow，复用 tasks 唯一状态迁移、
Dispatcher、Policy、精确哈希审批、Executor、独立 Verifier、Evidence Ledger 和熔断。

## 自己运行一遍

先启动 Docker Desktop 的 Linux 引擎；已有本地依赖未启动时，执行：

```powershell
Set-Location 'F:\Weipai AI Ops Brain'
. .\use-local-deps.ps1
docker compose -f .\deploy\docker-compose.yml up -d
.\check-deps.ps1
```

首次创建依赖环境的配置方法见 [本地部署说明](../deploy/README.md)。
以下命令均在仓库根目录执行，脚本会定位 uv，并读取本项目本机 Docker 依赖的配置。
不需要手动启动 API 或 Worker。

```powershell
.\check-releases.ps1
.\demo-releases.ps1 -Interactive
.\check.ps1
```

1. `check-releases.ps1` 创建独立临时库并升到当前 migration head，启动独立队列 Worker，
   验证发布专项。预期 37 passed、输出「Step 39 发布与变更场景验收全部通过」，
   最后自动清理临时库，退出码 0。
2. `demo-releases.ps1 -Interactive` 按次序演示正常发布、5xx 异常及高风险 SQL。
   卡片内容会在终端打印完整动作、参数、风险、回滚方案、验证和审批哈希；
   想走完整成功路径，在四次提示中输入 **批准**。无需输入真实凭证。
3. `check.ps1` 输出「统一检查全部通过」，退出码 0。前端仍按计划 Step 47 建立，
   本步没有新增前端工程。

自动演示可用 `demo-releases.ps1`。想额外运行完整数据库/Temporal 回归：

```powershell
. .\use-local-temporal.ps1
.\check-db.ps1
```

## 怎样确认结果能用

| 场景 | 终端/证据中应看到 | Fake 动作次数 |
| --- | --- | --- |
| 正常发布 | canary 审批 → 10% 灰度观测通过 → promote 独立审批 → 100% 推广 → 独立验证 → CLOSED；报告 outcome=released | 2 |
| 5xx 异常 | 灰度后 5xx/P99/成功率/日志/Trace 异常 → 停止推广 → Policy 放行暂停 → paused=true → 独立 rollback 审批 → 回到 v2.3.6 → 验证 → CLOSED；报告 outcome=rolled_back | 3 |
| 高风险 SQL | sql 检查 passed=false，DROP 样例为 L5，进入 NEED_HUMAN_JUDGMENT；没有动作审批和凭证签发。演示保存判断后转 ESCALATED | 0 |

报告包含 Git Diff、影响面、SQL、资源、监控、回滚六项检查，各结论引用真实 Evidence ID。
报告的 evidence_ids 和 UTC timeline 串起每轮检查、四类独立反证、精确动作计划、审批、
实际回执和验证结果。正常与异常场景打印报告内容及 Temporal Workflow ID。
可在 [本机 Temporal UI](http://localhost:8080) 按该 ID 查看状态历史、Timer 和审批信号；
演示结束后的 Workflow 保留完成历史，临时数据库会删除。

输入 **拒绝** 时当前发布进入 ESCALATED，不执行该审批所对应的动作；拒绝最初发布时写入 0。
异常回滚被拒绝时保留已执行的灰度和暂停，不执行回滚。演示随后继续下一独立场景。

## 执行与验证约束

- 灰度、推广、暂停、回滚均为 L3；缺省都需审批。
  演示只在 test 环境显式配置 `pause_release` 的 allow 规则，以展示异常自动暂停。
  实际缺省配置不会继承这条演示规则，回滚始终使用独立的新动作审批。
- 推广必须引用紧邻的灰度成功证据，暂停必须引用发布异常窗口，回滚必须引用暂停读回证据。
  每个阶段重新检查源发布材料、版本、上下游关系及宿主镜像白名单。
- 所有 SQL 变更（含 .sql 文件和 Diff 新增行中内嵌的写语句）均要求另行人工评审，
  DROP/TRUNCATE 标为 L5，其余 SQL 至少 L4，当前不会执行 SQL。
  缺失 Diff、过期/低置信图关系、未通过资源/监控/回滚检查也阻止发布。
- Executor 先提交固定 execution_id 的意图，再经 Dispatcher 签发默认 60 秒、至多 300 秒、
  绑定完整命令哈希的最小权限凭证。已提交请求重复/并发/丢响应重试复用原结果；
  Replay 复用原证据，不签发凭证、不调用写端。
- 独立 Verifier 查询执行后的 UTC 窗口，核对服务、集群、命名空间、Deployment、容器、UID、
  镜像、副本、暂停及流量目标，并检查 Deployment/Pod、5xx、P99、成功率、日志、Trace 和资源。
  缺数据或未恢复不能设置 RESOLVED；失败回到 INVESTIGATING，再停止自动尝试并转人工。
- Timer、审批等待、重试、恢复全部在 Temporal 中。持续恶化、动作失败、超 Action 上限等
  仍由既有熔断器锁存停止，并向 Fake 飞书通知接管，不能继续签发。

## 配置与适配范围

`RELEASE_CONFIG` 是可选环境 JSON，缺省 enabled=false。启用后的 Release 事件由本场景
完整验证，沿用 Step 22 的发布后延时触发器用于未启用本场景的历史路径，避免对尚未执行
的发布申请另起一个“发布后”验证任务。

```json
{"enabled":true,"canary_percent":10,"observation_seconds":60,"max_5xx_ratio":0.01,"max_p99_ms":500,"min_success_ratio":0.99}
```

这些阈值只来自进程环境；Ledger 保存规则指纹、批准目标和观测证据。
验收把观测 Timer 缩为 0.1 秒，并注入明确的 Fake 时间窗样本；缺省仍为 60 秒。

本步实际验收为本机 PostgreSQL/Temporal + Fake 发布源及动作端，不访问生产或真实飞书。
真实发布系统的申请/灰度流量 API、监控聚合协议和服务器动作授权尚未提供，当前启用门禁
只允许 local/test + Fake。10%/100% 流量为 Fake 动作端的显式状态，不把普通 Kubernetes
Deployment 的镜像更新当作真实流量灰度。现有 HTTP 动作端仍只允许 MockTransport。

没有新数据库迁移或中间件，head 保持 0013_runbook_maturity；没有进入 Step 40。

## 本次自检记录

发布专项 37 passed；统一检查 ruff/格式/mypy/Connector 边界/Git 全通过，
1872 passed/480 skipped；完整数据库/Temporal 回归 467 passed/3 skipped，
3 项跳过为既有独立时间跳跃测试。Windows PowerShell 5.1 自动演示和四次中文“批准”
交互实际通过，正常/异常均 CLOSED，SQL 场景零执行后 ESCALATED。
新脚本的语法与 UTF-8 BOM 已检查；旧 check-deps.ps1 补齐 BOM 后实际复跑通过，
四个本机依赖 healthy、Temporal SERVING、UI 两个入口 HTTP 200。临时测试库自动清理。

续接收尾时 Docker Desktop 因两个目录中的残留空套接字启动失败。已逐项确认目录内容，
保留旧目录备份后重新创建运行时目录，恢复 Docker 与本项目已有依赖容器。
恢复后已实查临时测试库数为 0，应用库 head 仍为 0013_runbook_maturity，依赖检查通过。
本次保留的运行时目录备份如下，供环境变更追踪：

- `C:\Users\38149\AppData\Local\Docker\run-startup-backup-3b8b4b2c490b488d8e45db7337285651`
- `C:\Users\38149\AppData\Local\Docker\run-startup-backup-3ad4276d87534aeabb8ac55c69243d72`
- `C:\Users\38149\AppData\Local\docker-secrets-engine-startup-backup-57c76d22ecab43cf8121caad1b8697cd`
