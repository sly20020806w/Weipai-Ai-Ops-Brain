import { expect, test } from '@playwright/test';

test('真实 API 任务筛选、证据读回、复盘与事件关联，以及手机布局', async ({ page, context }) => {
  test.setTimeout(90_000);
  const taskId = process.env.WEIPAI_FRONTEND_TASK_ID;
  const incidentId = process.env.WEIPAI_FRONTEND_INCIDENT_ID;
  const eventId = process.env.WEIPAI_FRONTEND_EVENT_ID;
  const password = process.env.WEIPAI_FRONTEND_SMOKE_PASSWORD;
  if (!taskId || !incidentId || !eventId || !password) throw new Error('缺少隔离验收数据身份');
  const remote: string[] = [];
  const errors: string[] = [];
  const writes: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await context.route('**/*', async (route) => {
    const request = route.request(); const url = new URL(request.url());
    if (url.hostname !== '127.0.0.1') { remote.push(url.origin); await route.abort(); return; }
    if (url.pathname.startsWith('/api/') && request.method() !== 'GET' && !url.pathname.startsWith('/api/auth/')) writes.push(url.pathname);
    await route.continue();
  });
  await page.goto(`/tasks/${taskId}`);
  expect(errors).toEqual([]);
  await expect(page).toHaveURL(/\/login$/);
  await page.getByLabel('账户', { exact: true }).fill('local-browser-owner');
  await page.getByLabel('密码', { exact: true }).fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/tasks/${taskId}$`));
  await expect(page.getByText('最近一次结论', { exact: false })).toBeVisible();
  await expect(page.getByText('状态时间线', { exact: true })).toBeVisible();
  await expect(page.getByText('Tool 调用', { exact: true })).toBeVisible();
  await expect(page.getByText('证据链', { exact: true })).toBeVisible();
  await expect(page.getByRole('navigation').locator('[aria-current="page"]')).toHaveText('AI 任务中心');
  const original = await (await page.request.get(`/api/tasks/${taskId}/evidence?limit=100`)).json();
  const conclusion = original.items.find((row: { source_tool: string }) => row.source_tool === 'agent.conclusion');
  expect(conclusion).toBeTruthy();
  const reference = conclusion.result_snapshot.root_cause.evidence_ids[0];
  const source = await (await page.request.get(`/api/evidence/${reference}`)).json();
  await page.getByRole('link', { name: `查看证据 ${reference}`, exact: true }).first().click();
  const drawer = page.getByRole('dialog');
  await expect(drawer.getByText(reference, { exact: true }).first()).toBeVisible();
  await expect(drawer.getByText(source.source_tool, { exact: true }).first()).toBeVisible();
  await expect(drawer.getByText('结果快照', { exact: true })).toBeVisible();
  await page.screenshot({ path: '../.cache/frontend-smoke/task-evidence.png', fullPage: true, animations: 'disabled' });
  await page.reload();
  await expect(page.getByRole('dialog').getByText(reference, { exact: true }).first()).toBeVisible();
  await page.getByRole('dialog').getByRole('button', { name: '关闭', exact: true }).click();
  await page.screenshot({ path: '../.cache/frontend-smoke/task-detail.png', fullPage: true, animations: 'disabled' });
  await page.goto('/tasks');
  await page.getByLabel('任务状态', { exact: true }).selectOption('CLOSED');
  await page.getByLabel('任务来源', { exact: true }).selectOption('Alert');
  await expect(page.locator('tbody tr.ant-table-row')).toHaveCount(1);
  await expect(page.locator('tbody tr.ant-table-row')).toContainText('支付 5xx 发布故障复盘');
  await page.reload();
  await expect(page.getByLabel('任务状态', { exact: true })).toHaveValue('CLOSED');
  await expect(page.getByLabel('任务来源', { exact: true })).toHaveValue('Alert');
  await page.screenshot({ path: '../.cache/frontend-smoke/task-list.png', fullPage: true, animations: 'disabled' });
  await page.goto(`/incidents/${incidentId}`);
  await expect(page.getByRole('heading', { name: '事故复盘', level: 1 })).toBeVisible();
  for (const title of ['事件现象', '影响范围', 'Timeline', '根因', '处理过程', '验证结果', '为什么没有提前发现',
    '监控改进', '告警改进', '架构改进', '自动化建议', 'Runbook变更']) {
    await expect(page.locator('.ant-card-head-title').filter({ hasText: title })).toBeVisible();
  }
  await page.screenshot({ path: '../.cache/frontend-smoke/incident.png', fullPage: true, animations: 'disabled' });
  await page.goto(`/events/${eventId}`);
  await expect(page.getByText('payment-service', { exact: true })).toBeVisible();
  await page.getByRole('link', { name: taskId, exact: true }).click();
  await expect(page.getByRole('heading', { name: '任务详情', level: 1 })).toBeVisible();
  await expect(page.locator('.query-state[role="status"]')).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: '../.cache/frontend-smoke/task-mobile.png', fullPage: true, animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.getByRole('link', { name: `查看证据 ${reference}`, exact: true }).first().click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.screenshot({ path: '../.cache/frontend-smoke/evidence-mobile.png', fullPage: true, animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  expect(remote).toEqual([]); expect(errors).toEqual([]); expect(writes).toEqual([]);
});
