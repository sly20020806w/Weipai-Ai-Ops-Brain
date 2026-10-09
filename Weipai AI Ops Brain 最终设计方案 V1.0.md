# Weipai AI Ops Brain 最终设计方案 V1.0

## 1. 平台定位

这是一个**独立于公司现有运维平台之外、只供我个人使用的 AI 业务运维平台**。

公司现有运维平台继续承担：

- CMDB
- 资源管理
- 发布
- 工单
- 权限
- 服务树
- 传统运维能力

本平台不重复建设这些基础能力。

本平台的职责是：

> 持续连接微派现有真实技术环境，自动建立并维护“微派运维实时认知模型”，通过事件、时间、状态和预测主动产生任务，由 AI 自主调查、调用工具、分析、决策，并通过 Runbook、权限、审批、执行、验证、复盘和学习形成完整闭环，最终尽可能代替我完成业务运维工作。

---

# 2. 最终目标

最终工作模式不是：

> 我发现问题 → 我找 AI → AI 告诉我怎么做。

而是：

> 环境发生变化 → 平台自己发现 → AI自己调查 → AI自己判断 → 能自动完成的自动完成 → 高风险找我审批 → 自动验证 → 自动复盘 → 更新经验。

最终我主要保留：

1. 机器无法获取的现实世界信息输入；
2. 业务取舍和特殊人工判断；
3. 高风险生产操作授权；
4. 必须由真人完成的沟通和组织协作。

其余尽量交由平台完成。

---

# 3. 总体架构

```text
                         我
              查看 / 对话 / 审批 / 接管
                         │
                         ▼
              Weipai AI Ops Brain
                         │
            ┌────────────┴────────────┐
            │                         │
       Codex Main Agent         Personal Console
            │
      Think / Plan / Tool
      Observe / Reason
            │
 ┌──────────┼────────────────────────────────────┐
 │          │              │                     │
 ▼          ▼              ▼                     ▼
Context   Trigger       Runbook              Knowledge
Graph     Center        Engine               Brain
 │          │              │                     │
 └──────────┴───────┬──────┴─────────────────────┘
                    ▼
              AI Task Engine
                    ▼
             Evidence Ledger
                    ▼
              Reviewer Agent
                    ▼
              Action Plan
                    ▼
              Policy Engine
               ↙          ↘
          自动允许       等待审批
               ↘          ↙
                 Executor
                    ▼
                 Verifier
                    ▼
               Postmortem
                    ▼
         Runbook / Knowledge / Learning
                    ▼
             Context Graph 更新
```

下面接公司现有真实系统：

```text
公司运维平台
阿里云 / 多云
ACK / Kubernetes
GitLab / GitHub
Jenkins / GitLab CI
ArgoCD / Helm
Prometheus
SLS
ARMS / OpenTelemetry
CMDB
工单系统
配置中心
数据库
Redis
MQ
DNS
CDN
SLB
飞书
其他内部系统
```

---

# 4. 不重复造轮子的原则

本平台只自研真正属于“微派 AI 运维大脑”的部分。

成熟能力尽量复用。

## 直接复用现有系统

- 公司运维平台 → CMDB、资源、发布、工单、权限
- Prometheus → Metrics
- SLS → Logs
- ARMS → Trace
- Kubernetes → Runtime
- Git → Code / Change
- Jenkins / ArgoCD → CI/CD / Deploy

## 可选择复用开源项目

- HolmesGPT → Kubernetes / Observability RCA 专项能力
- OpenSRE → Agent Tool Calling、Runbook、RCA 思路
- Keep → 告警聚合、去重、事件编排
- Temporal → 长生命周期 AI Task、审批、暂停、恢复、重试
- Argo Events → K8s 场景下的事件驱动

## 我们重点自研

- 微派 Context Graph
- AI Task统一模型
- Codex Main Agent
- 微派知识体系
- Runbook学习体系
- Evidence Ledger
- Reviewer Agent
- Policy Engine
- Approval Center
- Executor / Verifier
- Replay / AI评估
- Personal Console

---

# 5. 第一核心：Connector Hub

所有外部系统通过统一 Connector 接入。

例如：

```text
OpsPlatformConnector
AlibabaCloudConnector
KubernetesConnector
GitConnector
CICDConnector
PrometheusConnector
SLSConnector
ARMSConnector
TicketConnector
```

Codex不直接面对底层复杂 API。

统一暴露高级 Tool：

```text
get_service_context()
get_service_runtime()
get_dependencies()
get_recent_changes()
get_recent_deployments()
query_metrics()
query_logs()
query_traces()
query_events()
get_k8s_status()
get_cloud_resources()
search_incidents()
search_runbooks()
execute_action()
verify_action()
```

原则：

> AI只需要决定“我要查什么”，不需要知道底层系统具体怎么实现。

---

