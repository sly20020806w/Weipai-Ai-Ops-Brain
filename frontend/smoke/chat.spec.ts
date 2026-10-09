import { expect, test } from '@playwright/test';

test('真实 SSE 问答/追问、Evidence、Human 任务与审批门禁，以及手机布局', async ({ page, context }) => {
  test.setTimeout(150_000);
  const password = process.env.WEIPAI_FRONTEND_SMOKE_PASSWORD;
  if (!password) throw new Error('缺少进程内临时密码');
  const errors: string[] = [], remote: string[] = [], writes: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  // 在页面内观察真实 Fetch 流，避免 DevTools 对 SSE 响应体的缓存限制。
  await page.addInitScript(() => {
    const frames: string[] = [];
    Object.defineProperty(window, '__chatFrames', { value: frames });
    const original = window.fetch.bind(window);
    window.fetch = async (...args) => {
      const response = await original(...args);
      if (response.headers.get('content-type')?.startsWith('text/event-stream')) {
        void response.clone().text().then((text) => frames.push(text)).catch(() => {});
      }
      return response;
    };
  });
  await context.route('**/*', async (route) => {
    const request = route.request(), url = new URL(request.url());
    if (url.hostname !== '127.0.0.1') { remote.push(url.origin); await route.abort(); return; }
    if (url.pathname.startsWith('/api/') && request.method() !== 'GET' && !url.pathname.startsWith('/api/auth/')) writes.push(url.pathname);
    await route.continue();
  });
  await page.goto('/chat?service=payment-service&start=2026-10-01T01:00:00Z&end=2026-10-01T02:00:00Z');
  await page.getByLabel('账户', { exact: true }).fill('local-browser-owner');
  await page.getByLabel('密码', { exact: true }).fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'AI 对话', level: 1 })).toBeVisible();
  await page.getByLabel('问题', { exact: true }).fill('payment-service 为什么出现 5xx？请引用查询证据。');
  const response = page.waitForResponse((value) => value.url().endsWith('/api/chat') && value.request().method() === 'POST');
  await page.getByRole('button', { name: '发送问题', exact: true }).click();
  const firstResponse = await response;
  expect(firstResponse.status()).toBe(200);
  expect(firstResponse.headers()['content-type']).toContain('text/event-stream');
  await expect(page.getByText('已关闭', { exact: true })).toBeVisible({ timeout: 60_000 });
  const firstId = new URL(page.url()).searchParams.get('task')!;
  const first = await (await page.request.get(`/api/chat/${firstId}`)).json();
  expect(first.pending).toBe(false); expect(first.evidence_ids.length).toBeGreaterThan(0);
  await expect.poll(() => page.evaluate(() =>
    (window as unknown as Window & { __chatFrames: string[] }).__chatFrames.length)).toBe(1);
  const stream = await page.evaluate(() =>
    (window as unknown as Window & { __chatFrames: string[] }).__chatFrames[0]!);
  expect(stream.match(/event: delta/g)!.length).toBeGreaterThan(1);
  expect(stream).toContain('event: done');
  const evidenceId = first.evidence_ids[0];
  const evidence = await (await page.request.get(`/api/evidence/${evidenceId}`)).json();
  expect(evidence.task_id).toBe(firstId);
  await page.locator('.chat-answer').getByRole('link', { name: `查看证据 ${evidenceId}`, exact: true }).first().click();
  await expect(page.getByRole('dialog').getByText(evidence.source_tool, { exact: true }).first()).toBeVisible();
  await page.screenshot({ path: '../.cache/frontend-smoke/chat-evidence.png', fullPage: true, animations: 'disabled' });
  await page.reload();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('dialog').getByRole('button', { name: '关闭', exact: true }).click();
  await expect(page.getByText('已保存回答', { exact: true })).toBeVisible();
  await expect(page.locator('.chat-answer')).toHaveText(first.answer.replace(/\[Evidence:([0-9a-f-]{36})\]/g, '$1'));
  await page.getByLabel('问题', { exact: true }).fill('哪些证据支持这个判断？');
  const followResponse = page.waitForResponse((value) => value.url().endsWith('/api/chat'));
  await page.getByRole('button', { name: '发送问题', exact: true }).click();
  const followRequest = (await followResponse).request().postDataJSON();
  expect(followRequest.previous_task_id).toBe(firstId);
  await expect(page.getByText('已关闭', { exact: true })).toBeVisible({ timeout: 60_000 });
  const followId = new URL(page.url()).searchParams.get('task')!;
  expect(followId).not.toBe(firstId);
  const follow = await (await page.request.get(`/api/chat/${followId}`)).json();
  expect(follow.evidence_ids.every((id: string) => !first.evidence_ids.includes(id))).toBe(true);
  await page.screenshot({ path: '../.cache/frontend-smoke/chat-desktop.png', fullPage: true, animations: 'disabled' });
  await page.getByLabel('请求方式', { exact: true }).selectOption('task');
  await page.getByLabel('问题', { exact: true }).fill('请调查并回滚 payment-service v2.3.7 到 v2.3.6');
  await page.getByRole('button', { name: '发送问题', exact: true }).click();
  await expect(page.getByText('策略判定：需要审批', { exact: true })).toBeVisible({ timeout: 60_000 });
  const actionId = new URL(page.url()).searchParams.get('task')!;
  const action = await (await page.request.get(`/api/chat/${actionId}`)).json();
  expect(action.status).toBe('WAITING_APPROVAL');
  const plan = await (await page.request.get(`/api/evidence/${action.plan_evidence_id}`)).json();
  expect(plan.result_snapshot.actions[0].action.risk_level).toBe('L3');
  const calls = await (await page.request.get(`/api/tasks/${actionId}/tool-calls?limit=100`)).json();
  expect(calls.items.some((call: { operation: string }) => call.operation === 'execute_action')).toBe(false);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: '../.cache/frontend-smoke/chat-mobile.png', fullPage: true, animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.getByRole('link', { name: `查看证据 ${action.plan_evidence_id}`, exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.getByRole('dialog').getByRole('button', { name: '关闭', exact: true }).click();
  await page.goto('/tasks?source=Human');
  await expect(page.getByRole('heading', { name: 'AI 任务中心', level: 1 })).toBeVisible();
  const tasks = await (await page.request.get('/api/tasks?source=Human&limit=100')).json();
  for (const id of [firstId, followId, actionId]) {
    expect(tasks.items.some((task: { id: string; source: string }) => task.id === id && task.source === 'Human')).toBe(true);
  }
  await expect(page.locator('tbody').getByText('AI 对话：', { exact: false })).toHaveCount(3);
  expect(writes).toEqual(['/api/chat', '/api/chat', '/api/chat']);
  expect(remote).toEqual([]); expect(errors).toEqual([]);
});
