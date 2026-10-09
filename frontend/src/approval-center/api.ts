import { apiClient, csrfHeaders } from '../api/client';
import {
  approveApiTasksTaskIdApprovalPost, informApiTasksTaskIdInformationPost,
  interactionApiTasksTaskIdInteractionGet, judgeApiTasksTaskIdJudgmentPost, takeoverApiTasksTaskIdTakeoverPost,
} from '../api/generated/sdk.gen';
import type { AnswerInput, ApprovalInput, TakeoverInput } from '../api/generated/types.gen';
import { ReadError } from '../task-center/api';

export class ControlError extends Error {
  constructor(public readonly status: number) {
    super(status === 409 ? '任务版本或操作内容已变化，请刷新后重新核对。'
      : status === 404 ? '任务或待处理记录不存在，请刷新列表。'
      : status === 422 ? '输入格式不正确，请检查后重试。'
      : status === 403 ? '请求校验失败，请刷新页面后重试。'
      : status === 401 ? '会话已失效，请重新登录。'
      : '操作结果尚未确认。请保留当前内容并以相同内容重试；后端会去重已提交操作。');
  }
}

async function result<T>(request: Promise<{ data?: T; response?: Response }>) {
  const response = await request;
  if (!response.response?.ok || response.data === undefined) throw new ControlError(response.response?.status ?? 0);
  return response.data;
}

export async function interaction(id: string, signal: AbortSignal) {
  const response = await interactionApiTasksTaskIdInteractionGet({ client: apiClient, path: { task_id: id }, signal });
  if (!response.response?.ok || !response.data) throw new ReadError(response.response?.status ?? 0);
  return response.data;
}
export const approve = (id: string, body: ApprovalInput) => result(
  approveApiTasksTaskIdApprovalPost({ client: apiClient, path: { task_id: id }, body, headers: csrfHeaders() }));
export const answer = (id: string, kind: 'judgment' | 'information', body: AnswerInput) => result(
  kind === 'judgment' ? judgeApiTasksTaskIdJudgmentPost({ client: apiClient, path: { task_id: id }, body, headers: csrfHeaders() })
    : informApiTasksTaskIdInformationPost({ client: apiClient, path: { task_id: id }, body, headers: csrfHeaders() }));
export const takeover = (id: string, body: TakeoverInput) => result(
  takeoverApiTasksTaskIdTakeoverPost({ client: apiClient, path: { task_id: id }, body, headers: csrfHeaders() }));