# 6. 第二核心：Discovery Engine

Discovery Engine持续自动认识公司技术环境。

自动发现：

- 业务
- 应用
- 服务
- Git仓库
- 当前版本
- 集群
- Namespace
- Deployment
- Pod
- ECS
- RDS
- Redis
- MQ
- SLB
- VPC
- DNS
- CDN
- Pipeline
- 负责人
- 上下游关系
- 监控
- 日志
- Trace

并自动建立关联。

例如：

```text
payment-service
→ 属于充值业务
→ Git repo/payment
→ v2.3.7
→ ACK prod-cluster
→ 8 Pods
→ payment-db
→ payment-cache
→ payment-topic
→ gateway调用它
→ 它调用user-service
```

---

# 7. 第三核心：Context Graph

不重新建设第二套 CMDB。

公司 CMDB仍然是事实来源之一。

我们的平台建立：

# AI Context Graph

它把不同数据源连接成 AI 可以理解的关系模型。

每条关系必须有：

```text
source
confidence
first_seen
last_seen
freshness
```

例如：

```text
payment-service
CALLS
user-service
source = ARMS
confidence = 0.99
last_seen = 10 seconds ago
```

因此 AI知道哪些是事实，哪些可能已经过期。

---

# 8. Change Timeline

统一记录整个环境发生的变化：

```text
Git Commit
Merge Request
Build
Image
Release
ArgoCD Sync
Config Change
SQL
资源变更
扩缩容
网络变更
权限变更
```

形成：

```text
18:01 Commit
18:04 Merge
18:07 Build
18:09 Image
18:13 ArgoCD Sync
18:16 Deploy
18:19 P99异常
18:21 5xx异常
```

Codex排障时优先查询：

```text
get_recent_changes()
```

---

# 9. 四种主动工作机制

## 事件驱动

来源：

- 告警
- 工单
- 发布
- Git
- Kubernetes Event
- 云事件
- 配置变化

流程：

```text
Event
→ OpsEvent
→ AI Task
```

## 定时驱动

例如：

```text
09:00 开工巡检
每小时 容量检查
每天 资源治理
发布后10分钟 上线验证
```

## 状态驱动

持续比较：

```text
Current State
vs
Baseline
vs
Desired State
```

发现明显偏差：

```text
→ 创建 Investigation Task
```

## 预测驱动

根据趋势提前发现：

- 容量耗尽
- 流量增长
- 成本异常
- 资源瓶颈
- 可能的稳定性风险

实现：

> 现在还没坏，也可以提前干预。

---

# 10. AI Task Engine

所有工作统一成：

# AI Task

来源可能是：

```text
Alert
Ticket
Schedule
State
Prediction
Release
Human
AI
```

统一任务状态：

```text
NEW
CONTEXT_BUILDING
RUNBOOK_MATCHING
INVESTIGATING
RCA
PLANNING
NEED_HUMAN_JUDGMENT
WAITING_INFORMATION
WAITING_APPROVAL
EXECUTING
VERIFYING
RESOLVED
FAILED
AUTOMATION_ABORTED
ESCALATED
LEARNING
CLOSED
```

因此工单、告警、巡检、发布、容量任务全部共用同一套执行引擎。

---

# 11. Codex Main Agent

Codex作为整个系统的总运维大脑。

工作方式不是一次问答，而是：

```text
THINK
↓
PLAN
↓
TOOL CALL
↓
OBSERVE
↓
REASON
↓
下一步Tool Call
↓
……
↓
Evidence充分
↓
Conclusion
```

例如：

```text
5xx异常
↓
get_service_context()
↓
get_recent_changes()
↓
query_metrics()
↓
query_logs()
↓
query_traces()
↓
get_rds_status()
↓
compare_versions()
↓
形成RCA
```

HolmesGPT / OpenSRE可以作为专家能力被主 Agent 调用。

---

# 12. 多 Agent 模型

不做几十个自治 Agent。

采用：

# 一个主 Agent + 按需专家 Agent

例如：

```text
Codex Main Agent
├── Kubernetes Expert
├── Database Expert
├── Network Expert
├── Release Expert
├── Security Expert
├── Cost Expert
└── HolmesGPT / OpenSRE Expert
```

只有复杂问题才会调用专家。

最终决策仍由 Main Agent负责。

---

# 13. Runbook Engine

所有任务优先：

```text
search_runbooks()
```

如果有匹配：

```text
验证适用条件
→ 使用Runbook
```

如果不适用：

```text
退出Runbook
→ Codex自主调查
```

如果最终解决了新问题：

```text
Incident
→ Postmortem
→ Runbook Draft
```

Runbook成熟度：

```text
Draft
↓
Reviewed
↓
Verified
↓
Semi-Automated
↓
Approval-Automated
↓
Self-Healing
```

Runbook必须记录：

