import { describe, expect, it, vi } from 'vitest';
import { http, HttpResponse } from 'msw';
import { ApiError, apiClient, sessionExpiredEvent, setCsrfToken } from '../api/client';
import { listTasksApiTasksGet } from '../api/generated/sdk.gen';
import { authorize, origin, requests, server, session } from '../test/server';
import { readSession, signIn, signOut } from './api';

describe('生成客户端与现有鉴权契约', () => {
  it('未登录返回空会话；登录携带必需请求头和同源 Cookie 策略', async () => {
    expect(await readSession()).toBeNull();
    expect(await signIn({ username: session.actor, password: 'fake-password-for-tests' })).toEqual(session);
    expect(await readSession()).toEqual(session);
    const login = requests.find((request) => request.url.endsWith('/login'))!;
    expect(login.headers.get('X-Ops-Login')).toBe('1');
    expect(login.credentials).toBe('same-origin');
    expect(login.cache).toBe('no-store');
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it('刷新读取会话后，退出携带 CSRF 并撤销会话', async () => {
    authorize();
    const refreshed = await readSession();
    await signOut(refreshed!);
    expect(requests.at(-1)?.headers.get('X-CSRF-Token')).toBe(session.csrf_token);
    expect(await readSession()).toBeNull();
  });

  it.each([401, 403, 422, 429, 503])('登录 %s 不得成为成功会话', async (status) => {
    server.use(http.post(`${origin}/api/auth/login`, () => HttpResponse.json({ detail: '不展示服务端原始输入' }, { status })));
    await expect(signIn({ username: 'owner', password: 'fake-password-for-tests' })).rejects.toMatchObject({ status });
  });

  it('会话服务 503 不得被当作未登录', async () => {
    server.use(http.get(`${origin}/api/auth/me`, () => new HttpResponse(null, { status: 503 })));
    await expect(readSession()).rejects.toBeInstanceOf(ApiError);
  });

  it('任意生成业务 SDK 的 401 触发会话失效通知', async () => {
    const listener = vi.fn();
    window.addEventListener(sessionExpiredEvent, listener);
    server.use(http.get(`${origin}/api/tasks`, () => new HttpResponse(null, { status: 401 })));
    try {
      await listTasksApiTasksGet({ client: apiClient });
      expect(listener).toHaveBeenCalledOnce();
    } finally { window.removeEventListener(sessionExpiredEvent, listener); }
  });

  it('写请求复用内存 CSRF，拒绝向其他 Origin 发送会话', async () => {
    setCsrfToken(session.csrf_token);
    server.use(http.post(`${origin}/api/probe`, ({ request }) => {
      expect(request.headers.get('X-CSRF-Token')).toBe(session.csrf_token);
      return new HttpResponse(null, { status: 204 });
    }));
    await apiClient.post({ url: '/api/probe' });
    const refused = await apiClient.get({ url: '/api/auth/me', baseUrl: 'https://example.com' });
    expect(refused.error).toMatchObject({ message: 'API 请求必须同源' });
    expect(refused.response).toBeUndefined();
  });
});
