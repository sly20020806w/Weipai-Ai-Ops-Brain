import type { AuditView, MetricValue } from '../api/generated/types.gen';

export const auditLabels = { tool_call: 'Tool 调用', state_transition: '状态迁移', approval: '审批',
  execution: '动作执行', human_interaction: '人工交互', catalog_edit: '内容编辑' } satisfies Record<AuditView['event_type'], string>;
export function metricText(metric: MetricValue): string {
  if (metric.value === null) return '暂无可评价样本';
  const value = (metric.unit === 'ratio' ? metric.value * 100 : metric.value).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
  return `${value}${metric.unit === 'ratio' ? '%' : metric.unit === 'seconds' ? ' 秒' : metric.unit === 'calls' ? ' 次' : ` ${metric.unit}`}`;
}