- 适用条件
- 排除条件
- 诊断步骤
- 处理步骤
- 风险等级
- 回滚方案
- 验证方式
- 历史成功次数
- 失败次数
- 可信度
- 自动化等级

---

# 14. Evidence Ledger

所有 AI判断必须有证据链。

例如：

```text
Conclusion:
v2.3.7导致DB连接耗尽
Evidence:
E1
22:27部署v2.3.7
E2
22:30开始出现5xx
E3
SLS大量出现connection timeout
E4
RDS connections达到98%
E5
Git Diff显示连接池50→500
E6
ARMS Trace显示请求阻塞在RDS
```

原则：

> 不能因为“模型感觉像”，就直接下生产结论。

---

# 15. Reviewer Agent

关键故障、关键发布、重大变更：

主 Agent给出结论后，再启动 Reviewer。

Reviewer的任务：

> 尝试证明主 Agent 是错的。

例如：

```text
主Agent：
数据库导致故障
Reviewer：
检查是否可能是网络、Redis、发布、第三方依赖
```

只有证据仍然成立，才提升结论置信度。

由于公司 AI 网关基本不考虑 Token成本，可以充分采用这种多轮交叉验证模式。

---

# 16. Policy Engine

所有动作必须分风险等级：

```text
L0
只读查询
L1
低风险动作
L2
有限变更
L3
生产重要变更
L4
高风险生产操作
L5
破坏性 / 不可逆操作
```

例如：

```text
query_logs → L0
query_metrics → L0
restart_test_service → L1
scale_prod_service → L3
rollback_prod → L3
execute_prod_sql → L4
delete_database → L5
```

Codex不能绕过 Policy Engine。

---

# 17. Human Judgment 和 Approval 分开

## NEED_HUMAN_JUDGMENT

AI不知道哪个决策才真正正确。

例如：

- 业务取舍
- 成本和体验取舍
- 组织关系
- 公司隐性规则
- 现实信息缺失

需要我判断。

## WAITING_APPROVAL

AI已经知道应该怎么做。

只是：

> 操作风险高，需要我的明确授权。

这两个状态永久区分。

---

# 18. 权限体系

永久原则：

> AI计算可以激进，生产权限必须保守。

身份至少拆成：

```text
AI Reader
AI Executor
```

推荐生产执行使用：

```text
短时凭证
+
最小权限
+
动作级授权
```

例如批准：

```text
payment-service
v2.3.7 → v2.3.6
```

临时凭证只能完成这个动作。

不能操作其他服务。

---

# 19. Executor

真正执行：

- Kubernetes操作
- 发布
- 回滚
- 扩缩容
- 配置修改
- Pipeline
- 权限
- SQL
- 云资源

但 Executor不做决策。

只接受已经通过 Policy 的 Action。

---

# 20. Verifier

Executor成功 ≠ 任务成功。

执行后必须独立验证：

```text
Deployment状态
Pod状态
业务指标
5xx
P99
成功率
Trace
日志
资源状态
```

成功：

```text
RESOLVED
```

失败：

```text
重新进入INVESTIGATING
```

---

# 21. 自动熔断机制

AI不能无限尝试。

以下情况自动停止：

- 连续操作失败
- 指标继续恶化
- 影响范围扩大
- 证据出现冲突
- Runbook连续失败
- 超过最大Action次数

然后：

```text
AUTOMATION_ABORTED
→ Human Takeover
```

---

# 22. Postmortem Engine

每次事故结束自动输出：

```text
事件现象
影响范围
Timeline
证据链
根因
处理过程
验证结果
为什么没有提前发现
监控改进
告警改进
架构改进
自动化建议
Runbook变更
```

不仅生成报告，还要自动创建改进任务。

---

# 23. Knowledge Brain

保存机器无法自动获得的知识：

- 业务规则
- 公司规范
- SOP
- 架构文档
- 历史经验
- 特殊限制
- 团队约定
- 负责人变化
- 活动信息
- 业务优先级

原则：

> 机器事实自动同步，人只补机器无法知道的知识。

---

# 24. 工单系统

工单进入：

```text
Ticket
↓
AI分类
↓
自动补Context
↓
检查信息完整性
↓
Runbook
↓
Codex分析
↓
Human Judgment?
↓
Policy
↓
Approval?
↓
Executor
↓
Verifier
↓
自动回填工单
↓
关闭
↓
学习
```

支持：

- SQL / 数据工单
- 权限工单
- 资源申请
- 配置变更
- 业务咨询
- 故障工单
- 发布工单

---

# 25. 发布与变更

完整链路：

```text
发布申请
↓
Git Diff
↓
影响面分析
↓
SQL检查
↓
资源检查
↓
监控检查
↓
回滚检查
↓
灰度方案
↓
审批
↓
发布
↓
实时盯盘
↓
异常判断
↓
暂停 / 回滚
↓
上线验证
↓
报告
```

