import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { http, HttpResponse } from 'msw';
import { ConsoleApp } from '../app';
import { AppProviders } from '../providers';
import { authorize, origin, requests, server } from '../test/server';
import { fakeTask, fakeEvidence, evidenceId, taskId } from '../test/task-data';
import { emptyMetrics } from '../test/metrics-data';
import type { AuditView, MetricsReport } from '../api/generated/types.gen';

const auditId = '80000000-0000-4000-8000-000000000001';
const audit: AuditView = { id: auditId, actor: 'local-test-owner', event_type: 'approval', operation: 'approval.decide',
  outcome: 'approved', task_id: taskId, evidence_id: evidenceId, occurred_at: fakeTask.updated_at, details: { action_hash: 'original-hash', evidence_id: evidenceId } };
const windowQuery = 'start=2026-10-01T00%3A00%3A00Z&end=2026-10-09T00%3A00%3A00Z';
function mount(path: string, signedIn = true) {
  authorize(signedIn);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<AppProviders client={client}><MemoryRouter initialEntries={[path]}><ConsoleApp /></MemoryRouter></AppProviders>);
  return client;
}
const page = <T,>(items: T[], total = items.length, offset = 0) => ({ items, total, limit: 20, offset });
function auditMocks(value = audit) {
  server.use(http.get(`${origin}/api/audits`, ({ request }) => { requests.push(request); return HttpResponse.json(page([value], 21)); }),
    http.get(`${origin}/api/audits/:id`, ({ request }) => { requests.push(request); return HttpResponse.json(value); }),
    http.get(`${origin}/api/evidence/:id`, ({ params }) => HttpResponse.json({ ...fakeEvidence, id: params.id })));
}

