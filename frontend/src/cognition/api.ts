import { apiClient, csrfHeaders } from '../api/client';
import {
  servicesApiServicesGet, serviceApiServicesServiceNameGet, dependenciesApiServicesServiceNameDependenciesGet,
  runbooksApiRunbooksGet, runbookApiRunbooksRunbookIdGet, createRunbookApiRunbooksPost,
  updateRunbookApiRunbooksRunbookIdPut, deleteRunbookApiRunbooksRunbookIdDelete,
  knowledgeApiKnowledgeGet, knowledgeEntryApiKnowledgeEntryIdGet, createKnowledgeApiKnowledgePost,
  updateKnowledgeApiKnowledgeEntryIdPut, deleteKnowledgeApiKnowledgeEntryIdDelete,
} from '../api/generated/sdk.gen';
import type { KnowledgeInput, KnowledgeType, RunbookInput, RunbookMaturity } from '../api/generated/types.gen';
import { ReadError } from '../task-center/api';

export class CatalogError extends Error {
  constructor(public readonly status: number) {
    super(status === 409 ? '名称已存在或内容发生冲突，请核对后再保存。'
      : status === 422 ? '内容不符合要求，请检查必填项、条件、风险等级与有效期。'
      : status === 404 ? '记录已不存在，请返回列表核对。'
      : status === 403 ? '请求校验失败，请刷新页面后重试。'
      : status === 401 ? '会话已失效，请重新登录。'
      : '保存结果尚未确认。请先返回列表核对是否已保存，再决定是否重试。');
  }
}

async function read<T>(request: Promise<{ data?: T; response?: Response }>): Promise<T> {
  const result = await request;
  if (!result.response?.ok || result.data === undefined) throw new ReadError(result.response?.status ?? 0);
  return result.data;
}
async function write<T>(request: Promise<{ data?: T; response?: Response }>): Promise<T> {
  const result = await request;
  if (!result.response?.ok || result.data === undefined) throw new CatalogError(result.response?.status ?? 0);
  return result.data;
}
async function remove(request: Promise<{ response?: Response }>) {
  const result = await request;
  if (result.response?.status !== 204) throw new CatalogError(result.response?.status ?? 0);
}
export const services = (offset: number, signal: AbortSignal) => read(servicesApiServicesGet({
  client: apiClient, query: { limit: 20, offset }, signal,
}));
export const graph = (name: string, hops: number, direction: 'both' | 'upstream' | 'downstream', signal: AbortSignal) =>
  direction === 'both' ? read(serviceApiServicesServiceNameGet({ client: apiClient, path: { service_name: name }, query: { hops }, signal }))
    : read(dependenciesApiServicesServiceNameDependenciesGet({ client: apiClient, path: { service_name: name }, query: { hops, direction }, signal }));
export const runbooks = (offset: number, maturity: RunbookMaturity | undefined, signal: AbortSignal) =>
  read(runbooksApiRunbooksGet({ client: apiClient, query: { limit: 20, offset, maturity }, signal }));
export const knowledge = (offset: number, kind: KnowledgeType | undefined, signal: AbortSignal) =>
  read(knowledgeApiKnowledgeGet({ client: apiClient, query: { limit: 20, offset, kind }, signal }));
export const runbook = (id: string, signal: AbortSignal) => read(runbookApiRunbooksRunbookIdGet({ client: apiClient, path: { runbook_id: id }, signal }));
export const entry = (id: string, signal: AbortSignal) => read(knowledgeEntryApiKnowledgeEntryIdGet({ client: apiClient, path: { entry_id: id }, signal }));
export const saveRunbook = (body: RunbookInput, id?: string) => write(id
  ? updateRunbookApiRunbooksRunbookIdPut({ client: apiClient, path: { runbook_id: id }, body, headers: csrfHeaders() })
  : createRunbookApiRunbooksPost({ client: apiClient, body, headers: csrfHeaders() }));
export const saveKnowledge = (body: KnowledgeInput, id?: string) => write(id
  ? updateKnowledgeApiKnowledgeEntryIdPut({ client: apiClient, path: { entry_id: id }, body, headers: csrfHeaders() })
  : createKnowledgeApiKnowledgePost({ client: apiClient, body, headers: csrfHeaders() }));
export const deleteRunbook = (id: string) => remove(deleteRunbookApiRunbooksRunbookIdDelete({ client: apiClient, path: { runbook_id: id }, headers: csrfHeaders() }));
export const deleteKnowledge = (id: string) => remove(deleteKnowledgeApiKnowledgeEntryIdDelete({ client: apiClient, path: { entry_id: id }, headers: csrfHeaders() }));
