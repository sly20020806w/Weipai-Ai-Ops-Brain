import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { http, HttpResponse } from 'msw';
import { ConsoleApp } from '../app';
import { AppProviders } from '../providers';
import { authorize, origin, requests, server } from '../test/server';
import { conclusionId, eventId, evidenceId, fakeCall, fakeConclusion, fakeEvent, fakeEvidence, fakeHistory, fakeIncident, fakeTask, incidentId, sectionTitles, taskId } from '../test/task-data';
import { statusLabels } from './labels';

function mount(path: string, signedIn = true) {
  authorize(signedIn);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<AppProviders client={client}><MemoryRouter initialEntries={[path]}><ConsoleApp /></MemoryRouter></AppProviders>);
  return client;
}
function page<T>(items: T[], total = items.length, offset = 0) { return { items, total, limit: 20, offset }; }
function details() {
  server.use(
    http.get(`${origin}/api/tasks/${taskId}`, ({ request }) => { requests.push(request); return HttpResponse.json(fakeTask); }),
    http.get(`${origin}/api/tasks/${taskId}/evidence`, ({ request }) => { requests.push(request); return HttpResponse.json(page([fakeEvidence, fakeConclusion])); }),
    http.get(`${origin}/api/tasks/${taskId}/status-history`, ({ request }) => { requests.push(request); return HttpResponse.json(fakeHistory); }),
    http.get(`${origin}/api/tasks/${taskId}/tool-calls`, ({ request }) => { requests.push(request); return HttpResponse.json(page([fakeCall])); }),
    ...[fakeEvidence, fakeConclusion, { ...fakeEvidence, id: incidentId, source_tool: 'postmortem', result_snapshot: fakeIncident.report }].map((record) =>
      http.get(`${origin}/api/evidence/${record.id}`, ({ request }) => { requests.push(request); return HttpResponse.json(record); })),
  );
}

