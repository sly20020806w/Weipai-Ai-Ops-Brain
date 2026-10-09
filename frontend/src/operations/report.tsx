import { EvidenceLink, Time } from '../task-center/shared';

const labels: Record<string, string> = {
  task_id: '任务', service_name: '服务', title: '标题', phase_version: '阶段版本', mode: '检查范围',
  checks: '检查项目', label: '检查项', check_id: '检查标识', resource: '资源', area: '范围', category: '类别',
  outcome: '结果', reason: '依据', evidence_id: '证据', evidence_ids: '证据引用',
  observed_at: '观察时间', assessed_at: '评估时间', reviewed_at: '评审时间', occurred_at: '发生时间',
  source_reference: '源系统引用', runbook_evidence_ids: '手册匹配证据',
  root_cause: '根因', statement: '结论', findings: '调查发现', uncertainties: '待核实事项', confidence: '置信度',
  sections: '报告章节', conclusions: '结论', dimensions: '评审维度', dimension: '维度', finding: '发现',
  recommendation: '建议', citations: '引用', quote: '原文摘录', sources: '来源证据',
  proposal: '技术方案', runbooks: '运行手册', context: '环境上下文', standards: '公司规范', incidents: '历史故障',
  actions: '动作', action_id: '动作标识', action_name: '动作', target: '目标', parameters: '参数',
  risk_level: '风险等级', policy: '策略判定', decision: '判定', rollback: '回滚方案', verification: '验证方式',
  description: '说明', trigger: '触发条件', preconditions: '前提条件', timeline: '时间线',
  complete: '信息完整', passed: '验证通过', safe: '安全检查通过', clear: '复核通过',
  required_replicas: '所需副本数', baseline: '原始基线', purpose: '用途', alternatives: '替代原因',
  capacity_known: '容量数据已知', anomaly: '发现异常', ownership_matches: '资源归属一致',
  records: '原始记录', record_id: '记录标识', table: '记录来源', signature: '重复工作内容',
  kind: '工作类型', method: '建议方式', count: '重复次数', threshold: '阈值', suggestion: '建议',
  operation: '操作', actor: '操作人', start: '开始时间', end: '结束时间', window: '时间窗',
  ticket_id: '工单标识', ticket: '工单', classification: '分类', missing_fields: '缺失信息',
  conclusion: '结论', status: '状态', receipts: '关联任务', report_evidence_id: '报告证据',
  runbook_id: '手册标识', improvement_task_ids: '改进任务', verified_at: '验证时间',
  name: '名称', repetition: '重复劳动', executions: '执行回执', anomaly_tasks: '异常处置任务',
  stages: '发布阶段', assessments: '阶段评估', monitoring: '盯盘记录', sql: 'SQL 检查',
  replicas: '副本数', image: '镜像', environment: '环境', namespace: '命名空间',
};
const values: Record<string, string> = {
  abnormal: '异常', unknown: '待核实', healthy: '健康', risk: '风险', supported: '有依据',
  stability: '稳定性', capacity: '容量', security: '安全', cost: '成本',
  allow: '允许', need_approval: '需要审批', deny: '禁止', prepare: '资源准备', watch: '盯盘', cleanup: '资源回收',
  script: '评估脚本化', workflow: '评估工作流化', automatic_runbook: '评估自动运行手册', self_healing: '评估自愈',
};
const idPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const internalFields = new Set(['rule_fingerprint', 'config_hash', 'submission_hash', 'group_key', 'phase_version', 'verifying_version']);

// 报告是后端生成的 JsonValue；未知字段保留原文，不把缺失事实推断为成功。
export function ReportSnapshot({ value, field = '', references = false }: { value: unknown; field?: string; references?: boolean }) {
  if (typeof value === 'string') {
    if ((references || /(^evidence_ids?$|_evidence_ids?$|^observed_ids$)/.test(field)) && idPattern.test(value)) return <EvidenceLink id={value} />;
    if (/(_at$|^start$|^end$)/.test(field) && /^\d{4}-\d{2}-\d{2}T/.test(value)) return <Time value={value} />;
    const enumField = ['outcome', 'category', 'method', 'decision', 'purpose'].includes(field);
    return <span className="snapshot-text">{enumField ? values[value] ?? value : value}</span>;
  }
  if (Array.isArray(value)) return value.length ? <ol className="snapshot-list">{value.map((item, index) =>
    <li key={index}><ReportSnapshot value={item} field={field} references={references} /></li>)}</ol> : <span>无记录</span>;
  if (value && typeof value === 'object') return <dl className="snapshot-object">{Object.entries(value).filter(([key]) => !internalFields.has(key)).map(([key, item]) =>
    <div key={key}><dt>{labels[key] ?? key}</dt><dd><ReportSnapshot value={item} field={key} references={references || key === 'sources'} /></dd></div>)}</dl>;
  return <span>{value === null ? '未提供' : typeof value === 'boolean' ? value ? '是' : '否' : String(value)}</span>;
}