---

# 26. 巡检

定时任务自动执行：

```text
服务健康
K8s
云资源
数据库
Redis
MQ
磁盘
网络
监控
告警
日志
Trace
证书
DNS
容量
成本
安全
```

只向我暴露异常和需要判断的内容。

---

# 27. 架构评审

输入：

```text
技术方案
+
当前微派环境
+
公司规范
+
历史故障
```

AI评审：

- 稳定性
- 高可用
- 容量
- Kubernetes
- 云资源
- 网络
- 存储
- 安全
- 成本
- 运维复杂度
- 可观测性
- 发布和回滚

---

# 28. War Room / 重大保障

适用于：

- 重大活动
- 新服
- 业务迁移
- 大版本
- 高峰期

AI负责：

```text
容量评估
资源准备
监控检查
告警检查
Runbook检查
回滚检查
风险扫描
实时盯盘
异常处理
结束后资源回收
保障报告
```

---

# 29. Governance Engine

持续发现：

## 稳定性

- 单点
- 无PDB
- 无HPA
- 缺少副本
- 无Runbook
- 无监控

## 容量

- 资源瓶颈
- 增长趋势
- 未来耗尽时间

## 安全

- 权限过大
- 凭证风险
- 公网暴露
- 安全组风险

## 成本

- 闲置资源
- 低利用率
- 过度配置
- 临时资源未回收

---

# 30. Automation Discovery

系统自己统计重复劳动：

```text
重复工单
重复故障
重复Runbook
重复发布检查
重复人工操作
```

然后提出：

```text
是否可以脚本化？
是否可以Workflow化？
是否可以自动Runbook？
是否可以Self-Healing？
```

平台不仅代替我工作，还持续减少工作本身。

---

# 31. Replay Engine

历史Incident可以重新回放。

例如：

```text
只提供当时已有的数据
↓
让新版本Agent重新调查
↓
比较：
是否找到正确根因
Tool Call数量
耗时
误判
```

用于：

- Prompt升级
- 模型升级
- Tool升级
- Runbook升级

不用拿生产环境做实验。

---

# 32. AI能力评价体系

平台持续记录：

```text
RCA命中率
Runbook命中率
自动处理成功率
审批拒绝率
人工接管率
误报率
平均MTTR
平均Tool Call
验证失败率
自动化覆盖率
```

目标：

> 能量化证明 AI到底有没有越来越接近真正业务运维工程师。

---

# 33. 7×24运行模式

平台部署在云端。

24小时运行的是：

```text
Connector
Watcher
Webhook
Scheduler
State Engine
Prediction Engine
Task Worker
Agent Runtime
Policy
Executor
Verifier
```

不是让模型24小时不停思考。

而是：

```text
平时监听
↓
有事件
↓
唤醒Agent
↓
完成任务
↓
休眠
```

电脑关机不影响平台。

---

# 34. 前端最终主要页面

平台控制台最终只需要围绕这些核心入口：

```text
总览 Dashboard
AI Task Center
Incident Center
Service / Context Graph
Event Center
Release Center
Inspection Center
Ticket Center
Runbook Center
Risk Center
War Room
Architecture Review
Automation Center
Approval Center
Knowledge Center
Audit Center
AI Chat
```

---

# 36. 最终设计原则

永久遵守：

> 能从真实系统获取，就不要让我手工告诉 AI。
> 原始事实留在原系统，本平台维护关系和认知，不重新复制所有系统。
> AI调查可以积极，生产执行必须保守。
> 所有判断必须有证据。
> 所有高风险动作必须经过 Policy。
> 执行成功不等于任务成功，必须独立验证。
> 相同问题不能永远重复从零调查。
> 每次人工判断都要尽量转化为下一次可以自动判断的知识或规则。
> 每次故障都要考虑如何避免下一次发生。
> 每次重复工作都要考虑自动化。
> AI可以调用专家 Agent，但最终必须有一个主 Agent负责结论。
> 公司现有能力能复用就复用，不重复造轮子。
> 平台的目标不是展示 AI，而是真正完成我的业务运维工作。

---

# 37. 最终平台本质

最终整个系统只有一个闭环：

```text
CONNECT
连接微派
↓
UNDERSTAND
理解微派
↓
SENSE
感知变化
↓
THINK
AI调查和推理
↓
DECIDE
Runbook + Evidence + Reviewer + Policy
↓
ACT
审批 / 执行
↓
VERIFY
验证结果
↓
LEARN
复盘 / 知识 / Runbook / 自动化
↓
重新更新对微派的理解
```

不断循环。

最终目标：

# AI承担绝大多数可标准化、可数字化、可验证的业务运维工作。

我负责：

# 特殊业务判断、组织协作以及真正高风险的最终决策。