describe('Step 52 总览与审计', () => {
  it('待审批使用接口 total，点击后与审批中心一致，两种人工等待独立统计', async () => {
    server.use(http.get(`${origin}/api/tasks`, ({ request }) => {
      requests.push(request); const status = new URL(request.url).searchParams.get('status');
      const totals: Record<string, number> = { WAITING_APPROVAL: 25, NEED_HUMAN_JUDGMENT: 2, WAITING_INFORMATION: 3 };
      return HttpResponse.json(page(status === 'WAITING_APPROVAL' ? [{ ...fakeTask, status }] : [], totals[status ?? ''] ?? 0));
    }));
    mount('/dashboard');
    expect(await screen.findByLabelText('待审批数量')).toHaveTextContent('25');
    expect(await screen.findByLabelText('待人工判断数量')).toHaveTextContent('2');
    expect(await screen.findByLabelText('待补充信息数量')).toHaveTextContent('3');
    await userEvent.setup().click(screen.getByRole('link', { name: '处理待审批事项' }));
    await screen.findByText('共 25 条');
    expect(screen.getByRole('tab', { name: '待审批' })).toHaveAttribute('aria-selected', 'true');
  });
  it('运行中总数含所有阶段，最近记录取各阶段而非总列表第一页，等待及终态不计入', async () => {
    server.use(http.get(`${origin}/api/tasks`, ({ request }) => {
      requests.push(request); const status = new URL(request.url).searchParams.get('status');
      return HttpResponse.json(page(['INVESTIGATING', 'EXECUTING'].includes(status ?? '') ? [{ ...fakeTask, id: status === 'EXECUTING' ? auditId : taskId, status, title: `${status} 样例` }] : [], status === 'INVESTIGATING' ? 35 : status === 'EXECUTING' ? 12 : 0));
    }));
    mount('/dashboard'); expect(await screen.findByLabelText('运行中任务数量')).toHaveTextContent('47');
    expect(screen.getByRole('link', { name: 'EXECUTING 样例' })).toHaveAttribute('href', `/tasks/${auditId}`);
    const statuses = requests.filter((r) => new URL(r.url).pathname === '/api/tasks').map((r) => new URL(r.url).searchParams.get('status'));
    expect(statuses).not.toContain('CLOSED'); expect(statuses).not.toContain('FAILED'); expect(statuses).not.toContain(null);
  });
  it('全部十项指标显示接口值、单位、分子分母，null 与 0 明确区分', async () => {
    const data: MetricsReport = structuredClone(emptyMetrics);
    data.metrics[0] = { ...data.metrics[0], value: 0.875, numerator: 7, denominator: 8 };
    data.metrics[2] = { ...data.metrics[2], value: 0, numerator: 0, denominator: 4 };
    data.metrics[6] = { ...data.metrics[6], value: 125.25, numerator: 250.5, denominator: 2 };
    data.metrics[7] = { ...data.metrics[7], value: 4.5, numerator: 9, denominator: 2 };
    server.use(http.get(`${origin}/api/metrics`, ({ request }) => { requests.push(request); return HttpResponse.json(data); }));
    mount(`/dashboard?${windowQuery}`);
    expect(await screen.findByText('87.5%')).toBeInTheDocument(); expect(screen.getByText('0%')).toBeInTheDocument();
    expect(screen.getByText('125.25 秒')).toBeInTheDocument(); expect(screen.getByText('4.5 次')).toBeInTheDocument();
    expect(screen.getAllByText('暂无可评价样本')).toHaveLength(6);
    const card = screen.getByText('RCA命中率').closest('.ant-card')!;
    await userEvent.setup().click(within(card as HTMLElement).getByText('查看统计依据'));
    expect(within(card as HTMLElement).getByText('0.875')).toBeVisible();
    expect(within(card as HTMLElement).getByText('7')).toBeVisible(); expect(within(card as HTMLElement).getByText('8')).toBeVisible();
    const url = new URL(requests.find((r) => new URL(r.url).pathname === '/api/metrics')!.url);
    expect(url.searchParams.get('start')).toBe('2026-10-01T00:00:00.000Z');
  });
  it('总览独立失败不显示伪造零值，重试后恢复', async () => {
    server.use(http.get(`${origin}/api/tasks`, ({ request }) => new URL(request.url).searchParams.get('status') === 'WAITING_APPROVAL'
      ? new HttpResponse(null, { status: 503 }) : HttpResponse.json(page([]))));
    mount('/dashboard'); await screen.findByRole('alert');
    expect(screen.queryByLabelText('待审批数量')).not.toBeInTheDocument();
    expect(await screen.findByLabelText('待人工判断数量')).toHaveTextContent('0');
    server.resetHandlers(); await userEvent.setup().click(screen.getByRole('button', { name: '重试查询' }));
    expect(await screen.findByLabelText('待审批数量')).toHaveTextContent('0');
  });
  it('刷新总览更新待处理数量', async () => {
    let total = 1;
    server.use(http.get(`${origin}/api/tasks`, () => HttpResponse.json(page([], total))));
    mount('/dashboard'); expect(await screen.findByLabelText('待审批数量')).toHaveTextContent('1'); total = 2;
    await userEvent.setup().click(screen.getByRole('button', { name: '刷新总览' }));
    await waitFor(() => expect(screen.getByLabelText('待审批数量')).toHaveTextContent('2'));
  });
  it('审计按操作人、类型、UTC 半开时间窗联合查询且分页保留条件', async () => {
    auditMocks(); mount(`/audit?actor=local-test-owner&event_type=approval&${windowQuery}`);
    await screen.findByRole('link', { name: 'approval.decide' });
    const user = userEvent.setup(); await user.click(screen.getByTitle('2'));
    await waitFor(() => expect(requests.some((r) => new URL(r.url).searchParams.get('offset') === '20')).toBe(true));
    const url = new URL(requests.filter((r) => new URL(r.url).pathname === '/api/audits').at(-1)!.url);
    expect(url.searchParams.get('actor')).toBe('local-test-owner'); expect(url.searchParams.get('event_type')).toBe('approval');
    expect(url.searchParams.get('end')).toBe('2026-10-09T00:00:00.000Z');
    await user.click(screen.getByRole('link', { name: 'approval.decide' }));
    await screen.findByRole('heading', { name: '审计详情', level: 1 });
    await user.click(screen.getByRole('link', { name: '← 返回审计中心' }));
    expect(screen.getByLabelText('操作人')).toHaveValue('local-test-owner'); expect(screen.getByLabelText('操作类型')).toHaveValue('approval');
    await screen.findByText('共 21 条');
  });
  it('提交操作人和类型筛选重置页码，空条件恢复全部类型', async () => {
    auditMocks(); mount('/audit?page=2'); await screen.findByRole('link', { name: 'approval.decide' });
    const user = userEvent.setup(); await user.type(screen.getByLabelText('操作人'), ' local-test-owner ');
    await user.selectOptions(screen.getByLabelText('操作类型'), 'approval'); await user.click(screen.getByRole('button', { name: '查询审计' }));
    await waitFor(() => expect(requests.some((r) => new URL(r.url).searchParams.get('actor') === 'local-test-owner' && new URL(r.url).searchParams.get('offset') === '0')).toBe(true));
    await user.click(screen.getByRole('button', { name: '重置筛选' })); await waitFor(() => expect(screen.getByLabelText('操作人')).toHaveValue(''));
  });
  it('时间表单拒绝倒置窗口，修正后按 UTC 提交并保留操作人', async () => {
    auditMocks(); mount('/audit?actor=local-test-owner'); await screen.findByRole('link', { name: 'approval.decide' });
    const start = screen.getByLabelText('开始时间（UTC）'), end = screen.getByLabelText('结束时间（UTC，不含）');
    const before = requests.filter((r) => new URL(r.url).pathname === '/api/audits').length;
    fireEvent.change(start, { target: { value: '2026-10-09T01:00:00' } });
    fireEvent.change(end, { target: { value: '2026-10-08T01:00:00' } }); fireEvent.submit(start.closest('form')!);
    expect(requests.filter((r) => new URL(r.url).pathname === '/api/audits')).toHaveLength(before);
    fireEvent.change(end, { target: { value: '2026-10-10T01:00:00' } }); fireEvent.submit(start.closest('form')!);
    await waitFor(() => expect(requests.some((r) => new URL(r.url).searchParams.get('start') === '2026-10-09T01:00:00.000Z')).toBe(true));
    const url = new URL(requests.filter((r) => new URL(r.url).pathname === '/api/audits').at(-1)!.url);
    expect(url.searchParams.get('end')).toBe('2026-10-10T01:00:00.000Z'); expect(url.searchParams.get('actor')).toBe('local-test-owner');
  });
  it('审计详情精确打开 Evidence，源文本安全展示', async () => {
    auditMocks({ ...audit, details: { ...audit.details, note: '<img src=x onerror=alert(1)>' } }); mount(`/audit/${auditId}`);
    await screen.findByText('<img src=x onerror=alert(1)>'); expect(document.querySelector('img[src="x"]')).toBeNull();
    await userEvent.setup().click(screen.getAllByRole('link', { name: `查看证据 ${evidenceId}` })[0]!);
    const drawer = await screen.findByRole('dialog'); expect(await within(drawer).findByText(evidenceId, { exact: true })).toBeVisible();
    expect(within(drawer).getByText('query_metrics')).toBeVisible();
  });
  it('内容编辑审计没有关联任务或证据时正常显示', async () => {
    auditMocks({ ...audit, event_type: 'catalog_edit', task_id: null, evidence_id: null }); mount(`/audit/${auditId}`);
    await screen.findByText('内容编辑'); expect(screen.getByText('无关联任务')).toBeInTheDocument(); expect(screen.getByText('无证据引用')).toBeInTheDocument();
  });
  it.each([404, 422, 503])('审计详情 %s 显示错误并可重试', async (status) => {
    server.use(http.get(`${origin}/api/audits/:id`, () => new HttpResponse(null, { status })));
    mount(`/audit/${auditId}`); await screen.findByRole('alert');
    auditMocks(); await userEvent.setup().click(screen.getByRole('button', { name: '重试查询' })); await screen.findByText('original-hash');
  });
  it.each(['/audit?event_type=invalid', '/audit?start=2026-10-01', '/dashboard?start=2026-10-09T00:00:00Z&end=2026-10-01T00:00:00Z'])('无效筛选 %s 不发相关查询', async (path) => {
    mount(path); await screen.findByRole('alert');
    expect(requests.some((r) => ['/api/audits', '/api/metrics'].includes(new URL(r.url).pathname))).toBe(false);
  });
  it('未登录审计详情登录后返回原筛选深链接', async () => {
    auditMocks(); mount(`/audit/${auditId}?actor=local-test-owner`, false); const user = userEvent.setup();
    await user.type(await screen.findByLabelText('账户'), 'local-test-owner'); await user.type(screen.getByLabelText('密码'), 'fake-password-for-tests');
    await user.click(screen.getByRole('button', { name: '登录' })); await screen.findByRole('heading', { name: '审计详情', level: 1 });
    expect(screen.getByRole('link', { name: '← 返回审计中心' })).toHaveAttribute('href', '/audit?actor=local-test-owner');
  });
  it('审计查询 401 清空缓存并返回登录', async () => {
    server.use(http.get(`${origin}/api/audits`, () => new HttpResponse(null, { status: 401 })));
    const client = mount('/audit'); client.setQueryData(['private'], 'private'); await screen.findByRole('heading', { name: '欢迎回来' });
    expect(client.getQueryData(['private'])).toBeUndefined();
  });
});
