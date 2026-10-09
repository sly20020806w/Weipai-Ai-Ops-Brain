import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { http, HttpResponse } from 'msw';
import { ConsoleApp } from '../app';
import { AppProviders } from '../providers';
import { authorize, origin, requests, server, session } from '../test/server';
import { fakeGraph, fakeGuide, fakeRule, guideId, ruleId } from '../test/cognition-data';
import type { KnowledgeInput, RunbookInput } from '../api/generated/types.gen';

function mount(path: string, signedIn = true) {
  authorize(signedIn);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<AppProviders client={client}><MemoryRouter initialEntries={[path]}><ConsoleApp /></MemoryRouter></AppProviders>);
  return client;
}
function page<T>(items: T[], total = items.length, offset = 0) { return { items, total, offset, limit: 20 }; }
function catalogs() {
  let guide = structuredClone(fakeGuide), rule = structuredClone(fakeRule);
  let guideExists = true, ruleExists = true;
  const bodies: Array<RunbookInput | KnowledgeInput> = [];
  server.use(
    http.get(`${origin}/api/runbooks`, ({ request }) => { requests.push(request); return HttpResponse.json(page(guideExists ? [guide] : [])); }),
    http.get(`${origin}/api/knowledge`, ({ request }) => { requests.push(request); return HttpResponse.json(page(ruleExists ? [rule] : [])); }),
    http.get(`${origin}/api/runbooks/${guideId}`, () => guideExists ? HttpResponse.json(guide) : new HttpResponse(null, { status: 404 })),
    http.get(`${origin}/api/knowledge/${ruleId}`, () => ruleExists ? HttpResponse.json(rule) : new HttpResponse(null, { status: 404 })),
    ...['post', 'put'].flatMap((method) => [
      http[method as 'post' | 'put'](`${origin}/api/runbooks${method === 'put' ? '/' + guideId : ''}`, async ({ request }) => {
        requests.push(request); const body = await request.json() as RunbookInput; bodies.push(body);
        guide = { ...guide, ...body, maturity: 'draft', content_version: 3 }; guideExists = true;
        return HttpResponse.json(guide, { status: method === 'post' ? 201 : 200 });
      }),
      http[method as 'post' | 'put'](`${origin}/api/knowledge${method === 'put' ? '/' + ruleId : ''}`, async ({ request }) => {
        requests.push(request); const body = await request.json() as KnowledgeInput; bodies.push(body);
        rule = { ...rule, ...body }; ruleExists = true; return HttpResponse.json(rule, { status: method === 'post' ? 201 : 200 });
      }),
    ]),
    http.delete(`${origin}/api/runbooks/${guideId}`, ({ request }) => { requests.push(request); guideExists = false; return new HttpResponse(null, { status: 204 }); }),
    http.delete(`${origin}/api/knowledge/${ruleId}`, ({ request }) => { requests.push(request); ruleExists = false; return new HttpResponse(null, { status: 204 }); }),
  );
  return bodies;
}
async function replace(label: string, value: string) { const user = userEvent.setup(); const field = screen.getByLabelText(label); await user.clear(field); await user.type(field, value, { skipClick: true }); }