describe('Step 48 任务类页面', () => {
  it('状态与来源联合筛选交给后端，结果与 Fake 数据一致，判断与审批各有独立选项', async () => {
    const rows = [fakeTask, { ...fakeTask, id: conclusionId, title: '待审批发布', status: 'WAITING_APPROVAL', source: 'Release' },
      { ...fakeTask, id: incidentId, title: '待判断告警', status: 'NEED_HUMAN_JUDGMENT', source: 'Alert' }];
    server.use(http.get(`${origin}/api/tasks`, ({ request }) => {
      requests.push(request); const params = new URL(request.url).searchParams;
      return HttpResponse.json(page(rows.filter((row) => (!params.get('status') || row.status === params.get('status'))
        && (!params.get('source') || row.source === params.get('source')))));
    }));
    mount('/tasks'); const user = userEvent.setup();
    await screen.findByRole('link', { name: fakeTask.title });
    const status = screen.getByLabelText('任务状态');
    expect(within(status).getAllByRole('option')).toHaveLength(18);
    await user.selectOptions(status, 'WAITING_APPROVAL');
    await screen.findByRole('link', { name: '待审批发布' });
    await waitFor(() => expect(screen.queryByRole('link', { name: fakeTask.title })).not.toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText('任务来源'), 'Release');
    await waitFor(() => expect(requests.some((request) => request.url.includes('status=WAITING_APPROVAL') && request.url.includes('source=Release'))).toBe(true));
    await user.selectOptions(status, 'NEED_HUMAN_JUDGMENT');
    await screen.findByText('没有符合筛选条件的任务');
    await user.selectOptions(screen.getByLabelText('任务来源'), 'Alert');
    await screen.findByRole('link', { name: '待判断告警' });
  });

  it('分页查询正确 offset，换筛选回第一页，刷新保留筛选，未知 URL 值不下发非法枚举', async () => {
    server.use(http.get(`${origin}/api/tasks`, ({ request }) => { requests.push(request);
      const offset = Number(new URL(request.url).searchParams.get('offset'));
      return HttpResponse.json(page([{ ...fakeTask, title: `任务 offset ${offset}` }], 21, offset));
    }));
    mount('/tasks?status=invalid&source=invalid'); const user = userEvent.setup();
    await screen.findByRole('link', { name: '任务 offset 0' });
    expect(screen.getByLabelText('任务状态')).toHaveValue('');
    expect(requests.some((request) => request.url.includes('invalid'))).toBe(false);
    await user.click(screen.getByTitle('2'));
    await screen.findByRole('link', { name: '任务 offset 20' });
    await user.selectOptions(screen.getByLabelText('任务状态'), 'CLOSED');
    await screen.findByRole('link', { name: '任务 offset 0' });
    await user.click(screen.getByRole('button', { name: '刷新列表' }));
    await waitFor(() => expect(requests.filter((request) => request.url.includes('status=CLOSED') && request.url.includes('offset=0')).length).toBeGreaterThan(1));
  });

  it('任务详情展示时间线、Tool、结论，每个 Evidence ID 可精确打开原快照', async () => {
    details(); mount(`/tasks/${taskId}`); const user = userEvent.setup();
    await screen.findByText('连接池扩大导致连接耗尽');
    expect(screen.getByText('告警创建任务')).toBeInTheDocument();
    expect(screen.getByText('开始调查')).toBeInTheDocument();
    expect(screen.getAllByText('query_metrics').length).toBeGreaterThan(0);
    expect(screen.getByText('待排除网络因素')).toBeInTheDocument();
    for (const id of [evidenceId, conclusionId]) {
      await user.click(screen.getAllByRole('link', { name: `查看证据 ${id}` })[0]!);
      const drawer = await screen.findByRole('dialog');
      await within(drawer).findByText(id, { selector: 'dd' });
      expect(within(drawer).getByText('payment-service')).toBeInTheDocument();
      expect(requests.some((request) => new URL(request.url).pathname === `/api/evidence/${id}`)).toBe(true);
      await user.click(within(drawer).getByRole('button', { name: '关闭' }));
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    }
    await user.click(screen.getByRole('button', { name: '刷新任务' }));
    await waitFor(() => expect(requests.filter((request) => request.url.endsWith('/status-history'))).toHaveLength(2));
  });

  it('超过一百条证据仍加载后页结论，不把缺结论误判成尚未调查', async () => {
    details(); const filler = Array.from({ length: 100 }, (_, index) => ({ ...fakeEvidence, id: `30000000-0000-4000-8000-${String(index + 100).padStart(12, '0')}` }));
    server.use(http.get(`${origin}/api/tasks/${taskId}/evidence`, ({ request }) => {
      requests.push(request); const offset = Number(new URL(request.url).searchParams.get('offset'));
      return HttpResponse.json({ ...page(offset === 0 ? filler : [fakeConclusion], 101, offset), limit: 100 });
    }));
    mount(`/tasks/${taskId}`); await screen.findByText('连接池扩大导致连接耗尽');
    expect(requests.some((request) => request.url.includes('offset=100'))).toBe(true);
    expect(screen.queryByText('尚无主 Agent 调查结论')).not.toBeInTheDocument();
  });

  it('Tool 调用独立分页，失败没有伪造 Evidence，调用参数可以展开', async () => {
    details(); server.use(http.get(`${origin}/api/tasks/${taskId}/tool-calls`, ({ request }) => {
      requests.push(request); const offset = Number(new URL(request.url).searchParams.get('offset'));
      return HttpResponse.json(page([{ ...fakeCall, evidence_id: null, outcome: 'denied', operation: offset ? 'query_logs' : 'query_metrics' }], 21, offset));
    }));
    mount(`/tasks/${taskId}?call_page=2`);
    await screen.findByText('query_logs');
    expect(screen.getByText('已拒绝')).toBeInTheDocument();
    expect(screen.getByText('无证据（查看调用详情）')).toBeInTheDocument();
    expect(requests.some((request) => request.url.includes('/tool-calls?') && request.url.includes('offset=20'))).toBe(true);
  });

  it('事件按来源和服务筛选，事件详情链接到同一任务', async () => {
    details(); server.use(
      http.get(`${origin}/api/events`, ({ request }) => { requests.push(request); return HttpResponse.json(page([fakeEvent])); }),
      http.get(`${origin}/api/events/${eventId}`, () => HttpResponse.json(fakeEvent)),
    );
    mount('/events'); const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText('任务来源'), 'Alert');
    await user.type(screen.getByLabelText('服务名称'), 'payment-service');
    await user.click(screen.getByRole('button', { name: '查询服务' }));
    await waitFor(() => expect(requests.some((request) => request.url.includes('source=Alert') && request.url.includes('service_name=payment-service'))).toBe(true));
    await user.click(await screen.findByRole('link', { name: fakeEvent.title }));
    await screen.findByText('fake-fingerprint');
    await user.click(screen.getByRole('link', { name: taskId }));
    await screen.findByRole('heading', { name: '任务详情', level: 1 });
  });

  it('事故列表按服务筛选；详情保留十三章、时间线、改进任务及可打开的引用', async () => {
    details(); server.use(
      http.get(`${origin}/api/incidents`, ({ request }) => { requests.push(request); return HttpResponse.json(page([fakeIncident])); }),
      http.get(`${origin}/api/incidents/${incidentId}`, () => HttpResponse.json(fakeIncident)),
    );
    mount('/incidents'); const user = userEvent.setup();
    await user.type(await screen.findByLabelText('服务名称'), 'payment-service');
    await user.click(screen.getByRole('button', { name: '查询服务' }));
    await waitFor(() => expect(requests.some((request) => request.url.includes('/incidents?') && request.url.includes('service_name=payment-service'))).toBe(true));
    await user.click(await screen.findByRole('link', { name: '支付故障复盘' }));
    await screen.findByText('支付服务部署新版本');
    for (const title of sectionTitles) expect(screen.getByText(`${title}：Fake 证据支持的结论`)).toBeInTheDocument();
    expect(screen.getByText('完善连接数告警')).toBeInTheDocument();
    await user.click(screen.getAllByRole('link', { name: `查看证据 ${evidenceId}` })[0]!);
    const drawer = await screen.findByRole('dialog');
    await within(drawer).findByText('error_rate');
  });

  it.each([404, 422, 503])('详情 %s 显示失败且可重试，不显示假成功', async (status) => {
    details(); server.use(http.get(`${origin}/api/tasks/${taskId}`, () => new HttpResponse(null, { status })));
    mount(`/tasks/${taskId}`); expect(await screen.findByRole('alert')).toHaveTextContent('读取失败');
    expect(screen.queryByText('调查结论')).not.toBeInTheDocument();
    details(); await userEvent.setup().click(screen.getByRole('button', { name: '重试查询' }));
    await screen.findByText('连接池扩大导致连接耗尽');
  });

  it('切换到不存在的证据时不残留上一条快照', async () => {
    details(); const absent = '30000000-0000-4000-8000-000000000099';
    const reference = { ...fakeEvidence, result_snapshot: { evidence_id: absent, secret_marker: '上条快照' } };
    server.use(http.get(`${origin}/api/evidence/${evidenceId}`, () => HttpResponse.json(reference)),
      http.get(`${origin}/api/evidence/${absent}`, () => new HttpResponse(null, { status: 404 })));
    mount(`/tasks/${taskId}?evidence=${evidenceId}`);
    const drawer = await screen.findByRole('dialog'); await within(drawer).findByText('上条快照');
    await userEvent.setup().click(within(drawer).getByRole('link', { name: `查看证据 ${absent}` }));
    expect(await within(drawer).findByRole('alert')).toHaveTextContent('记录不存在');
    expect(within(drawer).queryByText('上条快照')).not.toBeInTheDocument();
  });

  it('业务查询 401 返回登录并清空全部任务缓存', async () => {
    details(); const client = mount(`/tasks/${taskId}`);
    await screen.findByText('连接池扩大导致连接耗尽');
    server.use(http.get(`${origin}/api/tasks/${taskId}`, () => new HttpResponse(null, { status: 401 })));
    await act(async () => { await client.invalidateQueries({ queryKey: ['task', taskId] }); });
    await screen.findByRole('heading', { name: '欢迎回来' });
    expect(client.getQueryData(['task-evidence', taskId])).toBeUndefined();
  });

  it('未登录的详情深链接登录后回到原任务和证据', async () => {
    details(); mount(`/tasks/${taskId}?evidence=${evidenceId}`, false); const user = userEvent.setup();
    await user.type(await screen.findByLabelText('账户'), 'local-test-owner');
    await user.type(screen.getByLabelText('密码'), 'fake-password-for-tests');
    await user.click(screen.getByRole('button', { name: '登录' }));
    await screen.findByRole('heading', { name: '任务详情', level: 1 });
    const drawer = await screen.findByRole('dialog'); await within(drawer).findByText('error_rate');
  });

  it('无结论与空调用正确显示；源系统文本作为普通文本展示', async () => {
    details(); const text = '<img src=x onerror=alert(1)>';
    server.use(http.get(`${origin}/api/tasks/${taskId}/evidence`, () => HttpResponse.json(page([{ ...fakeEvidence, result_snapshot: text }]))),
      http.get(`${origin}/api/tasks/${taskId}/tool-calls`, () => HttpResponse.json(page([]))),
      http.get(`${origin}/api/evidence/${evidenceId}`, () => HttpResponse.json({ ...fakeEvidence, result_snapshot: text })));
    mount(`/tasks/${taskId}?evidence=${evidenceId}`);
    await screen.findByText('尚无主 Agent 调查结论'); await screen.findByText('暂无 Tool 调用');
    const drawer = await screen.findByRole('dialog');
    expect(await within(drawer).findByText(text)).toBeInTheDocument();
    expect(drawer.querySelector('img[src="x"]')).toBeNull();
    expect(Object.keys(statusLabels)).toHaveLength(17);
  });
});
