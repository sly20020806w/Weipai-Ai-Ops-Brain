import type { GraphContext, KnowledgeView, RunbookView } from '../api/generated/types.gen';

export const guideId = '50000000-0000-4000-8000-000000000001';
export const ruleId = '50000000-0000-4000-8000-000000000002';
export const fakeGraph: GraphContext = {
  service_name: 'payment-service', as_of: '2026-10-08T02:00:00Z',
  nodes: [{ id: guideId, kind: 'service', name: 'payment-service', external_id: 'payment-service' },
    { id: ruleId, kind: 'rds', name: 'payment-db', external_id: 'rds/payment-db' }],
  edges: [{ id: 'edge-1', from_node_id: guideId, to_node_id: ruleId, relation: 'USES', source: 'Fake ARMS', confidence: 0.99,
    first_seen: '2026-10-07T00:00:00Z', last_seen: '2026-10-08T01:59:30Z', freshness_seconds: 30 }],
};
export const fakeGuide: RunbookView = {
  id: guideId, name: 'payment-check', description: '检查支付连接池', source: '历史事故复盘',
  applicability_conditions: [{ field: 'service_name', operator: 'equals', value: 'payment-service' }],
  exclusion_conditions: [{ field: 'title', operator: 'contains', value: '网络维护' }],
  diagnostic_steps: [{ description: '查看服务上下文', tool_name: 'get_service_context', parameters: { service_name: 'payment-service' }, risk_level: 'L0' }],
  handling_steps: [{ description: '精确审批后回滚版本', risk_level: 'L3' }], risk_level: 'L3',
  rollback_plan: '保留当前版本恢复方案', verification_steps: ['独立验证错误率恢复', '独立验证 P99 恢复'],
  maturity: 'verified', automation_level: 'manual', success_count: 3, failure_count: 0, confidence: 0.8, content_version: 2,
  created_at: '2026-10-07T00:00:00Z', updated_at: '2026-10-08T00:00:00Z', embedding_model: 'fake', embedding_dimensions: 4,
};
export const fakeRule: KnowledgeView = {
  id: ruleId, kind: 'business_rule', content: '支付链路优先保障', source: '本人业务约定',
  valid_from: '2026-10-07T00:00:00Z', expires_at: '2026-12-31T00:00:00Z', created_at: '2026-10-07T00:00:00Z',
  updated_at: '2026-10-08T00:00:00Z', embedding_model: 'fake', embedding_dimensions: 4,
};