describe('Step 50 认知页面', () => {
  it('服务列表进入 Fake 图，悬停边显示来源、置信度与新鲜度，键盘可选择边和节点', async () => {
    server.use(http.get(`${origin}/api/services`, () => HttpResponse.json(page([{ ...fakeGraph.nodes[0], name: '支付服务' }]))),
      http.get(`${origin}/api/services/payment-service`, ({ request }) => { requests.push(request); return HttpResponse.json(fakeGraph); }),
      http.get(`${origin}/api/services/payment-service/dependencies`, ({ request }) => { requests.push(request); return HttpResponse.json(fakeGraph); }));
    mount('/services'); const user = userEvent.setup();
    await user.click(await screen.findByRole('link', { name: '支付服务' }));
    const edge = await screen.findByRole('button', { name: '关系 payment-service → payment-db · USES · Fake ARMS' });
    await user.hover(edge); const tooltip = await screen.findByRole('tooltip');
    expect(tooltip).toHaveTextContent('Fake ARMS'); expect(tooltip).toHaveTextContent('99.0%'); expect(tooltip).toHaveTextContent('30 秒');
    await user.unhover(edge); edge.focus(); await user.keyboard('{Enter}');
    await screen.findByText('关系详情'); expect(screen.getAllByText('2026-10-08 01:59:30.000 UTC').length).toBeGreaterThan(0);
    await user.click(screen.getByRole('button', { name: '节点 payment-db' })); await screen.findByText('rds/payment-db');
    await user.selectOptions(screen.getByLabelText('邻居跳数'), '3'); await user.selectOptions(screen.getByLabelText('关系方向'), 'upstream');
    await waitFor(() => expect(requests.some((request) => request.url.includes('hops=3') && request.url.includes('direction=upstream'))).toBe(true));
  });

  it('成熟度和知识类型筛选、分页及无效 URL 值采用 API 合约', async () => {
    server.use(http.get(`${origin}/api/runbooks`, ({ request }) => { requests.push(request); const params = new URL(request.url).searchParams;
      return HttpResponse.json(page(params.get('maturity') === 'draft' ? [] : [fakeGuide], 21, Number(params.get('offset')))); }));
    mount('/runbooks?maturity=invalid'); const user = userEvent.setup(); await screen.findByRole('link', { name: fakeGuide.name });
    expect(screen.getByLabelText('手册成熟度')).toHaveValue('');
    await user.click(screen.getByTitle('2')); await waitFor(() => expect(requests.some((request) => request.url.includes('offset=20'))).toBe(true));
    await user.selectOptions(screen.getByLabelText('手册成熟度'), 'draft'); await screen.findByText('暂无运行手册');
    expect(requests.at(-1)?.url).toContain('offset=0'); expect(requests.at(-1)?.url).toContain('maturity=draft');
    await user.click(screen.getByRole('link', { name: '知识中心' }));
    await user.selectOptions(screen.getByLabelText('知识类型'), 'standard');
    await waitFor(() => expect(requests.some((request) => request.url.includes('kind=standard'))).toBe(true));
  });

  it('Knowledge 新建、编辑、删除后列表刷新，写入含 CSRF 和 UTC 时间', async () => {
    const bodies = catalogs(); mount('/knowledge'); const user = userEvent.setup();
    await screen.findByRole('link', { name: fakeRule.content });
    await user.click(screen.getByRole('link', { name: '新建知识' }));
    await user.type(screen.getByLabelText('知识内容'), '支付规则新建'); await user.type(screen.getByLabelText('知识来源'), '人工业务说明');
    await user.click(screen.getByRole('button', { name: '保存知识' })); await screen.findByRole('heading', { name: '知识详情' }); await screen.findByText('支付规则新建');
    await user.click(screen.getByRole('button', { name: '编辑知识' })); await replace('知识内容', '支付规则编辑');
    await user.click(screen.getByRole('button', { name: '保存知识' })); await screen.findByText('支付规则编辑');
    await user.click(screen.getByRole('link', { name: '← 知识列表' })); await screen.findByRole('link', { name: '支付规则编辑' });
    await user.click(screen.getByRole('link', { name: '支付规则编辑' })); await user.click(await screen.findByRole('button', { name: '删除知识' }));
    expect(requests.filter((request) => request.method === 'DELETE')).toHaveLength(0);
    await user.click(screen.getByRole('button', { name: '确认删除' })); await screen.findByText('暂无知识条目');
    expect(bodies).toHaveLength(2); expect((bodies[0] as KnowledgeInput).valid_from).toMatch(/Z$/);
    expect(requests.filter((request) => ['POST', 'PUT', 'DELETE'].includes(request.method)).every((request) => request.headers.get('X-CSRF-Token') === session.csrf_token)).toBe(true);
  });

  it('Runbook 编辑完整内容，管理字段不提交，退回草稿；删除刷新列表', async () => {
    const bodies = catalogs(); mount('/runbooks'); const user = userEvent.setup();
    await user.click(await screen.findByRole('link', { name: fakeGuide.name }));
    await screen.findByText(/任务标题 包含 网络维护/); await user.click(screen.getByRole('button', { name: '编辑手册' }));
    await replace('手册说明', '更新后的诊断');
    await user.click(screen.getByRole('button', { name: '保存手册' })); await screen.findByText('更新后的诊断');
    expect(screen.getByText('草稿 / 人工')).toBeInTheDocument();
    expect(bodies[0]).toMatchObject({ exclusion_conditions: fakeGuide.exclusion_conditions, diagnostic_steps: fakeGuide.diagnostic_steps, rollback_plan: fakeGuide.rollback_plan });
    expect(bodies[0]).not.toHaveProperty('maturity'); expect(bodies[0]).not.toHaveProperty('success_count'); expect(bodies[0]).not.toHaveProperty('actor');
    await user.click(screen.getByRole('button', { name: '删除手册' })); await user.click(screen.getByRole('button', { name: '确认删除' })); await screen.findByText('暂无运行手册');
  });

  it('Runbook 新建必须完整填写，新增排除/诊断/处理行能删除并提交正确内容', async () => {
    const bodies = catalogs(); mount('/runbooks/new'); const user = userEvent.setup(); await screen.findByLabelText('手册标识');
    for (const [label, value] of [['手册标识', 'new-payment-guide'], ['手册说明', '支付诊断'], ['手册来源', '复盘'], ['适用条件 1 值', 'payment-service'],
      ['诊断 1 说明', '读取上下文'], ['诊断 1 Tool 名称', 'get_service_context'], ['处理 1 说明', '批准后回滚'], ['回滚方案', '恢复原版本'], ['验证方式（每行一条）', '验证错误率\n验证 P99']]) {
      await user.type(screen.getByLabelText(label!), value!);
    }
    await user.click(screen.getByRole('button', { name: '添加排除条件' })); await user.type(screen.getByLabelText('排除条件 1 值'), '维护窗口');
    await user.click(screen.getByRole('button', { name: '添加诊断步骤' })); await user.click(screen.getByRole('button', { name: '删除诊断 2' }));
    await user.click(screen.getByRole('button', { name: '添加处理步骤' })); await user.click(screen.getByRole('button', { name: '删除处理 2' }));
    await user.click(screen.getByRole('button', { name: '保存手册' })); await screen.findByText('new-payment-guide', { selector: '.ant-card-head-title' });
    expect(bodies[0]).toMatchObject({ verification_steps: ['验证错误率', '验证 P99'], risk_level: 'L3', diagnostic_steps: [{ risk_level: 'L0', parameters: {} }], exclusion_conditions: [{ value: '维护窗口' }] });
  });

  it.each(['[]', '{bad'])('无效诊断 JSON %s 不发送更新；风险等级不能低于处理步骤', async (json) => {
    catalogs(); mount(`/runbooks/${guideId}`); const user = userEvent.setup(); await user.click(await screen.findByRole('button', { name: '编辑手册' }));
    const field = screen.getByLabelText('诊断 1 参数（JSON 对象）'); await user.clear(field); await user.paste(json);
    await user.click(screen.getByRole('button', { name: '保存手册' })); expect(await screen.findByRole('alert')).toHaveTextContent('JSON 对象');
    await user.clear(field); await user.paste('{}'); await user.selectOptions(screen.getByLabelText('风险等级', { exact: true }), 'L0');
    await user.click(screen.getByRole('button', { name: '保存手册' })); expect(await screen.findByRole('alert')).toHaveTextContent('不能低于');
    expect(requests.filter((request) => request.method === 'PUT')).toHaveLength(0);
  });

  it('知识有效期倒序被拒，编辑与取消保留已有 UTC 有效期', async () => {
    catalogs(); mount(`/knowledge/${ruleId}`); const user = userEvent.setup(); await user.click(await screen.findByRole('button', { name: '编辑知识' }));
    expect(screen.getByLabelText('生效时间（UTC）')).toHaveValue('2026-10-07T00:00');
    await user.clear(screen.getByLabelText('失效时间（UTC，可留空）')); await user.type(screen.getByLabelText('失效时间（UTC，可留空）'), '2025-01-01T00:00');
    await user.click(screen.getByRole('button', { name: '保存知识' })); expect(await screen.findByRole('alert')).toHaveTextContent('结束必须晚于');
    expect(requests.filter((request) => request.method === 'PUT')).toHaveLength(0);
    await user.click(screen.getByRole('button', { name: '取消' })); await screen.findByText('2026-12-31 00:00:00.000 UTC');
  });

  it.each([409, 422, 503])('保存 %s 不显示成功、不丢失输入、不自动重试', async (status) => {
    catalogs(); server.use(http.put(`${origin}/api/knowledge/${ruleId}`, async ({ request }) => { requests.push(request); await new Promise((resolve) => setTimeout(resolve, 150)); return new HttpResponse(null, { status }); }));
    mount(`/knowledge/${ruleId}`); const user = userEvent.setup(); await user.click(await screen.findByRole('button', { name: '编辑知识' })); await replace('知识内容', '保留草稿');
    await user.dblClick(screen.getByRole('button', { name: '保存知识' })); await screen.findByRole('alert'); expect(screen.getByLabelText('知识内容')).toHaveValue('保留草稿');
    expect(requests.filter((request) => request.method === 'PUT')).toHaveLength(1);
  });

  it.each([404, 422, 503])('图查询 %s 显示错误，重试后可以恢复', async (status) => {
    server.use(http.get(`${origin}/api/services/payment-service`, () => new HttpResponse(null, { status })));
    mount('/services/payment-service'); await screen.findByRole('alert');
    server.use(http.get(`${origin}/api/services/payment-service`, () => HttpResponse.json(fakeGraph)));
    await userEvent.setup().click(screen.getByRole('button', { name: '重试查询' })); await screen.findByRole('button', { name: '节点 payment-db' });
  });

  it('知识详情登录深链接恢复，文本安全展示，401 清理目录缓存', async () => {
    catalogs(); const text = '<img src=x onerror=alert(1)>';
    server.use(http.get(`${origin}/api/knowledge/${ruleId}`, () => HttpResponse.json({ ...fakeRule, content: text })));
    const client = mount(`/knowledge/${ruleId}`, false); const user = userEvent.setup();
    await user.type(await screen.findByLabelText('账户'), 'local-test-owner'); await user.type(screen.getByLabelText('密码'), 'fake-password-for-tests'); await user.click(screen.getByRole('button', { name: '登录' }));
    await screen.findByText(text); expect(document.querySelector('img[src="x"]')).toBeNull();
    server.use(http.get(`${origin}/api/knowledge/${ruleId}`, () => new HttpResponse(null, { status: 401 })));
    await act(async () => { await client.invalidateQueries({ queryKey: ['catalog', 'knowledge'] }); });
    await screen.findByRole('heading', { name: '欢迎回来' }); expect(client.getQueryData(['catalog', 'knowledge', 'detail', ruleId])).toBeUndefined();
  });
});
