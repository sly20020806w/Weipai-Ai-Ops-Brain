import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { http, HttpResponse } from 'msw';
import type { InteractionView, TaskStatus, TaskView } from '../api/generated/types.gen';
import { ConsoleApp } from '../app';
import { AppProviders } from '../providers';
import { authorize, origin, requests, server, session } from '../test/server';
import { conclusionId, evidenceId, fakeEvidence, fakeTask, taskId } from '../test/task-data';

const approvalId = '70000000-0000-4000-8000-000000000001';
const questionId = '70000000-0000-4000-8000-000000000002';
const hash = 'a'.repeat(64);
const snapshot = { task_id: taskId, status: 'WAITING_APPROVAL' as const, version: 7 };
const approval: InteractionView = { task_id: taskId, status: 'WAITING_APPROVAL', status_version: 7,
  approval_request_evidence_id: evidenceId, approval: { approval_id: approvalId, task_id: taskId, wait_version: 7,
    action_hash: hash, plan_evidence_id: evidenceId, plan: { task_id: taskId, environment: 'test', planning_version: 6,
      conclusion_evidence_id: conclusionId, review_evidence_id: evidenceId,
      summary: { statement: '回滚支付服务以恢复业务', evidence_ids: [evidenceId] }, actions: [{
        action: { id: 'rollback-payment', name: 'rollback_service', service_name: 'payment-service', risk_level: 'L3',
          parameters: { from_version: 'v2.3.7', target_version: 'v2.3.6' }, preconditions: ['当前仍运行 v2.3.7'],
          rationale: { statement: '新版本扩大连接池', evidence_ids: [evidenceId] },
          rollback: { description: '恢复原版本', trigger: '回滚后继续恶化', parameters: { target_version: 'v2.3.7' } },
          verification: { checks: ['检查 5xx 与 P99'], success_criteria: '错误率恢复基线', failure_response: '返回调查并通知接管' } },
        policy: { action_name: 'rollback_service', decision: 'need_approval', environment: 'test', risk_level: 'L3', reason: '生产重要变更需授权' },
      }] } } };

function human(status: 'NEED_HUMAN_JUDGMENT' | 'WAITING_INFORMATION'): InteractionView {
  return { task_id: taskId, status, status_version: 7, question: { task: { ...snapshot, status },
    question_id: questionId, question_evidence_id: evidenceId, question: '请确认支付业务优先级', resume_status: 'INVESTIGATING' } };
}
function mount(path = `/approvals/${taskId}`, signedIn = true) {
  authorize(signedIn);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<AppProviders client={client}><MemoryRouter initialEntries={[path]}><ConsoleApp /></MemoryRouter></AppProviders>);
  return client;
}
function details(view: InteractionView = approval) {
  const state = { task: { ...fakeTask, status: view.status, status_version: view.status_version }, view };
  server.use(
    http.get(`${origin}/api/tasks/${taskId}`, ({ request }) => { requests.push(request); return HttpResponse.json(state.task); }),
    http.get(`${origin}/api/tasks/${taskId}/interaction`, ({ request }) => { requests.push(request); return HttpResponse.json(state.view); }),
    http.get(`${origin}/api/tasks/${taskId}/status-history`, () => HttpResponse.json([])),
    http.get(`${origin}/api/evidence/${evidenceId}`, () => HttpResponse.json(fakeEvidence)),
  );
  return state;
}
const receipt = { task_id: taskId, operation_id: 'fake-control', evidence_id: evidenceId, outcome: 'signaled' };
async function confirmApproval(decision: 'approved' | 'rejected' = 'approved') {
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: decision === 'approved' ? '批准动作' : '拒绝动作' }));
  const confirm = screen.getByRole('region', { name: '核对本次操作' });
  return { user, confirm, button: within(confirm).getByRole('button', { name: decision === 'approved' ? '确认批准动作' : '确认拒绝动作' }) };
}

