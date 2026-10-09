import { apiClient, ApiError, setCsrfToken } from '../api/client';
import { loginApiAuthLoginPost, logoutApiAuthLogoutPost, meApiAuthMeGet } from '../api/generated/sdk.gen';
import type { LoginApiAuthLoginPostData, SessionResponse } from '../api/generated/types.gen';

export async function readSession(signal?: AbortSignal): Promise<SessionResponse | null> {
  const { data, response } = await meApiAuthMeGet({ client: apiClient, signal });
  if (!response) throw new ApiError(0);
  if (response.status === 401) return null;
  if (!response.ok || !data) throw new ApiError(response.status);
  setCsrfToken(data.csrf_token);
  return data;
}

export async function signIn(body: LoginApiAuthLoginPostData['body']): Promise<SessionResponse> {
  const { data, response } = await loginApiAuthLoginPost({
    client: apiClient, body, headers: { 'X-Ops-Login': '1' },
  });
  if (!response) throw new ApiError(0);
  if (!response.ok || !data) throw new ApiError(response.status);
  setCsrfToken(data.csrf_token);
  return data;
}

export async function signOut(session: SessionResponse): Promise<void> {
  const { response } = await logoutApiAuthLogoutPost({
    client: apiClient, headers: { 'X-CSRF-Token': session.csrf_token },
  });
  if (!response) throw new ApiError(0);
  if (!response.ok && response.status !== 401) throw new ApiError(response.status);
  setCsrfToken(undefined);
}
