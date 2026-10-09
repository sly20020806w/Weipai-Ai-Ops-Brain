import { apiClient } from '../api/client';
import {
  evidenceDetailApiEvidenceEvidenceIdGet, eventDetailApiEventsEventIdGet,
  incidentDetailApiIncidentsIncidentIdGet, listEventsApiEventsGet, listIncidentsApiIncidentsGet,
  listTasksApiTasksGet, taskDetailApiTasksTaskIdGet, taskEvidenceApiTasksTaskIdEvidenceGet,
  taskHistoryApiTasksTaskIdStatusHistoryGet, taskToolCallsApiTasksTaskIdToolCallsGet,
} from '../api/generated/sdk.gen';
import type { ListEventsApiEventsGetData, ListIncidentsApiIncidentsGetData, ListTasksApiTasksGetData } from '../api/generated/types.gen';

export class ReadError extends Error {
  constructor(public readonly status: number) {
    super(status === 404 ? '记录不存在或已不可用。' : status === 422 ? '查询条件无效，请重置筛选或检查链接。'
      : status === 401 ? '会话已失效，请重新登录。' : '查询暂时不可用，请稍后重试。');
  }
}

async function read<T>(request: Promise<{ data?: T; response?: Response }>): Promise<T> {
  const result = await request;
  if (!result.response?.ok || result.data === undefined) throw new ReadError(result.response?.status ?? 0);
  return result.data;
}

export const tasks = (query: ListTasksApiTasksGetData['query'], signal: AbortSignal) =>
  read(listTasksApiTasksGet({ client: apiClient, query, signal }));
export const events = (query: ListEventsApiEventsGetData['query'], signal: AbortSignal) =>
  read(listEventsApiEventsGet({ client: apiClient, query, signal }));
export const incidents = (query: ListIncidentsApiIncidentsGetData['query'], signal: AbortSignal) =>
  read(listIncidentsApiIncidentsGet({ client: apiClient, query, signal }));
export const task = (id: string, signal: AbortSignal) =>
  read(taskDetailApiTasksTaskIdGet({ client: apiClient, path: { task_id: id }, signal }));
export const event = (id: string, signal: AbortSignal) =>
  read(eventDetailApiEventsEventIdGet({ client: apiClient, path: { event_id: id }, signal }));
export const incident = (id: string, signal: AbortSignal) =>
  read(incidentDetailApiIncidentsIncidentIdGet({ client: apiClient, path: { incident_id: id }, signal }));
export const evidence = (id: string, signal: AbortSignal) =>
  read(evidenceDetailApiEvidenceEvidenceIdGet({ client: apiClient, path: { evidence_id: id }, signal }));
export const history = (id: string, signal: AbortSignal) =>
  read(taskHistoryApiTasksTaskIdStatusHistoryGet({ client: apiClient, path: { task_id: id }, signal }));
export const toolCalls = (id: string, offset: number, signal: AbortSignal) =>
  read(taskToolCallsApiTasksTaskIdToolCallsGet({ client: apiClient, path: { task_id: id }, query: { limit: 20, offset }, signal }));

// 查询接口按采集时间升序分页；读完各页才能展示后续 RCA，不能假设结论在第一页。
export async function taskEvidence(id: string, signal: AbortSignal) {
  const first = await read(taskEvidenceApiTasksTaskIdEvidenceGet({ client: apiClient,
    path: { task_id: id }, query: { limit: 100, offset: 0 }, signal }));
  const items = [...first.items];
  for (let offset = 100; offset < first.total; offset += 100) {
    const next = await read(taskEvidenceApiTasksTaskIdEvidenceGet({ client: apiClient,
      path: { task_id: id }, query: { limit: 100, offset }, signal }));
    items.push(...next.items);
  }
  return items;
}