describe('Step 49 审批中心', () => {
  it('审批、人工判断与补充信息使用各自状态列表，筛选与分页保存在 URL', async () => {
    const rows: TaskView[] = [
      { ...fakeTask, title: '待审批支付回滚', status: 'WAITING_APPROVAL' },
      { ...fakeTask, id: approvalId, title: '待判断业务取舍', status: 'NEED_HUMAN_JUDGMENT' },
      { ...fakeTask, id: questionId, title: '待补充监控窗口', status: 'WAITING_INFORMATION' },
    ];
    server.use(http.get(`${origin}/api/tasks`, ({ request }) => {
      requests.push(request); const params = new URL(request.url).searchParams;
      return HttpResponse.json({ items: rows.filter((row) => !params.get('status') || row.status === params.get('status')),
        total: 21, limit: 20, offset: Number(params.get('offset')) });
    }));
    mount('/approvals'); const user = userEvent.setup();
    const list = await screen.findByRole('region', { name: '待审批列表' });
    await within(list).findByRole('link', { name: '待审批支付回滚' });
    expect(within(list).queryByText('待判断业务取舍')).not.toBeInTheDocument();
    await user.click(screen.getByTitle('2'));
    await waitFor(() => expect(requests.some((request) => request.url.includes('offset=20'))).toBe(true));
    await user.click(screen.getByRole('tab', { name: '待人工判断' }));
    const judgment = await screen.findByRole('region', { name: '待人工判断列表' });
    await within(judgment).findByRole('link', { name: '待判断业务取舍' });
    expect(within(judgment).queryByText('待审批支付回滚')).not.toBeInTheDocument();
    expect(requests.some((request) => request.url.includes('status=NEED_HUMAN_JUDGMENT') && request.url.includes('offset=0'))).toBe(true);
    await user.click(screen.getByRole('tab', { name: '待补充信息' }));
    await screen.findByRole('link', { name: '待补充监控窗口' });
    expect(requests.some((request) => request.url.includes('status=WAITING_INFORMATION'))).toBe(true);
  });

  it('批准携带精确动作哈希/审批身份/版本与 CSRF，刷新后展示真实 EXECUTING 并保留证据回执', async () => {
    const state = details(); let body: unknown; let csrf: string | null = null;
    server.use(http.post(`${origin}/api/tasks/${taskId}/approval`, async ({ request }) => {
      requests.push(request); body = await request.json(); csrf = request.headers.get('X-CSRF-Token');
      state.task = { ...state.task, status: 'EXECUTING', status_version: 8 };
      state.view = { task_id: taskId, status: 'EXECUTING', status_version: 8 };
      return HttpResponse.json(receipt, { status: 202 });
    }));
    mount(); await screen.findByText('检查 5xx 与 P99');
    expect(screen.getByText('成功标准：错误率恢复基线')).toBeVisible();
    expect(screen.getByText('当前仍运行 v2.3.7')).toBeVisible();
    const { user, confirm, button } = await confirmApproval();
    expect(requests.filter((request) => request.method === 'POST')).toHaveLength(0);
    expect(within(confirm).getByText(hash)).toBeVisible();
    await user.click(button);
    await screen.findByText('操作已记录并发送恢复信号');
    expect(body).toEqual({ approval_id: approvalId, wait_version: 7, action_hash: hash, decision: 'approved' });
    expect(csrf).toBe(session.csrf_token);
    expect(await screen.findByText('执行中')).toBeVisible();
    expect(screen.queryByRole('button', { name: '批准动作' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('link', { name: `查看证据 ${evidenceId}` }));
    await within(await screen.findByRole('dialog')).findByText('query_metrics');
  });

  it('拒绝单独提交 rejected；取消核对不发出任何操作', async () => {
    details(); let body: unknown;
    server.use(http.post(`${origin}/api/tasks/${taskId}/approval`, async ({ request }) => { requests.push(request);
      body = await request.json(); return HttpResponse.json(receipt, { status: 202 }); }));
    mount(); const first = await confirmApproval(); await first.user.click(within(first.confirm).getByRole('button', { name: '取消' }));
    expect(requests.filter((request) => request.method === 'POST')).toHaveLength(0);
    const next = await confirmApproval('rejected'); await next.user.click(next.button);
    await screen.findByText('操作已记录并发送恢复信号');
    expect(body).toEqual({ approval_id: approvalId, wait_version: 7, action_hash: hash, decision: 'rejected' });
  });

  it.each([
    ['NEED_HUMAN_JUDGMENT', 'judgment', '你的判断', '提交判断'],
    ['WAITING_INFORMATION', 'information', '补充信息', '提交信息'],
  ] as const)('%s 回答使用对应接口与问题身份，回答不会调用审批', async (status, endpoint, label, submit) => {
    details(human(status)); let body: unknown;
    server.use(http.post(`${origin}/api/tasks/${taskId}/${endpoint}`, async ({ request }) => { requests.push(request);
      body = await request.json(); return HttpResponse.json(receipt, { status: 202 }); }));
    mount(); const user = userEvent.setup();
    const input = await screen.findByLabelText(label);
    expect(screen.queryByRole('button', { name: '批准动作' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: submit })).toBeDisabled();
    await user.type(input, '   '); expect(screen.getByRole('button', { name: submit })).toBeDisabled();
    await user.clear(input); await user.type(input, ' 优先保证支付业务 ');
    await user.click(screen.getByRole('button', { name: submit }));
    await user.click(screen.getByRole('button', { name: `确认${submit}` }));
    await screen.findByText('操作已记录并发送恢复信号');
    expect(body).toEqual({ question_id: questionId, wait_version: 7, answer: '优先保证支付业务' });
    expect(requests.filter((request) => request.method === 'POST').map((request) => new URL(request.url).pathname)).toEqual([`/api/tasks/${taskId}/${endpoint}`]);
  });

  it('旧等待恢复问题使用后端 recovery_question_id，不自行生成身份', async () => {
    details({ task_id: taskId, status: 'WAITING_INFORMATION', status_version: 7, recovery_question_id: questionId,
      recovery: { task: { ...snapshot, status: 'WAITING_INFORMATION' }, question: '请补充实际恢复条件', resume_status: 'INVESTIGATING' } });
    let body: unknown;
    server.use(http.post(`${origin}/api/tasks/${taskId}/information`, async ({ request }) => { body = await request.json(); return HttpResponse.json(receipt, { status: 202 }); }));
    mount(); const user = userEvent.setup(); await user.type(await screen.findByLabelText('补充信息'), '窗口已确认');
    await user.click(screen.getByRole('button', { name: '提交信息' }));
    await user.click(screen.getByRole('button', { name: '确认提交信息' }));
    await screen.findByText('操作已记录并发送恢复信号');
    expect(body).toEqual({ question_id: questionId, wait_version: 7, answer: '窗口已确认' });
  });

  it('接管提交当前状态版本、中文原因与 CSRF；新状态从 API 返回', async () => {
    const state = details(human('NEED_HUMAN_JUDGMENT')); let body: unknown;
    server.use(http.post(`${origin}/api/tasks/${taskId}/takeover`, async ({ request }) => { requests.push(request); body = await request.json();
      expect(request.headers.get('X-CSRF-Token')).toBe(session.csrf_token);
      state.task = { ...state.task, status: 'ESCALATED', status_version: 8 };
      state.view = { task_id: taskId, status: 'ESCALATED', status_version: 8 };
      return HttpResponse.json({ ...receipt, outcome: 'taken_over' }, { status: 202 }); }));
    mount(); const user = userEvent.setup();
    await user.type(await screen.findByLabelText('接管原因'), '由我接管本次业务处理');
    await user.click(screen.getByRole('button', { name: '接管任务' }));
    await user.click(screen.getByRole('button', { name: '确认接管' }));
    await screen.findByText('接管操作已接纳');
    expect(body).toEqual({ expected_version: 7, reason: '由我接管本次业务处理' });
    await screen.findByText('已转人工');
  });

  it('审批版本冲突 409 禁止盲目重发，刷新后必须核对新的哈希和版本', async () => {
    const state = details();
    server.use(http.post(`${origin}/api/tasks/${taskId}/approval`, () => new HttpResponse(null, { status: 409 })));
    mount(); const { user, button } = await confirmApproval(); await user.click(button);
    expect(await screen.findByRole('alert')).toHaveTextContent('任务版本或操作内容已变化');
    expect(screen.getByRole('button', { name: '确认批准动作' })).toBeDisabled();
    state.task = { ...state.task, status_version: 9 };
    state.view = { ...approval, status_version: 9, approval: { ...approval.approval!, wait_version: 9, action_hash: 'b'.repeat(64) } };
    await user.click(screen.getByRole('button', { name: '刷新并重新核对' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '批准动作' })).toBeEnabled());
    await user.click(screen.getByRole('button', { name: '批准动作' }));
    expect(within(screen.getByRole('region', { name: '核对本次操作' })).getByText('b'.repeat(64))).toBeVisible();
  });

  it('503/丢响应不自动重试，锁定原决定并按完全相同内容手动重投', async () => {
    details(human('WAITING_INFORMATION')); const bodies: unknown[] = [];
    server.use(http.post(`${origin}/api/tasks/${taskId}/information`, async ({ request }) => { bodies.push(await request.json());
      return bodies.length === 1 ? new HttpResponse(null, { status: 503 }) : HttpResponse.json(receipt, { status: 202 }); }));
    mount(); const user = userEvent.setup(); await user.type(await screen.findByLabelText('补充信息'), '已确认实际窗口');
    await user.click(screen.getByRole('button', { name: '提交信息' }));
    await user.click(screen.getByRole('button', { name: '确认提交信息' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('操作结果尚未确认');
    expect(bodies).toHaveLength(1); expect(screen.getByLabelText('补充信息')).toBeDisabled();
    expect(screen.getByRole('button', { name: '取消' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '以相同内容重试' }));
    await screen.findByText('操作已记录并发送恢复信号'); expect(bodies).toHaveLength(2); expect(bodies[0]).toEqual(bodies[1]);
  });

  it('提交期间禁止重复点击及切换决定，不乐观伪造 EXECUTING', async () => {
    details(); let release: () => void = () => {}; let calls = 0;
    server.use(http.post(`${origin}/api/tasks/${taskId}/approval`, async () => { calls++;
      await new Promise<void>((resolve) => { release = resolve; }); return HttpResponse.json(receipt, { status: 202 }); }));
    mount(); const { user, button } = await confirmApproval(); await user.dblClick(button);
    await waitFor(() => expect(calls).toBe(1));
    expect(screen.getByRole('button', { name: '拒绝动作' })).toBeDisabled();
    expect(screen.queryByText('执行中')).not.toBeInTheDocument();
    await act(async () => { release(); }); await screen.findByText('操作已记录并发送恢复信号');
  });

  it.each([404, 422, 403])('操作 %s 有中文错误且不显示成功', async (status) => {
    details(); server.use(http.post(`${origin}/api/tasks/${taskId}/approval`, () => new HttpResponse(null, { status })));
    mount(); const { user, button } = await confirmApproval(); await user.click(button);
    expect(await screen.findByRole('alert')).toHaveTextContent('操作未确认');
    expect(screen.queryByText('操作已记录并发送恢复信号')).not.toBeInTheDocument();
  });

  it('操作 401 返回登录，清空操作和任务缓存', async () => {
    details(); server.use(http.post(`${origin}/api/tasks/${taskId}/approval`, () => new HttpResponse(null, { status: 401 })));
    const client = mount(); const { user, button } = await confirmApproval(); await user.click(button);
    await screen.findByRole('heading', { name: '欢迎回来' });
    expect(client.getQueryData(['interaction', taskId])).toBeUndefined();
    expect(screen.queryByRole('region', { name: '核对本次操作' })).not.toBeInTheDocument();
  });

  it('审批尚未生成、查询失败或状态版本不一致均不出现批准按钮', async () => {
    const state = details({ ...approval, approval: null }); const client = mount();
    await screen.findByText('当前审批单尚未就绪或已变化，请刷新后核对。');
    expect(screen.queryByRole('button', { name: '批准动作' })).not.toBeInTheDocument();
    state.view = { ...approval, status_version: 6 };
    await act(async () => { await client.invalidateQueries({ queryKey: ['interaction', taskId] }); });
    expect(screen.queryByRole('button', { name: '批准动作' })).not.toBeInTheDocument();
    server.use(http.get(`${origin}/api/tasks/${taskId}/interaction`, () => new HttpResponse(null, { status: 503 })));
    await act(async () => { await client.invalidateQueries({ queryKey: ['interaction', taskId] }); });
    await screen.findByRole('alert');
    expect(screen.queryByRole('button', { name: '批准动作' })).not.toBeInTheDocument();
  });

  it('成功回执后旧等待版本不能改投相反决定，等待刷新到下一版本', async () => {
    details(); server.use(http.post(`${origin}/api/tasks/${taskId}/approval`, () => HttpResponse.json(receipt, { status: 202 })));
    mount(); const { user, button } = await confirmApproval(); await user.click(button);
    await screen.findByText('操作已记录并发送恢复信号');
    await waitFor(() => expect(screen.getByRole('button', { name: '拒绝动作' })).toBeDisabled());
    expect(screen.getByRole('button', { name: '批准动作' })).toBeDisabled();
  });

  it('新问题身份到达后不继承旧问题未提交的回答', async () => {
    const state = details(human('NEED_HUMAN_JUDGMENT')); const client = mount(); const user = userEvent.setup();
    await user.type(await screen.findByLabelText('你的判断'), '旧问题的未提交草稿');
    state.view = { ...state.view, question: { ...state.view.question!, question_id: approvalId, question: '新的业务取舍问题' } };
    await act(async () => { await client.invalidateQueries({ queryKey: ['interaction', taskId] }); });
    await screen.findByText('新的业务取舍问题');
    expect(screen.getByLabelText('你的判断')).toHaveValue('');
    expect(screen.getByRole('button', { name: '提交判断' })).toBeDisabled();
  });

  it.each(['CLOSED', 'RESOLVED'] satisfies TaskStatus[])('已结束的 %s 任务没有接管按钮', async (status) => {
    details({ task_id: taskId, status, status_version: 7 }); mount();
    await screen.findByText('当前任务已完成，无法接管。');
    expect(screen.queryByRole('button', { name: '接管任务' })).not.toBeInTheDocument();
  });

  it('未登录人工处理深链接登录后返回；恢复问题文本按普通文本展示', async () => {
    details({ ...human('NEED_HUMAN_JUDGMENT'), question: { ...human('NEED_HUMAN_JUDGMENT').question!, question: '<img src=x onerror=alert(1)>' } });
    mount(`/approvals/${taskId}?kind=judgment`, false); const user = userEvent.setup();
    await user.type(await screen.findByLabelText('账户'), 'local-test-owner');
    await user.type(screen.getByLabelText('密码'), 'fake-password-for-tests');
    await user.click(screen.getByRole('button', { name: '登录' }));
    await screen.findByRole('heading', { name: '人工处理详情', level: 1 });
    expect(await screen.findByText('<img src=x onerror=alert(1)>')).toBeVisible();
    expect(document.querySelector('img[src="x"]')).toBeNull();
  });
});
