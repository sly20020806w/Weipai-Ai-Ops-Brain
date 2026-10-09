import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { http, HttpResponse } from 'msw';
import { ConsoleApp } from '../app';
import { AppProviders } from '../providers';
import { authorize, origin, requests, server } from '../test/server';
import { fakeEvidence, evidenceId, taskId } from '../test/task-data';
import type { ChatAnswer, ChatInput } from '../api/generated/types.gen';

const nextId = '63000000-0000-4000-8000-000000000001';
const answer: ChatAnswer = { task_id: taskId, status: 'CLOSED', pending: false,
  answer: `连接池变更导致耗尽。[Evidence:${evidenceId}]`, evidence_ids: [evidenceId] };
const frame = (event: string, data: unknown) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
const receipt = (id = taskId) => ({ task_id: id, event_id: nextId, workflow_id: `ai-task-${id}`, duplicate: false });
const full = (value = answer) => frame('task', receipt(value.task_id)) + frame('evidence', { evidence_ids: value.evidence_ids })
  + frame('delta', { text: value.answer ?? '' }) + frame('done', value);
const bodies: ChatInput[] = [];
function mount(path = '/chat') {
  bodies.length = 0; authorize();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const view = render(<AppProviders client={client}><MemoryRouter initialEntries={[path]}><ConsoleApp /></MemoryRouter></AppProviders>);
  return { ...view, client };
}
function respond(value = answer) {
  server.use(http.post(`${origin}/api/chat`, async ({ request }) => {
    requests.push(request); bodies.push(await request.json() as ChatInput);
    return new HttpResponse(full(value), { headers: { 'Content-Type': 'text/event-stream' } });
  }));
}
async function send(message = '支付为什么报错？') {
  await screen.findByLabelText('问题');
  fireEvent.change(screen.getByLabelText('问题'), { target: { value: message } });
  await userEvent.setup().click(screen.getByRole('button', { name: '发送问题' }));
}
async function complete() { await screen.findByText('已关闭', { exact: true }); }

