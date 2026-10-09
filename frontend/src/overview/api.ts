import { apiClient } from '../api/client';
import { auditApiAuditsAuditIdGet, auditsApiAuditsGet, metricsApiMetricsGet } from '../api/generated/sdk.gen';
import type { AuditsApiAuditsGetData, MetricsApiMetricsGetData, TaskStatus } from '../api/generated/types.gen';
import { ReadError, tasks } from '../task-center/api';

export const activeStatuses = ['NEW', 'CONTEXT_BUILDING', 'RUNBOOK_MATCHING', 'INVESTIGATING', 'RCA',
  'PLANNING', 'EXECUTING', 'VERIFYING', 'RESOLVED', 'LEARNING'] satisfies TaskStatus[];

// 每个阶段分别读取 total 和最近十条，避免只筛选总列表第一页而漏算任务。
export async function activeTasks(signal: AbortSignal) {
  const pages = await Promise.all(activeStatuses.map((status) => tasks({ status, limit: 10, offset: 0 }, signal)));
  return { total: pages.reduce((sum, page) => sum + page.total, 0),
    items: pages.flatMap((page) => page.items).sort((a, b) => b.created_at.localeCompare(a.created_at) || a.id.localeCompare(b.id)).slice(0, 10),
    stages: pages.map((page, i) => ({ status: activeStatuses[i]!, total: page.total })) };
}
async function read<T>(request: Promise<{ data?: T; response?: Response }>): Promise<T> {
  const result = await request;
  if (!result.response?.ok || result.data === undefined) throw new ReadError(result.response?.status ?? 0);
  return result.data;
}
export const metrics = (query: MetricsApiMetricsGetData['query'], signal: AbortSignal) =>
  read(metricsApiMetricsGet({ client: apiClient, query, signal }));
export const audits = (query: AuditsApiAuditsGetData['query'], signal: AbortSignal) =>
  read(auditsApiAuditsGet({ client: apiClient, query, signal }));
export const audit = (id: string, signal: AbortSignal) =>
  read(auditApiAuditsAuditIdGet({ client: apiClient, path: { audit_id: id }, signal }));
