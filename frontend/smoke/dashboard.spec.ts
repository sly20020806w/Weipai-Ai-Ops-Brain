import { expect, test } from '@playwright/test';
import type { MetricsReport, PageTaskView, PageAuditView } from '../src/api/generated/types.gen';

test('真实总览待审批与审批中心一致、十项指标、审计联合筛选与 Evidence', async ({ page, context }) => {
  test.setTimeout(90_000);
  const password = process.env.WEIPAI_FRONTEND_SMOKE_PASSWORD;
  if (!password) throw new Error('缺少隔离验收身份');
  const remote: string[] = [], writes: string[] = [], errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await context.route('**/*', async (route) => {
    const request = route.request(), url = new URL(request.url());
    if (url.hostname !== '127.0.0.1') { remote.push(url.origin); await route.abort(); return; }
    if (url.pathname.startsWith('/api/') && request.method() !== 'GET' && url.pathname !== '/api/auth/login') {
      writes.push(url.pathname); await route.abort(); return;
    }
    await route.continue();
  });
  await page.goto('/dashboard'); await expect(page).toHaveURL(/\/login$/);
  await page.getByLabel('账户', { exact: true }).fill('local-browser-owner');
  await page.getByLabel('密码', { exact: true }).fill(password); await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('heading', { name: '总览', exact: true })).toBeVisible();
  for (const [status, label, kind] of [
    ['WAITING_APPROVAL', '待审批', 'approval'], ['NEED_HUMAN_JUDGMENT', '待人工判断', 'judgment'], ['WAITING_INFORMATION', '待补充信息', 'information'],
  ]) {
    const response = await page.request.get(`/api/tasks?status=${status}&limit=20`); expect(response.status()).toBe(200);
    const data = await response.json() as PageTaskView; expect(data.total).toBeGreaterThan(0);
    await expect(page.getByLabel(`${label}数量`, { exact: true })).toHaveText(String(data.total));
    await page.getByRole('link', { name: `处理${label}事项`, exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/approvals\\?kind=${kind}$`));
    await expect(page.getByText(`共 ${data.total} 条`, { exact: true })).toBeVisible();
    await page.goto('/dashboard');
  }
  const metricsResponse = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/metrics');
  await page.getByRole('button', { name: '刷新总览', exact: true }).click();
  const report = await (await metricsResponse).json() as MetricsReport;
  expect(report.metrics).toHaveLength(10);
  for (const metric of report.metrics) {
    const card = page.locator(`[data-metric="${metric.name}"]`); await expect(card).toBeVisible();
    await card.locator('summary').click();
    const values = card.locator('dd');
    await expect(values.nth(0)).toHaveText(metric.value === null ? '未知' : String(metric.value));
    await expect(values.nth(1)).toHaveText(String(metric.numerator)); await expect(values.nth(2)).toHaveText(String(metric.denominator));
    await expect(values.nth(3)).toHaveText(metric.unit);
  }
  await page.screenshot({ path: '../.cache/frontend-smoke/dashboard-desktop.png', fullPage: true, animations: 'disabled' });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: '../.cache/frontend-smoke/dashboard-mobile.png', fullPage: true, animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/audit');
  await page.getByLabel('操作人', { exact: true }).fill('local-browser-seed');
  await page.getByLabel('操作类型', { exact: true }).selectOption('catalog_edit');
  await page.getByRole('button', { name: '查询审计', exact: true }).click();
  await expect(page.locator('tbody tr.ant-table-row')).toHaveCount(2);
  const start = await page.getByLabel('开始时间（UTC）', { exact: true }).inputValue();
  const end = await page.getByLabel('结束时间（UTC，不含）', { exact: true }).inputValue();
  const dataResponse = await page.request.get(`/api/audits?actor=local-browser-seed&event_type=catalog_edit&start=${encodeURIComponent(start + 'Z')}&end=${encodeURIComponent(end + 'Z')}`);
  const data = await dataResponse.json() as PageAuditView;
  expect(data.total).toBe(2); expect(data.items.every((row) => row.actor === 'local-browser-seed' && row.event_type === 'catalog_edit')).toBe(true);
  await page.getByRole('button', { name: '应用时间窗', exact: true }).click(); await page.reload();
  await expect(page.getByLabel('操作人', { exact: true })).toHaveValue('local-browser-seed');
  await expect(page.getByLabel('操作类型', { exact: true })).toHaveValue('catalog_edit');
  await expect(page.locator('tbody tr.ant-table-row')).toHaveCount(2);
  await page.locator(`a[href^="/audit/${data.items[0].id}"]`).click();
  await expect(page.getByRole('heading', { name: '审计详情', exact: true })).toBeVisible();
  await expect(page.locator('.record-meta dd').filter({ hasText: /^local-browser-seed$/ })).toBeVisible();
  await page.getByRole('link', { name: '← 返回审计中心', exact: true }).click(); await expect(page.getByLabel('操作人')).toHaveValue('local-browser-seed');
  const toolResponse = await page.request.get('/api/audits?event_type=tool_call&actor=codex-main-agent');
  expect(toolResponse.status()).toBe(200);
  const tools = await toolResponse.json() as PageAuditView;
  const record = tools.items.find((row) => row.evidence_id); expect(record).toBeTruthy();
  await page.goto(`/audit/${record!.id}`); await expect(page.getByText(record!.id, { exact: true })).toBeVisible();
  await page.screenshot({ path: '../.cache/frontend-smoke/audit-desktop.png', fullPage: true, animations: 'disabled' });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('link', { name: `查看证据 ${record!.evidence_id}`, exact: true }).first().click();
  const drawer = page.getByRole('dialog'); await expect(drawer.getByText(record!.evidence_id!, { exact: true })).toBeVisible();
  await page.screenshot({ path: '../.cache/frontend-smoke/audit-evidence-mobile.png', fullPage: true, animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  expect(remote).toEqual([]); expect(writes).toEqual([]); expect(errors).toEqual([]);
});
