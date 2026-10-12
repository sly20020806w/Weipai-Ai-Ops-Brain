const { chromium, expect } = require('../frontend/node_modules/@playwright/test');
const fs = require('node:fs/promises');
(async () => {
  const origin = process.argv[2];
  if (new URL(origin).hostname !== '127.0.0.1') throw new Error('只允许本机镜像地址');
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const errors = [], remote = [];
    await context.route('**/*', async route => {
      if (new URL(route.request().url()).origin !== origin) {
        remote.push('external-request'); await route.abort(); return;
      }
      await route.continue();
    });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(origin + '/approvals');
    await expect(page).toHaveURL(/\/login$/);
    await page.getByLabel('账户', { exact: true }).fill('image-check');
    await page.getByLabel('密码', { exact: true }).fill(process.env.IMAGE_BROWSER_PASSWORD);
    const response = page.waitForResponse(response => response.url().endsWith('/api/auth/login'));
    await page.getByRole('button', { name: '登录', exact: true }).click();
    expect((await response).status()).toBe(200);
    await expect(page.getByRole('heading', { name: '审批中心', level: 1 })).toBeVisible();
    const nav = page.getByRole('navigation', { name: '主导航' });
    await expect(nav.getByRole('link')).toHaveCount(17);
    const labels = await nav.getByRole('link').allTextContents();
    for (const label of labels) {
      await nav.getByRole('link', { name: label.trim(), exact: true }).click();
      await expect(page.getByRole('heading', { name: label.trim(), level: 1 })).toBeVisible();
    }
    await page.reload();
    await expect(page.getByRole('heading', { name: 'AI 对话', level: 1 })).toBeVisible();
    await fs.mkdir('F:/Weipai AI Ops Brain/.cache/images-browser', { recursive: true });
    await page.screenshot({ path: 'F:/Weipai AI Ops Brain/.cache/images-browser/desktop.png' });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole('button', { name: '打开导航' }).click();
    await page.getByRole('dialog').getByRole('link', { name: 'AI 任务中心', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'AI 任务中心', level: 1 })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
    await page.screenshot({ path: 'F:/Weipai AI Ops Brain/.cache/images-browser/mobile.png' });
    const logout = page.waitForResponse(response => response.url().endsWith('/api/auth/logout'));
    await page.getByRole('button', { name: '退出登录' }).click();
    expect((await logout).status()).toBe(204);
    await expect(page).toHaveURL(/\/login$/);
    expect((await page.request.get(origin + '/api/auth/me')).status()).toBe(401);
    expect(errors).toEqual([]);
    expect(remote).toEqual([]);
    console.log('镜像 Edge 浏览器：17 入口、刷新、手机导航、登录/退出和无外部请求全部通过');
  } finally { await browser.close(); }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
