import { defineConfig } from '@playwright/test';

const baseURL = process.env.WEIPAI_FRONTEND_SMOKE_URL;
if (!baseURL || new URL(baseURL).hostname !== '127.0.0.1') throw new Error('浏览器验收要求隔离的本机演示服务');

export default defineConfig({
  testDir: './smoke', workers: 1, retries: 0, forbidOnly: true,
  timeout: 30_000, reporter: 'list',
  outputDir: '../.cache/frontend-smoke/results',
  use: {
    baseURL, channel: 'msedge', headless: true,
    viewport: { width: 1440, height: 1000 },
    // 凭证只在进程中；不录制请求体、Cookie 或带令牌的跟踪文件。
    trace: 'off', screenshot: 'off', video: 'off',
  },
});
