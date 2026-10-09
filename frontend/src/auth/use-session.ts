import { useEffect } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { sessionExpiredEvent, setCsrfToken } from '../api/client';
import { readSession } from './api';

export const sessionKey = ['session'] as const;

export function useSession() {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: sessionKey,
    queryFn: ({ signal }) => readSession(signal),
    staleTime: 30_000, retry: false,
    // 会话查询是页面进入/聚焦时的身份校验，不承担 AI Task 生命周期。
    refetchOnWindowFocus: true,
  });
  useEffect(() => {
    const expired = () => {
      void client.cancelQueries();
      client.removeQueries({ predicate: (entry) => entry.queryKey[0] !== 'session' });
      client.setQueryData(sessionKey, null);
      setCsrfToken(undefined);
    };
    window.addEventListener(sessionExpiredEvent, expired);
    return () => window.removeEventListener(sessionExpiredEvent, expired);
  }, [client]);
  return query;
}
