# Weipai AI Ops Brain — 项目核心规格（SPEC）

> 提炼自《Weipai AI Ops Brain 最终设计方案 V1.0》。原方案是唯一事实来源，有出入以原方案为准。

## 1. 定位
- **是什么**：独立于公司运维平台、只供我个人使用的 AI 业务运维平台。持续连接微派真实环境，维护"微派运维实时认知模型"，由事件、时间、状态、预测主动产生任务，AI 自主调查、决策，经 Runbook、权限、审批、执行、验证、复盘、学习闭环，尽可能代替我完成业务运维。
- **不做什么**：不重建 CMDB、资源、发布、工单、权限、服务树等公司已有能力；原始事实留在原系统，本平台只维护关系和认知。
- **人的角色**：不是"我发现问题再问 AI"，而是平台自己发现、AI 自己调查判断、能自动的自动完成。我只保留：机器拿不到的现实信息、业务取舍与特殊判断、高风险生产授权、必须真人完成的沟通协作。

## 2. 一个闭环，一条主链路
CONNECT 连接 → UNDERSTAND 理解 → SENSE 感知 → THINK 调查推理 → DECIDE 决策 → ACT 审批/执行 → VERIFY 验证 → LEARN 学习 → 重新更新对微派的理解。

```text
Trigger Center + Context Graph + Runbook Engine + Knowledge Brain → AI Task Engine → Codex Main Agent 调查
→ Evidence Ledger → Reviewer Agent → Action Plan → Policy Engine → 自动允许 | 等待审批
→ Executor → Verifier → Postmortem → Runbook / Knowledge / Learning → 更新 Context Graph
```

## 3. 能力规格
- **CONNECT · Connector Hub**：所有外部系统（公司运维平台、阿里云、K8s、Git、CI/CD、Prometheus/SLS/ARMS、CMDB、工单、中间件、飞书等）统一经 Connector 接入，对 AI 只暴露高级 Tool（get_service_context、get_recent_changes、query_metrics/logs/traces、search_runbooks、execute_action、verify_action 等）。AI 只决定"查什么"。
- **UNDERSTAND · Discovery + Context Graph**：自动发现业务、服务、仓库、版本、K8s/云资源、Pipeline、负责人、上下游、监控/日志/Trace 并建立关联，形成 AI 可理解的关系模型，不是第二套 CMDB。每条关系必带 source、confidence、first_seen、last_seen、freshness，让 AI 分清事实与可能过期的信息。
- **UNDERSTAND · Change Timeline**：统一记录代码、构建、镜像、发布、Sync、配置、SQL、资源、扩缩容、网络、权限变更；排障优先查最近变更。
- **UNDERSTAND · Knowledge Brain**：只存机器无法自动获得的知识（业务规则、规范、SOP、历史经验、特殊限制、团队约定、业务优先级等）。机器事实自动同步，人只补机器不知道的。
- **SENSE · 四种主动驱动**：事件（告警、工单、发布、Git、K8s/云事件、配置变化 → OpsEvent → AI Task）、定时（开工巡检、容量检查、资源治理、发布后验证）、状态（Current vs Baseline vs Desired 明显偏差 → Investigation Task）、预测（容量、流量、成本、瓶颈趋势，没坏也提前干预）。
- **THINK · AI Task Engine**：所有工作统一为 AI Task（来源 Alert/Ticket/Schedule/State/Prediction/Release/Human/AI），共用一套引擎。统一状态：NEW、CONTEXT_BUILDING、RUNBOOK_MATCHING、INVESTIGATING、RCA、PLANNING、NEED_HUMAN_JUDGMENT、WAITING_INFORMATION、WAITING_APPROVAL、EXECUTING、VERIFYING、RESOLVED、FAILED、AUTOMATION_ABORTED、ESCALATED、LEARNING、CLOSED。
- **THINK · Codex Main Agent**：总运维大脑，以 Think → Plan → Tool Call → Observe → Reason 多轮循环调查，证据充分才下结论。一个主 Agent + 按需专家（K8s、数据库、网络、发布、安全、成本、HolmesGPT/OpenSRE），只有复杂问题才调专家，最终决策由主 Agent 负责。
- **DECIDE · Runbook Engine**：所有任务优先 search_runbooks()，验证适用条件才使用，不适用就退出转自主调查。Runbook 必须记录适用/排除条件、诊断与处理步骤、风险等级、回滚、验证方式、成功/失败次数、可信度、自动化等级。
- **DECIDE · Evidence Ledger**：所有 AI 判断必须有证据链（结论 + E1…En），不能因"模型感觉像"就下生产结论。
- **DECIDE · Reviewer Agent**：关键故障、关键发布、重大变更，由 Reviewer 专门尝试证明主 Agent 是错的，证据仍成立才提升置信度；Token 成本不是约束，可充分多轮交叉验证。
- **DECIDE · Policy Engine**：所有动作分 L0 只读、L1 低风险、L2 有限变更、L3 生产重要变更、L4 高风险生产操作、L5 破坏性/不可逆（如 query_logs L0、rollback_prod L3、execute_prod_sql L4、delete_database L5）。Codex 不能绕过 Policy。
- **DECIDE · 判断 ≠ 审批（永久区分）**：NEED_HUMAN_JUDGMENT = AI 不知道哪个决策正确（业务/成本取舍、组织关系、隐性规则、现实信息缺失）；WAITING_APPROVAL = AI 知道该怎么做，只因风险高需要我明确授权。
- **ACT · 权限与 Executor**：AI 计算可以激进，生产权限必须保守。身份至少拆为 AI Reader / AI Executor，生产执行用短时凭证 + 最小权限 + 动作级授权，凭证只能完成被批准的那一个动作。Executor 不做决策，只执行已通过 Policy 的 Action。
- **VERIFY · Verifier 与熔断**：执行成功 ≠ 任务成功，须独立验证资源状态、业务指标、5xx、P99、成功率、Trace、日志；成功 → RESOLVED，失败 → 重回 INVESTIGATING。连续操作失败、指标恶化、影响扩大、证据冲突、Runbook 连续失败、超最大 Action 次数 → AUTOMATION_ABORTED → Human Takeover。
- **LEARN · 复盘与进化**：每次事故自动 Postmortem（Timeline、证据链、根因、为何没提前发现、监控/告警/架构/自动化改进），并自动创建改进任务。新问题沉淀为 Runbook Draft，按 Draft → Reviewed → Verified → Semi-Automated → Approval-Automated → Self-Healing 成熟。Automation Discovery 统计重复劳动，提出脚本化、Workflow 化、自动 Runbook、Self-Healing，持续减少工作本身。
- **LEARN · Replay 与评价**：只用当时已有数据让新版本 Agent 重新调查历史 Incident，比较根因、Tool Call 数、耗时、误判，支撑 Prompt/模型/Tool/Runbook 升级，不拿生产做实验。持续度量 RCA 命中率、自动处理成功率、审批拒绝率、人工接管率、误报率、MTTR 等，量化 AI 是否越来越接近真正的业务运维工程师。

