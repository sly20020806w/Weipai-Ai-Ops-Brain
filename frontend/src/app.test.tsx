import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { http, HttpResponse } from 'msw';
import { ConsoleApp } from './app';
import { sessionExpiredEvent } from './api/client';
import { AppProviders } from './providers';
import { safeReturnPath } from './navigation';
import { authorize, origin, requests, server, session } from './test/server';

function LocationProbe() {
  const location = useLocation();
  return <output aria-label="当前路由">{location.pathname}</output>;
}

function mount(path = '/dashboard', state?: object) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const view = render(<AppProviders client={client}>
    <MemoryRouter initialEntries={[{ pathname: path, state }]}><ConsoleApp /><LocationProbe /></MemoryRouter>
  </AppProviders>);
  return { ...view, client };
}

async function login() {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText('账户'), 'local-test-owner');
  await user.type(screen.getByLabelText('密码'), 'fake-password-for-tests');
  await user.click(screen.getByRole('button', { name: '登录' }));
  return user;
}

describe('Step 47 前端外壳', () => {
  it('未登录深链接跳转登录，成功后返回原入口', async () => {
    mount('/approvals');
    await screen.findByRole('heading', { name: '欢迎回来' });
    expect(screen.getByLabelText('当前路由')).toHaveTextContent('/login');
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
    await login();
    await screen.findByRole('heading', { name: '审批中心', level: 1 });
    expect(screen.getByLabelText('当前路由')).toHaveTextContent('/approvals');
  });

  it('登录后 17 个入口按设计顺序出现，逐一导航到独立页面', async () => {
    authorize(); mount();
    const nav = await screen.findByRole('navigation', { name: '主导航' });
    const expected = ['总览', 'AI 任务中心', '事故中心', '服务与上下文图', '事件中心', '发布中心', '巡检中心',
      '工单中心', '运行手册中心', '风险中心', '重大保障', '架构评审', '自动化中心', '审批中心', '知识中心', '审计中心', 'AI 对话'];
    const links = within(nav).getAllByRole('link');
    expect(links.map((link) => link.textContent)).toEqual(expected);
    const user = userEvent.setup();
    for (const label of expected) {
      await user.click(within(nav).getByRole('link', { name: label }));
      expect(screen.getByRole('heading', { name: label, level: 1 })).toBeInTheDocument();
      expect(within(nav).getByRole('link', { name: label })).toHaveAttribute('aria-current', 'page');
    }
    expect(requests.every((request) => ['/api/metrics', '/api/audits', '/api/auth/me', '/api/tasks', '/api/events', '/api/incidents', '/api/services', '/api/runbooks', '/api/knowledge', '/api/releases', '/api/tickets', '/api/inspections', '/api/risks', '/api/war-rooms', '/api/architecture-reviews', '/api/automations'].includes(new URL(request.url).pathname))).toBe(true);
  }, 15_000);

  it('浏览器刷新通过服务端会话恢复，退出回登录，清空业务缓存', async () => {
    authorize();
    const { client } = mount('/services');
    await screen.findByRole('heading', { name: '服务与上下文图', level: 1 });
    expect(screen.getByText(session.actor)).toBeInTheDocument();
    client.setQueryData(['tasks'], ['fake-private-data']);
    await userEvent.setup().click(screen.getByRole('button', { name: '退出登录' }));
    await screen.findByRole('heading', { name: '欢迎回来' });
    expect(client.getQueryData(['tasks'])).toBeUndefined();
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it('会话失效立即隐藏控制台且返回登录', async () => {
    authorize(); const { client } = mount('/tickets');
    await screen.findByRole('heading', { name: '工单中心', level: 1 });
    client.setQueryData(['private-record'], { private: true });
    act(() => window.dispatchEvent(new Event(sessionExpiredEvent)));
    await screen.findByRole('heading', { name: '欢迎回来' });
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
    expect(client.getQueryData(['private-record'])).toBeUndefined();
  });

  it('错误密码显示失败并清空密码，不能显示控制台', async () => {
    mount('/login');
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText('账户'), 'owner');
    await user.type(screen.getByLabelText('密码'), 'wrong-password');
    await user.click(screen.getByRole('button', { name: '登录' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('账户或密码不正确');
    await waitFor(() => expect(screen.getByLabelText('密码')).toHaveValue(''));
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
  });

  it.each([403, 429, 503])('登录 %s 提供可重试提示，不自动重发密码', async (status) => {
    server.use(http.post(`${origin}/api/auth/login`, ({ request }) => {
      requests.push(request); return new HttpResponse(null, { status });
    }));
    mount('/login'); await login();
    expect(await screen.findByRole('alert')).toBeVisible();
    expect(requests.filter((request) => request.url.endsWith('/login'))).toHaveLength(1);
  });

  it('后端暂不可用显示连接错误，重试后才能进入登录', async () => {
    server.use(http.get(`${origin}/api/auth/me`, () => new HttpResponse(null, { status: 503 })));
    mount();
    expect(await screen.findByRole('alert')).toHaveTextContent('暂时无法验证会话');
    expect(screen.queryByLabelText('密码')).not.toBeInTheDocument();
    server.resetHandlers();
    await userEvent.setup().click(screen.getByRole('button', { name: '重新连接' }));
    await screen.findByRole('heading', { name: '欢迎回来' });
  });

  it('退出 503 保留会话并明确失败', async () => {
    authorize(); mount();
    server.use(http.post(`${origin}/api/auth/logout`, () => new HttpResponse(null, { status: 503 })));
    await screen.findByRole('heading', { name: '总览', level: 1 });
    await userEvent.setup().click(screen.getByRole('button', { name: '退出登录' }));
    expect(await screen.findByRole('alert')).toBeVisible();
    expect(screen.getByRole('navigation')).toBeInTheDocument();
  });

  it('未知路由有可恢复的中文页面', async () => {
    authorize(); mount('/unknown');
    await screen.findByRole('heading', { name: '页面未找到', level: 1 });
  });

  it('已经登录时访问登录页返回工作台', async () => {
    authorize(); mount('/login');
    await screen.findByRole('heading', { name: '总览', level: 1 });
    expect(screen.queryByLabelText('密码')).not.toBeInTheDocument();
  });

  it.each(['https://example.com', '//example.com', '/login', '/unknown', '/tasks\\evil', undefined, 42])(
    '拒绝无效登录返回路径 %s', (path) => expect(safeReturnPath(path)).toBe('/dashboard'),
  );

  it('登录页面无法通过路由状态跳转到其他站点', async () => {
    mount('/login', { from: 'https://example.com' }); await login();
    await screen.findByRole('heading', { name: '总览', level: 1 });
    expect(screen.getByLabelText('当前路由')).toHaveTextContent('/dashboard');
  });
});
