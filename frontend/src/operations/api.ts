import { apiClient } from '../api/client';
import {
  listReleases, getReleases, listTickets, getTickets, listInspections, getInspections,
  listWarRooms, getWarRooms, listArchitectureReviews, getArchitectureReviews,
  listAutomations, getAutomations, risksApiRisksGet, riskApiRisksRiskIdGet,
} from '../api/generated/sdk.gen';
import type { ListReleasesData, RisksApiRisksGetData } from '../api/generated/types.gen';
import { ReadError } from '../task-center/api';

export const centers = {
  releases: { title: '发布中心', description: '查看发布检查、灰度、盯盘、回滚与上线验证。', endpoint: 'releases', list: listReleases, detail: getReleases },
  tickets: { title: '工单中心', description: '跟进分类、补充信息、处理、独立验证与工单回填。', endpoint: 'tickets', list: listTickets, detail: getTickets },
  inspections: { title: '巡检中心', description: '查看服务健康检查与治理报告，跟进已发现的风险。', endpoint: 'inspections', list: listInspections, detail: getInspections },
  'war-room': { title: '重大保障', description: '跟进容量评估、资源准备、实时盯盘与回收报告。', endpoint: 'war-rooms', list: listWarRooms, detail: getWarRooms },
  architecture: { title: '架构评审', description: '结合环境、规范与历史故障查看十二个维度的评审。', endpoint: 'architecture-reviews', list: listArchitectureReviews, detail: getArchitectureReviews },
  automation: { title: '自动化中心', description: '查看重复劳动的记录与自动化建议，跟进关联任务。', endpoint: 'automations', list: listAutomations, detail: getAutomations },
} as const;
export type Center = keyof typeof centers;

async function read<T>(request: Promise<{ data?: T; response?: Response }>): Promise<T> {
  const result = await request;
  if (!result.response?.ok || result.data === undefined) throw new ReadError(result.response?.status ?? 0);
  return result.data;
}
export const scenarios = (center: Center, query: ListReleasesData['query'], signal: AbortSignal) =>
  read(centers[center].list({ client: apiClient, query, signal }));
export const scenario = (center: Center, id: string, signal: AbortSignal) =>
  read(centers[center].detail({ client: apiClient, path: { task_id: id }, signal }));
export const risks = (query: RisksApiRisksGetData['query'], signal: AbortSignal) =>
  read(risksApiRisksGet({ client: apiClient, query, signal }));
export const risk = (id: string, signal: AbortSignal) =>
  read(riskApiRisksRiskIdGet({ client: apiClient, path: { risk_id: id }, signal }));
