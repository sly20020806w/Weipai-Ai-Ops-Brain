import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { http, HttpResponse } from 'msw';
import { ConsoleApp } from '../app';
import { AppProviders } from '../providers';
import { authorize, origin, requests, server } from '../test/server';
import { fakeTask, fakeEvent, fakeEvidence, taskId, evidenceId } from '../test/task-data';
import type { RiskView, ScenarioDetail } from '../api/generated/types.gen';
import { centers } from './api';

const riskId = '70000000-0000-4000-8000-000000000001';
const reportId = '30000000-0000-4000-8000-000000000099';
const fakeRisk: RiskView = { id: riskId, service_name: 'payment-service', resource: 'payment-deployment',
  check_id: 'pdb_present', category: 'stability', outcome: 'abnormal', active: true, episode: 1,
  first_seen: fakeTask.created_at, last_seen: fakeTask.updated_at, cleared_at: null,
  opening_evidence_id: evidenceId, latest_evidence_id: reportId, notification_evidence_id: null };
function mount(path: string, signedIn = true) {
  authorize(signedIn);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<AppProviders client={client}><MemoryRouter initialEntries={[path]}><ConsoleApp /></MemoryRouter></AppProviders>);
  return client;
}
const page = <T,>(items: T[], total = items.length) => ({ items, total, limit: 20, offset: 0 });
const cases = Object.entries(centers) as Array<[keyof typeof centers, (typeof centers)[keyof typeof centers]]>;
function fixture(center: keyof typeof centers): ScenarioDetail {
  const tool = ({ releases: 'release.report', tickets: 'ticket.conclusion', inspections: 'inspection.report',
    'war-room': 'war_room.report', architecture: 'architecture.report', automation: 'automation.suggestion' })[center];
  const content = ({
    releases: { checks: [{ label: 'SQL 检查', outcome: 'unknown', reason: '需人工核对高风险 SQL', evidence_id: evidenceId }] },
    tickets: { statement: '权限已独立验证并回填工单', evidence_ids: [evidenceId] },
    inspections: { checks: [{ label: '缺少 PDB', outcome: 'abnormal', evidence_id: evidenceId }, { label: '监控覆盖', outcome: 'unknown' }] },
    'war-room': { sections: [{ title: '资源回收', conclusions: [{ statement: '仅回收本次增加的副本', evidence_ids: [evidenceId] }] }] },
    architecture: { dimensions: ['稳定性', '高可用', '容量', 'Kubernetes', '云资源', '网络', '存储', '安全', '成本', '运维复杂度', '可观测性', '发布和回滚'].map((dimension) => ({ dimension, outcome: 'unknown', finding: '需补充材料', citations: [{ evidence_id: evidenceId, quote: '原方案' }] })) },
    automation: { method: 'script', records: Array.from({ length: 5 }, (_, i) => ({ record_id: `原始记录${i + 1}`, evidence_id: evidenceId })) },
  })[center];
  return { task: { ...fakeTask, title: `${centers[center].title}样例` }, event: fakeEvent,
    evidence: [fakeEvidence, { ...fakeEvidence, id: reportId, source_tool: tool, result_snapshot: content }] };
}
function mocks(center: keyof typeof centers) {
  const detail = fixture(center), endpoint = centers[center].endpoint;
  server.use(http.get(`${origin}/api/${endpoint}`, ({ request }) => { requests.push(request); return HttpResponse.json(page([detail])); }),
    http.get(`${origin}/api/${endpoint}/${taskId}`, ({ request }) => { requests.push(request); return HttpResponse.json(detail); }),
    http.get(`${origin}/api/evidence/:id`, ({ params }) => HttpResponse.json({ ...fakeEvidence, id: params.id })),
  );
  return detail;
}

