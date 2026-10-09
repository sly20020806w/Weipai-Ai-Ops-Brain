import type { EventView, EvidenceView, HistoryView, IncidentHit, TaskView, ToolCallView } from '../api/generated/types.gen';

export const taskId = '10000000-0000-4000-8000-000000000001';
export const eventId = '20000000-0000-4000-8000-000000000001';
export const evidenceId = '30000000-0000-4000-8000-000000000001';
export const conclusionId = '30000000-0000-4000-8000-000000000002';
export const incidentId = '30000000-0000-4000-8000-000000000003';
const time = '2026-10-08T03:00:00Z';
export const fakeTask: TaskView = { id: taskId, title: '支付服务 5xx 告警调查', source: 'Alert', status: 'CLOSED',
  status_version: 3, created_at: time, updated_at: time };
export const fakeEvent: EventView = { id: eventId, task_id: taskId, title: '支付告警事件', source: 'Alert', origin: 'prometheus',
  external_id: 'fake-alert-1', service_name: 'payment-service', fingerprint: 'fake-fingerprint', occurred_at: time, created_at: time };
export const fakeEvidence: EvidenceView = { id: evidenceId, task_id: taskId, source_tool: 'query_metrics',
  parameters: { service_name: 'payment-service' }, result_snapshot: { error_rate: 0.15 }, source_reference: 'fake://metrics/payment', collected_at: time, created_at: time };
export const fakeConclusion: EvidenceView = { ...fakeEvidence, id: conclusionId, source_tool: 'agent.conclusion', result_snapshot: {
  root_cause: { statement: '连接池扩大导致连接耗尽', evidence_ids: [evidenceId] },
  findings: [{ statement: '5xx 随发布升高', evidence_ids: [evidenceId] }], confidence: 0.8, uncertainties: ['待排除网络因素'],
} };
export const fakeHistory: HistoryView[] = [
  { id: '40000000-0000-4000-8000-000000000001', task_id: taskId, sequence: 0, from_status: null, to_status: 'NEW', reason: '告警创建任务', actor: 'trigger', changed_at: time },
  { id: '40000000-0000-4000-8000-000000000002', task_id: taskId, sequence: 1, from_status: 'NEW', to_status: 'INVESTIGATING', reason: '开始调查', actor: 'main-agent', changed_at: '2026-10-08T03:01:00Z' },
];
export const fakeCall: ToolCallView = { id: '50000000-0000-4000-8000-000000000001', task_id: taskId, actor: 'main-agent',
  operation: 'query_metrics', outcome: 'success', evidence_id: evidenceId, details: { parameters: fakeEvidence.parameters, mode: 'live' }, occurred_at: time };
export const sectionTitles = ['事件现象', '影响范围', 'Timeline', '证据链', '根因', '处理过程', '验证结果',
  '为什么没有提前发现', '监控改进', '告警改进', '架构改进', '自动化建议', 'Runbook变更'];
export const fakeIncident: IncidentHit = { evidence_id: incidentId, report: { task_id: taskId, service_name: 'payment-service',
  title: '支付故障复盘', sections: sectionTitles.map((title) => ({ title, conclusions: [{ statement: `${title}：Fake 证据支持的结论`, evidence_ids: [evidenceId] }] })) as IncidentHit['report']['sections'],
  improvements: [{ statement: '完善连接数告警', evidence_ids: [evidenceId] }],
  timeline: [{ occurred_at: time, description: '支付服务部署新版本', evidence_id: evidenceId }],
  runbook_id: '60000000-0000-4000-8000-000000000001', improvement_task_ids: [taskId],
} };
