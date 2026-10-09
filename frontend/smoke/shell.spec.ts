import { expect, test } from '@playwright/test';

const labels = ['总览', 'AI 任务中心', '事故中心', '服务与上下文图', '事件中心', '发布中心', '巡检中心',
  '工单中心', '运行手册中心', '风险中心', '重大保障', '架构评审', '自动化中心', '审批中心', '知识中心', '审计中心', 'AI 对话'];

test('真实 Cookie 登录、17 个入口、刷新、会话撤销与移动端导航', async ({ page, context }) => {
  const password = process.env.WEIPAI_FRONTEND_SMOKE_PASSWORD;
  if (!password) throw new Error('未收到进程内临时密码');
  const remote: string[] = [];
  const apiPaths: string[] = [];
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  await context.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname !== '127.0.0.1') {
      remote.push(url.origin); await route.abort(); return;
    }
    if (url.pathname.startsWith('/api/')) apiPaths.push(url.pathname);
    await route.continue();
  });
  await page.goto('/approvals');
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await page.screenshot({ path: '../.cache/frontend-smoke/login.png', fullPage: true, animations: 'disabled' });
  await page.getByLabel('账户', { exact: true }).fill('local-browser-owner');
  await page.getByLabel('密码', { exact: true }).fill(password);
  const loginResponse = page.waitForResponse((response) => response.url().endsWith('/api/auth/login'));
  await page.getByRole('button', { name: '登录', exact: true }).click();
  expect((await loginResponse).status()).toBe(200);
  await expect(page.getByRole('heading', { name: '审批中心', level: 1 })).toBeVisible();
  const cookies = await context.cookies();
  expect(cookies.find((cookie) => cookie.name === 'ops_session')).toMatchObject({ httpOnly: true, sameSite: 'Strict' });
  const nav = page.getByRole('navigation', { name: '主导航' });
  await expect(nav.getByRole('link')).toHaveCount(17);
  for (const label of labels) {
    await nav.getByRole('link', { name: label, exact: true }).click();
    await expect(page.getByRole('heading', { name: label, level: 1 })).toBeVisible();
  }
  await page.reload();
  await expect(page.getByRole('heading', { name: 'AI 对话', level: 1 })).toBeVisible();
  await nav.getByRole('link', { name: '总览', exact: true }).click();
  await expect(nav.locator('[aria-current="page"]')).toHaveText('总览');
  await page.screenshot({ path: '../.cache/frontend-smoke/desktop.png', fullPage: true, animations: 'disabled' });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: '../.cache/frontend-smoke/mobile.png', fullPage: true, animations: 'disabled' });
  await page.getByRole('button', { name: '打开导航' }).click();
  const drawer = page.getByRole('dialog');
  await expect(drawer.getByRole('link')).toHaveCount(17);
  await drawer.getByRole('link', { name: '知识中心', exact: true }).click();
  await expect(drawer).not.toBeVisible();
  await expect(page.getByRole('heading', { name: '知识中心', level: 1 })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  const logoutResponse = page.waitForResponse((response) => response.url().endsWith('/api/auth/logout'));
  await page.getByRole('button', { name: '退出登录' }).click();
  expect((await logoutResponse).status()).toBe(204);
  await expect(page).toHaveURL(/\/login$/);
  await page.goBack();
  await expect(page).toHaveURL(/\/login$/);
  expect((await page.request.get('/api/auth/me')).status()).toBe(401);
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0]);
  expect(apiPaths.every((path) => ['/api/metrics', '/api/audits', '/api/auth/me', '/api/auth/login', '/api/auth/logout', '/api/tasks', '/api/events', '/api/incidents', '/api/services', '/api/runbooks', '/api/knowledge', '/api/releases', '/api/tickets', '/api/inspections', '/api/risks', '/api/war-rooms', '/api/architecture-reviews', '/api/automations'].includes(path))).toBe(true);
  expect(remote).toEqual([]);
  expect(pageErrors).toEqual([]);
});
