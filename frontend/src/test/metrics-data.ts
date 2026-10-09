import type { MetricsReport } from '../api/generated/types.gen';

export const emptyMetrics: MetricsReport = { samples: [], metrics: [
  { name: 'rca_hit_rate', label: 'RCA命中率', numerator: 0, denominator: 0, value: null, unit: 'ratio' },
  { name: 'runbook_hit_rate', label: 'Runbook命中率', numerator: 0, denominator: 0, value: null, unit: 'ratio' },
  { name: 'automatic_success_rate', label: '自动处理成功率', numerator: 0, denominator: 0, value: null, unit: 'ratio' },
  { name: 'approval_rejection_rate', label: '审批拒绝率', numerator: 0, denominator: 0, value: null, unit: 'ratio' },
  { name: 'human_takeover_rate', label: '人工接管率', numerator: 0, denominator: 0, value: null, unit: 'ratio' },
  { name: 'false_alert_rate', label: '误报率', numerator: 0, denominator: 0, value: null, unit: 'ratio' },
  { name: 'mean_mttr', label: '平均MTTR', numerator: 0, denominator: 0, value: null, unit: 'seconds' },
  { name: 'mean_tool_calls', label: '平均Tool Call', numerator: 0, denominator: 0, value: null, unit: 'calls' },
  { name: 'verification_failure_rate', label: '验证失败率', numerator: 0, denominator: 0, value: null, unit: 'ratio' },
  { name: 'automation_coverage', label: '自动化覆盖率', numerator: 0, denominator: 0, value: null, unit: 'ratio' },
] };