describe('Step 51 运营页面', () => {
  it.each(cases)('%s 使用 Fake 接口渲染列表、详情、报告和精确证据', async (center, definition) => {
    const detail = mocks(center); mount(`/${center}`); const user = userEvent.setup();
    await user.click(await screen.findByRole('link', { name: detail.task.title }));
    await screen.findByRole('heading', { name: `${definition.title}详情`, level: 1 });
    expect(screen.getByRole('link', { name: '查看任务详情与状态时间线' })).toHaveAttribute('href', `/tasks/${taskId}`);
    if (center === 'architecture') expect(screen.getAllByText('需补充材料')).toHaveLength(12);
    if (center === 'automation') expect(screen.getByText('原始记录5')).toBeInTheDocument();
    if (center === 'inspections') { expect(screen.getByText('异常')).toBeInTheDocument(); expect(screen.getByText('待核实')).toBeInTheDocument(); }
    await user.click(screen.getAllByRole('link', { name: `查看证据 ${evidenceId}`, hidden: true }).at(-1)!);
    const drawer = await screen.findByRole('dialog'); await within(drawer).findByText('query_metrics');
    expect(within(drawer).getByText(evidenceId)).toBeInTheDocument();
    expect(requests.filter((request) => request.method !== 'GET')).toHaveLength(0);
  });

  it('运营状态与服务联合筛选，分页并在改筛选时回到第一页，详情返回保留筛选', async () => {
    const detail = mocks('releases');
    server.use(http.get(`${origin}/api/releases`, ({ request }) => { requests.push(request); return HttpResponse.json(page([detail], 21)); }));
    mount('/releases?status=invalid'); const user = userEvent.setup();
    await screen.findByRole('link', { name: detail.task.title }); expect(screen.getByLabelText('任务状态')).toHaveValue('');
    await user.click(screen.getByTitle('2')); await waitFor(() => expect(requests.at(-1)?.url).toContain('offset=20'));
    await user.selectOptions(screen.getByLabelText('任务状态'), 'WAITING_APPROVAL');
    await user.type(screen.getByLabelText('服务名称'), 'payment-service'); await user.click(screen.getByRole('button', { name: '查询服务' }));
    await waitFor(() => expect(requests.at(-1)?.url).toContain('service_name=payment-service'));
    expect(requests.at(-1)?.url).toContain('offset=0'); expect(requests.at(-1)?.url).toContain('status=WAITING_APPROVAL');
    await user.click(screen.getByRole('link', { name: detail.task.title })); await screen.findByText('报告与证据');
    await user.click(screen.getByRole('link', { name: '← 返回发布中心' }));
    expect(await screen.findByLabelText('任务状态')).toHaveValue('WAITING_APPROVAL'); expect(screen.getByLabelText('服务名称')).toHaveValue('payment-service');
  });

  it('风险列表和详情独立展示异常/恢复状态，三个 Evidence 引用均可读回', async () => {
    const notified = { ...fakeRisk, notification_evidence_id: reportId };
    server.use(http.get(`${origin}/api/risks`, () => HttpResponse.json(page([notified]))),
      http.get(`${origin}/api/risks/${riskId}`, () => HttpResponse.json(notified)),
      http.get(`${origin}/api/evidence/:id`, ({ params }) => HttpResponse.json({ ...fakeEvidence, id: params.id })));
    mount('/risks'); const user = userEvent.setup(); await user.click(await screen.findByRole('link', { name: '缺少 PDB' }));
    await screen.findByText('风险证据'); expect(screen.getByText('未恢复')).toBeInTheDocument();
    for (const link of screen.getAllByRole('link', { name: /查看证据/ })) {
      await user.click(link); const drawer = await screen.findByRole('dialog'); await within(drawer).findByText('query_metrics');
      await user.click(within(drawer).getByRole('button', { name: '关闭' })); await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    }
    expect(screen.getByRole('link', { name: '查看服务巡检记录' })).toHaveAttribute('href', '/inspections?service_name=payment-service');
  });

  it('风险类别、false 恢复状态与服务联合筛选不丢失，待核实不会显示健康', async () => {
    server.use(http.get(`${origin}/api/risks`, ({ request }) => { requests.push(request); return HttpResponse.json(page([{ ...fakeRisk, active: false, outcome: 'unknown' }])); }));
    mount('/risks?category=invalid&active=invalid'); const user = userEvent.setup(); await screen.findByText('待核实');
    await user.selectOptions(screen.getByLabelText('风险类别'), 'security'); await user.selectOptions(screen.getByLabelText('恢复状态'), 'false');
    await user.type(screen.getByLabelText('服务名称'), 'payment-service'); await user.click(screen.getByRole('button', { name: '查询服务' }));
    await waitFor(() => expect(requests.at(-1)?.url).toContain('service_name=payment-service'));
    expect(requests.at(-1)?.url).toContain('category=security'); expect(requests.at(-1)?.url).toContain('active=false'); expect(screen.queryByText('健康')).not.toBeInTheDocument();
  });

  it.each([404, 422, 503])('详情 %s 显示可恢复错误，重试不保留旧内容', async (status) => {
    server.use(http.get(`${origin}/api/tickets/${taskId}`, () => new HttpResponse(null, { status })));
    mount(`/tickets/${taskId}`); expect(await screen.findByRole('alert')).toHaveTextContent('读取失败');
    mocks('tickets'); await userEvent.setup().click(screen.getByRole('button', { name: '重试查询' })); await screen.findByText(/工单结论/);
  });

  it('401 清除运营缓存并返回登录', async () => {
    mocks('releases'); const client = mount('/releases'); await screen.findByText('发布中心样例');
    server.use(http.get(`${origin}/api/releases`, () => new HttpResponse(null, { status: 401 })));
    await userEvent.setup().click(screen.getByRole('button', { name: '刷新列表' })); await screen.findByRole('heading', { name: '欢迎回来' });
    expect(client.getQueriesData({ queryKey: ['operations'] })).toEqual([]);
  });

  it('未登录运营详情在登录后返回原链接和筛选', async () => {
    mocks('war-room'); mount(`/war-room/${taskId}?status=CLOSED`, false); const user = userEvent.setup();
    await user.type(await screen.findByLabelText('账户'), 'local-test-owner'); await user.type(screen.getByLabelText('密码'), 'fake-password-for-tests');
    await user.click(screen.getByRole('button', { name: '登录' })); await screen.findByRole('heading', { name: '重大保障详情' });
    expect(screen.getByRole('link', { name: '← 返回重大保障' })).toHaveAttribute('href', '/war-room?status=CLOSED');
  });

  it('巡检报告链接进入同服务风险中心，结束任务仍显示未恢复风险', async () => {
    mocks('inspections'); server.use(http.get(`${origin}/api/risks`, ({ request }) => { requests.push(request); return HttpResponse.json(page([fakeRisk])); }));
    mount(`/inspections/${taskId}`); const user = userEvent.setup(); await user.click(await screen.findByRole('link', { name: '查看关联服务风险' }));
    await screen.findByRole('link', { name: '缺少 PDB' }); expect(requests.at(-1)?.url).toContain('service_name=payment-service'); expect(within(screen.getByRole('table')).getByText('未恢复')).toBeInTheDocument();
  });

  it('切换运营详情时不显示上一场景证据，源文本按纯文本显示', async () => {
    mocks('tickets'); const dangerous = '<img src=x onerror=alert(1)>'; const detail = fixture('releases');
    detail.evidence[1]!.result_snapshot = { statement: dangerous, evidence_ids: [evidenceId] };
    server.use(http.get(`${origin}/api/releases`, () => HttpResponse.json(page([detail]))), http.get(`${origin}/api/releases/${taskId}`, () => HttpResponse.json(detail)));
    mount(`/tickets/${taskId}`); const user = userEvent.setup(); await screen.findByText(/工单结论/);
    await user.click(screen.getByRole('link', { name: '发布中心' })); await user.click(await screen.findByRole('link', { name: detail.task.title }));
    await screen.findByText(dangerous); expect(screen.queryByText('权限已独立验证并回填工单')).not.toBeInTheDocument(); expect(document.querySelector('img[src="x"]')).toBeNull();
  });
});