describe('Step 53 AI 对话页面', () => {
  it('SSE 按段渲染，跨网络字节的中文和分段引用可点击原始 Evidence', async () => {
    let streamController!: ReadableStreamDefaultController<Uint8Array>;
    server.use(http.post(`${origin}/api/chat`, async ({ request }) => {
      requests.push(request); bodies.push(await request.json() as ChatInput);
      return new HttpResponse(new ReadableStream<Uint8Array>({ start(controller) { streamController = controller; } }),
        { headers: { 'Content-Type': 'text/event-stream' } });
    }), http.get(`${origin}/api/evidence/${evidenceId}`, () => HttpResponse.json(fakeEvidence)));
    mount(); await send();
    await waitFor(() => expect(streamController).toBeDefined());
    const push = (text: string) => streamController.enqueue(new TextEncoder().encode(text));
    await act(async () => { push(frame('task', receipt()) + ': heartbeat\n\n' + frame('evidence', { evidence_ids: [evidenceId] })); });
    const first = new TextEncoder().encode(frame('delta', { text: '第一段中文' }));
    await act(async () => { streamController.enqueue(first.slice(0, 32)); });
    await act(async () => { streamController.enqueue(first.slice(32)); });
    await screen.findByText('第一段中文', { exact: true });
    expect(screen.queryByText('已关闭', { exact: true })).not.toBeInTheDocument();
    await act(async () => { push(frame('delta', { text: '[Evidence:' })); });
    expect(screen.getByText('第一段中文[Evidence:', { exact: true })).toBeInTheDocument();
    await act(async () => { push(frame('delta', { text: evidenceId + ']第二段' })); });
    await screen.findByText('第二段', { exact: true });
    await act(async () => { push(frame('done', { ...answer, answer: `第一段中文[Evidence:${evidenceId}]第二段` })); streamController.close(); });
    await complete();
    await userEvent.setup().click(screen.getAllByRole('link', { name: `查看证据 ${evidenceId}` })[0]!);
    const drawer = await screen.findByRole('dialog');
    expect(within(drawer).getByText('query_metrics', { exact: true })).toBeInTheDocument();
    expect(bodies[0]).toMatchObject({ mode: 'question', service_name: 'payment-service', previous_task_id: null });
    expect(requests.find((request) => request.method === 'POST')?.headers.get('X-CSRF-Token')).toBe('fake-session-csrf');
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it('追问使用上一轮任务且新建 request_id；更换服务不会携带旧任务', async () => {
    let count = 0;
    server.use(http.post(`${origin}/api/chat`, async ({ request }) => {
      bodies.push(await request.json() as ChatInput); count++;
      return new HttpResponse(full({ ...answer, task_id: count === 1 ? taskId : nextId }), { headers: { 'Content-Type': 'text/event-stream' } });
    }));
    mount(); await send(); await complete();
    await send('哪些证据支持？'); await waitFor(() => expect(bodies).toHaveLength(2));
    await waitFor(() => expect(screen.getAllByText('已关闭', { exact: true })).toHaveLength(2));
    expect(bodies[1]?.previous_task_id).toBe(taskId);
    expect(bodies[1]?.request_id).not.toBe(bodies[0]?.request_id);
    fireEvent.change(screen.getByLabelText('服务标识'), { target: { value: 'checkout-service' } });
    await send('调查另一个服务'); await waitFor(() => expect(bodies).toHaveLength(3));
    expect(bodies[2]?.previous_task_id).toBeNull();
  });

  it('断流不自动重试，手动重试复用原请求并替换已显示片段', async () => {
    server.use(http.post(`${origin}/api/chat`, async ({ request }) => {
      bodies.push(await request.json() as ChatInput);
      return new HttpResponse(bodies.length === 1 ? frame('task', receipt()) + frame('delta', { text: '尚未完整' }) : full(),
        { headers: { 'Content-Type': 'text/event-stream' } });
    }));
    mount(); await send(); await screen.findByText('回答暂未完成');
    expect(bodies).toHaveLength(1);
    fireEvent.change(screen.getByLabelText('服务标识'), { target: { value: 'different-service' } });
    fireEvent.change(screen.getByLabelText('问题'), { target: { value: '已修改的新问题' } });
    await userEvent.setup().click(screen.getByRole('button', { name: '按原内容重试' })); await complete();
    expect(bodies).toHaveLength(2); expect(bodies[1]).toEqual(bodies[0]);
    expect(screen.queryByText('尚未完整')).not.toBeInTheDocument();
    expect(screen.queryByText('回答暂未完成')).not.toBeInTheDocument();
  });

  it('错误事件保留任务，可读取持久化回答，不额外创建任务', async () => {
    server.use(http.post(`${origin}/api/chat`, () => new HttpResponse(frame('task', receipt()) + frame('error', { task_id: taskId, message: '内部未知错误' }),
      { headers: { 'Content-Type': 'text/event-stream' } })),
    http.get(`${origin}/api/chat/${taskId}`, () => HttpResponse.json(answer)));
    mount(); await send(); await screen.findByText('回答暂未完成');
    expect(screen.queryByText('内部未知错误')).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole('button', { name: '读取已保存回答' })); await complete();
    expect(screen.getByRole('link', { name: `查看任务 ${taskId}` })).toHaveAttribute('href', `/tasks/${taskId}`);
  });

  it('刷新链接恢复已保存回答，pending 只提供手动读取', async () => {
    let count = 0;
    server.use(http.get(`${origin}/api/chat/${taskId}`, () => { count++; return HttpResponse.json(count === 1 ? { ...answer, answer: null, pending: true, evidence_ids: [] } : answer); }));
    mount(`/chat?task=${taskId}`);
    await screen.findByText('任务仍在处理，可手动读取已保存回答。'); expect(count).toBe(1);
    await userEvent.setup().click(screen.getByRole('button', { name: '读取已保存回答' })); await complete();
    expect(count).toBe(2);
  });

  it.each([404, 422, 503])('恢复 %s 显示中文错误并能重新读取', async (status) => {
    server.use(http.get(`${origin}/api/chat/${taskId}`, () => HttpResponse.json({}, { status })));
    mount(`/chat?task=${taskId}`); await screen.findByText('读取失败');
    server.use(http.get(`${origin}/api/chat/${taskId}`, () => HttpResponse.json(answer)));
    await userEvent.setup().click(screen.getByRole('button', { name: '重试查询' })); await complete();
  });

  it('SSE 401 清空身份和业务缓存，返回登录', async () => {
    server.use(http.post(`${origin}/api/chat`, () => new HttpResponse(null, { status: 401 })));
    const { client } = mount(); client.setQueryData(['private-chat'], 'private');
    await send(); await screen.findByRole('heading', { name: '欢迎回来' });
    expect(client.getQueryData(['private-chat'])).toBeUndefined();
    expect(screen.queryByLabelText('对话记录')).not.toBeInTheDocument();
  });

  it.each([409, 422, 503])('POST %s 不自动重试并保留问题', async (status) => {
    let count = 0;
    server.use(http.post(`${origin}/api/chat`, () => { count++; return new HttpResponse(null, { status }); }));
    mount(); await send('保留这个问题'); await screen.findByText('回答暂未完成');
    expect(count).toBe(1); expect(screen.getByLabelText('问题')).toHaveValue('保留这个问题');
    expect(screen.queryByRole('button', { name: '按原内容重试' }) !== null).toBe(status === 503);
  });

  it('处置模式显示真实等待审批、Policy 和计划引用，不发送批准请求', async () => {
    respond({ ...answer, status: 'WAITING_APPROVAL', policy_decision: 'need_approval', plan_evidence_id: nextId });
    mount(); await screen.findByLabelText('请求方式');
    await userEvent.setup().selectOptions(screen.getByLabelText('请求方式'), 'task'); await send('请回滚');
    await screen.findByText('策略判定：需要审批');
    expect(screen.getByRole('link', { name: '查看人工处理' })).toHaveAttribute('href', `/approvals/${taskId}`);
    expect(bodies[0]?.mode).toBe('task');
    expect(requests.filter((request) => request.method === 'POST').map((request) => new URL(request.url).pathname)).toEqual(['/api/chat']);
  });

  it('无效服务/空问题/单边或倒置或超长时间窗拒绝发送；有效窗口转换 UTC', async () => {
    respond(); mount(); await send(' ');
    expect(bodies).toHaveLength(0);
    fireEvent.change(screen.getByLabelText('问题'), { target: { value: '支付调查' } });
    await userEvent.setup().click(screen.getByText('查询时间窗（可选）'));
    for (const [start, end] of [['2026-10-01T01:00:00Z', ''], ['bad', '2026-10-01T02:00:00Z'],
      ['2026-10-01T03:00:00Z', '2026-10-01T02:00:00Z'], ['2026-10-01T01:00:00Z', '2026-10-03T02:00:00Z']]) {
      fireEvent.change(screen.getByLabelText('开始时间（UTC）'), { target: { value: start } });
      fireEvent.change(screen.getByLabelText('结束时间（UTC）'), { target: { value: end } });
      await userEvent.setup().click(screen.getByRole('button', { name: '发送问题' })); expect(bodies).toHaveLength(0);
    }
    fireEvent.change(screen.getByLabelText('开始时间（UTC）'), { target: { value: '2026-10-01T09:00:00+08:00' } });
    fireEvent.change(screen.getByLabelText('结束时间（UTC）'), { target: { value: '2026-10-01T10:00:00+08:00' } });
    await userEvent.setup().click(screen.getByRole('button', { name: '发送问题' })); await complete();
    expect(bodies[0]).toMatchObject({ start: '2026-10-01T01:00:00.000Z', end: '2026-10-01T02:00:00.000Z' });
  });

  it('连续点击只提交一次，停止接收保留已接纳任务', async () => {
    let streamController!: ReadableStreamDefaultController<Uint8Array>;
    server.use(http.post(`${origin}/api/chat`, async ({ request }) => {
      requests.push(request); bodies.push(await request.json() as ChatInput);
      return new HttpResponse(new ReadableStream<Uint8Array>({ start(controller) { streamController = controller; controller.enqueue(new TextEncoder().encode(frame('task', receipt()))); } }),
        { headers: { 'Content-Type': 'text/event-stream' } });
    }));
    mount(); await screen.findByLabelText('问题');
    fireEvent.change(screen.getByLabelText('问题'), { target: { value: '等待回答' } });
    const button = screen.getByRole('button', { name: '发送问题' }); fireEvent.click(button); fireEvent.click(button);
    await screen.findByRole('link', { name: `查看任务 ${taskId}` });
    await userEvent.setup().click(screen.getByRole('button', { name: '停止接收' }));
    await screen.findByText(/回答连接已停止/);
    expect(bodies).toHaveLength(1);
    expect(requests.filter((request) => request.method !== 'GET').map((request) => new URL(request.url).pathname)).toEqual(['/api/chat']);
    try { streamController.close(); } catch { /* 流可能已被取消 */ }
  });

  it('回答文本安全展示，未核对的 Evidence 不生成链接', async () => {
    respond({ ...answer, answer: `<img src=x onerror=alert(1)>[Evidence:${nextId}][Evidence:${evidenceId}]` });
    const { container } = mount(); await send(); await complete();
    expect(container.querySelector('.chat-answer img')).toBeNull();
    expect(screen.queryByRole('link', { name: `查看证据 ${nextId}` })).not.toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: `查看证据 ${evidenceId}` })).toHaveLength(2);
  });

  it('done 的任务身份不符时拒绝交付', async () => {
    server.use(http.post(`${origin}/api/chat`, () => new HttpResponse(frame('task', receipt()) + frame('done', { ...answer, task_id: nextId }),
      { headers: { 'Content-Type': 'text/event-stream' } })));
    mount(); await send(); await screen.findByText('回答暂未完成');
    expect(screen.queryByText('已关闭', { exact: true })).not.toBeInTheDocument();
  });

  it('无回答转人工不把片段当完成结论；新对话清除旧追问', async () => {
    respond({ ...answer, answer: null, evidence_ids: [], status: 'ESCALATED' });
    mount(); await send(); await screen.findByText('当前没有可交付回答，请查看任务详情与待处理事项。');
    expect(screen.queryByLabelText('基于上一轮追问')).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole('button', { name: '新对话' }));
    expect(screen.queryByText('第 1 轮 · payment-service')).not.toBeInTheDocument();
    expect(screen.getByText('从一个问题开始')).toBeInTheDocument();
  });

  it('无效任务链接不发出读取请求', async () => {
    mount('/chat?task=bad'); await screen.findByText('对话链接无效');
    expect(requests.some((request) => new URL(request.url).pathname.startsWith('/api/chat'))).toBe(false);
  });
});
