import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// 使用同源代理保留 Cookie / Origin / CSRF；后端仍负责全部鉴权。
const target = new URL(process.env.WEIPAI_API_TARGET ?? 'http://127.0.0.1:8000');
if (target.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(target.hostname)
  || target.username || target.password || target.search || target.hash || target.pathname !== '/') {
  throw new Error('本地 API 代理只允许无凭证的回环 HTTP Origin');
}

export default defineConfig({
  envDir: false,
  plugins: [react()],
  build: {
    rolldownOptions: { output: { strictExecutionOrder: true, codeSplitting: { groups: [
      { name: 'react', test: /node_modules[\\/]\.pnpm[\\/](react|scheduler)[^\\/]*[\\/]/, priority: 20 },
      { name: 'ui', test: /node_modules[\\/]\.pnpm[\\/](antd|@ant-design|@rc-component|rc-)[^\\/]*[\\/]/, priority: 10, maxSize: 1_000_000 },
      { name: 'vendor', test: /node_modules/, priority: 5 },
    ] } } },
  },
  server: {
    host: '127.0.0.1', port: 5173, strictPort: true,
    proxy: { '/api': { target: target.origin, changeOrigin: false } },
  },
  preview: {
    host: '127.0.0.1', port: 5173, strictPort: true,
    proxy: { '/api': { target: target.origin, changeOrigin: false } },
  },
  test: {
    environment: 'jsdom',
    // 与本机数据库/Temporal 验收共用资源；长表单交互保留完整断言和有限时限。
    maxWorkers: 1,
    testTimeout: 15_000,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    environmentOptions: { jsdom: { url: 'http://127.0.0.1:5173/' } },
    restoreMocks: true,
  },
});
