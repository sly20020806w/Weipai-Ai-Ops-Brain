import { createClient } from './generated/client';

// 会话 Cookie 由浏览器管理；CSRF 只留在内存，刷新后从 /auth/me 重新获取。
let csrfToken: string | undefined;
export const sessionExpiredEvent = 'ops-session-expired';
export const apiClient = createClient({
  baseUrl: window.location.origin, credentials: 'same-origin', cache: 'no-store',
});

export function setCsrfToken(value: string | undefined) {
  csrfToken = value;
}

export function csrfHeaders() {
  if (!csrfToken) throw new Error('会话校验信息已失效，请重新登录。');
  return { 'X-CSRF-Token': csrfToken };
}

apiClient.interceptors.request.use((request) => {
  const url = new URL(request.url);
  if (url.origin !== window.location.origin) throw new Error('API 请求必须同源');
  if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method) && url.pathname !== '/api/auth/login' && csrfToken) {
    request.headers.set('X-CSRF-Token', csrfToken);
  }
  return request;
});

apiClient.interceptors.response.use((response, request) => {
  if (response.status === 401 && !request.signal.aborted && new URL(request.url).pathname !== '/api/auth/login') {
    csrfToken = undefined;
    window.dispatchEvent(new Event(sessionExpiredEvent));
  }
  return response;
});

export class ApiError extends Error {
  constructor(public readonly status: number) {
    super(status === 401 ? '账户或密码不正确，请重新输入。'
      : status === 403 ? '请求校验失败，请刷新页面后重试。'
      : status === 429 ? '登录尝试过于频繁，请稍后重试。'
      : status === 422 ? '输入格式不正确，请检查后重试。'
      : status === 503 ? '登录服务暂时不可用，请稍后重试。'
      : '暂时无法连接服务，请检查本地服务后重试。');
    this.name = 'ApiError';
  }
}
