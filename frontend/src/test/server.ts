import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import type { SessionResponse } from '../api/generated/types.gen';
import { emptyMetrics } from './metrics-data';

export const origin = 'http://127.0.0.1:5173';
export const session: SessionResponse = {
  actor: 'local-test-owner', csrf_token: 'fake-session-csrf', expires_at: '2099-01-01T00:00:00Z',
};
export const requests: Request[] = [];
let signedIn = false;
export function authorize(value = true) { signedIn = value; }
export function resetServer() { signedIn = false; requests.length = 0; server.resetHandlers(); }

export const server = setupServer(
  ...['audits', 'tasks', 'events', 'incidents', 'services', 'runbooks', 'knowledge', 'releases', 'tickets', 'inspections', 'risks', 'war-rooms', 'architecture-reviews', 'automations'].map((path) => http.get(`${origin}/api/${path}`, ({ request }) => {
    requests.push(request); return HttpResponse.json({ items: [], total: 0, limit: 20, offset: 0 });
  })),
  http.get(`${origin}/api/metrics`, ({ request }) => { requests.push(request); return HttpResponse.json(emptyMetrics); }),
  http.get(`${origin}/api/auth/me`, ({ request }) => {
    requests.push(request);
    return signedIn ? HttpResponse.json(session) : HttpResponse.json({ detail: '请先登录' }, { status: 401 });
  }),
  http.post(`${origin}/api/auth/login`, async ({ request }) => {
    requests.push(request);
    const body = await request.json();
    if (request.headers.get('X-Ops-Login') !== '1') return new HttpResponse(null, { status: 403 });
    if (typeof body !== 'object' || body === null || !('password' in body) || body.password !== 'fake-password-for-tests') {
      return HttpResponse.json({ detail: '登录失败' }, { status: 401 });
    }
    signedIn = true;
    return HttpResponse.json(session);
  }),
  http.post(`${origin}/api/auth/logout`, ({ request }) => {
    requests.push(request);
    if (request.headers.get('X-CSRF-Token') !== session.csrf_token) return new HttpResponse(null, { status: 403 });
    signedIn = false;
    return new HttpResponse(null, { status: 204 });
  }),
);
