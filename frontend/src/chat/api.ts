import { apiClient, csrfHeaders, sessionExpiredEvent, setCsrfToken } from '../api/client';
import { chatAnswerApiChatTaskIdGet, chatApiChatPost } from '../api/generated/sdk.gen';
import type { ChatAnswer, ChatInput, EventReceipt } from '../api/generated/types.gen';

export const isUuid = (value: unknown): value is string => typeof value === 'string'
  && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
const object = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === 'object';
const references = (value: unknown): value is string[] => Array.isArray(value) && value.every(isUuid);

// SSE 在 OpenAPI 中为 unknown；边界核验后使用生成的契约类型。
function isAnswer(value: unknown): value is ChatAnswer {
  return object(value) && isUuid(value.task_id) && typeof value.status === 'string' && typeof value.pending === 'boolean'
    && (value.answer == null || typeof value.answer === 'string')
    && (value.evidence_ids === undefined || references(value.evidence_ids))
    && ['conclusion_evidence_id', 'review_evidence_id', 'plan_evidence_id'].every((field) => value[field] == null || isUuid(value[field]))
    && (value.policy_decision == null || typeof value.policy_decision === 'string');
}

export class ChatError extends Error {
  constructor(public readonly status = 0) {
    super(status === 401 ? '会话已失效，请重新登录。' : status === 403 ? '请求校验失败，请刷新后重试。'
      : status === 404 ? '对话或上一轮回答不存在，请检查链接或开始新对话。'
      : status === 409 ? '原请求内容存在冲突，请核对任务后开始新问题。'
      : status === 422 ? '输入无效，请检查服务、问题与 UTC 时间窗。'
      : '回答连接中断或暂时不可用。可读取已保存回答，或按原内容重试。');
  }
}

export async function readAnswer(taskId: string, signal?: AbortSignal): Promise<ChatAnswer> {
  const { data, response } = await chatAnswerApiChatTaskIdGet({ client: apiClient, path: { task_id: taskId }, signal });
  if (!response?.ok || !isAnswer(data)) throw new ChatError(response?.status ?? 0);
  return data;
}

export async function streamAnswer(input: ChatInput, signal: AbortSignal, handlers: {
  task: (receipt: EventReceipt) => void; evidence: (ids: string[]) => void;
  delta: (text: string) => void; done: (answer: ChatAnswer) => void;
}): Promise<void> {
  let taskId: string | undefined, completed = false, failure: unknown;
  const { stream } = await chatApiChatPost({ client: apiClient, body: input, headers: csrfHeaders(), signal,
    sseMaxRetryAttempts: 1, // 重发完整回答，由用户选择原请求重试，避免自动叠加文字。
    fetch: async (request) => {
      const response = await globalThis.fetch(request);
      // 生成的 SSE 客户端不调用 response interceptor；在此复用会话失效约定。
      if (response.status === 401 && !signal.aborted) {
        setCsrfToken(undefined); window.dispatchEvent(new Event(sessionExpiredEvent));
      }
      if (!response.ok) throw new ChatError(response.status);
      if (!response.headers.get('Content-Type')?.startsWith('text/event-stream')) throw new ChatError();
      return response;
    },
    onSseError: (error) => { failure = error; },
    onSseEvent: ({ event, data }) => {
      if (signal.aborted || data === undefined) return;
      if (completed || !object(data)) throw new ChatError();
      if (event === 'task') {
        if (taskId || !isUuid(data.task_id) || !isUuid(data.event_id) || typeof data.workflow_id !== 'string'
          || typeof data.duplicate !== 'boolean') throw new ChatError();
        taskId = data.task_id;
        handlers.task({ task_id: taskId, event_id: data.event_id, workflow_id: data.workflow_id, duplicate: data.duplicate });
      } else if (!taskId) throw new ChatError();
      else if (event === 'evidence' && references(data.evidence_ids)) handlers.evidence(data.evidence_ids);
      else if (event === 'delta' && typeof data.text === 'string') handlers.delta(data.text);
      else if (event === 'done' && isAnswer(data) && data.task_id === taskId) { completed = true; handlers.done(data); }
      else throw new ChatError();
    },
  });
  for await (const payload of stream) { void payload; }
  if (signal.aborted) throw new DOMException('连接已停止', 'AbortError');
  if (failure || !completed) throw failure instanceof ChatError ? failure : new ChatError();
}
