import { expect, test } from '@playwright/test';
import type { PageScenarioView, ScenarioDetail, PageRiskView } from '../src/api/generated/types.gen';

test('七类真实运营列表/详情、巡检风险与 Evidence、筛选刷新和手机布局', async ({ page, context }) => {
  test.setTimeout(120_000);
  const password = process.env.WEIPAI_FRONTEND_SMOKE_PASSWORD;
  const inspectionId = process.env.WEIPAI_FRONTEND_INSPECTION_ID;
  if (!password || !inspectionId) throw new Error('缺少隔离运营页面验收身份');
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
  const sessionResponse = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/auth/me');
  await page.goto(`/inspections/${inspectionId}?service_name=payment-service`);
  expect((await sessionResponse).status()).toBe(401);
  await expect(page).toHaveURL(/\/login$/, { timeout: 15_000 });
  await page.getByLabel('账户', { exact: true }).fill('local-browser-owner');
  await page.getByLabel('密码', { exact: true }).fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/inspections/${inspectionId}\\?service_name=payment-service$`));
  for (const [path, endpoint, label] of [
    ['releases', 'releases', '发布中心'], ['tickets', 'tickets', '工单中心'], ['inspections', 'inspections', '巡检中心'],
    ['war-room', 'war-rooms', '重大保障'], ['architecture', 'architecture-reviews', '架构评审'], ['automation', 'automations', '自动化中心'],
  ]) {
    const listResponse = await page.request.get(`/api/${endpoint}?service_name=payment-service`);
    expect(listResponse.status()).toBe(200);
    const list = await listResponse.json() as PageScenarioView;
    expect(list.items.length).toBeGreaterThan(0);
    const item = list.items.find((row) => row.task.status === 'CLOSED') ?? list.items[0];
    await page.goto(`/${path}?service_name=payment-service`);
    await expect(page.getByRole('heading', { name: label, exact: true })).toBeVisible();
    const titleLink = page.locator(`a[href^="/${path}/${item.task.id}"]`);
    await titleLink.click();
    await expect(page.getByRole('heading', { name: `${label}详情`, exact: true })).toBeVisible();
    const detailResponse = await page.request.get(`/api/${endpoint}/${item.task.id}`);
    const detail = await detailResponse.json() as ScenarioDetail;
    expect(detail.evidence.length).toBeGreaterThan(0);
    const report = detail.evidence.find((record) => record.source_tool.endsWith('.report') || record.source_tool === 'automation.suggestion') ?? detail.evidence.at(-1)!;
    const section = page.locator(`details:has(> p > a[aria-label="查看证据 ${report.id}"])`);
    if (!await section.evaluate((element) => (element as HTMLDetailsElement).open)) await section.locator('summary').click();
    await section.locator(':scope > p > a').click();
    const drawer = page.getByRole('dialog'); await expect(drawer.getByText(report.id, { exact: true })).toBeVisible();
    await expect(drawer.getByText(report.source_tool, { exact: true })).toBeVisible();
    await drawer.getByRole('button', { name: '关闭' }).click(); await expect(drawer).not.toBeVisible();
    if (path === 'architecture') {
      const reportSection = page.locator('details[open]').filter({ hasText: '十二维评审报告' });
      await expect(reportSection).toContainText('高可用'); await expect(reportSection).toContainText('发布和回滚');
      await page.screenshot({ path: '../.cache/frontend-smoke/operations-architecture.png', fullPage: true, animations: 'disabled' });
    }
  }
  await page.goto(`/inspections/${inspectionId}`);
  await expect(page.getByText('已关闭', { exact: true })).toBeVisible();
  await page.getByRole('link', { name: '查看关联服务风险' }).click();
  const riskResponse = await page.request.get('/api/risks?service_name=payment-service');
  const risks = await riskResponse.json() as PageRiskView;
  expect(risks.total).toBe(4);
  await expect(page.locator('tbody tr.ant-table-row')).toHaveCount(4);
  for (const label of ['缺少 PDB', '缺少 HPA', '证书有效期', '闲置 ECS']) await expect(page.getByRole('link', { name: label, exact: true })).toBeVisible();
  await page.screenshot({ path: '../.cache/frontend-smoke/operations-risks.png', fullPage: true, animations: 'disabled' });
  await page.getByLabel('风险类别', { exact: true }).selectOption('stability');
  await page.getByLabel('恢复状态', { exact: true }).selectOption('true');
  await expect(page.locator('tbody tr.ant-table-row')).toHaveCount(2); await page.reload();
  await expect(page.getByLabel('风险类别', { exact: true })).toHaveValue('stability');
  await expect(page.getByLabel('恢复状态', { exact: true })).toHaveValue('true');
  await page.getByRole('link', { name: '缺少 PDB', exact: true }).click();
  await expect(page.getByRole('heading', { name: '风险详情', exact: true })).toBeVisible();
  await expect(page.locator('.record-meta dd').filter({ hasText: /^未恢复$/ })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: '../.cache/frontend-smoke/operations-risk-mobile.png', fullPage: true, animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.getByRole('link', { name: /查看证据/ }).first().click();
  const riskDialog = page.getByRole('dialog');
  await expect(riskDialog).toBeVisible();
  await expect(riskDialog.locator('.record-meta').getByText(risks.items.find((risk) => risk.check_id === 'pdb_present')!.opening_evidence_id, { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.getByRole('dialog').getByRole('button', { name: '关闭' }).click();
  await page.getByRole('link', { name: '← 返回风险中心' }).click();
  await expect(page.getByLabel('风险类别', { exact: true })).toHaveValue('stability');
  await page.goto(`/inspections/${inspectionId}`); await expect(page.getByText('报告与证据', { exact: true })).toBeVisible();
  await page.screenshot({ path: '../.cache/frontend-smoke/operations-inspection-mobile.png', fullPage: true, animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  expect(remote).toEqual([]); expect(writes).toEqual([]); expect(errors).toEqual([]);
});