## 4. 业务场景
- **工单**：分类 → 补 Context → 检查信息完整性 → Runbook → 分析 → 人工判断? → Policy → 审批? → 执行 → 验证 → 自动回填关闭 → 学习。
- **发布与变更**：发布申请 → Git Diff → 影响面 → SQL/资源/监控/回滚检查 → 灰度 → 审批 → 发布 → 实时盯盘 → 暂停/回滚 → 上线验证 → 报告。
- **巡检 / 架构评审 / War Room / Governance**：定时巡检只向我暴露异常和需要判断的内容；结合当前环境、公司规范、历史故障评审技术方案；为重大活动、新服、迁移、大版本、高峰期做全程保障；持续发现稳定性、容量、安全、成本隐患。

## 5. 复用与自研
- **复用**：公司运维平台、Prometheus、SLS、ARMS、Kubernetes、Git、Jenkins/ArgoCD 直接复用；HolmesGPT、OpenSRE、Keep、Temporal、Argo Events 可选复用。
- **重点自研**：Context Graph、AI Task 统一模型、Codex Main Agent、微派知识体系、Runbook 学习体系、Evidence Ledger、Reviewer Agent、Policy Engine、Approval Center、Executor/Verifier、Replay/AI 评估、Personal Console。

## 6. 运行与控制台
- **7×24 云端**：Connector、Watcher、Webhook、Scheduler、State/Prediction Engine、Task Worker、Agent Runtime、Policy、Executor、Verifier 常驻。模型不是 24 小时思考，而是平时监听 → 有事件唤醒 Agent → 完成任务 → 休眠；我的电脑关机不影响平台。
- **Personal Console**：我在这里查看、对话、审批、接管。入口包括 Dashboard、AI Task、Incident、Service/Context Graph、Event、Release、Inspection、Ticket、Runbook、Risk、War Room、Architecture Review、Automation、Approval、Knowledge、Audit、AI Chat。

## 7. 永久原则
1. 能从真实系统获取，就不要让我手工告诉 AI；原始事实留在原系统，本平台维护关系和认知。
2. AI 调查可以积极，生产执行必须保守；所有判断必须有证据，所有高风险动作必须经过 Policy。
3. 执行成功不等于任务成功，必须独立验证。
4. 相同问题不能永远从零调查；每次人工判断都尽量转化为下一次可以自动判断的知识或规则。
5. 每次故障都要考虑如何避免下一次；每次重复工作都要考虑自动化。
6. 可以调用专家 Agent，但最终必须有一个主 Agent 负责结论。
7. 公司现有能力能复用就复用；平台的目标不是展示 AI，而是真正完成我的业务运维工作。

**最终目标**：AI 承担绝大多数可标准化、可数字化、可验证的业务运维工作；我负责特殊业务判断、组织协作以及真正高风险的最终决策。
