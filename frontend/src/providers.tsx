import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { PropsWithChildren } from 'react';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false, refetchOnWindowFocus: true },
    mutations: { retry: false },
  },
});

export function AppProviders({ children, client = queryClient }: PropsWithChildren<{ client?: QueryClient }>) {
  return (
    <ConfigProvider locale={zhCN} theme={{ token: {
      colorPrimary: '#5668e8', borderRadius: 10, colorText: '#243047',
      fontFamily: '"Segoe UI", "Microsoft YaHei", sans-serif',
    } }}>
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    </ConfigProvider>
  );
}
